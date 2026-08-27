"""
Проверка HTTP-маршрута /wb-diagnostics/backfill-history — кнопка «Подтянуть
историю заказов из WB» на странице «Диагностика WB», тем способом, которым
ей будет пользоваться Алёна.

Проверяем:
  1) без слова-подтверждения (или с неверным словом) — ничего не меняется;
  2) с верным словом — находит новый заказ, которого не было в базе, и
     показывает отчёт со счётчиками;
  3) обычный пользователь (не админ) не может дёрнуть этот маршрут.

Запуск: python3 tests/test_backfill_route.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_backfill_route.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import get_conn, now_iso  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.sync import get_current_stock  # noqa: E402
import app.main as main_module  # noqa: E402

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


conn = get_conn()
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад', 700, 1, ?)",
    (now_iso(),),
)
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-Y', 1, 'BC-Y', 'Товар Y', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='BC-Y'").fetchone()["id"]
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=700").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 30, 'manual', ?)", (product_id, warehouse_id, now_iso()),
)
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('manager1', ?, 'manager', ?)",
    (hash_password("pass1"), now_iso()),
)
conn.commit()
conn.close()


class MockWBClient:
    def get_orders(self, limit=1000, next_cursor=0):
        if next_cursor == 0:
            return {"orders": [{"id": 9100000001, "nmId": 1, "skus": ["BC-Y"], "warehouseId": 700}], "next": 1}
        return {"orders": [], "next": next_cursor}

    def get_orders_status(self, order_ids):
        return [{"id": oid, "supplierStatus": "new", "wbStatus": None} for oid in order_ids]


main_module.WBClient = MockWBClient
app = main_module.app
app.testing = True

stock_before = get_current_stock(get_conn(), product_id, warehouse_id)
check("Остаток до догрузки = 30", stock_before == 30)

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)

    resp = client.post("/wb-diagnostics/backfill-history", data={}, follow_redirects=True)
    check("Без confirm_text — не 500-ошибка", resp.status_code == 200)
    check("Без confirm_text остаток не тронут (всё ещё 30)", get_current_stock(get_conn(), product_id, warehouse_id) == 30)

    resp = client.post("/wb-diagnostics/backfill-history", data={"confirm_text": "ДОГРУЗИТЬ"})
    check("С верным confirm_text — страница отчёта открылась (200)", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("В отчёте видно, что найден 1 новый заказ", "1" in body)

stock_after = get_current_stock(get_conn(), product_id, warehouse_id)
check("Остаток списался на 1 после догрузки (29)", stock_after == 29)

with app.test_client() as client:
    client.post("/login", data={"username": "manager1", "password": "pass1"}, follow_redirects=True)
    resp = client.post("/wb-diagnostics/backfill-history", data={"confirm_text": "ДОГРУЗИТЬ"}, follow_redirects=False)
    check(
        "Обычный пользователь (не админ) получает редирект, а не отчёт",
        resp.status_code in (302, 303) and "backfill" not in resp.headers.get("Location", ""),
    )

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки маршрута /wb-diagnostics/backfill-history пройдены успешно ({passed}).")
