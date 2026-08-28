"""
Проверка страницы «Ozon» — остатки Ozon от 28.08.2026, полностью вручную
(без подключения к Ozon API, по запросу Алёны — отдельная вкладка, отдельно
от WB, чтобы не путать один с другим).

Проверяем:
  1) страница /ozon открывается любому вошедшему пользователю, по умолчанию
     остаток 0 для товара, у которого ещё не было записи в ozon_stock;
  2) /ozon/set создаёт запись при первом сохранении и пишет запись в
     ozon_stock_log (old=0, new=<значение>);
  3) повторное сохранение ОБНОВЛЯЕТ ту же запись (не плодит дубликаты) и
     добавляет новую строку в лог (old=<пред. значение>, new=<новое>);
  4) отрицательное количество и нечисловое значение отклоняются, запись не
     меняется;
  5) обычный пользователь без прав на редактирование (can_edit=False —
     в этом приложении такой роли нет по умолчанию, поэтому проверяем на
     уровне функции can_edit через прямой POST от НЕ залогиненного —
     редирект на логин) — здесь также проверяем, что просмотр НЕ требует
     прав редактирования, только логина;
  6) остатки Ozon НИКАК не отражаются на общем остатке WB (get_current_stock
     по складам WB не меняется).

Запуск: python3 tests/test_ozon.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_ozon.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
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


init_db()
conn = get_conn()
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад', 1, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-O', 1, 'BC-O', 'Товар O', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='BC-O'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 50, 'manual', ?)", (product_id, warehouse_id, now_iso()),
)
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('viewer1', ?, 'viewer', ?)",
    (hash_password("passv"), now_iso()),
)
conn.commit()
conn.close()

app = main_module.app
app.testing = True

# --- 1) незалогиненный получает редирект на логин
with app.test_client() as client:
    resp = client.get("/ozon", follow_redirects=False)
    check("Незалогиненный: редирект (не 200)", resp.status_code in (302, 303))

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    resp = client.get("/ozon")
    check("Админ: страница /ozon открывается (200)", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("По умолчанию остаток товара на Ozon = 0 (строки ещё не было)", ">0<" in body or "value=\"0\"" in body)

# --- 2) первое сохранение — создаёт запись + лог
with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    resp = client.post(
        "/ozon/set", data={"product_id": str(product_id), "quantity": "12", "comment": "первичный ввод"},
        follow_redirects=True,
    )
    check("Первое сохранение — 200 OK", resp.status_code == 200)

conn = get_conn()
row = conn.execute("SELECT * FROM ozon_stock WHERE product_id = ?", (product_id,)).fetchone()
check("Запись в ozon_stock создана", row is not None)
check("Количество = 12", row["quantity"] == 12)
log_rows = conn.execute("SELECT * FROM ozon_stock_log WHERE product_id = ? ORDER BY id", (product_id,)).fetchall()
check("В логе одна запись", len(log_rows) == 1)
check("Лог: old_quantity=0, new_quantity=12", log_rows[0]["old_quantity"] == 0 and log_rows[0]["new_quantity"] == 12)
conn.close()

# --- 3) повторное сохранение — обновляет, не дублирует
with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    client.post("/ozon/set", data={"product_id": str(product_id), "quantity": "30"}, follow_redirects=True)

conn = get_conn()
rows_count = conn.execute("SELECT COUNT(*) AS c FROM ozon_stock WHERE product_id = ?", (product_id,)).fetchone()["c"]
check("После повторного сохранения запись в ozon_stock всё ещё одна (не дубль)", rows_count == 1)
row2 = conn.execute("SELECT * FROM ozon_stock WHERE product_id = ?", (product_id,)).fetchone()
check("Количество обновилось до 30", row2["quantity"] == 30)
log_rows2 = conn.execute("SELECT * FROM ozon_stock_log WHERE product_id = ? ORDER BY id", (product_id,)).fetchall()
check("В логе теперь 2 записи", len(log_rows2) == 2)
check("Вторая запись лога: old=12, new=30", log_rows2[1]["old_quantity"] == 12 and log_rows2[1]["new_quantity"] == 30)
conn.close()

# --- 4) отрицательное и нечисловое количество отклоняются
with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    resp = client.post("/ozon/set", data={"product_id": str(product_id), "quantity": "-5"}, follow_redirects=True)
    check("Отрицательное количество: ошибка показана", "не может быть отрицательным" in resp.get_data(as_text=True))
    resp2 = client.post("/ozon/set", data={"product_id": str(product_id), "quantity": "abc"}, follow_redirects=True)
    check("Нечисловое количество: ошибка показана", "целым числом" in resp2.get_data(as_text=True))

conn = get_conn()
row3 = conn.execute("SELECT * FROM ozon_stock WHERE product_id = ?", (product_id,)).fetchone()
check("После неудачных попыток количество не изменилось (всё ещё 30)", row3["quantity"] == 30)
log_count_final = conn.execute("SELECT COUNT(*) AS c FROM ozon_stock_log WHERE product_id = ?", (product_id,)).fetchone()["c"]
check("Неудачные попытки не добавили записей в лог (их по-прежнему 2)", log_count_final == 2)
conn.close()

# --- 5) viewer: может смотреть страницу, но не может сохранять
with app.test_client() as client:
    client.post("/login", data={"username": "viewer1", "password": "passv"}, follow_redirects=True)
    resp = client.get("/ozon")
    check("viewer: страница /ozon открывается (только просмотр)", resp.status_code == 200)
    resp2 = client.post("/ozon/set", data={"product_id": str(product_id), "quantity": "99"}, follow_redirects=True)
    check("viewer: сохранение отклонено (нет прав)", "Недостаточно прав" in resp2.get_data(as_text=True))

conn = get_conn()
row_after_viewer = conn.execute("SELECT * FROM ozon_stock WHERE product_id = ?", (product_id,)).fetchone()
check("viewer не смог изменить количество (всё ещё 30)", row_after_viewer["quantity"] == 30)
conn.close()

# --- 6) остатки Ozon никак не влияют на остаток WB
conn = get_conn()
wb_stock = get_current_stock(conn, product_id, warehouse_id)
check("Остаток WB не изменился от правок Ozon (всё ещё 50)", wb_stock == 50)
conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки страницы Ozon пройдены успешно ({passed}).")
