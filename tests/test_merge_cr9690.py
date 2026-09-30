"""
Слияние «1.4 Шейвер CR-9690» и «1Шейвер CR9690_2» — третья находка Алёны
30.09.2026, и самая коварная из всех: сначала (в этот же день, раньше) она
попросила НЕ сливать эти карточки, только почистить у первой ошибочный Ozon
SKU 5439425498 (см. _FIELD_FIXES). Позже, увидев на дашборде расхождение
остатков (20 против -1), она передумала и попросила всё-таки слить —
«остатки одного товара не могут отличаться».

Дальше на боевом сайте вскрылось ещё одно: слияние наполовину повисло —
Ozon SKU дубля перенёсся на каноническую карточку, а сама деактивация дубля
и перенос его остатка — нет, и карточка-дубль осталась отдельной, активной,
с собственным (уже некорректным) остатком. Из-за того что дубль раньше
искали ПО ozon_sku, а не он сам уже потерял этот ozon_sku (он переехал на
каноническую), повторный деплой не мог сам себя починить — поиск дубля
находил каноническую карточку саму на себя. Исправлено:
  1) дубль теперь ищем по его собственному, никогда не переносимому sku;
  2) полнота слияния проверяется по факту (is_active дубля), а не по
     отдельному флагу в schema_migrations — поэтому "застрявшее" на середине
     слияние само доедет до конца на следующем деплое;
  3) если слияние всё-таки упадёт — ошибка пишется в migration_errors и не
     блокирует ни другие находки, ни остальные миграции.

Проверяем всё это через полный init_db()/_migrate(), а не вызовом
внутренних функций по отдельности — в том числе порядок между
_apply_field_fixes и _merge_additional_found_products.

Запуск: python3 tests/test_merge_cr9690.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_merge_cr9690.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402

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


# ============================================================ сценарий 1:
# обычный случай — обе карточки ещё полностью нетронуты (как в первый раз,
# когда Алёна это нашла), обе миграции (field-fix + merge) должны
# отработать вместе, в правильном порядке, за один деплой.
init_db()
conn = get_conn()
conn.execute("DELETE FROM schema_migrations WHERE name LIKE '2026_09_30_fix_field_%'")

conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад тест', 1, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]

# ровно как на живом сайте: у канонической — ошибочный Ozon SKU и остаток 20
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, ozon_sku, name, created_at) VALUES "
    "('1.4 Шейвер CR-9690', 499229213, '2045398929227', 5439425498, '1.4 Шейвер CR-9690', ?)",
    (now_iso(),),
)
canonical_id = conn.execute("SELECT id FROM products WHERE nm_id=499229213").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 20, 'manual', ?)",
    (canonical_id, warehouse_id, now_iso()),
)

# у дубля — настоящий Ozon SKU и остаток -1
conn.execute(
    "INSERT INTO products (sku, ozon_sku, name, created_at) VALUES "
    "('1Шейвер CR9690_2', 5708841384, '1Шейвер CR9690_2', ?)",
    (now_iso(),),
)
duplicate_id = conn.execute("SELECT id FROM products WHERE ozon_sku=5708841384").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'writeoff', -1, 'manual', ?)",
    (duplicate_id, warehouse_id, now_iso()),
)
conn.commit()
conn.close()

# --- имитируем следующий деплой: обе миграции применяются вместе, в
# реальном порядке из _migrate()
init_db()

conn = get_conn()
canonical = conn.execute("SELECT * FROM products WHERE id=?", (canonical_id,)).fetchone()
duplicate = conn.execute("SELECT * FROM products WHERE id=?", (duplicate_id,)).fetchone()

check("Каноническая карточка: имя/sku не тронуты (уже были в порядке)",
      canonical["name"] == "1.4 Шейвер CR-9690" and canonical["sku"] == "1.4 Шейвер CR-9690")
check("Каноническая карточка: настоящий Ozon SKU дубля перенёсся (не остался ошибочный)",
      canonical["ozon_sku"] == 5708841384)
check("Каноническая карточка: nm_id/штрихкод не потеряны",
      canonical["nm_id"] == 499229213 and canonical["barcode"] == "2045398929227")
check("Карточка-дубль: помечена неактивной", duplicate["is_active"] == 0)
check("Карточка-дубль: ozon_sku снят", duplicate["ozon_sku"] is None)

total = conn.execute(
    "SELECT COALESCE(SUM(delta), 0) AS q FROM stock_movements WHERE product_id=?", (canonical_id,)
).fetchone()["q"]
check(f"Итоговый остаток на канонической карточке = 19 (20 + (-1)), получено {total}", total == 19)
still_on_duplicate = conn.execute(
    "SELECT COUNT(*) AS c FROM stock_movements WHERE product_id=?", (duplicate_id,)
).fetchone()["c"]
check("На карточке-дубле движений больше нет (оба переехали на каноническую)", still_on_duplicate == 0)
errors_count = conn.execute("SELECT COUNT(*) AS c FROM migration_errors").fetchone()["c"]
check("Ошибок в migration_errors нет", errors_count == 0)
conn.close()

# --- идемпотентность (повторный деплой ничего не ломает)
conn = get_conn()
ozon_before = conn.execute("SELECT ozon_sku FROM products WHERE id=?", (canonical_id,)).fetchone()["ozon_sku"]
conn.close()
init_db()
conn = get_conn()
ozon_after = conn.execute("SELECT ozon_sku FROM products WHERE id=?", (canonical_id,)).fetchone()["ozon_sku"]
check("Повторный деплой: Ozon SKU канонической карточки не изменился повторно", ozon_before == ozon_after)
conn.close()

# ============================================================ сценарий 2:
# ИМЕННО ТО, ЧТО СЛУЧИЛОСЬ НА БОЕВОМ САЙТЕ — слияние "застряло" на середине:
# Ozon SKU уже перенёсся на каноническую, а дубль так и остался активным,
# сам по себе, со своим собственным остатком. Раньше повторный деплой ничего
# не чинил (дубля по ozon_sku было уже не найти — им теперь владеет
# каноническая). Проверяем, что теперь это само доезжает до конца.
TEST_DB2 = "/tmp/wb_stock_test_merge_cr9690_stuck.db"
if os.path.exists(TEST_DB2):
    os.remove(TEST_DB2)

# DATABASE_PATH читается один раз при импорте app.config (см. test_field_fixes.py) —
# поэтому сценарий 2 целиком выполняется в отдельном процессе со своей базой,
# а не переключением os.environ внутри уже запущенного.
import subprocess  # noqa: E402

setup_and_check_script = f"""
import os, sys
sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
os.environ["DATABASE_PATH"] = {TEST_DB2!r}
os.environ["SECRET_KEY"] = "test"
from app.database import init_db, get_conn, now_iso

