"""
Проверка sync.backfill_order_history() — догрузки истории заказов через
общий метод WB (/api/v3/orders), в дополнение к обычному /orders/new.
Нужна из-за находки 27.08.2026: 126 из 131 реальной отмены Алёны не были
в нашей базе вообще ни в каком виде, потому что клиент отменял заказ
быстрее, чем раз в SYNC_INTERVAL_MINUTES — и заказ пропадал из «новых» ещё
до нашего опроса.

Проверяем:
  1) уже известный заказ пропускается, не дублируется;
  2) новый АКТИВНЫЙ заказ — остаток списывается, как обычно;
  3) новый заказ, который к моменту проверки уже ОТМЕНЁН — остаток не
     трогается вовсе (не списываем и сразу же не возвращаем);
  4) новый заказ без сопоставленного склада — просто фиксируется, без движения;
  5) постраничная догрузка проходит несколько страниц (использует next-курсор);
  6) повторный запуск не находит уже найденное заново (идемпотентность);
  7) защита от бесконечного цикла (BACKFILL_MAX_PAGES) срабатывает и не виснет.

Запуск: python3 tests/test_backfill_history.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_backfill.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
from app.sync import backfill_order_history, get_current_stock, BACKFILL_MAX_PAGES  # noqa: E402

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
    """pages: {next_cursor_запрошенный -> {"orders": [...], "next": ...}}.
    statuses: order_id (int) -> (supplierStatus, wbStatus)."""

    def __init__(self, pages, statuses=None):
        self.pages = pages
        self.statuses = statuses or {}
        self.get_orders_calls = []

    def get_orders(self, limit=1000, next_cursor=0):
        self.get_orders_calls.append(next_cursor)
        return self.pages.get(next_cursor, {"orders": [], "next": next_cursor})

    def get_orders_status(self, order_ids):
        out = []
        for oid in order_ids:
            if oid in self.statuses:
                sup, wb = self.statuses[oid]
                out.append({"id": oid, "supplierStatus": sup, "wbStatus": wb})
        return out


init_db()
conn = get_conn()
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад', 500, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=500").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-Z', 1, 'BC-Z', 'Товар Z', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='BC-Z'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 50, 'manual', ?)", (product_id, warehouse_id, now_iso()),
)
# Заказ A уже известен нашей базе — не должен попасть в "discovered".
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('9000000001', ?, ?, 1, 'complete', 1, ?, ?)""",
    (product_id, warehouse_id, now_iso(), now_iso()),
)
conn.commit()
conn.close()

# Заказ B: новый, активный, склад известен (wb_warehouse_id=500) -> должен списать остаток.
order_b = {"id": 9000000002, "nmId": 1, "skus": ["BC-Z"], "warehouseId": 500}
# Заказ C: новый, но УЖЕ отменён клиентом (wbStatus) -> остаток не трогаем.
order_c = {"id": 9000000003, "nmId": 1, "skus": ["BC-Z"], "warehouseId": 500}
# Заказ D: новый, активный, но склад WB не сопоставлен ни с одним нашим.
order_d = {"id": 9000000004, "nmId": 1, "skus": ["BC-Z"], "warehouseId": 999999}
# Заказ A (дубль) на странице — тоже не должен задвоиться.
order_a_dup = {"id": 9000000001, "nmId": 1, "skus": ["BC-Z"], "warehouseId": 500}

pages = {
    0: {"orders": [order_a_dup, order_b], "next": 1},
    1: {"orders": [order_c, order_d], "next": 2},
    2: {"orders": [], "next": 2},  # пустая страница — конец истории
}
statuses = {
    9000000002: ("new", None),
    9000000003: ("new", "declined_by_client"),
    9000000004: ("new", None),
}

client = MockWBClient(pages, statuses)
stock_before = get_current_stock(get_conn(), product_id, warehouse_id)
check("Остаток до догрузки = 50", stock_before == 50)

report = backfill_order_history(client)

check("Прошлись по нескольким страницам (несколько разных next-курсоров)", len(set(client.get_orders_calls)) >= 3)
check("Найдено 3 новых заказа (B, C, D — A уже был известен)", report["discovered"] == 3)
check("Активных с списанием — 1 (заказ B)", report["added_active"] == 1)
check("Уже отменённых на момент проверки — 1 (заказ C)", report["added_cancelled"] == 1)
check("Без сопоставленного склада — 1 (заказ D)", report["skipped_no_warehouse"] == 1)
check("Ошибок нет", report["errors"] == [])

conn = get_conn()
order_b_row = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='9000000002'").fetchone()
check("Заказ B создан со статусом 'new'", order_b_row["status"] == "new")
check("Заказ B: stock_deducted=1", order_b_row["stock_deducted"] == 1)

order_c_row = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='9000000003'").fetchone()
check("Заказ C создан сразу со статусом 'cancel'", order_c_row["status"] == "cancel")
check("Заказ C: stock_deducted=0 (остаток не трогали)", order_c_row["stock_deducted"] == 0)
check("Заказ C: wb_status сохранён", order_c_row["wb_status"] == "declined_by_client")

order_d_row = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='9000000004'").fetchone()
check("Заказ D создан, но без склада", order_d_row["warehouse_id"] is None)
check("Заказ D: stock_deducted=0", order_d_row["stock_deducted"] == 0)

stock_after = get_current_stock(conn, product_id, warehouse_id)
check("Остаток снизился ровно на 1 (только заказ B) — стало 49", stock_after == 49)

movements_count = conn.execute(
    "SELECT COUNT(*) AS c FROM stock_movements WHERE wb_order_id IN "
    "(SELECT id FROM wb_orders WHERE wb_order_id IN ('9000000002','9000000003','9000000004'))"
).fetchone()["c"]
check("Создано ровно одно движение (для заказа B, не для C и D)", movements_count == 1)
conn.close()

# --- Идемпотентность: повторный запуск с теми же страницами не находит ничего нового.
client2 = MockWBClient(pages, statuses)
report2 = backfill_order_history(client2)
check("Повторный запуск: discovered=0 (все уже известны)", report2["discovered"] == 0)
check("Повторный запуск: остаток не изменился повторно", get_current_stock(get_conn(), product_id, warehouse_id) == 49)


# --- Защита от бесконечного цикла: курсор двигается вечно, но заказы всегда
# уже известны (эмулируем "мусорный" ответ WB, который никогда не отдаёт
# пустую страницу) — должны остановиться на BACKFILL_MAX_PAGES и не зависнуть.
class NeverEndingMockWBClient:
    def get_orders(self, limit=1000, next_cursor=0):
        # Всегда отдаёт один и тот же уже известный заказ и всегда двигает курсор дальше.
        return {"orders": [order_a_dup], "next": next_cursor + 1}

    def get_orders_status(self, order_ids):
        return []


report3 = backfill_order_history(NeverEndingMockWBClient())
check("Бесконечный курсор: остановились (не зависли)", True)  # сам факт, что мы сюда дошли
check("Бесконечный курсор: discovered=0 (заказ уже был известен)", report3["discovered"] == 0)
check(
    f"Бесконечный курсор: сообщение о достижении предела {BACKFILL_MAX_PAGES} страниц в ошибках",
    any("предел" in e and str(BACKFILL_MAX_PAGES) in e for e in report3["errors"]),
)

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки догрузки истории заказов пройдены успешно ({passed}).")
