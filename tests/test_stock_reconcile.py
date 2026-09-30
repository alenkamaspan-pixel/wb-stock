"""
Страница «Сверка остатков» (/stock/reconcile, 30.09.2026): вместо того чтобы
руками считать нужное приход/списание, Алёна вводит по каждому товару и
ФФ (виртуальные склады внутри ФФ уже просуммированы в одну цифру — по
просьбе Алёны от 30.09.2026, они не показываются по отдельности) ФАКТИЧЕСКОЕ
количество, а система сама вычисляет и пишет одну корректирующую запись
(movement_type=adjustment) на разницу.

Проверяем:
  1) GET показывает по ФФ суммарный остаток сразу по ОБОИМ виртуальным
     складам этого ФФ (а не только по одному);
  2) POST с одним изменённым значением создаёт ровно одну корректировку на
     верную разницу, посчитанную от суммы по ФФ, а не от одного склада;
  3) пустые поля игнорируются — остальные остатки не трогаются;
  4) если ввели то же самое число, что и сейчас (сумма по ФФ) — движение не
     создаётся;
  5) можно обнулить остаток отдельного склада без ФФ (кейс "Без ФФ", который
     и запросила Алёна);
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
# ДВА виртуальных склада внутри ОДНОГО ФФ — именно это Алёна попросила
# суммировать в одну строку, а не показывать по отдельности.
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, fulfillment_center_id, is_active, created_at) "
    "VALUES ('Склад тест Сверка А', 888, ?, 1, ?)", (ff_id, now_iso()),
)
warehouse_a = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=888").fetchone()["id"]
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, fulfillment_center_id, is_active, created_at) "
    "VALUES ('Склад тест Сверка Б (виртуальный)', 890, ?, 1, ?)", (ff_id, now_iso()),
)
warehouse_b = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=890").fetchone()["id"]

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

# товар А: 50 на складе А + 8 на складе Б (том же ФФ) = 58 суммарно по ФФ
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 50, 'manual', ?)",
    (product_a, warehouse_a, now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 8, 'manual', ?)",
    (product_a, warehouse_b, now_iso()),
)
# товар Б: только на складе А, 20
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 20, 'manual', ?)",
    (product_b, warehouse_a, now_iso()),
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

check("До сверки: товар А, склад А = 50", get_current_stock(get_conn(), product_a, warehouse_a) == 50)
check("До сверки: товар А, склад Б = 8", get_current_stock(get_conn(), product_a, warehouse_b) == 8)
check("До сверки: товар Б, склад А = 20", get_current_stock(get_conn(), product_b, warehouse_a) == 20)
check("До сверки: товар А без ФФ = 5", get_current_stock(get_conn(), product_a, no_ff_warehouse_id) == 5)

# --- GET показывает СУММУ по ФФ (50+8=58), а не отдельные склады
resp = client.get("/stock/reconcile")
html = resp.get_data(as_text=True)
check("Страница открывается", resp.status_code == 200)
check("На странице виден заголовок ФФ", "ФФ «ФФ Тест Сверка»" in html)
check("Видна СУММА по ФФ для товара А (58), а не отдельный склад (50 или 8)", ">58<" in html)
check("Отдельные названия виртуальных складов НЕ показаны", "виртуальный" not in html)
check("Видна сумма для товара Б (20)", ">20<" in html)
check("Виден блок отдельного склада без ФФ", "Склад без ФФ Сверка" in html)

# найдём имя инпута для товара А в этом ФФ, чтобы использовать тот же
# warehouse_id (представитель), что выбрала сама страница
import re  # noqa: E402
m = re.search(rf'name="actual_(\d+)_{product_a}"', html)
check("Нашли поле ввода для товара А (представитель склада)", m is not None)
rep_warehouse_id = int(m.group(1))
check("Представитель — один из складов этого ФФ (А или Б)", rep_warehouse_id in (warehouse_a, warehouse_b))

# --- POST: меняем товар А на ФФ (58 -> 70), товар Б не трогаем
resp = client.post(
    "/stock/reconcile",
    data={
        "note": "тестовая сверка",
        f"actual_{rep_warehouse_id}_{product_a}": "70",
        f"actual_{rep_warehouse_id}_{product_b}": "",
        f"actual_{no_ff_warehouse_id}_{product_a}": "",
    },
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Сообщение о внесённой корректировке", "Внесено корректировок: 1" in html)

total_a_after = get_current_stock(get_conn(), product_a, warehouse_a) + get_current_stock(get_conn(), product_a, warehouse_b)
check(f"Сумма по ФФ для товара А стала 70 (получено {total_a_after})", total_a_after == 70)
check("Товар Б на ФФ не тронут (остался 20)", get_current_stock(get_conn(), product_b, warehouse_a) == 20)
check("Товар А без ФФ не тронут (остался 5, пустое поле)", get_current_stock(get_conn(), product_a, no_ff_warehouse_id) == 5)

conn = get_conn()
adj = conn.execute(
    "SELECT * FROM stock_movements WHERE movement_type='adjustment' AND product_id=?", (product_a,)
).fetchone()
check("Создана ровно одна корректирующая запись для товара А", adj is not None)
check("Разница посчитана верно от суммы по ФФ (70-58=12)", adj is not None and adj["delta"] == 12)
check("Корректировка записана на склад-представитель этого ФФ", adj is not None and adj["warehouse_id"] == rep_warehouse_id)
check("Источник — manual", adj is not None and adj["source"] == "manual")
check(
    "Комментарий содержит имя пользователя и пометку",
    adj is not None and "manager_recon_test" in adj["comment"] and "тестовая сверка" in adj["comment"],
)
conn.close()

# --- то же число, что и сейчас (сумма по ФФ) — движение не создаётся
resp = client.post(
    "/stock/reconcile",
    data={"note": "", f"actual_{rep_warehouse_id}_{product_b}": "20"},
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Совпадающее значение: сообщение 'ничего не изменилось'", "Ничего не изменилось" in html)
check("Товар Б всё ещё 20 (движение не создано)", get_current_stock(get_conn(), product_b, warehouse_a) == 20)
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
    data={f"actual_{rep_warehouse_id}_{product_b}": "999"},
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Viewer: недостаточно прав", "Недостаточно прав" in html)
check("Viewer не смог изменить остаток товара Б (всё ещё 20)", get_current_stock(get_conn(), product_b, warehouse_a) == 20)

print()
print(f"Итого: {passed} успешно, {failed} провалено.")
if failed:
    sys.exit(1)
print("Все проверки страницы «Сверка остатков» пройдены успешно.")
