"""
Страница «Сверка остатков» (/stock/reconcile, 30.09.2026): вместо того чтобы
руками считать нужное приход/списание, Алёна вводит по каждому товару и
месту хранения ФАКТИЧЕСКОЕ количество, а система сама вычисляет и пишет
одну корректирующую запись (movement_type=adjustment) на разницу.

Проверяем:
  1) GET показывает текущий остаток по каждому товару/складу;
  2) POST с одним изменённым значением создаёт ровно одну корректировку на
     верную разницу (и только для этой пары товар+склад);
  3) пустые поля игнорируются — остальные остатки не трогаются;
  4) если ввели то же самое число, что и сейчас — движение не создаётся
     (разница 0, незачем засорять историю);
  5) можно обнулить остаток (кейс "Без ФФ", который и запросила Алёна);
  6) viewer не может вносить корректировки;
  7) комментарий-пояснение («note») попадает в запись движения.

Запуск: python3 tests/test_stock_reconcile.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_stock_reconcile.db"
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
    "INSERT INTO fulfillment_centers (name, is_active, created_at) VALUES ('ФФ Тест Сверка', 1, ?)",
    (now_iso(),),
)
ff_id = conn.execute("SELECT id FROM fulfillment_centers WHERE name='ФФ Тест Сверка'").fetchone()["id"]
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, fulfillment_center_id, is_active, created_at) "
    "VALUES ('Склад тест Сверка', 888, ?, 1, ?)", (ff_id, now_iso()),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=888").fetchone()["id"]

# отдельный склад БЕЗ ФФ — именно такой кейс (мусорные остатки "Без ФФ")
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, fulfillment_center_id, is_active, created_at) "
    "VALUES ('Склад без ФФ Сверка', 889, NULL, 1, ?)", (now_iso(),),
)
no_ff_warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=889").fetchone()["id"]

conn.execute(
    "INSERT INTO products (sku, name, created_at) VALUES ('RECON-A', 'Товар А для сверки', ?)",
    (now_iso(),),
)
product_a = conn.execute("SELECT id FROM products WHERE sku='RECON-A'").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, name, created_at) VALUES ('RECON-B', 'Товар Б для сверки', ?)",
    (now_iso(),),
)
product_b = conn.execute("SELECT id FROM products WHERE sku='RECON-B'").fetchone()["id"]

conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 50, 'manual', ?)",
    (product_a, warehouse_id, now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 20, 'manual', ?)",
    (product_b, warehouse_id, now_iso()),
)
# "мусорный" остаток без ФФ — ровно то, что Алёна просила обнулить
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 5, 'manual', ?)",
    (product_a, no_ff_warehouse_id, now_iso()),
)

conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'manager', ?)",
    ("manager_recon_test", hash_password("pass12345"), now_iso()),
)
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'viewer', ?)",
    ("viewer_recon_test", hash_password("pass12345"), now_iso()),
)
conn.commit()
conn.close()

main_module.app.config["TESTING"] = True
client = main_module.app.test_client()
client.post("/login", data={"username": "manager_recon_test", "password": "pass12345"})

check("До сверки: товар А на складе с ФФ = 50", get_current_stock(get_conn(), product_a, warehouse_id) == 50)
check("До сверки: товар Б на складе с ФФ = 20", get_current_stock(get_conn(), product_b, warehouse_id) == 20)
check("До сверки: товар А без ФФ = 5", get_current_stock(get_conn(), product_a, no_ff_warehouse_id) == 5)

# --- GET показывает текущие остатки
resp = client.get("/stock/reconcile")
html = resp.get_data(as_text=True)
check("Страница открывается", resp.status_code == 200)
check("На странице виден текущий остаток товара А (50)", ">50<" in html)
check("На странице виден текущий остаток товара Б (20)", ">20<" in html)
check("На странице виден блок «Без ФФ»", "Без ФФ" in html)

# --- POST: меняем только товар А на складе с ФФ (50 -> 65), остальное пусто
resp = client.post(
    "/stock/reconcile",
    data={
        "note": "тестовая сверка",
        f"actual_{warehouse_id}_{product_a}": "65",
        f"actual_{warehouse_id}_{product_b}": "",
        f"actual_{no_ff_warehouse_id}_{product_a}": "",
    },
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Сообщение о внесённой корректировке", "Внесено корректировок: 1" in html)

check("Товар А на складе с ФФ стал 65", get_current_stock(get_conn(), product_a, warehouse_id) == 65)
check("Товар Б на складе с ФФ не тронут (остался 20)", get_current_stock(get_conn(), product_b, warehouse_id) == 20)
check("Товар А без ФФ не тронут (остался 5, пустое поле)", get_current_stock(get_conn(), product_a, no_ff_warehouse_id) == 5)

conn = get_conn()
adj = conn.execute(
    "SELECT * FROM stock_movements WHERE movement_type='adjustment' AND product_id=? AND warehouse_id=?",
    (product_a, warehouse_id),
).fetchone()
check("Создана ровно одна корректирующая запись", adj is not None)
check("Разница посчитана верно (65-50=15)", adj is not None and adj["delta"] == 15)
check("Источник — manual", adj is not None and adj["source"] == "manual")
check("Комментарий содержит имя пользователя и пометку", adj is not None and "manager_recon_test" in adj["comment"] and "тестовая сверка" in adj["comment"])
conn.close()

# --- то же число, что и сейчас — движение не создаётся
resp = client.post(
    "/stock/reconcile",
    data={"note": "", f"actual_{warehouse_id}_{product_b}": "20"},
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Совпадающее значение: сообщение 'ничего не изменилось'", "Ничего не изменилось" in html)
check("Товар Б всё ещё 20 (движение не создано)", get_current_stock(get_conn(), product_b, warehouse_id) == 20)
conn = get_conn()
count_b_adj = conn.execute(
    "SELECT COUNT(*) AS c FROM stock_movements WHERE movement_type='adjustment' AND product_id=?", (product_b,)
).fetchone()["c"]
check("Ни одной корректировки по товару Б не появилось", count_b_adj == 0)
conn.close()

# --- обнуление "мусорного" остатка без ФФ (тот самый запрошенный кейс)
resp = client.post(
    "/stock/reconcile",
    data={"note": "обнуление Без ФФ", f"actual_{no_ff_warehouse_id}_{product_a}": "0"},
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Обнуление: сообщение о корректировке", "Внесено корректировок: 1" in html)
check("Товар А без ФФ теперь 0", get_current_stock(get_conn(), product_a, no_ff_warehouse_id) == 0)
conn = get_conn()
adj_zero = conn.execute(
    "SELECT * FROM stock_movements WHERE movement_type='adjustment' AND product_id=? AND warehouse_id=?",
    (product_a, no_ff_warehouse_id),
).fetchone()
check("Корректировка на -5 (обнуление 5 -> 0)", adj_zero is not None and adj_zero["delta"] == -5)
conn.close()

# --- viewer не может вносить корректировки
client2 = main_module.app.test_client()
client2.post("/login", data={"username": "viewer_recon_test", "password": "pass12345"})
resp = client2.post(
    "/stock/reconcile",
    data={f"actual_{warehouse_id}_{product_b}": "999"},
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Viewer: недостаточно прав", "Недостаточно прав" in html)
check("Viewer не смог изменить остаток товара Б (всё ещё 20)", get_current_stock(get_conn(), product_b, warehouse_id) == 20)

print()
print(f"Итого: {passed} успешно, {failed} провалено.")
if failed:
    sys.exit(1)
print("Все проверки страницы «Сверка остатков» пройдены успешно.")
