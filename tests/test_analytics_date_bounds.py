"""
28.08.2026: Алёна заметила, что «Журнал движений» на странице «Аналитика»
не показывает ничего за текущие сутки, хотя заказы и отмены совершенно
точно приходили (см. /wb-orders-log — там они есть). Причина: SQLite
datetime(...) возвращает границу диапазона с ПРОБЕЛОМ между датой и
временем, а created_at пишется через Python isoformat() с буквой 'T' (см.
now_iso() в database.py) — при сравнении строк 'T' больше пробела, поэтому
любая запись за ту же календарную дату, что и верхняя граница (date_to),
вылетала из BETWEEN независимо от времени суток. Иными словами — всё, что
случилось начиная с 00:00 UTC (=03:00 по Москве) СЕГОДНЯ, всегда пропадало
из «Аналитики», хотя реальные остатки (dashboard, /wb-orders-log) были верны
всё это время — баг был только в отчёте, не в самих данных.

Проверяем на движении, у которого created_at выставлен ЧЕРЕЗ ТОТ ЖЕ САМЫЙ
now_iso(), что использует всё остальное приложение (а не руками написанная
строка) — иначе тест мог бы случайно не воспроизвести баг форматом даты.

Запуск: python3 tests/test_analytics_date_bounds.py
"""
import os
import sys
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_analytics_date_bounds.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
from app.analytics import (  # noqa: E402
    get_period_stats, get_daily_series, get_cancellations_table, get_movements_journal,
)

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
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES ('SKU-D', 1, 'BC-D', 'Товар D', ?)",
    (now_iso(),),
)
product_id = conn.execute("SELECT id FROM products WHERE barcode='BC-D'").fetchone()["id"]

# Движение "прямо сейчас" — той же функцией now_iso(), что использует вся
# остальная синхронизация, а НЕ руками написанной строкой с другим форматом.
right_now = now_iso()
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'sale', -1, 'wb_sync', ?)", (product_id, warehouse_id, right_now),
)

# Движение-отмена, тоже "прямо сейчас" — именно такое Алёна ждала увидеть.
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'sale_reversal', 1, 'wb_sync', ?)", (product_id, warehouse_id, right_now),
)
conn.commit()
conn.close()

# Диапазон дат "как из <input type=date>" — сегодняшний день по МСК, ровно
# как формирует его /analytics по умолчанию (см. _analytics_params в main.py).
today_msk = (dt.datetime.utcnow() + dt.timedelta(hours=3)).date().isoformat()
date_from = date_to = today_msk

conn = get_conn()

period = get_period_stats(conn, date_from, date_to)
check(
    "get_period_stats: сегодняшнее движение НЕ потеряно из отчёта",
    len(period) == 1 and period[0]["product_id"] == product_id,
)
if period:
    check("get_period_stats: net_sold учитывает и продажу, и отмену (1 - 1 = 0)", period[0]["net_sold"] == 0)

daily = get_daily_series(conn, date_from, date_to)
check("get_daily_series: сегодняшний день присутствует в ряду", len(daily) == 1)

cancellations = get_cancellations_table(conn, date_from, date_to)
check(
    "get_cancellations_table: сегодняшняя отмена видна в отчёте",
    len(cancellations) == 1 and cancellations[0]["cancelled_qty"] == 1,
)

journal = get_movements_journal(conn, date_from, date_to)
check("get_movements_journal: обе сегодняшние записи присутствуют", len(journal) == 2)

conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки границ дат в аналитике пройдены успешно ({passed}).")
