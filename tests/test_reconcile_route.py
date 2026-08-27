"""
Проверка HTTP-маршрута /wb-diagnostics/reconcile — именно тем способом,
которым Алёна будет им пользоваться (кнопка + слово-подтверждение на
странице «Диагностика WB»), а не напрямую через sync.reconcile_all_orders().

Проверяем:
  1) без слова-подтверждения (или с неверным словом) — ничего не меняется;
  2) с верным словом — заказ, отменённый клиентом (wbStatus), но всё ещё
     числящийся 'complete' со списанным остатком, чинится, и в отчёте видно
     название товара/склада и количество;
  3) обычный пользователь (не админ) не может дёрнуть этот маршрут.

Запуск: python3 tests/test_reconcile_route.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_reconcile_route.db"
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
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад', 1, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-X', 1, 'BC-X', 'Товар X', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='BC-X'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 10, 'manual', ?)", (product_id, warehouse_id, now_iso()),
)
# Заказ уже 'complete' (обычный sync его не перепроверяет), но по факту отменён клиентом.
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('3000000001', ?, ?, 2, 'complete', 1, ?, ?)""",
    (product_id, warehouse_id, now_iso(), now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, created_at) "
    "VALUES (?, ?, 'sale', -2, 'wb_sync', (SELECT id FROM wb_orders WHERE wb_order_id='3000000001'), ?)",
    (product_id, warehouse_id, now_iso()),
)
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('manager1', ?, 'manager', ?)",
    (hash_password("pass1"), now_iso()),
)
conn.commit()
conn.close()


class MockWBClient:
    def get_orders_status(self, order_ids):
        return [
            {"id": oid, "supplierStatus": "complete", "wbStatus": "declined_by_client"}
            for oid in order_ids if oid == 3000000001
        ]


main_module.WBClient = MockWBClient
app = main_module.app
app.testing = True

stock_before = get_current_stock(get_conn(), product_id, warehouse_id)
check("Остаток до сверки занижен (8 = 10 - 2)", stock_before == 8)

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)

    # 1) без слова подтверждения — ничего не меняется
    resp = client.post("/wb-diagnostics/reconcile", data={}, follow_redirects=True)
    check("Без confirm_text — не 500-ошибка", resp.status_code == 200)
    stock_unchanged = get_current_stock(get_conn(), product_id, warehouse_id)
    check("Без confirm_text остаток не тронут (всё ещё 8)", stock_unchanged == 8)

    # 2) с верным словом — заказ должен исправиться
    resp = client.post("/wb-diagnostics/reconcile", data={"confirm_text": "ПЕРЕСЧИТАТЬ"})
    check("С верным confirm_text — страница отчёта открылась (200)", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("В отчёте виден ID заказа", "3000000001" in body)
    check("В отчёте видно название товара", "Товар X" in body)
    check("В отчёте видно возвращённое количество (+2)", "+2" in body)

stock_after = get_current_stock(get_conn(), product_id, warehouse_id)
check("Остаток вернулся к 10 после сверки через HTTP-маршрут", stock_after == 10)

# 3) обычный пользователь не может дёрнуть маршрут
with app.test_client() as client:
    client.post("/login", data={"username": "manager1", "password": "pass1"}, follow_redirects=True)
    resp = client.post("/wb-diagnostics/reconcile", data={"confirm_text": "ПЕРЕСЧИТАТЬ"}, follow_redirects=False)
    check(
        "Обычный пользователь (не админ) получает редирект, а не отчёт",
        resp.status_code in (302, 303) and "wb_reconcile_result" not in resp.headers.get("Location", ""),
    )

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки маршрута /wb-diagnostics/reconcile пройдены успешно ({passed}).")
