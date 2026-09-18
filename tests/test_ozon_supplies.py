"""
Проверка поставок Ozon FBO (app/ozon_sync.refresh_supplies и load_supply) —
18.09.2026.

Проверяем:
  1) refresh_supplies подтягивает поставку и её состав, ничего не грузит на
     остаток сама по себе;
  2) без отмеченного ФФ-источника (is_ozon_fbo_source) загрузка невозможна,
     с понятной ошибкой;
  3) после того как ФФ-источник отмечен — «Загрузить поставку» переносит
     остаток с него на «Ozon FBO» одной операцией (и списание, и приход);
  4) повторная загрузка той же поставки отклоняется, остаток не меняется;
  5) обновление списка поставок НЕ трогает состав уже загруженной поставки
     (защита от искажения задним числом);
  6) виртуальный склад «Ozon FBO» не привязан ни к одному ФФ (общий).

Запуск: python3 tests/test_ozon_supplies.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_ozon_supplies.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app import ozon_sync  # noqa: E402
from app.sync import get_current_stock  # noqa: E402

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"[OK ] {label}")
        passed += 1
    else:
        print(f"[FAIL] {label}")
        failed += 1


init_db()
conn = get_conn()
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('admin', ?, 'admin', ?)",
    (hash_password("x"), now_iso()),
)
user_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()["id"]

conn.execute("INSERT INTO fulfillment_centers (name, is_active, created_at) VALUES ('МСК', 1, ?)", (now_iso(),))
ff_id = conn.execute("SELECT id FROM fulfillment_centers WHERE name='МСК'").fetchone()["id"]
conn.execute(
    "INSERT INTO warehouses (name, fulfillment_center_id, is_active, created_at) VALUES ('МСК склад', ?, 1, ?)",
    (ff_id, now_iso()),
)
msk_wh_id = conn.execute("SELECT id FROM warehouses WHERE name='МСК склад'").fetchone()["id"]
conn.execute("INSERT INTO products (sku, ozon_sku, name, created_at) VALUES ('A', 111, 'Товар A', ?)", (now_iso(),))
product_id = conn.execute("SELECT id FROM products WHERE ozon_sku=111").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 200, 'manual', ?)", (product_id, msk_wh_id, now_iso()),
)
conn.commit()
conn.close()


class FakeClient:
    def __init__(self, bundle_items=None):
        self._bundle_items = bundle_items if bundle_items is not None else [
            {"sku": 111, "offer_id": "a", "quantity": 30, "name": "Товар A"}
        ]

    def list_supply_orders(self, states=None, limit=100):
        return [42]

    def get_supply_orders_info(self, order_ids):
        return [{"supply_order_id": 42, "state": "CREATED", "bundle_id": "B1"}]

    def get_supply_bundle(self, bundle_ids):
        return self._bundle_items


# --- 1) refresh_supplies подтягивает поставку, ничего не грузит на остаток
r1 = ozon_sync.refresh_supplies(FakeClient())
check("refresh_supplies: 1 новая поставка", r1["discovered"] == 1)
conn = get_conn()
supply = conn.execute("SELECT * FROM ozon_supplies WHERE supply_order_id='42'").fetchone()
check("Поставка создана, loaded=0", supply is not None and supply["loaded"] == 0)
items = conn.execute("SELECT * FROM ozon_supply_items WHERE supply_id=?", (supply["id"],)).fetchall()
check("Состав поставки подтянут (1 позиция, 30 шт)", len(items) == 1 and items[0]["quantity"] == 30)
check("refresh_supplies сам по себе НЕ трогает остаток (всё ещё 200)", get_current_stock(conn, product_id, msk_wh_id) == 200)
conn.close()

# --- 2) без отмеченного ФФ-источника — загрузка невозможна
res_no_ff = ozon_sync.load_supply(supply["id"], user_id=user_id)
check("Без ФФ-источника: загрузка отклонена", res_no_ff["ok"] is False)
check("Без ФФ-источника: понятная причина в ошибке", "ФФ-источник" in res_no_ff["error"])

# --- отмечаем ФФ-источник
conn = get_conn()
conn.execute("UPDATE fulfillment_centers SET is_ozon_fbo_source = 1 WHERE id = ?", (ff_id,))
conn.commit()
conn.close()

# --- 3) загрузка переносит остаток одной операцией
res_load = ozon_sync.load_supply(supply["id"], user_id=user_id)
check("Загрузка поставки: ok=True", res_load["ok"] is True)
check("Загрузка поставки: перемещена 1 позиция", res_load["items_moved"] == 1)

conn = get_conn()
fbo_wh_id = ozon_sync.get_or_create_ozon_fbo_warehouse(conn)
check("Остаток списан с МСК (200 -> 170)", get_current_stock(conn, product_id, msk_wh_id) == 170)
check("Остаток пришёл на Ozon FBO (0 -> 30)", get_current_stock(conn, product_id, fbo_wh_id) == 30)
fbo_wh_row = conn.execute("SELECT * FROM warehouses WHERE id = ?", (fbo_wh_id,)).fetchone()
check("«Ozon FBO» не привязан ни к одному ФФ (общий склад)", fbo_wh_row["fulfillment_center_id"] is None)
conn.close()

# --- 4) повторная загрузка отклоняется
res_double = ozon_sync.load_supply(supply["id"], user_id=user_id)
check("Повторная загрузка той же поставки: отклонена", res_double["ok"] is False)
conn = get_conn()
check("Остаток МСК не изменился от повторной попытки (всё ещё 170)", get_current_stock(conn, product_id, msk_wh_id) == 170)
check("Остаток Ozon FBO не изменился от повторной попытки (всё ещё 30)", get_current_stock(conn, product_id, fbo_wh_id) == 30)
conn.close()

# --- 5) refresh после загрузки не трогает состав загруженной поставки
r2 = ozon_sync.refresh_supplies(FakeClient(bundle_items=[
    {"sku": 111, "offer_id": "a", "quantity": 999, "name": "Товар A (искажённое количество)"}
]))
conn = get_conn()
items_after = conn.execute("SELECT * FROM ozon_supply_items WHERE supply_id=?", (supply["id"],)).fetchall()
check("Состав уже загруженной поставки не переписан (всё ещё 30, не 999)", items_after[0]["quantity"] == 30)
conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки поставок Ozon FBO пройдены успешно ({passed}).")
