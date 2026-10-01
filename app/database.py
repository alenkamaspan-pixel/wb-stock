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
from app.models import ProductCategory

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
    -- 30.09.2026 (по просьбе Алёны): категория товара (см. ProductCategory
    -- в app/models.py) — чтобы список товаров не был «разбросан и
    -- запутан». NULL = ещё не отнесён ни к одной категории (показывается
    -- отдельной группой «Без категории», а не теряется молча).
    category TEXT,
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

-- 30.09.2026: журнал ошибок точечных миграций (слияния карточек,
-- исправления полей) — до этого при сбое внутри одной находки строка в
-- schema_migrations всё равно писалась (или не писалась, но без всякого
-- следа), и разобраться, что пошло не так, можно было только по логам
-- Railway, к которым доступа нет. Теперь при сбое любой отдельной находки
-- (см. _merge_additional_found_products) конкретная ошибка сохраняется сюда
-- вместо того, чтобы молча остаться неизвестной — видно на /wb-diagnostics.
CREATE TABLE IF NOT EXISTS migration_errors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    migration_name TEXT NOT NULL,
    error_text TEXT NOT NULL,
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

    product_category_cols = {row["name"] for row in conn.execute("PRAGMA table_info(products)").fetchall()}
    if "category" not in product_category_cols:
        conn.execute("ALTER TABLE products ADD COLUMN category TEXT")
        conn.commit()

    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations "
        "(name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    conn.commit()
    _merge_known_duplicate_products(conn)
    # ВАЖНО: порядок здесь принципиален для карточки CR-9690 (см. записи
    # ниже) — сначала чистим у неё ошибочный Ozon SKU (_apply_field_fixes),
    # ТОЛЬКО ПОТОМ сливаем в неё вторую карточку с настоящим Ozon SKU
    # (_merge_additional_found_products): перенос идентификатора при слиянии
    # происходит, только если у канонической карточки поле сейчас пустое
    # (см. _merge_identifier) — будь порядок обратным, настоящий Ozon SKU
    # дубля не перенёсся бы, потому что поле было бы всё ещё занято старым
    # ошибочным значением.
    _apply_field_fixes(conn)
    _merge_additional_found_products(conn)
    _backfill_product_categories(conn)


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


def _get_product_by_sku(conn: sqlite3.Connection, sku):
    return conn.execute("SELECT * FROM products WHERE sku = ?", (sku,)).fetchone()


def _reassign_product_references(conn: sqlite3.Connection, old_id: int, new_id: int) -> None:
    """Переносит ВСЕ ссылки на карточку old_id на карточку new_id — общая
    часть логики слияния, используется и большой миграцией дублей от
    28-30.09.2026, и точечными слияниями отдельных карточек, которые Алёна
    находит уже после неё (см. _merge_known_duplicate_products и
    _merge_kp2116_duplicate ниже)."""
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
    # 30.09.2026: журнал «Внешние списания» тоже ссылается на product_id —
    # при слиянии переносим и его записи, иначе история списаний дубля
    # осиротеет (сам movement_id внутри неё уже трогать не надо — эти
    # движения переехали на канонический product_id строкой выше через
    # stock_movements).
    conn.execute("UPDATE external_writeoffs SET product_id = ? WHERE product_id = ?", (new_id, old_id))


def _merge_identifier(conn: sqlite3.Connection, canonical_id: int, duplicate_id: int, column: str) -> None:
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


def _merge_known_duplicate_products(conn: sqlite3.Connection) -> None:
    already = conn.execute(
        "SELECT 1 FROM schema_migrations WHERE name = ?", (_MERGE_MIGRATION_NAME,)
    ).fetchone()
    if already:
        return

    for group in _DUPLICATE_PRODUCT_GROUPS:
        canonical = _get_product_by_sku(conn, group["canonical_sku"])
        if not canonical:
            continue
        for dup_sku in group["duplicate_skus"]:
            duplicate = _get_product_by_sku(conn, dup_sku)
            if not duplicate or duplicate["id"] == canonical["id"]:
                continue
            for column in ("nm_id", "barcode", "ozon_sku"):
                _merge_identifier(conn, canonical["id"], duplicate["id"], column)
            _reassign_product_references(conn, duplicate["id"], canonical["id"])
            conn.execute(
                "UPDATE products SET is_active = 0, "
                "name = ? WHERE id = ?",
                (f"[СЛИТО В «{canonical['name']}» — можно удалить] {duplicate['name']}", duplicate["id"]),
            )

    for dead_sku in _DEAD_EMPTY_PRODUCT_SKUS:
        dead = _get_product_by_sku(conn, dead_sku)
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


