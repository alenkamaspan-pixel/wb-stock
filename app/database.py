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

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    wb_warehouse_id INTEGER UNIQUE,
    is_synced_to_wb INTEGER NOT NULL DEFAULT 1,
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
    stock_pushed INTEGER DEFAULT 0,
    message TEXT
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
    finally:
        conn.close()


def now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")
