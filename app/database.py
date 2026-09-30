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
    -- 28.08.2026: фото/логотип для меню профиля — храним прямо как data URL
    -- (data:image/...;base64,...), без отдельного файлового хранилища: проще
    -- и надёжнее при деплое на Railway (нет отдельного диска для аплоадов).
    avatar_data_url TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fulfillment_centers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    -- 18.09.2026: ФФ, откуда физически уезжают поставки на Ozon FBO — нужен,
    -- чтобы при нажатии «Загрузить поставку» знать, с какого склада списывать
    -- (см. app/ozon_sync.py). Ozon API не сообщает, кто физически собрал
    -- поставку — это чисто внутреннее знание продавца, поэтому не берём
    -- ниоткуда автоматически, а даёте отметить сами на странице «Склады».
    -- Ровно у одного активного ФФ должен быть этот флаг — если у Алёны
    -- появится второй источник поставок Ozon, логику надо будет расширить.
    is_ozon_fbo_source INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS warehouses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    -- ID склада в WB — нужен только чтобы сопоставлять входящие заказы с
    -- вашим складом. Приложение никогда не пишет по этому ID обратно в WB.
    wb_warehouse_id INTEGER UNIQUE,
    -- 18.09.2026: то же самое, но для Ozon FBS — ID склада отгрузки из
    -- личного кабинета Ozon (Настройки → FBS → склад). У одного склада
    -- заполняется только одно из двух полей (wb_warehouse_id ЛИБО
    -- ozon_warehouse_id) — это разные площадки. Виртуальный склад
    -- «Ozon FBO» (см. app/ozon_sync.py) — отдельная строка без обоих полей
    -- и без fulfillment_center_id (общий, не привязан ни к одному ФФ).
    ozon_warehouse_id INTEGER UNIQUE,
    -- Один физический ФФ (фулфилмент-центр) может обслуживать сразу
    -- несколько таких складов (регионов WB/Ozon) — см. fulfillment_centers.
    fulfillment_center_id INTEGER REFERENCES fulfillment_centers(id),
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sku TEXT UNIQUE NOT NULL,
    nm_id INTEGER UNIQUE,
    barcode TEXT UNIQUE,
    -- 18.09.2026: SKU товара на Ozon (числовой идентификатор карточки в
    -- личном кабинете Ozon, не путать с вашим sku выше) — по нему
    -- сопоставляются заказы FBS и позиции поставок FBO. offer_id (ваш
    -- собственный артикул на Ozon) тоже приходит в ответах API, но как
    -- текст — можно не хранить отдельно, для сопоставления достаточно SKU.
    ozon_sku INTEGER UNIQUE,
    name TEXT NOT NULL,
    -- 30.09.2026: карточки, слитые в рамках чистки дублей (см. _migrate_
    -- merge_duplicate_products) — скрыты из выбора в формах, но не удалены:
    -- удаление окончательное и необратимое, поэтому его делает только сама
    -- Алёна руками, с подтверждением, на странице «Товары».
    is_active INTEGER NOT NULL DEFAULT 1,
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
-- 18.09.2026: та же механика распространена на Ozon — alias_ozon_sku работает
-- совершенно так же, только ключом служит SKU карточки на Ozon, см.
-- ozon_sync._find_or_create_product.
CREATE TABLE IF NOT EXISTS product_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias_barcode TEXT UNIQUE,
    alias_nm_id INTEGER UNIQUE,
    alias_ozon_sku INTEGER UNIQUE,
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

-- 18.09.2026: реальная интеграция с Ozon API (пришла на смену ручным
-- ozon_stock/ozon_stock_log выше — те таблицы оставлены как есть, только для
-- разовой сверки остатков при переходе, дальше не используются). Два разных
-- потока: FBS-заказы (ozon_postings, авто-списание, зеркало wb_orders) и
-- FBO-поставки (ozon_supplies/ozon_supply_items, загрузка по кнопке, зеркало
-- идеи с warehouses.import-from-wb, но с защитой от повторной загрузки).
-- Один posting_number (отправление) у Ozon может содержать НЕСКОЛЬКО разных
-- товаров сразу (в отличие от заказа WB, где одна позиция = одна строка) —
-- поэтому ключ строки здесь не сам posting_number, а пара
-- (posting_number, line_no) — line_no это просто порядковый номер товара
-- внутри массива products в ответе Ozon.
CREATE TABLE IF NOT EXISTS ozon_postings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    posting_number TEXT NOT NULL,
    line_no INTEGER NOT NULL DEFAULT 0,
    ozon_sku INTEGER,
    offer_id TEXT,
    ozon_warehouse_id INTEGER,
    product_id INTEGER,
    warehouse_id INTEGER,
    quantity INTEGER NOT NULL DEFAULT 1,
    status TEXT DEFAULT 'new',
    stock_deducted INTEGER NOT NULL DEFAULT 0,
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ozon_supplies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_order_id TEXT UNIQUE NOT NULL,
    status TEXT,
    loaded INTEGER NOT NULL DEFAULT 0,
    loaded_at TEXT,
    loaded_by_id INTEGER REFERENCES users(id),
    raw_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ozon_supply_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supply_id INTEGER NOT NULL REFERENCES ozon_supplies(id),
    ozon_sku INTEGER,
    offer_id TEXT,
    name_hint TEXT,
    quantity INTEGER NOT NULL,
    product_id INTEGER
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ozon_postings_line
    ON ozon_postings(posting_number, line_no);

