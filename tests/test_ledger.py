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
from app.sync import sync_once, get_current_stock, get_stock_by_ff, get_stock_locations  # noqa: E402


class MockWBClient:
    """Имитирует WBClient: отдаёт заранее заданные ответы, ничего не шлёт по сети.
    Реальный WBClient тоже ничего не пишет в WB — только читает заказы/статусы."""
    def __init__(self):
        self.new_orders_queue = []
        self.status_updates = {}

    def get_new_orders(self):
        orders, self.new_orders_queue = self.new_orders_queue, []
        return orders

    def get_orders_status(self, order_ids):
        # Реальный WB отдаёт статус сборочного задания в supplierStatus, а не в
        # "status" — раньше мок был неправильным (совпадал со старым багом
        # в sync.py и поэтому не мог его поймать).
        return [{"id": oid, "supplierStatus": self.status_updates[oid]} for oid in order_ids if oid in self.status_updates]


def check(label, condition):
    status = "OK " if condition else "FAIL"
    print(f"[{status}] {label}")
    if not condition:
        raise SystemExit(1)


init_db()
conn = get_conn()
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) "
    "VALUES ('Тестовый склад', 555, 1, ?)", (now_iso(),)
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

# --- Шаг 3: повторный синк того же заказа не должен задвоить списание ---
mock3 = MockWBClient()
mock3.new_orders_queue = [{"orderId": 1001, "nmId": 999, "skus": ["TESTBARCODE1"], "warehouseId": 555}]
result3 = sync_once(mock3)
check("Дублирующийся заказ не создал новых движений", result3["movements_created"] == 0)
conn = get_conn()
check("Остаток не изменился (всё ещё 10)", get_current_stock(conn, product_id, warehouse_id) == 10)
conn.close()

# --- Шаг 4: агрегация остатков по ФФ (фулфилмент-центрам) ---
conn = get_conn()
cur = conn.execute("INSERT INTO fulfillment_centers (name, is_active, created_at) VALUES ('ФФ Тест', 1, ?)", (now_iso(),))
ff_id = cur.lastrowid
conn.execute("UPDATE warehouses SET fulfillment_center_id = ? WHERE id = ?", (ff_id, warehouse_id))
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, fulfillment_center_id, is_active, created_at) "
    "VALUES ('Второй склад того же ФФ', 556, ?, 1, ?)", (ff_id, now_iso()),
)
warehouse2_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=556").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 5, 'manual', ?)",
    (product_id, warehouse2_id, now_iso()),
)
conn.commit()

groups = get_stock_by_ff(conn)
ff_group = next((g for g in groups if g["ff_id"] == ff_id), None)
check("группа ФФ появилась в агрегации", ff_group is not None)
total = next(t["quantity"] for t in ff_group["totals"] if t["product_id"] == product_id)
# ФФ — единственный уровень, на котором остаток вообще что-то значит (виртуальные
# склады WB внутри одного ФФ физически делят одну и ту же кучу товара), поэтому
# get_stock_by_ff больше не отдаёт разбивку по складам — только итог по ФФ.
check(f"итого по ФФ = 15 (10 + 5), получено {total}", total == 15)
check(f"ff_total = 15, получено {ff_group['ff_total']}", ff_group["ff_total"] == 15)
check("в группе ФФ нет разбивки по складам (rows)", "rows" not in ff_group)
check("в группе ФФ нет разбивки по складам (warehouse_totals)", "warehouse_totals" not in ff_group)
conn.close()

# --- Шаг 5: get_stock_locations — один ФФ = одно место хранения ---
conn = get_conn()
locations = get_stock_locations(conn)
loc_keys = {loc["key"] for loc in locations}
check(f"ФФ с двумя складами даёт ОДНУ запись в местах хранения, получено {loc_keys}", f"ff:{ff_id}" in loc_keys)
check(
    "оба виртуальных склада этого ФФ НЕ фигурируют как отдельные места",
    f"wh:{warehouse_id}" not in loc_keys and f"wh:{warehouse2_id}" not in loc_keys,
)
conn.close()

# --- Шаг 6: сбой на этапе проверки статусов не должен ронять уже обработанные
# новые заказы (раньше вся синхронизация была одной транзакцией и откатывала
# ВСЁ, если WB API падал на втором этапе — из-за этого зависали и новые заказы) ---
conn = get_conn()
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 100, 'manual', ?)",
    (product_id, warehouse_id, now_iso()),
)
conn.commit()
conn.close()


class FailingStatusMockWBClient(MockWBClient):
    """Новые заказы обрабатываются нормально, а проверка статусов всегда падает —
    имитирует WB, отклоняющий слишком большой/проблемный запрос /orders/status."""
    def get_orders_status(self, order_ids):
        from app.wb_client import WBApiError
        raise WBApiError("имитация сбоя WB на проверке статусов")


mock6 = FailingStatusMockWBClient()
mock6.new_orders_queue = [{"orderId": 2002, "nmId": 999, "skus": ["TESTBARCODE1"], "warehouseId": 555}]
stock_before = get_current_stock(get_conn(), product_id, warehouse_id)
result6 = sync_once(mock6)
check(f"статус синка при сбое проверки статусов = warning, получено {result6['status']}", result6["status"] == "warning")
check("новый заказ всё равно обработан (movements_created >= 1)", result6["movements_created"] >= 1)
conn = get_conn()
stock_after = get_current_stock(conn, product_id, warehouse_id)
check(
    f"остаток по новому заказу списался, несмотря на сбой этапа статусов ({stock_before} -> {stock_after})",
    stock_after == stock_before - 1,
)
conn.close()
conn.close()

print("\nВсе проверки бизнес-логики пройдены успешно.")