# 30.09.2026 (вторым заходом и далее, уже после большой чистки выше): Алёна
# просматривает страницу «Товары» на живом сайте и время от времени находит
# ещё одну пару дублей, которой не было в исходном списке
# _DUPLICATE_PRODUCT_GROUPS. В отличие от него, здесь каждая запись находится
# не по sku (он у "битых" карточек часто сам испорчен — например, стал текстом
# самого nm_id/barcode или заглушкой типа "KP-2116.", похоже на автосоздание
# карточки при заказе/поставке, для которых ещё не было известно человеко-
# читаемое название), а по тому идентификатору, который у неё точно на месте
# (nm_id или ozon_sku). И, в отличие от основной миграции (там у канонической
# карточки имя уже было в порядке), здесь иногда нужно поправить name/sku и
# самой "живой" карточке — задаётся через final_name/final_sku, если не
# нужно — просто None.
#
# ВАЖНО: каждая запись отмечается в schema_migrations СВОИМ ОТДЕЛЬНЫМ именем
# (не одним общим на весь список) — именно поэтому в этот список можно
# спокойно дописывать новые находки при следующих правках: старые записи уже
# отмечены выполненными и не повторятся, а новые всё равно применятся при
# следующем деплое, даже на базе, где часть списка уже когда-то отработала.
_ADDITIONAL_MERGES = [
    {
        # «Машинка кинг KP-2116»: канонической была "битая" карточка WB
        # (имя/sku = просто числа nm_id/barcode) — чиним ей и имя, и sku.
        "canonical_match": ("nm_id", 1657611777),
        "duplicate_match": ("ozon_sku", 5762682150),
        "final_name": "Машинка кинг KP-2116",
        "final_sku": "KP-2116",
    },
    {
        # «6 Шейвер CR-1325»: канонической уже была нормальная карточка WB —
        # просто переносим на неё Ozon SKU дубля-заглушки "шейвер сг-1325",
        # имя/sku канонической не трогаем (final_name/final_sku = None).
        "canonical_match": ("nm_id", 1535320580),
        "duplicate_match": ("ozon_sku", 5799938415),
        "final_name": None,
        "final_sku": None,
    },
    {
        # «1.4 Шейвер CR-9690» / «1Шейвер CR9690_2»: изначально (30.09.2026,
        # первое обсуждение) Алёна попросила эти карточки НЕ сливать — только
        # почистить у первой ошибочный Ozon SKU (см. _FIELD_FIXES выше).
        # Позже в тот же день, увидев на дашборде, что остаток по второй
        # карточке ушёл в минус (-1) при 20 на первой, решила, что раз это
        # один физический товар — остатки не должны расходиться, и попросила
        # всё-таки слить. Канонической оставляем «1.4 Шейвер CR-9690» — у неё
        # настоящие nm_id/штрихкод WB, нужные для автосопоставления заказов.
        # Итоговый остаток после слияния = сумма обеих карточек (20 + (-1) =
        # 19 шт.) — это ожидаемо и правильно, отдельно ничего не подгоняем.
        #
        # ВАЖНО (найдено 30.09.2026 на боевых данных): дубль-карточку здесь
        # ищем по sku, А НЕ по ozon_sku, хотя изначально её нашли именно по
        # ozon_sku 5708841384. Причина: на реальном сайте перенос Ozon SKU на
        # каноническую карточку УЖЕ произошёл (это самая первая часть
        # слияния), а вот сама деактивация дубля и перенос остатка — нет
        # (слияние не дошло до конца по неизвестной причине, см.
        # migration_errors на /wb-diagnostics). Из-за этого при повторном
        # запуске поиск дубля ПО ozon_sku находил уже каноническую карточку
        # (она теперь тоже им владеет) вместо настоящего дубля — искать нужно
        # по её собственному, никогда не переносимому sku.
        "canonical_match": ("nm_id", 499229213),
        "duplicate_match": ("sku", "1Шейвер CR9690_2"),
        "final_name": None,
        "final_sku": None,
    },
]


