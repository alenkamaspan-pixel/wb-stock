"""
Проверка sync.undo_history_backfill() — аварийной отмены последствий бага в
backfill_order_history() от 27.08.2026: та функция ошибочно списывала
остаток по КАЖДОМУ найденному через общий метод WB заказу, включая
заказы многолетней давности, задолго до этого приложения (WB отдаёт
историю за всё время, а не только «свежую»).

Проверяем:
  1) движения и заказы, созданные сбойным запуском (помечены характерным
     комментарием), полностью удаляются;
  2) остаток возвращается к состоянию ДО бага;
  3) ничего из того, что было в базе ДО сбойного запуска — обычные заказы,
     обычные отмены (в том числе без движения, из-за несопоставленного
     склада) — не трогается;
  4) повторный запуск ничего не находит (идемпотентность) — на удалённых
     данных second run просто отчитывается нулями.

Запуск: python3 tests/test_undo_backfill.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_undo_backfill.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
from app.sync import undo_history_backfill, get_current_stock, BACKFILL_MOVEMENT_MARKER  # noqa: E402

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
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад', 1, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-Q', 1, 'BC-Q', 'Товар Q', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='BC-Q'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 100, 'manual', ?)", (product_id, warehouse_id, now_iso()),
)

# --- Легитимный заказ №1: обычная продажа, активная (не должна пострадать).
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('1000000001', ?, ?, 3, 'new', 1, ?, ?)""",
    (product_id, warehouse_id, now_iso(), now_iso()),
)
legit_order_1_id = conn.execute("SELECT id FROM wb_orders WHERE wb_order_id='1000000001'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, "
    "comment, created_at) VALUES (?, ?, 'sale', -3, 'wb_sync', ?, 'Заказ WB 1000000001', ?)",
    (product_id, warehouse_id, legit_order_1_id, now_iso()),
)

# --- Легитимный заказ №2: обычная отмена через reconcile (тоже есть движение,
# просто другой тип и другой текст комментария — не должна пострадать).
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('1000000002', ?, ?, 2, 'cancel', 0, ?, ?)""",
    (product_id, warehouse_id, now_iso(), now_iso()),
)
legit_order_2_id = conn.execute("SELECT id FROM wb_orders WHERE wb_order_id='1000000002'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, "
    "comment, created_at) VALUES (?, ?, 'sale', -2, 'wb_sync', ?, 'Заказ WB 1000000002', ?)",
    (product_id, warehouse_id, legit_order_2_id, now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, "
    "comment, created_at) VALUES (?, ?, 'sale_reversal', 2, 'wb_sync', ?, "
    "'Отмена заказа WB 1000000002 (найдено разовой сверкой)', ?)",
    (product_id, warehouse_id, legit_order_2_id, now_iso()),
)

# --- Легитимный заказ №3: отменён, но БЕЗ движения вообще (склад не был
# сопоставлен на момент создания) — граничный случай, тоже НЕ должен
# попасть под удаление, хотя формально "status='cancel' и нет движений".
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('1000000003', ?, NULL, 1, 'cancel', 0, ?, ?)""",
    (product_id, now_iso(), now_iso()),
)

# --- Заказ, испорченный сбойной догрузкой истории: движение с характерной
# пометкой — ИМЕННО ЕГО и должна найти и удалить undo_history_backfill().
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('9999999901', ?, ?, 1, 'new', 1, ?, ?)""",
    (product_id, warehouse_id, now_iso(), now_iso()),
)
tainted_order_1_id = conn.execute("SELECT id FROM wb_orders WHERE wb_order_id='9999999901'").fetchone()["id"]
conn.execute(
    f"INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, "
    f"comment, created_at) VALUES (?, ?, 'sale', -1, 'wb_sync', ?, "
    f"'Заказ WB 9999999901 {BACKFILL_MOVEMENT_MARKER}', ?)",
    (product_id, warehouse_id, tainted_order_1_id, now_iso()),
)
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('9999999902', ?, ?, 5, 'new', 1, ?, ?)""",
    (product_id, warehouse_id, now_iso(), now_iso()),
)
tainted_order_2_id = conn.execute("SELECT id FROM wb_orders WHERE wb_order_id='9999999902'").fetchone()["id"]
conn.execute(
    f"INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, "
    f"comment, created_at) VALUES (?, ?, 'sale', -5, 'wb_sync', ?, "
    f"'Заказ WB 9999999902 {BACKFILL_MOVEMENT_MARKER}', ?)",
    (product_id, warehouse_id, tainted_order_2_id, now_iso()),
)
conn.commit()
conn.close()

# Итого до отмены: 100 (приход) - 3 (заказ1) - 2 (заказ2) + 2 (возврат заказа2) - 1 (испорч.1) - 5 (испорч.2) = 91
stock_before = get_current_stock(get_conn(), product_id, warehouse_id)
check("Остаток до отмены = 91 (100 - 3 - 2 + 2 - 1 - 5)", stock_before == 91)

report = undo_history_backfill()
check("Удалено ровно 2 испорченных движения", report["movements_deleted"] == 2)
check("Удалено ровно 2 испорченных заказа", report["orders_deleted"] == 2)

conn = get_conn()
check("Испорченный заказ 9999999901 удалён из wb_orders", conn.execute(
    "SELECT id FROM wb_orders WHERE wb_order_id='9999999901'").fetchone() is None)
check("Испорченный заказ 9999999902 удалён из wb_orders", conn.execute(
    "SELECT id FROM wb_orders WHERE wb_order_id='9999999902'").fetchone() is None)

check("Легитимный заказ 1 НЕ тронут", conn.execute(
    "SELECT id FROM wb_orders WHERE wb_order_id='1000000001'").fetchone() is not None)
check("Легитимный заказ 2 (обычная отмена) НЕ тронут", conn.execute(
    "SELECT id FROM wb_orders WHERE wb_order_id='1000000002'").fetchone() is not None)
check("Легитимный заказ 3 (отменён без движения, склад не сопоставлен) НЕ тронут", conn.execute(
    "SELECT id FROM wb_orders WHERE wb_order_id='1000000003'").fetchone() is not None)

legit_movements_left = conn.execute(
    "SELECT COUNT(*) AS c FROM stock_movements WHERE source='wb_sync'"
).fetchone()["c"]
check("Остались ровно 3 легитимных движения (продажа1, продажа2, возврат2)", legit_movements_left == 3)

stock_after = get_current_stock(conn, product_id, warehouse_id)
check("Остаток восстановлен до 97 (91 + 1 + 5 — вернули испорченное списание)", stock_after == 97)
conn.close()

# --- Повторный запуск: испорченного больше нет, отчёт должен быть нулевым.
report2 = undo_history_backfill()
check("Повторный запуск: движений для удаления больше нет", report2["movements_deleted"] == 0)
check("Повторный запуск: заказов для удаления больше нет", report2["orders_deleted"] == 0)
check("Повторный запуск: остаток не изменился (97)", get_current_stock(get_conn(), product_id, warehouse_id) == 97)

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки отмены догрузки истории пройдены успешно ({passed}).")
