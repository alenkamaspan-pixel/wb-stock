"""
Точечное исправление одного испорченного поля на карточке (без слияния
карточек) — механизм _apply_field_fixes, список _FIELD_FIXES.

Сценарий 30.09.2026: у «1.4 Шейвер CR-9690» (nm_id=499229213) был указан
Ozon SKU 5439425498, которого на самом деле не существует на Ozon (Алёна
проверила и подтвердила) — карточку НЕ сливаем с «1Шейвер CR9690_2» (это
разные карточки на один физический товар, списания по ним намеренно
раздельные), просто чистим неверное значение поля.

Проверяем: значение действительно очищается, идемпотентность повторного
запуска, и что при НЕОЖИДАННОМ текущем значении (Алёна уже сама поправила
руками до деплоя) ничего не перезатирается.

Запуск: python3 tests/test_field_fixes.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_field_fixes.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso, _apply_field_fixes  # noqa: E402

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
conn.execute("DELETE FROM schema_migrations WHERE name LIKE '2026_09_30_fix_field_%'")

conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, ozon_sku, name, created_at) VALUES "
    "('1.4 Шейвер CR-9690', 499229213, '2045398929227', 5439425498, '1.4 Шейвер CR-9690', ?)",
    (now_iso(),),
)
conn.execute(
    "INSERT INTO products (sku, ozon_sku, name, created_at) VALUES "
    "('1Шейвер CR9690_2', 5708841384, '1Шейвер CR9690_2', ?)",
    (now_iso(),),
)
conn.commit()
conn.close()

conn = get_conn()
_apply_field_fixes(conn)
conn.close()

conn = get_conn()
fixed = conn.execute("SELECT * FROM products WHERE nm_id=499229213").fetchone()
other = conn.execute("SELECT * FROM products WHERE ozon_sku=5708841384").fetchone()
check("Ошибочный Ozon SKU у CR-9690 очищен (NULL)", fixed["ozon_sku"] is None)
check("Карточка НЕ слита — осталась активной, sku/имя не тронуты",
      fixed["is_active"] == 1 and fixed["sku"] == "1.4 Шейвер CR-9690")
check("Вторая карточка «1Шейвер CR9690_2» отдельная и не тронута",
      other is not None and other["ozon_sku"] == 5708841384 and other["is_active"] == 1)
check("Обе карточки — РАЗНЫЕ id (не слиты в одну)", fixed["id"] != other["id"])
conn.close()

# --- идемпотентность
conn = get_conn()
_apply_field_fixes(conn)
conn.close()
conn = get_conn()
still_null = conn.execute("SELECT ozon_sku FROM products WHERE nm_id=499229213").fetchone()["ozon_sku"]
check("Повторный запуск: значение по-прежнему NULL (не сломалось)", still_null is None)
conn.close()

# --- защита: если Алёна уже сама поправила поле на что-то другое до деплоя,
# наш фикс это не должен перезатирать. DATABASE_PATH читается один раз при
# импорте app.config, поэтому проверяем это в отдельном процессе — со своей
# свежей базой — а не переключением os.environ внутри уже запущенного.
import subprocess  # noqa: E402

TEST_DB2 = "/tmp/wb_stock_test_field_fixes_2.db"
if os.path.exists(TEST_DB2):
    os.remove(TEST_DB2)
script = f"""
import os, sys
sys.path.insert(0, {os.path.dirname(os.path.dirname(os.path.abspath(__file__)))!r})
os.environ["DATABASE_PATH"] = {TEST_DB2!r}
os.environ["SECRET_KEY"] = "test"
from app.database import init_db, get_conn, now_iso, _apply_field_fixes
init_db()
conn = get_conn()
conn.execute("DELETE FROM schema_migrations WHERE name LIKE '2026_09_30_fix_field_%'")
conn.execute(
    "INSERT INTO products (sku, nm_id, ozon_sku, name, created_at) VALUES "
    "('1.4 Шейвер CR-9690', 499229213, 9999999, '1.4 Шейвер CR-9690', ?)",
    (now_iso(),),
)
conn.commit()
conn.close()
conn = get_conn()
_apply_field_fixes(conn)
conn.close()
conn = get_conn()
untouched = conn.execute("SELECT ozon_sku FROM products WHERE nm_id=499229213").fetchone()["ozon_sku"]
print("RESULT", untouched)
"""
proc = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True)
check(
    "Если значение уже другое (правка руками до деплоя) — не перезатёрто",
    "RESULT 9999999" in proc.stdout,
)
if "RESULT 9999999" not in proc.stdout:
    print(proc.stdout, proc.stderr)

print()
print(f"Итого: {passed} успешно, {failed} провалено.")
if failed:
    sys.exit(1)
print("Все проверки точечных исправлений полей пройдены успешно.")
