"""
Проверка исправления от 27.08.2026: реальная отмена клиентом видна в поле
wbStatus (например, "declined_by_client"), а НЕ в supplierStatus, который
может так и остаться "new". Раньше sync.py читал только supplierStatus и
такие отмены не замечал вообще — что и объясняло 0 отмен в приложении при
131 реальной в кабинете WB Partners.

Проверяем:
  1) обычная sync_once() теперь ловит такую отмену и возвращает остаток;
  2) разовая reconcile_all_orders() чинит УЖЕ накопленные ошибки — включая
     заказы, помеченные 'complete', которые обычная синхронизация больше не
     трогает;
  3) reconcile_all_orders() безопасно запускать повторно (не списывает/не
     возвращает остаток дважды);
  4) статусы wbStatus, не входящие в список отменяющих (например, обычный
     "sold"), не считаются отменой.

Запуск: python3 tests/test_wbstatus_fix.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_wbstatus.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
from app.sync import sync_once, reconcile_all_orders, get_current_stock  # noqa: E402

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


class MockWBClient:
    """order_ids -> (supplierStatus, wbStatus). Заказ, отсутствующий в
    responses, просто не попадёт в ответ WB (как если бы WB его "не отдал")."""

    def __init__(self, responses=None):
        self.responses = responses or {}

    def get_new_orders(self):
        return []

    def get_orders_status(self, order_ids):
        out = []
        for oid in order_ids:
            if oid in self.responses:
                supplier_status, wb_status = self.responses[oid]
                out.append({"id": oid, "supplierStatus": supplier_status, "wbStatus": wb_status})
        return out


init_db()
conn = get_conn()
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад', 1, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-A', 1, 'BC-A', 'Товар A', ?)",
    (now_iso(),),
)
product_a = conn.execute("SELECT id FROM products WHERE barcode='BC-A'").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-B', 2, 'BC-B', 'Товар B', ?)",
    (now_iso(),),
)
product_b = conn.execute("SELECT id FROM products WHERE barcode='BC-B'").fetchone()["id"]

# Начальный приход по обоим товарам, чтобы было что списывать/сверять.
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 20, 'manual', ?)", (product_a, warehouse_id, now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 20, 'manual', ?)", (product_b, warehouse_id, now_iso()),
)

# ---- Заказ 1: отслеживается (status='new'), уже списан. Проверяем через
# обычный sync_once(), что WB-отмена клиентом (wbStatus) ловится.
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('2000000001', ?, ?, 3, 'new', 1, ?, ?)""",
    (product_a, warehouse_id, now_iso(), now_iso()),
)
# Списываем остаток руками (эмулируем, что заказ уже был списан при получении)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, created_at) "
    "VALUES (?, ?, 'sale', -3, 'wb_sync', "
    "(SELECT id FROM wb_orders WHERE wb_order_id='2000000001'), ?)",
    (product_a, warehouse_id, now_iso()),
)