-- 30.09.2026: раздел «Внешние списания» (по просьбе Алёны — максимально
-- простая ручная модель для всего, что уезжает с ФФ на маркетплейс и дальше
-- не учитывается как отдельный склад с текущим балансом). Три вида (kind):
-- 'fbo_wb', 'fbo_ozon', 'fbs_ozon' — три вкладки одного раздела. Каждая
-- запись здесь всегда идёт в паре с обычным списанием в stock_movements
-- (movement_id) — именно оно уменьшает остаток на складе, эта таблица —
-- только читаемый журнал (дата/количество/артикул/комментарий) для истории,
-- без какого-либо отдельного баланса.
CREATE TABLE IF NOT EXISTS external_writeoffs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    product_id INTEGER NOT NULL REFERENCES products(id),
    ff_id INTEGER REFERENCES fulfillment_centers(id),
    quantity INTEGER NOT NULL,
    comment TEXT,
    movement_id INTEGER REFERENCES stock_movements(id),
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

    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "avatar_data_url" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_data_url TEXT")
        conn.commit()

    # 18.09.2026: колонки под интеграцию с Ozon — на уже задеплоенной базе их
    # ещё нет, добавляем точечно, как и остальные миграции здесь.
    ff_cols = {row["name"] for row in conn.execute("PRAGMA table_info(fulfillment_centers)").fetchall()}
    if "is_ozon_fbo_source" not in ff_cols:
        conn.execute(
            "ALTER TABLE fulfillment_centers ADD COLUMN is_ozon_fbo_source INTEGER NOT NULL DEFAULT 0"
        )
        conn.commit()

    # Примечание: SQLite не разрешает ALTER TABLE ... ADD COLUMN с UNIQUE —
    # добавляем колонку обычной, а уникальность обеспечиваем отдельным
    # индексом ниже (эффект тот же).
    wh_cols = {row["name"] for row in conn.execute("PRAGMA table_info(warehouses)").fetchall()}
    if "ozon_warehouse_id" not in wh_cols:
        conn.execute("ALTER TABLE warehouses ADD COLUMN ozon_warehouse_id INTEGER")
        conn.commit()

    product_cols = {row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "ozon_sku" not in product_cols:
        conn.execute("ALTER TABLE products ADD COLUMN ozon_sku INTEGER")
        conn.commit()

    alias_cols = {row["name"] for row in conn.execute("PRAGMA table_info(product_aliases)").fetchall()}
    if "alias_ozon_sku" not in alias_cols:
        conn.execute("ALTER TABLE product_aliases ADD COLUMN alias_ozon_sku INTEGER")
        conn.commit()

    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_warehouses_ozon_id ON warehouses(ozon_warehouse_id)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_products_ozon_sku ON products(ozon_sku)")
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_aliases_ozon_sku ON product_aliases(alias_ozon_sku)")
    conn.commit()

    product_active_cols = {row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "is_active" not in product_active_cols:
        conn.execute("ALTER TABLE products ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1")
        conn.commit()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    _merge_known_duplicate_products(conn)


# 30.09.2026: разовое слияние карточек-дублей, найденных при большой сверке
# остатков (см. обсуждение с Алёной — одна карточка на артикул, никаких
# призраков). Выполняется РОВНО ОДИН РАЗ на каждой базе (см. schema_migrations)
# — дальше её можно менять/переоткрывать руками, повторно она не тронет.
#
# Что делает для каждой пары (канонический sku, [дублирующие sku]):
#   1. Переносит на канонический недостающие идентификаторы (nm_id/barcode/
#      ozon_sku) с карточки-дубля — саму историю движений НЕ пересчитывает и
#      не пытается угадать "правильные" итоговые остатки: их Алёна выставит
#      сама вручную после чистки (см. обсуждение).
#   2. Переносит ВСЕ существующие движения и упоминания дубля (в
#      stock_movements, wb_orders, ozon_postings, ozon_supply_items,
#      product_aliases, ozon_stock_log) на канонический product_id — история
#      не теряется, просто больше не разбита на разные карточки.
#   3. Помечает карточку-дубль неактивной и переименовывает с явной пометкой
#      «слито», чтобы её было видно на «Товары» и можно было удалить руками
#      (см. product_delete в main.py) — сама я это удаление не выполняю.
_DUPLICATE_PRODUCT_GROUPS = [
    {"canonical_sku": "5 шейвер CR-1324", "duplicate_skus": ["CR-1324", "CR-1324 OZ"]},
    {"canonical_sku": "Электробритва серая KP-1029 ОЗОН", "duplicate_skus": ["KING KP-1029 OZ"]},
    {"canonical_sku": "Электробритва CR-1230 оранжевая ОЗОН", "duplicate_skus": ["CR-1230 OZ"]},
    {"canonical_sku": "Триммер зеленый CR-135 ЮДС", "duplicate_skus": ["CR-135"]},
    {"canonical_sku": "триммер черный MP 642", "duplicate_skus": ["MP-642"]},
    {"canonical_sku": "2281 триммер оранж с сенсор", "duplicate_skus": ["МР-2281"]},
    {"canonical_sku": "1.4 Шейвер CR-9690", "duplicate_skus": ["1Шейвер CR9690"]},
]

# Полностью мёртвые карточки без единого движения — просто помечаем для
# удаления, переносить нечего.
_DEAD_EMPTY_PRODUCT_SKUS = ["2045277239249", "2047460305823", "1.4_2"]

_MERGE_MIGRATION_NAME = "2026_09_30_merge_duplicate_products"


def _merge_known_duplicate_products(conn: sqlite3.Connection) -> None:
    already = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (_MERGE_MIGRATION_NAME,)
    ).fetchone()
    if already:
        return

    def _get_product(sku):
        return conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()

    def _reassign_movements(old_id: int, new_id: int) -> None:
        conn.execute("UPDATE stock_movements SET product_id = ? WHERE product_id = ?", (new_id, old_id))
        conn.execute("UPDATE wb_orders SET product_id = ? WHERE product_id = ?", (new_id, old_id))
        conn.execute("UPDATE ozon_postings SET product_id = ? WHERE product_id = ?", (new_id, old_id))
        conn.execute("UPDATE ozon_supply_items SET product_id = ? WHERE product_id = ?", (new_id, old_id))
        conn.execute(
            "UPDATE product_aliases SET target_product_id = ? WHERE target_product_id = ?",
            (new_id, old_id),
        )
        # ozon_stock.product_id уникальный — если у обеих карточек была своя
        # строка, оставляем только каноническую, чтобы не словить UNIQUE.
        conn.execute("DELETE FROM ozon_stock WHERE product_id = ?", (old_id,))
        conn.execute("UPDATE ozon_stock_log SET product_id = ? WHERE product_id = ?", (new_id, old_id))

    def _merge_identifier(canonical_id: int, duplicate_id: int, column: str) -> None:
        dup_value = conn.execute(
            f"SELECT {column} AS v FROM products WHERE id = ?", (duplicate_id,)
        ).fetchone()["v"]
        if dup_value is None:
            return
        canonical_value = conn.execute(
            f"SELECT {column} AS v FROM products WHERE id = ?", (canonical_id,)
        ).fetchone()["v"]
        # Снимаем значение с дубля в любом случае (иначе не удастся ни
        # переиспользовать его на канонической карточке при совпадающем
        # UNIQUE-ограничении, ни оставить дубль как есть — он больше не
        # должен участвовать в сопоставлении новых заказов).
        conn.execute(f"UPDATE products SET {column} = NULL WHERE id = ?", (duplicate_id,))
        if canonical_value is None:
            conn.execute(f"UPDATE products SET {column} = ? WHERE id = ?", (dup_value, canonical_id))

    for group in _DUPLICATE_PRODUCT_GROUPS:
        canonical = _get_product(group["canonical_sku"])
        if not canonical:
            continue
        for dup_sku in group["duplicate_skus"]:
            duplicate = _get_product(dup_sku)
            if not duplicate or duplicate["id"] == canonical["id"]:
                continue
            for column in ("nm_id", "barcode", "ozon_sku"):
                _merge_identifier(canonical["id"], duplicate["id"], column)
            _reassign_movements(duplicate["id"], canonical["id"])
            conn.execute(
                "UPDATE products SET is_active = 0, "
                "name = ? WHERE id = ?",
                (f"[СЛИТО В «{canonical['name']}» — можно удалить] {duplicate['name']}", duplicate["id"]),
            )

    for dead_sku in _DEAD_EMPTY_PRODUCT_SKUS:
        dead = _get_product(dead_sku)
        if not dead:
            continue
        conn.execute(
            "UPDATE products SET is_active = 0, name = ? WHERE id = ?",
            (f"[ПУСТАЯ КАРТОЧКА — можно удалить] {dead['name']}", dead["id"]),
        )

    conn.execute(
        "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)",
        (_MERGE_MIGRATION_NAME, now_iso()),
    )
    conn.commit()


def now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")
