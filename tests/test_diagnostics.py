"""
Проверка страницы «Диагностика WB» (/wb-diagnostics) — она должна:
  1) корректно посчитать статусы заказов в нашей базе;
  2) найти заказы с нечисловым ID (никогда не проверяются на смену статуса);
  3) найти незавершённые заказы без склада / без списания;
  4) при точечной проверке ID показать ВЕСЬ сырой ответ WB (включая поля,
     которые обычный синк не читает, например wbStatus), а не только
     supplierStatus.

Запуск: python3 tests/test_diagnostics.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_diag.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import get_conn, now_iso  # noqa: E402
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


# bootstrap() уже создал таблицы и первого админа при импорте app.main
conn = get_conn()

# Товар и склад — минимально нужны для внешнего ключа в wb_orders/stock_movements
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('sku1', 1, 'bc1', 'Товар 1', ?)",
    (now_iso(),),
)
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад 1', 111, 1, ?)",
    (now_iso(),),
)
conn.commit()

# Заказ 1: обычный числовой ID, отслеживается, есть склад и списание — ни в
# одну "проблемную" категорию попадать не должен.
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('1000000001', 1, 1, 1, 'new', 1, ?, ?)""",
    (now_iso(), now_iso()),
)
# Заказ 2: нечисловой ID — должен попасть в раздел "нечисловые ID".
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('WB-ABC-2', 1, 1, 1, 'new', 1, ?, ?)""",
    (now_iso(), now_iso()),
)
# Заказ 3: числовой ID, но без склада и без списания — должен попасть в
# раздел "незавершённые без склада/списания".
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('1000000003', 1, NULL, 1, 'new', 0, ?, ?)""",
    (now_iso(), now_iso()),
)
# Заказ 4: уже отменён у нас — не должен считаться "отслеживаемым" ни в одном разделе.
conn.execute(
    """INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, status,
       stock_deducted, created_at, updated_at) VALUES ('1000000004', 1, 1, 1, 'cancel', 0, ?, ?)""",
    (now_iso(), now_iso()),
)
conn.commit()
conn.close()


class MockWBClient:
    """Имитирует ответ WB на POST /api/v3/orders/status, добавляя гипотетическое
    поле wbStatus, которого supplierStatus не покрывает — ровно тот случай,
    который эта страница должна суметь показать."""

    def get_orders_status(self, order_ids):
        results = []
        for oid in order_ids:
            if oid == 1000000001:
                results.append({"id": oid, "supplierStatus": "new", "wbStatus": "declined"})
            elif oid == 5601809825:
                results.append({"id": oid, "supplierStatus": "cancel", "wbStatus": "declined"})
            # 9999999999 намеренно не отдаём в ответе вообще — имитируем
            # заказ, о котором WB "не помнит" (или который никогда не
            # существовал под этим ID).
        return results


main_module.WBClient = MockWBClient
app = main_module.app
app.testing = True

with app.test_client() as client:
    resp = client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    check("Логин администратора прошёл успешно", resp.status_code == 200)

    resp = client.get("/wb-diagnostics")
    check("Страница диагностики открывается (200)", resp.status_code == 200)
    body = resp.get_data(as_text=True)

    check("Заказ с нечисловым ID показан в разделе 3", "WB-ABC-2" in body)
    check("Заказ без склада/списания показан в разделе 4", "1000000003" in body)
    check("Отменённый заказ НЕ попал в 'незавершённые' списки", body.count("1000000004") == 0)

    # Точечная проверка: один заказ есть и у нас, и в ответе WB (с доп. полем
    # wbStatus), второй — есть только в ответе WB (нет в нашей базе, ключевой
    # сценарий "отменили ДО того как заказ попал в нашу базу"), третий не
    # существует нигде.
    resp = client.get("/wb-diagnostics?order_ids=1000000001,5601809825,9999999999")
    check("Точечная проверка отвечает 200", resp.status_code == 200)
    body = resp.get_data(as_text=True)

    check("Поле wbStatus из сырого ответа WB показано на странице", "wbStatus" in body and "declined" in body)
    check(
        "Заказ, отменённый ДО появления в нашей базе, помечен как отсутствующий у нас",
        "WB о нём знает, а мы нет" in body,
    )
    check(
        "Заказ, которого нет ни у нас, ни в ответе WB, помечен как отсутствующий в ответе WB",
        body.count("5601809825") >= 1,
    )

# Доступ обычного (не-админа) пользователя должен быть закрыт
conn = get_conn()
from app.auth import hash_password  # noqa: E402
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('manager1', ?, 'manager', ?)",
    (hash_password("pass1"), now_iso()),
)
conn.commit()
conn.close()

with app.test_client() as client:
    client.post("/login", data={"username": "manager1", "password": "pass1"}, follow_redirects=True)
    resp = client.get("/wb-diagnostics", follow_redirects=False)
    check(
        "Обычный пользователь (не админ) не может открыть диагностику напрямую",
        resp.status_code in (302, 303) and "/wb-diagnostics" not in resp.headers.get("Location", ""),
    )

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки диагностики пройдены успешно ({passed}).")
