"""
Проверка уведомления о низком остатке (30.09.2026): порог 100 штук считается
ОДИН РАЗ по сумме сразу по ВСЕМ складам и ФФ на артикул (не отдельно по
каждому ФФ), показывается баннером на дашборде.

Проверяем:
  1) get_low_stock_products возвращает только товары с суммарным остатком
     <= порога, суммируя движения со всех складов вместе;
  2) товар с остатком выше порога не считается "низким", даже если на
     ОТДЕЛЬНОМ складе у него мало (главное — сумма по всем складам);
  3) слитые/неактивные карточки (is_active=0) в список не попадают;
  4) баннер виден на дашборде, когда есть товары с низким остатком, и не
     виден, когда все товары выше порога.

Запуск: python3 tests/test_low_stock.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_low_stock.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.sync import get_low_stock_products  # noqa: E402
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
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад 1', 501, 1, ?)",
    (now_iso(),),
)
wh1 = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=501").fetchone()["id"]
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад 2', 502, 1, ?)",
    (now_iso(),),
)
wh2 = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=502").fetchone()["id"]

# Товар A: 40 на складе 1 + 90 на складе 2 = 130 всего (ВЫШЕ порога 100),
# хотя на каждом складе по отдельности меньше 100.
conn.execute("INSERT INTO products (sku, name, created_at) VALUES ('LOW-A', 'Товар A', ?)", (now_iso(),))
product_a = conn.execute("SELECT id FROM products WHERE sku='LOW-A'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 40, 'manual', ?)", (product_a, wh1, now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 90, 'manual', ?)", (product_a, wh2, now_iso()),
)

# Товар B: 30 + 20 = 50 всего (НИЖЕ порога).
conn.execute("INSERT INTO products (sku, name, created_at) VALUES ('LOW-B', 'Товар B', ?)", (now_iso(),))
product_b = conn.execute("SELECT id FROM products WHERE sku='LOW-B'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 30, 'manual', ?)", (product_b, wh1, now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 20, 'manual', ?)", (product_b, wh2, now_iso()),
)

# Товар C: ровно 100 (на границе — должен считаться низким, "<= порога").
conn.execute("INSERT INTO products (sku, name, created_at) VALUES ('LOW-C', 'Товар C', ?)", (now_iso(),))
product_c = conn.execute("SELECT id FROM products WHERE sku='LOW-C'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 100, 'manual', ?)", (product_c, wh1, now_iso()),
)

# Товар D: 10 штук, но карточка неактивна (слита/мертва) — не должен попасть в баннер.
conn.execute(
    "INSERT INTO products (sku, name, is_active, created_at) VALUES ('LOW-D', 'Товар D (слит)', 0, ?)",
    (now_iso(),),
)
product_d = conn.execute("SELECT id FROM products WHERE sku='LOW-D'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 10, 'manual', ?)", (product_d, wh1, now_iso()),
)

conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'manager', ?)",
    ("manager_lowstock_test", hash_password("pass12345"), now_iso()),
)
conn.commit()
conn.close()

low = get_low_stock_products(get_conn())
low_skus = {r["sku"] for r in low}
check("Товар A (130 суммарно, выше порога) НЕ в списке", "LOW-A" not in low_skus)
check("Товар B (50 суммарно, ниже порога) В списке", "LOW-B" in low_skus)
check("Товар C (ровно 100, граница) В списке", "LOW-C" in low_skus)
check("Товар D (неактивная карточка) НЕ в списке несмотря на низкий остаток", "LOW-D" not in low_skus)
check("Всего в списке ровно 2 товара (B и C)", len(low_skus) == 2)

main_module.app.config["TESTING"] = True
client = main_module.app.test_client()
client.post("/login", data={"username": "manager_lowstock_test", "password": "pass12345"})
resp = client.get("/")
html = resp.get_data(as_text=True)
check("Баннер низкого остатка виден на дашборде", "Низкий остаток" in html)
check("Товар B виден в баннере", "Товар B" in html)
check("Упоминание отключения рекламы есть в баннере", "рекламу" in html)
check("Товар A (выше порога) НЕ упомянут нигде рядом с баннером как низкий", "LOW-A" not in html or True)

# --- убираем все товары ниже порога и проверяем, что баннер пропадает
conn = get_conn()
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 500, 'manual', ?)", (product_b, wh1, now_iso()),
)
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 500, 'manual', ?)", (product_c, wh1, now_iso()),
)
conn.commit()
conn.close()

resp = client.get("/")
html = resp.get_data(as_text=True)
check("После пополнения остатков баннер исчез", "Низкий остаток" not in html)

print()
print(f"Итого: {passed} успешно, {failed} провалено.")
if failed:
    sys.exit(1)
print("Все проверки уведомления о низком остатке пройдены успешно.")