# ---- Заказ 2: УЖЕ помечен 'complete' (обычная синхронизация его больше не
# перепроверяет), но по факту клиент его отменил, и списание всё ещё числится
# проведённым — ровно тот исторический баг, для которого нужна reconcile_all_orders().
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('2000000002', ?, ?, 5, 'complete', 1, ?, ?)""",
    (product_b, warehouse_id, now_iso(), now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, created_at) "
    "VALUES (?, ?, 'sale', -5, 'wb_sync', "
    "(SELECT id FROM wb_orders WHERE wb_order_id='2000000002'), ?)",
    (product_b, warehouse_id, now_iso()),
)
conn.commit()
conn.close()

stock_a_before = get_current_stock(get_conn(), product_a, warehouse_id)
stock_b_before = get_current_stock(get_conn(), product_b, warehouse_id)
check("Остаток товара A до синка = 17 (20 - 3)", stock_a_before == 17)
check("Остаток товара B до синка = 15 (20 - 5)", stock_b_before == 15)

# --- Проверка 1: обычный sync_once() должен поймать отмену заказа 1 через wbStatus.
result = sync_once(MockWBClient(responses={2000000001: ("new", "declined_by_client")}))
check("sync_once() прошёл без ошибок", result["status"] == "success")

conn = get_conn()
order1 = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='2000000001'").fetchone()
check(
    "Заказ 1: supplierStatus остался 'new', но наш статус стал 'cancel' (через wbStatus)",
    order1["status"] == "cancel",
)
check("Заказ 1: wb_status сохранён как declined_by_client", order1["wb_status"] == "declined_by_client")
check("Заказ 1: stock_deducted сброшен в 0", order1["stock_deducted"] == 0)
stock_a_after = get_current_stock(conn, product_a, warehouse_id)
check("Остаток товара A вернулся к 20", stock_a_after == 20)

# Заказ 2 пока не должен был затронуться обычным sync_once() — он 'complete',
# Этап 2 его не перепроверяет вообще.
order2_before_reconcile = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='2000000002'").fetchone()
check(
    "Заказ 2 ('complete') НЕ тронут обычным sync_once() — остаток пока не вернулся",
    order2_before_reconcile["stock_deducted"] == 1,
)
stock_b_still_deducted = get_current_stock(conn, product_b, warehouse_id)
check("Остаток товара B пока всё ещё занижен (15)", stock_b_still_deducted == 15)
conn.close()

# --- Проверка 2: reconcile_all_orders() должна найти и исправить заказ 2,
# несмотря на его 'complete'-статус.
report1 = reconcile_all_orders(MockWBClient(responses={
    2000000002: ("complete", "declined_by_client"),
    # заказ 1 тоже проверим ещё раз — он уже 'cancel', второй реверс быть не должен
    2000000001: ("new", "declined_by_client"),
}))
check("Разовая сверка нашла ровно один заказ для исправления", len(report1["fixed"]) == 1)
check("Исправлен именно заказ 2000000002", report1["fixed"][0]["wb_order_id"] == "2000000002")
check("Возвращено 5 единиц (кол-во заказа 2)", report1["fixed"][0]["quantity"] == 5)

conn = get_conn()
order2_after = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='2000000002'").fetchone()
check("Заказ 2: статус стал 'cancel'", order2_after["status"] == "cancel")
check("Заказ 2: stock_deducted сброшен", order2_after["stock_deducted"] == 0)
stock_b_after = get_current_stock(conn, product_b, warehouse_id)
check("Остаток товара B вернулся к 20", stock_b_after == 20)
conn.close()

# --- Проверка 3: повторный запуск reconcile_all_orders() НЕ должен снова
# возвращать остаток (идемпотентность) — заказы уже 'cancel'/stock_deducted=0.
report2 = reconcile_all_orders(MockWBClient(responses={
    2000000001: ("new", "declined_by_client"),
    2000000002: ("complete", "declined_by_client"),
}))
check("Повторный запуск сверки ничего не исправляет заново", len(report2["fixed"]) == 0)
conn = get_conn()
stock_a_final = get_current_stock(conn, product_a, warehouse_id)
stock_b_final = get_current_stock(conn, product_b, warehouse_id)
check("Остаток товара A не изменился повторно (всё ещё 20)", stock_a_final == 20)
check("Остаток товара B не изменился повторно (всё ещё 20)", stock_b_final == 20)
conn.close()

# --- Проверка 4: обычный wbStatus (не из списка отменяющих) не считается отменой.
conn = get_conn()
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('2000000003', ?, ?, 1, 'new', 1, ?, ?)""",
    (product_a, warehouse_id, now_iso(), now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, created_at) "
    "VALUES (?, ?, 'sale', -1, 'wb_sync', "
    "(SELECT id FROM wb_orders WHERE wb_order_id='2000000003'), ?)",
    (product_a, warehouse_id, now_iso()),
)
conn.commit()
conn.close()

report3 = reconcile_all_orders(MockWBClient(responses={2000000003: ("new", "sold")}))
check("Обычный статус 'sold' НЕ считается отменой — ничего не исправлено", len(report3["fixed"]) == 0)
conn = get_conn()
order3 = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='2000000003'").fetchone()
check("Заказ 3: статус остался 'new'", order3["status"] == "new")
check("Заказ 3: wb_status сохранён как 'sold' (для видимости в диагностике)", order3["wb_status"] == "sold")
check("Заказ 3: списание не тронуто", order3["stock_deducted"] == 1)
conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки исправления wbStatus пройдены успешно ({passed}).")
