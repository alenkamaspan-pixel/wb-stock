"""
Проверка HTTP-маршрута /wb-diagnostics/undo-backfill — кнопка «Отменить
последствия догрузки истории» на странице «Диагностика WB», тем способом,
которым ей будет пользоваться Алёна после инцидента 27.08.2026.

Запуск: python3 tests/test_undo_backfill_route.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_undo_backfill_route.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import get_conn, now_iso  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.sync import get_current_stock, BACKFILL_MOVEMENT_MARKER  # noqa: E402
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
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад', 900, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=900").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-V', 1, 'BC-V', 'Товар V', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='BC-V'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 40, 'manual', ?)", (product_id, warehouse_id, now_iso()),
)
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('9300000001', ?, ?, 6, 'new', 1, ?, ?)""",
    (product_id, warehouse_id, now_iso(), now_iso()),
)
tainted_id = conn.execute("SELECT id FROM wb_orders WHERE wb_order_id='9300000001'").fetchone()["id"]
conn.execute(
    f"INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, wb_order_id, "
    f"comment, created_at) VALUES (?, ?, 'sale', -6, 'wb_sync', ?, 'Заказ WB 9300000001 {BACKFILL_MOVEMENT_MARKER}', ?)",
    (product_id, warehouse_id, tainted_id, now_iso()),
)
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('manager1', ?, 'manager', ?)",
    (hash_password("pass1"), now_iso()),
)
conn.commit()
conn.close()

app = main_module.app
app.testing = True

stock_before = get_current_stock(get_conn(), product_id, warehouse_id)
check("Остаток до отмены занижен (34 = 40 - 6)", stock_before == 34)

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)

    resp = client.post("/wb-diagnostics/undo-backfill", data={}, follow_redirects=True)
    check("Без confirm_text — не 500-ошибка", resp.status_code == 200)
    check("Без confirm_text остаток не тронут (всё ещё 34)", get_current_stock(get_conn(), product_id, warehouse_id) == 34)

    resp = client.post("/wb-diagnostics/undo-backfill", data={"confirm_text": "ОТМЕНИТЬ"})
    check("С верным confirm_text — страница отчёта открылась (200)", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("В отчёте видно, что удалено 1 движение", "1" in body)

stock_after = get_current_stock(get_conn(), product_id, warehouse_id)
check("Остаток вернулся к 40 после отмены через HTTP-маршрут", stock_after == 40)

with app.test_client() as client:
    client.post("/login", data={"username": "manager1", "password": "pass1"}, follow_redirects=True)
    resp = client.post("/wb-diagnostics/undo-backfill", data={"confirm_text": "ОТМЕНИТЬ"}, follow_redirects=False)
    check(
        "Обычный пользователь (не админ) получает редирект, а не отчёт",
        resp.status_code in (302, 303) and "undo" not in resp.headers.get("Location", ""),
    )

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки маршрута /wb-diagnostics/undo-backfill пройдены успешно ({passed}).")