init_db()
conn = get_conn()
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад тест 2', 1, 1, ?)",
    (now_iso(),),
)
wh2 = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]

# каноническая — УЖЕ с правильным перенесённым ozon_sku (как на бою)
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, ozon_sku, name, created_at) VALUES "
    "('1.4 Шейвер CR-9690', 499229213, '2045398929227', 5708841384, '1.4 Шейвер CR-9690', ?)",
    (now_iso(),),
)
canonical_id = conn.execute("SELECT id FROM products WHERE nm_id=499229213").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 20, 'manual', ?)",
    (canonical_id, wh2, now_iso()),
)
# дубль — уже БЕЗ ozon_sku (его уже сняли), но всё ещё АКТИВНЫЙ и со своим
# собственным остатком -1 — именно то самое "застрявшее на середине" состояние
conn.execute(
    "INSERT INTO products (sku, ozon_sku, name, is_active, created_at) VALUES "
    "('1Шейвер CR9690_2', NULL, '1Шейвер CR9690_2', 1, ?)",
    (now_iso(),),
)
duplicate_id = conn.execute("SELECT id FROM products WHERE sku='1Шейвер CR9690_2'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'writeoff', -1, 'manual', ?)",
    (duplicate_id, wh2, now_iso()),
)
conn.commit()
conn.close()

# --- ещё один деплой (та самая ситуация "загрузила код, а на дашборде опять
# то же самое" — до фикса это оставалось бы в подвешенном состоянии навсегда)
init_db()

conn = get_conn()
canonical = conn.execute("SELECT * FROM products WHERE id=?", (canonical_id,)).fetchone()
duplicate = conn.execute("SELECT * FROM products WHERE id=?", (duplicate_id,)).fetchone()
total = conn.execute(
    "SELECT COALESCE(SUM(delta), 0) AS q FROM stock_movements WHERE product_id=?", (canonical_id,)
).fetchone()["q"]
still_on_duplicate = conn.execute(
    "SELECT COUNT(*) AS c FROM stock_movements WHERE product_id=?", (duplicate_id,)
).fetchone()["c"]
print("RESULT", canonical["ozon_sku"], duplicate["is_active"], total, still_on_duplicate)
"""
proc = subprocess.run([sys.executable, "-c", setup_and_check_script], capture_output=True, text=True)
result_line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT")), "")
parts = result_line.split()

check("Застрявший сценарий: подпроцесс отработал без ошибок", proc.returncode == 0)
if proc.returncode != 0 or not parts:
    print(proc.stdout, proc.stderr)
    canonical_ozon_sku, duplicate_is_active, total, still_on_duplicate = None, None, None, None
else:
    canonical_ozon_sku = int(parts[1])
    duplicate_is_active = int(parts[2])
    total = int(parts[3])
    still_on_duplicate = int(parts[4])

check("Застрявший сценарий: каноническая карточка ozon_sku не потеряла (5708841384)",
      canonical_ozon_sku == 5708841384)
check("Застрявший сценарий: карточка-дубль теперь ДЕАКТИВИРОВАНА", duplicate_is_active == 0)
check(f"Застрявший сценарий: остаток на канонической = 19 (20 + (-1)), получено {total}", total == 19)
check("Застрявший сценарий: движений на дубле больше нет", still_on_duplicate == 0)

print()
print(f"Итого: {passed} успешно, {failed} провалено.")
if failed:
    sys.exit(1)
print("Все проверки слияния CR-9690 (включая самовосстановление застрявшего слияния) пройдены успешно.")
