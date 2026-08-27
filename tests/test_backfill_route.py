"""
27.08.2026: /wb-diagnostics/backfill-history оказался багованным (списывал
остаток по многолетним старым заказам — см. undo_history_backfill()) и был
ОТКЛЮЧЁН. Этот тест проверяет именно отключённое состояние — что маршрут
существует (на случай, если в браузере ещё открыта старая форма), но
ничего не делает и не мутирует данные, каким бы confirm_text ни прислали.

Запуск: python3 tests/test_backfill_route.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_backfill_route_disabled.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import get_conn, now_iso  # noqa: E402
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
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад', 800, 1, ?)",
    (now_iso(),),
)
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-W', 1, 'BC-W', 'Товар W', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='BC-W'").fetchone()["id"]
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=800").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 15, 'manual', ?)", (product_id, warehouse_id, now_iso()),
)
conn.commit()
conn.close()


class MockWBClient:
    """Если бы маршрут и правда вызвал backfill_order_history(), этот мок
    вернул бы кучу «древних» активных заказов и списал бы остаток — если
    после теста остаток НЕ изменился, значит маршрут действительно
    отключён и функцию не вызывает."""

    def get_orders(self, limit=1000, next_cursor=0):
        if next_cursor == 0:
            return {
                "orders": [{"id": i, "nmId": 1, "skus": ["BC-W"], "warehouseId": 800} for i in range(9200000001, 9200000011)],
                "next": 1,
            }
        return {"orders": [], "next": next_cursor}

    def get_orders_status(self, order_ids):
        return [{"id": oid, "supplierStatus": "complete", "wbStatus": None} for oid in order_ids]


main_module.WBClient = MockWBClient
app = main_module.app
app.testing = True

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)

    resp = client.post("/wb-diagnostics/backfill-history", data={"confirm_text": "ДОГРУЗИТЬ"}, follow_redirects=True)
    check("Маршрут отвечает (не 500/404)", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("В ответе есть объяснение, что функция отключена", "отключен" in body.lower())

check("Остаток НЕ изменился (backfill_order_history не вызывался) — всё ещё 15",
      get_current_stock(get_conn(), product_id, warehouse_id) == 15)

conn = get_conn()
orders_count = conn.execute("SELECT COUNT(*) AS c FROM wb_orders").fetchone()["c"]
check("Ни одного нового заказа не создано", orders_count == 0)
conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки отключённого маршрута backfill-history пройдены успешно ({passed}).")
