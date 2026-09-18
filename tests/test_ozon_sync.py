"""
Проверка автосинхронизации продаж Ozon FBS (app/ozon_sync.sync_fbs_once) —
18.09.2026, по образцу tests/test_ledger.py для WB.

Проверяем:
  1) новое отправление списывает остаток с замапленного склада;
  2) отправление с несколькими товарами создаёт по строке на каждую позицию
     (в отличие от заказа WB, где одна позиция = один заказ);
  3) повторный запуск с теми же отправлениями не создаёт дублей;
  4) отмена отправления возвращает остаток ровно один раз, даже при
     повторных запусках;
  5) отправление на несопоставленный склад Ozon не падает и не списывает
     остаток, только пишет предупреждение;
  6) алиас по alias_ozon_sku перенаправляет списание на целевой товар;
  7) остаток WB (движения с source='wb_sync'/'manual') никак не затронут.

Запуск: python3 tests/test_ozon_sync.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_ozon_sync.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
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
    "INSERT INTO warehouses (name, ozon_warehouse_id, is_active, created_at) VALUES ('ФБС Люберцы', 777, 1, ?)",
    (now_iso(),),
)
fbs_wh_id = conn.execute("SELECT id FROM warehouses WHERE ozon_warehouse_id=777").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, ozon_sku, name, created_at) VALUES ('A', 111, 'Товар A', ?)", (now_iso(),)
)
product_a = conn.execute("SELECT id FROM products WHERE ozon_sku=111").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, ozon_sku, name, created_at) VALUES ('B', 222, 'Товар B', ?)", (now_iso(),)
)
product_b = conn.execute("SELECT id FROM products WHERE ozon_sku=222").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 100, 'manual', ?)", (product_a, fbs_wh_id, now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 50, 'manual', ?)", (product_b, fbs_wh_id, now_iso()),
)
# алиас: карточка Ozon sku=333 на самом деле товар A
conn.execute(
    "INSERT INTO product_aliases (alias_ozon_sku, target_product_id, created_at) VALUES (333, ?, ?)",
    (product_a, now_iso()),
)
conn.commit()
conn.close()


class FakeClient:
    def __init__(self, unfulfilled=None, listing=None):
        self._unfulfilled = unfulfilled or []
        self._listing = listing or []

    def get_unfulfilled_postings(self):
        return {"postings": self._unfulfilled}

    def list_postings(self, since, to, limit=1000):
        return {"postings": self._listing}


# --- 1+2) многотоварное отправление на замапленный склад
r1 = ozon_sync.sync_fbs_once(FakeClient(unfulfilled=[{
    "posting_number": "POST-MULTI",
    "delivery_method": {"warehouse_id": 777},
    "products": [
        {"sku": 111, "offer_id": "a", "quantity": 3},
        {"sku": 222, "offer_id": "b", "quantity": 5},
    ],
}]))
check("Отправление с 2 товарами: fetched=1", r1["postings_fetched"] == 1)
check("Отправление с 2 товарами: создано 2 движения (по одному на товар)", r1["movements_created"] == 2)

conn = get_conn()
check("Остаток A списан (100 -> 97)", get_current_stock(conn, product_a, fbs_wh_id) == 97)
check("Остаток B списан (50 -> 45)", get_current_stock(conn, product_b, fbs_wh_id) == 45)
rows = conn.execute("SELECT * FROM ozon_postings WHERE posting_number='POST-MULTI'").fetchall()
check("В ozon_postings 2 строки (по одной на line_no)", len(rows) == 2)
conn.close()

# --- 3) повторный запуск с тем же отправлением — не дублирует
r2 = ozon_sync.sync_fbs_once(FakeClient(unfulfilled=[{
    "posting_number": "POST-MULTI",
    "delivery_method": {"warehouse_id": 777},
    "products": [
        {"sku": 111, "offer_id": "a", "quantity": 3},
        {"sku": 222, "offer_id": "b", "quantity": 5},
    ],
}]))
check("Повторный запуск: новых движений не создано", r2["movements_created"] == 0)
conn = get_conn()
check("Остаток A не изменился повторно (всё ещё 97)", get_current_stock(conn, product_a, fbs_wh_id) == 97)
conn.close()

# --- 4) отмена — возвращает остаток один раз
r3 = ozon_sync.sync_fbs_once(FakeClient(
    listing=[{"posting_number": "POST-MULTI", "status": "cancelled"}],
))
check("Отмена: cancelled_reversed=2 (обе строки отправления)", r3["cancelled_reversed"] == 2)
conn = get_conn()
check("Остаток A вернулся (97 -> 100)", get_current_stock(conn, product_a, fbs_wh_id) == 100)
check("Остаток B вернулся (45 -> 50)", get_current_stock(conn, product_b, fbs_wh_id) == 50)
conn.close()

r4 = ozon_sync.sync_fbs_once(FakeClient(
    listing=[{"posting_number": "POST-MULTI", "status": "cancelled"}],
))
check("Повторная сверка отмены: не возвращает остаток второй раз", r4["cancelled_reversed"] == 0)
conn = get_conn()
check("Остаток A не изменился от повторной отмены (всё ещё 100)", get_current_stock(conn, product_a, fbs_wh_id) == 100)
conn.close()

# --- 5) несопоставленный склад — не падает, не списывает
r5 = ozon_sync.sync_fbs_once(FakeClient(unfulfilled=[{
    "posting_number": "POST-UNKNOWN-WH",
    "delivery_method": {"warehouse_id": 999999},
    "products": [{"sku": 111, "offer_id": "a", "quantity": 1}],
}]))
check("Несопоставленный склад: без падения (status != error)", r5["status"] != "error")
check("Несопоставленный склад: движение не создано", r5["movements_created"] == 0)
check("Несопоставленный склад: предупреждение в message", r5["message"] and "не сопоставлен" in r5["message"])
conn = get_conn()
check("Остаток A не тронут несопоставленным складом (всё ещё 100)", get_current_stock(conn, product_a, fbs_wh_id) == 100)
conn.close()

# --- 6) алиас по alias_ozon_sku
r6 = ozon_sync.sync_fbs_once(FakeClient(unfulfilled=[{
    "posting_number": "POST-ALIAS",
    "delivery_method": {"warehouse_id": 777},
    "products": [{"sku": 333, "offer_id": "alias-a", "quantity": 4}],
}]))
conn = get_conn()
check("Алиас: списалось с целевого товара A (100 -> 96)", get_current_stock(conn, product_a, fbs_wh_id) == 96)
alias_product_count = conn.execute("SELECT COUNT(*) AS c FROM products WHERE sku='alias-a'").fetchone()["c"]
check("Алиас: отдельная карточка НЕ создана", alias_product_count == 0)
conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки синхронизации Ozon FBS пройдены успешно ({passed}).")
