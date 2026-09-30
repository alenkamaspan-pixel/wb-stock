"""
Проверка раздела «Внешние списания» (30.09.2026): FBO WB / FBO Ozon / FBS
Ozon — один общий раздел с тремя вкладками, каждое списание одновременно
(а) уменьшает обычный остаток склада (stock_movements) и (б) пишется в
журнал external_writeoffs с пометкой направления (kind).

Проверяем:
  1) списание в любую из трёх вкладок реально уменьшает остаток на складе;
  2) в external_writeoffs появляется запись с правильным kind/количеством/
     ссылкой на созданное движение;
  3) страница показывает только записи выбранной вкладки (?kind=...);
  4) без прав на редактирование (viewer) списать нельзя;
  5) старые маршруты авто-загрузки поставок Ozon FBO отключены и ничего не
     меняют (ни в базе, ни в внешних вызовах — их там больше нет).

Запуск: python3 tests/test_external_writeoffs.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_external_writeoffs.db"
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
    "INSERT INTO fulfillment_centers (name, is_active, created_at) VALUES ('ФФ Тест', 1, ?)",
    (now_iso(),),
)
ff_id = conn.execute("SELECT id FROM fulfillment_centers WHERE name='ФФ Тест'").fetchone()["id"]
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, fulfillment_center_id, is_active, created_at) "
    "VALUES ('Склад тест', 777, ?, 1, ?)", (ff_id, now_iso()),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=777").fetchone()["id"]

conn.execute(
    "INSERT INTO products (sku, name, created_at) VALUES ('EXT-TEST', 'Товар для внешних списаний', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE sku='EXT-TEST'").fetchone()["id"]

conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 100, 'manual', ?)",
    (product_id, warehouse_id, now_iso()),
)

conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'manager', ?)",
    ("manager_ext_test", hash_password("pass12345"), now_iso()),
)
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'viewer', ?)",
    ("viewer_ext_test", hash_password("pass12345"), now_iso()),
)
conn.commit()
conn.close()

main_module.app.config["TESTING"] = True
client = main_module.app.test_client()
client.post("/login", data={"username": "manager_ext_test", "password": "pass12345"})

stock_before = get_current_stock(get_conn(), product_id, warehouse_id)
check("До списания: остаток 100", stock_before == 100)

# --- списание в FBO WB
resp = client.post(
    "/external-writeoffs/new",
    data={
        "kind": "fbo_wb", "product_id": str(product_id), "location": f"wh:{warehouse_id}",
        "quantity": "30", "comment": "поставка 30.09",
    },
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Списание FBO WB подтверждено сообщением", "добавлено" in html)

stock_after_wb = get_current_stock(get_conn(), product_id, warehouse_id)
check(f"После списания FBO WB: остаток уменьшился на 30 (получено {stock_after_wb})", stock_after_wb == 70)

conn = get_conn()
entry_wb = conn.execute(
    "SELECT * FROM external_writeoffs WHERE kind='fbo_wb'"
).fetchone()
check("Запись в журнале FBO WB создана", entry_wb is not None)
check("Запись в журнале: количество 30", entry_wb["quantity"] == 30)
check("Запись в журнале: комментарий сохранён", entry_wb["comment"] == "поставка 30.09")
check("Запись в журнале: ФФ проставлен", entry_wb["ff_id"] == ff_id)
movement = conn.execute(
    "SELECT * FROM stock_movements WHERE id = ?", (entry_wb["movement_id"],)
).fetchone()
check("Запись ссылается на реальное движение-списание", movement is not None and movement["delta"] == -30)
check("Движение отмечено как обычное ручное списание (movement_type)", movement["movement_type"] == "writeoff")
conn.close()

# --- списание в FBO Ozon
client.post(
    "/external-writeoffs/new",
    data={
        "kind": "fbo_ozon", "product_id": str(product_id), "location": f"wh:{warehouse_id}",
        "quantity": "10", "comment": "",
    },
    follow_redirects=True,
)
# --- списание в FBS Ozon
client.post(
    "/external-writeoffs/new",
    data={
        "kind": "fbs_ozon", "product_id": str(product_id), "location": f"wh:{warehouse_id}",
        "quantity": "5", "comment": "",
    },
    follow_redirects=True,
)

stock_final = get_current_stock(get_conn(), product_id, warehouse_id)
check(f"После всех трёх списаний: остаток 100-30-10-5=55 (получено {stock_final})", stock_final == 55)

conn = get_conn()
count_fbo_wb = conn.execute("SELECT COUNT(*) AS c FROM external_writeoffs WHERE kind='fbo_wb'").fetchone()["c"]
count_fbo_ozon = conn.execute("SELECT COUNT(*) AS c FROM external_writeoffs WHERE kind='fbo_ozon'").fetchone()["c"]
count_fbs_ozon = conn.execute("SELECT COUNT(*) AS c FROM external_writeoffs WHERE kind='fbs_ozon'").fetchone()["c"]
check("Ровно 1 запись в FBO WB", count_fbo_wb == 1)
check("Ровно 1 запись в FBO Ozon", count_fbo_ozon == 1)
check("Ровно 1 запись в FBS Ozon", count_fbs_ozon == 1)
conn.close()

# --- страница показывает только записи выбранной вкладки
resp = client.get("/external-writeoffs?kind=fbo_ozon")
html = resp.get_data(as_text=True)
check("Страница FBO Ozon: своя запись видна (по количеству '-10')", "-10" in html)
check("Страница FBO Ozon: комментарий записи FBO WB не показан", "поставка 30.09" not in html)

# --- viewer не может списывать
client2 = main_module.app.test_client()
client2.post("/login", data={"username": "viewer_ext_test", "password": "pass12345"})
resp = client2.post(
    "/external-writeoffs/new",
    data={
        "kind": "fbo_wb", "product_id": str(product_id), "location": f"wh:{warehouse_id}",
        "quantity": "1", "comment": "",
    },
    follow_redirects=True,
)
html = resp.get_data(as_text=True)
check("Viewer: недостаточно прав", "Недостаточно прав" in html)
stock_after_viewer_attempt = get_current_stock(get_conn(), product_id, warehouse_id)
check("Viewer не смог списать — остаток не изменился (всё ещё 55)", stock_after_viewer_attempt == 55)

# --- старые авто-маршруты Ozon FBO отключены
resp = client.post("/ozon/supplies/refresh", data={}, follow_redirects=True)
html = resp.get_data(as_text=True)
check("Обновление списка поставок FBO Ozon отключено (сообщение об этом показано)", "отключен" in html)

resp = client.post("/ozon/supplies/999/load", data={}, follow_redirects=True)
html = resp.get_data(as_text=True)
check("Загрузка поставки FBO Ozon отключена (сообщение об этом показано)", "отключен" in html)

print()
print(f"Итого: {passed} успешно, {failed} провалено.")
if failed:
    sys.exit(1)
print("Все проверки внешних списаний пройдены успешно.")
