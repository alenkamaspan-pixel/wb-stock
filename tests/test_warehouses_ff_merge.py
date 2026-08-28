"""
28.08.2026: страницы «Склады» и «ФФ» объединены в одну (/warehouses) — по
просьбе Алёны, навигация была тесной и съезжала на два ряда. Роуты создания/
изменения/удаления ФФ и складов (main.py) НЕ менялись по сути — просто
теперь оба списка рендерятся на одной странице, а старый /fulfillment-centers
стал редиректом (не 404), на случай если где-то была прямая ссылка/закладка.

Проверяем:
  1) /warehouses показывает и раздел «Фулфилмент-центры (ФФ)», и «Список
     складов» на одной странице;
  2) /fulfillment-centers (GET) редиректит на /warehouses, а не 404;
  3) создание/изменение/удаление ФФ по-прежнему работает и видно на
     объединённой странице (все /fulfillment-centers/... POST-маршруты живы);
  4) права доступа не изменились — viewer не может ничего создавать/менять;
  5) обычное управление складами (создание/изменение) по-прежнему работает.

Запуск: python3 tests/test_warehouses_ff_merge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_warehouses_ff_merge.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import get_conn, now_iso  # noqa: E402
from app.auth import hash_password  # noqa: E402
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
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('viewer1', ?, 'viewer', ?)",
    (hash_password("passv"), now_iso()),
)
conn.commit()
conn.close()

app = main_module.app
app.testing = True

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)

    # --- 1) объединённая страница показывает оба раздела
    resp = client.get("/warehouses")
    check("/warehouses открывается (200)", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("На /warehouses есть раздел «Фулфилмент-центры (ФФ)»", "Фулфилмент-центры" in body)
    check("На /warehouses есть раздел «Список складов»", "Список складов" in body)

    # --- 2) старый адрес редиректит, а не 404
    resp = client.get("/fulfillment-centers", follow_redirects=False)
    check("/fulfillment-centers редиректит (302/303)", resp.status_code in (302, 303))
    check("...именно на /warehouses", resp.headers.get("Location", "").rstrip("/").endswith("/warehouses"))
    resp_follow = client.get("/fulfillment-centers", follow_redirects=True)
    check("/fulfillment-centers с follow_redirects даёт 200 (не 404)", resp_follow.status_code == 200)

    # --- 3) создание ФФ по-прежнему работает, видно на объединённой странице
    resp = client.post("/fulfillment-centers/new", data={"name": "Тестовый ФФ"}, follow_redirects=True)
    check("Создание ФФ — 200 OK", resp.status_code == 200)
    check("Новый ФФ виден на /warehouses", "Тестовый ФФ" in resp.get_data(as_text=True))

conn = get_conn()
ff_row = conn.execute("SELECT * FROM fulfillment_centers WHERE name='Тестовый ФФ'").fetchone()
check("ФФ реально создан в базе", ff_row is not None)
ff_id = ff_row["id"]
conn.close()

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)

    # создаём склад и привязываем его к ФФ через редактирование ФФ
    resp = client.post(
        "/warehouses/new", data={"name": "Тестовый склад", "wb_warehouse_id": "999"}, follow_redirects=True,
    )
    check("Создание склада — 200 OK", resp.status_code == 200)

    conn = get_conn()
    wh_id = conn.execute("SELECT id FROM warehouses WHERE name='Тестовый склад'").fetchone()["id"]
    conn.close()

    resp = client.post(
        f"/fulfillment-centers/{ff_id}/edit",
        data={"name": "Тестовый ФФ", "is_active": "1", "warehouse_ids": [str(wh_id)]},
        follow_redirects=True,
    )
    check("Изменение ФФ (привязка склада) — 200 OK", resp.status_code == 200)

conn = get_conn()
wh_after = conn.execute("SELECT * FROM warehouses WHERE id = ?", (wh_id,)).fetchone()
check("Склад теперь привязан к тестовому ФФ", wh_after["fulfillment_center_id"] == ff_id)
conn.close()

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    resp = client.post(f"/fulfillment-centers/{ff_id}/delete", follow_redirects=True)
    check("Удаление ФФ — 200 OK", resp.status_code == 200)

conn = get_conn()
check("ФФ удалён из базы", conn.execute("SELECT id FROM fulfillment_centers WHERE id = ?", (ff_id,)).fetchone() is None)
wh_after_delete = conn.execute("SELECT * FROM warehouses WHERE id = ?", (wh_id,)).fetchone()
check("Склад НЕ удалён, просто отвязан от ФФ (fulfillment_center_id стал NULL)",
      wh_after_delete is not None and wh_after_delete["fulfillment_center_id"] is None)
conn.close()

# --- 4) права: viewer не может ничего создавать/менять
with app.test_client() as client:
    client.post("/login", data={"username": "viewer1", "password": "passv"}, follow_redirects=True)
    resp = client.post("/fulfillment-centers/new", data={"name": "Чужой ФФ"}, follow_redirects=True)
    check("viewer: создание ФФ отклонено", "Недостаточно прав" in resp.get_data(as_text=True))
    resp2 = client.post("/warehouses/new", data={"name": "Чужой склад"}, follow_redirects=True)
    check("viewer: создание склада отклонено", "Недостаточно прав" in resp2.get_data(as_text=True))

conn = get_conn()
check("Чужой ФФ от viewer не создан", conn.execute(
    "SELECT id FROM fulfillment_centers WHERE name='Чужой ФФ'").fetchone() is None)
conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки объединения «Склады + ФФ» пройдены успешно ({passed}).")