def _merge_additional_found_products(conn: sqlite3.Connection) -> None:
    """30.09.2026, исправлено после того, как выяснилось на боевых данных:
    раньше отметка "эта находка обработана" (schema_migrations) писалась
    БЕЗУСЛОВНО в конце, даже если карточки не нашлись или слияние упало с
    ошибкой на середине — из-за этого слияние CR-9690/CR9690_2 могло
    навсегда "застрять" наполовину (Ozon SKU уже перенесён, а сама карточка-
    дубль так и осталась активной сама по себе), и повторный деплой это
    больше не мог исправить.

    Теперь вместо непрозрачного флага проверяем РЕАЛЬНОЕ состояние: если
    карточка-дубль уже неактивна — значит, слияние для неё уже случилось
    (флаг больше не нужен), и так само по себе получается идемпотентно и
    может повторяться на каждом деплое, пока не получится. Если слияние
    всё-таки упадёт с ошибкой — она сохраняется в migration_errors (видно на
    /wb-diagnostics) и не мешает ни другим находкам из этого списка, ни
    остальным миграциям при запуске."""
    for spec in _ADDITIONAL_MERGES:
        canonical_col, canonical_val = spec["canonical_match"]
        duplicate_col, duplicate_val = spec["duplicate_match"]
        migration_name = (
            f"2026_09_30_merge_extra_{canonical_col}_{canonical_val}_{duplicate_col}_{duplicate_val}"
        )

        canonical = conn.execute(
            f"SELECT * FROM products WHERE {canonical_col} = ?", (canonical_val,)
        ).fetchone()
        duplicate = conn.execute(
            f"SELECT * FROM products WHERE {duplicate_col} = ?", (duplicate_val,)
        ).fetchone()

        if not canonical or not duplicate or canonical["id"] == duplicate["id"] or not duplicate["is_active"]:
            # Нечего сливать: одна из карточек не найдена, это уже одна и та
            # же карточка, или дубль уже неактивен — слияние для него уже
            # случилось раньше (в этом самом или в предыдущем деплое).
            continue

        try:
            for column in ("nm_id", "barcode", "ozon_sku"):
                _merge_identifier(conn, canonical["id"], duplicate["id"], column)
            _reassign_product_references(conn, duplicate["id"], canonical["id"])
            final_name = spec["final_name"] or canonical["name"]
            conn.execute(
                "UPDATE products SET is_active = 0, name = ? WHERE id = ?",
                (f"[СЛИТО В «{final_name}» — можно удалить] {duplicate['name']}", duplicate["id"]),
            )
            if spec["final_name"] or spec["final_sku"]:
                new_sku = spec["final_sku"]
                if new_sku:
                    # sku UNIQUE — на случай, если новый sku уже почему-то
                    # занят другой карточкой, не падаем: оставляем текущий.
                    sku_taken = conn.execute(
                        "SELECT 1 FROM products WHERE sku = ? AND id != ?", (new_sku, canonical["id"])
                    ).fetchone()
                    if sku_taken:
                        new_sku = None
                if new_sku:
                    conn.execute(
                        "UPDATE products SET name = ?, sku = ? WHERE id = ?",
                        (final_name, new_sku, canonical["id"]),
                    )
                else:
                    conn.execute("UPDATE products SET name = ? WHERE id = ?", (final_name, canonical["id"]))
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.execute(
                "INSERT INTO migration_errors (migration_name, error_text, created_at) VALUES (?, ?, ?)",
                (migration_name, str(e), now_iso()),
            )
            conn.commit()
            continue


# 30.09.2026: точечные исправления одного испорченного поля на карточке,
# которую саму по себе сливать НЕ нужно (в отличие от _ADDITIONAL_MERGES
# выше) — Алёна замечает такое на живом сайте так же, как и дубли. Тот же
# принцип: каждая запись отмечена своим отдельным именем в
# schema_migrations, список можно дописывать новыми находками при
# следующих правках. Перед изменением поля сверяем его ТЕКУЩЕЕ значение с
# ожидаемым (expected_current_value) — если оно уже другое (например, Алёна
# сама успела поправить карточку руками до деплоя), ничего не трогаем, чтобы
# не затереть её собственную правку чем-то устаревшим.
_FIELD_FIXES = [
    {
        # «1.4 Шейвер CR-9690»: указанный Ozon SKU 5439425498 ошибочный —
        # такого SKU на Ozon не существует (Алёна проверила и подтвердила
        # 30.09.2026). Настоящий Ozon SKU этого же физического товара —
        # 5708841384 — и так уже стоит на отдельной карточке
        # «1Шейвер CR9690_2»; по решению Алёны эти две карточки НЕ сливаем
        # (списания и остатки по ним ведутся раздельно намеренно) — тут
        # только чистим неверное значение, карточка остаётся одна и та же.
        "match": ("nm_id", 499229213),
        "column": "ozon_sku",
        "expected_current_value": 5439425498,
        "new_value": None,
    },
]


