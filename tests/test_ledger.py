"""
Проверка ключевой бизнес-логики без обращения к реальному WB API —
подменяем WBClient моком с заранее заданными ответами.

Запуск: python3 tests/test_ledger.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
from app.sync import sync_once, get_current_stock  # noqa: E402


class MockWBClient:
    """Имитирует WBClient: отдаёт заранее заданные ответы, ничего не шлёт по сети."""
    def __init__(self):
        self.new_orders_queue = []
        self.status_updates = {}
        self.pushed_stocks = []  # [(warehouse_id, items)]

    def get_new_orders(self):
        orders, self.new_orders_queue = self.new_orders_queue, []
        return orders

    def get_orders_status(self, order_ids):
        return [{"id": oid, "status": self.status_updates[oid]} for oid in order_ids if oid in self.status_updates]

    def update_stocks(self, wb_warehouse_id, items):
        self.pushed_stocks.append((wb_warehouse_id, items))


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(1)


init_db()
conn = get_conn()
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_synced_to_wb, is_active, created_at) "
    "VALUES ('Тестовый склад', 555, 1, 1, ?)", (now_iso(),)
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=555").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES (?, ?, ?, ?, ?)",
    ("TEST-SKU", 999, "TESTBARCODE1", "Тестовый товар", now_iso()),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='TESTBARCODE1'").fetchone()["id"]
# Начальный приход — 10 единиц, чтобы было что продавать
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 10, 'manual', ?)",
    (product_id, warehouse_id, now_iso()),
)
conn.commit()
conn.close()

check("Начальный остаток = 10", get_current_stock(get_conn(), product_id, warehouse_id) == 10)

# --- Шаг 1: приходит новый заказ WB на этот товар с этого склада ---
mock = MockWBClient()
mock.new_orders_queue = [{"orderId": 1001, "nmId": 999, "skus": ["TESTBARCODE1"], "warehouseId": 555}]
result = sync_once(mock)

check("Синк прошёл успешно", result["status"] == "success")
check("Заказ учтён (orders_fetched=1)", result["orders_fetched"] == 1)
check("Создано ровно одно движение (списание)", result["movements_created"] == 1)

conn = get_conn()
stock_after_order = get_current_stock(conn, product_id, warehouse_id)
check(f"Остаток списан до 9 (было {stock_after_order})", stock_after_order == 9)

order = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='1001'").fetchone()
check("Заказ сохранён со статусом new", order["status"] == "new")
check("Флаг stock_deducted выставлен", order["stock_deducted"] == 1)
conn.close()

check("Остаток отправлен обратно в WB", len(mock.pushed_stocks) == 1)
check(
    "В WB отправлено правильное количество (9)",
    mock.pushed_stocks[0] == (555, [{"sku": "TESTBARCODE1", "amount": 9}]),
)

# --- Шаг 2: заказ отменяется — остаток должен вернуться ---
mock2 = MockWBClient()
mock2.status_updates = {1001: "cancel"}
result2 = sync_once(mock2)

check("Второй синк прошёл успешно", result2["status"] == "success")
check("Создано движение возврата остатка", result2["movements_created"] == 1)

conn = get_conn()
stock_after_cancel = get_current_stock(conn, product_id, warehouse_id)
check(f"Остаток вернулся к 10 (было {stock_after_cancel})", stock_after_cancel == 10)
order2 = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='1001'").fetchone()
check("Статус заказа обновился на cancel", order2["status"] == "cancel")
check("Флаг stock_deducted сброшен", order2["stock_deducted"] == 0)
conn.close()

check(
    "После отмены в WB снова отправлено верное количество (10)",
    mock2.pushed_stocks[0] == (555, [{"sku": "TESTBARCODE1", "amount": 10}]),
)

# --- Шаг 3: повторный синк того же заказа не должен задвоить списание ---
mock3 = MockWBClient()
mock3.new_orders_queue = [{"orderId": 1001, "nmId": 999, "skus": ["TESTBARCODE1"], "warehouseId": 555}]
result3 = sync_once(mock3)
check("Дублирующийся заказ не создал новых движений", result3["movements_created"] == 0)
conn = get_conn()
check("Остаток не изменился (всё ещё 10)", get_current_stock(conn, product_id, warehouse_id) == 10)
conn.close()

print("\nВсе проверки бизнес-логики пройдены успешно.")
