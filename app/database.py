"""
Слой работы с БД.

Для скорости и надёжности MVP используется встроенный в Python sqlite3 —
никаких дополнительных пакетов ставить не нужно ни здесь, ни при деплое.
Если объём вырастет (много пользователей одновременно, тысячи SKU),
это единственное место, которое придётся поменять на Postgres — вся
остальная логика работает через функции db_query/db_execute ниже и её
трогать не придётся. Это осознанное решение под её текущий масштаб (см. README,
раздел «Известные ограничения и что доделать при росте»).
"""
import sqlite3
import datetime as dt
from contextlib import contextmanager

from app.config import DATABASE_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'manager',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fulfillment_centers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    -- ID склада в WB — нужен только чтобы сопоставлять входящие заказы с
    -- вашим складом. Приложение никогда не пишет по этому ID обратно в WB.
    wb_warehouse_id INTEGER UNIQUE,
    -- Один физический ФФ (фулфилмент-центр) может обслуживать сразу
    -- несколько таких складов (регионов WB) — см. fulfillment_centers.
    fulfillment_center_id INTEGER REFERENCES fulfillment_centers(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT UNIQUE NOT NULL,
    nm_id INTEGER UNIQUE,
    barcode TEXT UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stock_movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    warehouse_id INTEGER NOT NULL REFERENCES warehouses(id),
    movement_type TEXT NOT NULL,
    delta INTEGER NOT NULL,
    source TEXT NOT NULL,
    related_movement_id INTEGER,
    wb_order_id INTEGER,
    comment TEXT,
    created_by_id INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS wb_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wb_order_id TEXT UNIQUE NOT NULL,
    nm_id INTEGER,
    barcode TEXT,
    wb_warehouse_id INTEGER,
    product_id INTEGER,
    warehouse_id INTEGER,
    quantity INTEGER DEFAULT 1,
    status TEXT DEFAULT 'new',
    -- Статус WB из поля wbStatus (в отличие от status выше, который отражает
    -- supplierStatus/нашу нормализацию) — хранится отдельно, потому что
    -- 27.08.2026 выяснилось: клиент может отменить заказ, а supplierStatus
    -- при этом останется 'new' — реальная отмена видна только тут. См. sync.py.
    wb_status TEXT,
    price INTEGER,
    order_date TEXT,
    stock_deducted INTEGER DEFAULT 0,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    orders_fetched INTEGER DEFAULT 0,
    movements_created INTEGER DEFAULT 0,
    message TEXT
);

-- 28.08.2026: слияние карточек WB, которые физически — один и тот же товар
-- (например, "9690-2 карта" nm_id=1454601004 — это на самом деле "Шейвер 1.4"
-- CR-9690, просто вторая карточка на WB). Если для входящего заказа найден
-- алиас по barcode/nm_id — списание идёт сразу на target_product_id, у самой
-- карточки-алиаса свой остаток больше не ведётся. См. sync._find_or_create_product.
CREATE TABLE IF NOT EXISTS product_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_barcode TEXT UNIQUE,
    alias_nm_id INTEGER UNIQUE,
    target_product_id INTEGER NOT NULL REFERENCES products(id),
    comment TEXT,
    created_at TEXT NOT NULL
);

-- 28.08.2026: остатки Ozon — пока считаются отдельно от WB и вручную (без
-- подключения к Ozon API). Сознательно НЕ используют общий stock_movements —
-- это не событийный журнал заказов/приходов, а просто текущее число по
-- каждому товару, которое вводит человек. История изменений — в
-- ozon_stock_log, только для прозрачности (кто/когда поменял), без какого-либо
-- влияния на остатки WB.
CREATE TABLE IF NOT EXISTS ozon_stock (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL UNIQUE REFERENCES products(id),
    quantity INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    updated_by_id INTEGER REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS ozon_stock_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_id INTEGER NOT NULL REFERENCES products(id),
    old_quantity INTEGER NOT NULL,
    new_quantity INTEGER NOT NULL,
    comment TEXT,
    created_by_id INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL
);
"""


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DATABASE_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def db_session():
    """Контекстный менеджер: 'with db_session() as db: ...' — коммитит при успехе,
    откатывает при исключении, всегда закрывает соединение."""
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    conn = get_conn()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Точечные миграции для уже существующих (задеплоенных) баз — без потери
    данных. CREATE TABLE IF NOT EXISTS в SCHEMA новые таблицы создаёт сам, а
    вот новую КОЛОНКУ в уже существующей таблице так не добавить — поэтому
    здесь руками, по одной, и только если её ещё нет."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(warehouses)").fetchall()}
    if "fulfillment_center_id" not in cols:
        conn.execute(
            "ALTER TABLE warehouses ADD COLUMN fulfillment_center_id "
            "INTEGER REFERENCES fulfillment_centers(id)"
        )
        conn.commit()

    order_cols = {row["name"] for row in conn.execute("PRAGMA table_info(wb_orders)").fetchall()}
    if "wb_status" not in order_cols:
        conn.execute("ALTER TABLE wb_orders ADD COLUMN wb_status TEXT")
        conn.commit()


def now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")