def _apply_field_fixes(conn: sqlite3.Connection) -> None:
    """30.09.2026, тот же фикс, что и в _merge_additional_found_products: не
    отмечаем находку "обработанной" безусловно — если карточка ещё не
    существует (например, эта точечная правка была написана раньше, чем
    появился сам товар), пробуем на каждом следующем деплое, пока она не
    появится, вместо того чтобы навсегда решить, что чинить нечего."""
    for spec in _FIELD_FIXES:
        match_col, match_val = spec["match"]
        migration_name = f"2026_09_30_fix_field_{match_col}_{match_val}_{spec['column']}"

        row = conn.execute(f"SELECT * FROM products WHERE {match_col} = ?", (match_val,)).fetchone()
        if not row or row[spec["column"]] != spec["expected_current_value"]:
            # Нечего чинить: карточки ещё нет, ИЛИ поле уже не равно
            # ожидаемому (либо мы его уже сами почистили раньше, либо Алёна
            # успела поправить руками) — в обоих случаях трогать не нужно.
            continue

        try:
            conn.execute(
                f"UPDATE products SET {spec['column']} = ? WHERE id = ?",
                (spec["new_value"], row["id"]),
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.execute(
                "INSERT INTO migration_errors (migration_name, error_text, created_at) VALUES (?, ?, ?)",
                (migration_name, str(e), now_iso()),
            )
            conn.commit()


# 30.09.2026 (по просьбе Алёны): «список товаров разбросан и запутан» —
# раскладываем уже существующие карточки по категориям один раз, по их
# собственному sku (он, в отличие от ozon_sku/nm_id, никогда не переносится
# при слиянии дублей — см. историю CR-9690 выше). Дальше категорию можно
# свободно менять руками на странице «Товары»: эта раскладка трогает только
# карточки с ПУСТЫМ (NULL) category — то, что уже проставлено (в т.ч. самой
# Алёной), никогда не перезаписывается повторно.
_PRODUCT_CATEGORY_BACKFILL = {
    "1 Шейвер CR-9690": ProductCategory.SHAVERS,
    "2 Шейвер CR-827": ProductCategory.SHAVERS,
    "3 Шейвер CR-9650": ProductCategory.SHAVERS,
    "4 Шейвер KP-1004": ProductCategory.SHAVERS,
    "5 шейвер CR-1324": ProductCategory.SHAVERS,
    "6 Шейвер CR-1325": ProductCategory.SHAVERS,
    "Электробритва CR-1230 оранжевая ОЗОН": ProductCategory.ELECTRIC_RAZORS,
    "Электробритва серая KP-1029 ОЗОН": ProductCategory.ELECTRIC_RAZORS,
    "2281 триммер оранж с сенсор": ProductCategory.TRIMMERS,
    "Триммер зеленый CR-135 ЮДС": ProductCategory.TRIMMERS,
    "триммер черный MP 642": ProductCategory.TRIMMERS,
    "2278 MPRO Машинка оранжевая ЮДС": ProductCategory.CLIPPERS,
    "Машинка кинг KP-2116": ProductCategory.CLIPPERS,
    "Блендер белый 767075120": ProductCategory.BLENDERS,
    "Блендер черный 980103097": ProductCategory.BLENDERS,
}


def _backfill_product_categories(conn: sqlite3.Connection) -> None:
    for sku, category in _PRODUCT_CATEGORY_BACKFILL.items():
        migration_name = f"2026_09_30_category_{sku}"
        row = conn.execute("SELECT id, category FROM products WHERE sku = ?", (sku,)).fetchone()
        if not row or row["category"] is not None:
            # Нечего делать: карточки ещё нет, ИЛИ категория уже стоит (в
            # т.ч. потому что Алёна сама её поменяла руками) — не трогаем.
            continue
        try:
            conn.execute("UPDATE products SET category = ? WHERE id = ?", (category, row["id"]))
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.execute(
                "INSERT INTO migration_errors (migration_name, error_text, created_at) VALUES (?, ?, ?)",
                (migration_name, str(e), now_iso()),
            )
            conn.commit()


def now_iso() -> str:
    return dt.datetime.utcnow().isoformat(timespec="seconds")
