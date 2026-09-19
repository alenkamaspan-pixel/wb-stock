"""
Бизнес-логика интеграции с Ozon.

Два независимых потока, как обсуждалось с Алёной (18.09.2026):

1. FBS-продажи — синхронизируются автоматически, по той же схеме, что и
   заказы WB (см. app/sync.py): новое отправление -> сразу списываем остаток
   с ФФ, на который замаплен склад отгрузки Ozon; отменённое — возвращаем.
   Один нюанс, которого нет у WB: одно отправление Ozon может содержать
   НЕСКОЛЬКО разных товаров сразу — поэтому в ozon_postings одна строка это
   не отправление, а строка товара внутри него (см. database.py).

2. FBO-поставки — НЕ синхронизируются автоматически. Состав поставки вносит
   менеджер в личном кабинете Ozon, наше приложение только читает готовый
   список и даёт нажать «Загрузить поставку», которая одной операцией
   переносит весь её состав с ФФ-источника (см.
   fulfillment_centers.is_ozon_fbo_source) на виртуальный склад «Ozon FBO».
   Каждую поставку можно загрузить только один раз — см. ozon_supplies.loaded.

Оба потока пишут движения с source='ozon_sync' и НИКАК не переиспользуют
остаток WB — виртуальный склад «Ozon FBO» и склады FBS Ozon физически
отдельные строки в warehouses (см. database.py), просто с ozon_warehouse_id
вместо wb_warehouse_id.
"""
import datetime as dt
import json
import sqlite3

from app.database import get_conn, now_iso
from app.models import MovementType, MovementSource
from app.ozon_client import OzonClient, OzonApiError

OZON_FBO_WAREHOUSE_NAME = "Ozon FBO"

# Статусы отправления FBS, которые Ozon использует для отмены — по аналогии с
# WB_STATUS_CANCEL_VALUES в sync.py: список собран по документации и
# независимым клиентам на 18.09.2026, НЕ проверен на реальных данных (см.
# предупреждение в ozon_client.py). Если после первых синхронизаций
# обнаружится ещё не учтённое значение статуса отмены — его будет видно на
# странице «Заказы Ozon» текстом как есть, и его нужно будет дописать сюда.
OZON_CANCEL_STATUSES = {"cancelled", "canceled"}


def _resolve_alias(conn: sqlite3.Connection, ozon_sku):
    if not ozon_sku:
        return None
    alias = conn.execute(
        "SELECT target_product_id FROM product_aliases WHERE alias_ozon_sku = ?", (ozon_sku,)
    ).fetchone()
    return alias["target_product_id"] if alias else None


def _find_or_create_product(conn: sqlite3.Connection, ozon_sku, offer_id, name_hint: str) -> int:
    aliased_product_id = _resolve_alias(conn, ozon_sku)
    if aliased_product_id:
        return aliased_product_id

    product = None
    if ozon_sku:
        product = conn.execute("SELECT * FROM products WHERE ozon_sku = ?", (ozon_sku,)).fetchone()
    if product:
        return product["id"]

    # Неизвестный на Ozon товар (нет ни в products.ozon_sku, ни в алиасах) —
    # заготовка, чтобы ничего не потерять, донастроить можно на «Товары».
    sku = offer_id or (f"ozon-{ozon_sku}" if ozon_sku else f"unknown-ozon-{dt.datetime.utcnow().timestamp()}")
    cur = conn.execute(
        "INSERT INTO products (sku, ozon_sku, name, created_at) VALUES (?, ?, ?, ?)",
        (sku, ozon_sku, name_hint or sku, now_iso()),
    )
    return cur.lastrowid


def _find_warehouse_by_ozon_id(conn: sqlite3.Connection, ozon_warehouse_id):
    if not ozon_warehouse_id:
        return None
    return conn.execute(
        "SELECT * FROM warehouses WHERE ozon_warehouse_id = ?", (ozon_warehouse_id,)
    ).fetchone()


def get_or_create_ozon_fbo_warehouse(conn: sqlite3.Connection) -> int:
    """Единый виртуальный склад «Ozon FBO» — один на всё приложение, без
    привязки к ФФ (её выбор от 18.09.2026: не по ФФ и не по складам Ozon,
    один общий). Создаётся автоматически при первом обращении."""
    row = conn.execute("SELECT id FROM warehouses WHERE name = ?", (OZON_FBO_WAREHOUSE_NAME,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        "INSERT INTO warehouses (name, is_active, created_at) VALUES (?, 1, ?)",
        (OZON_FBO_WAREHOUSE_NAME, now_iso()),
    )
    return cur.lastrowid


def get_ozon_fbo_source_warehouse_id(conn: sqlite3.Connection):
    """Канонический склад ФФ, отмеченного как источник поставок Ozon FBO (см.
    fulfillment_centers.is_ozon_fbo_source — включается на странице «Склады»).
    None, если такой ФФ ещё не отмечен — тогда загрузка поставки невозможна,
    об этом явно сказано пользователю (см. main.py)."""
    ff = conn.execute(
        "SELECT id FROM fulfillment_centers WHERE is_ozon_fbo_source = 1 AND is_active = 1 LIMIT 1"
    ).fetchone()
    if not ff:
        return None
    canonical = conn.execute(
        "SELECT id FROM warehouses WHERE fulfillment_center_id = ? AND is_active = 1 ORDER BY id LIMIT 1",
        (ff["id"],),
    ).fetchone()
    return canonical["id"] if canonical else None


def _add_movement(conn, product_id, warehouse_id, movement_type, delta, comment, related_movement_id=None):
    cur = conn.execute(
        """INSERT INTO stock_movements
           (product_id, warehouse_id, movement_type, delta, source, related_movement_id, comment, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, warehouse_id, movement_type, delta, MovementSource.OZON_SYNC,
         related_movement_id, comment, now_iso()),
    )
    return cur.lastrowid


# --------------------------------------------------------------- FBS-продажи
def sync_fbs_once(client: OzonClient | None = None) -> dict:
    """Один цикл синхронизации FBS — читает ещё не собранные отправления и
    списывает остаток; отдельно сверяет уже известные отправления за
    последние сутки на предмет отмены. Безопасно вызывать повторно (как
    sync_once в sync.py) — уже известные (posting_number, line_no) просто
    пропускаются."""
    client = client or OzonClient()
    conn = get_conn()

    postings_fetched = 0
    movements_created = 0
    log_lines: list[str] = []

    try:
        # /v4/posting/fbs/unfulfilled/list тоже отдаёт максимум 100 штук за
        # раз (см. ozon_client.py) — листаем так же, как список отправлений
        # ниже, с тем же разумным потолком в 20 страниц.
        postings = []
        offset = 0
        page_size = 100
        for _ in range(20):
            result = client.get_unfulfilled_postings(limit=page_size, offset=offset)
            page = result.get("postings", []) if isinstance(result, dict) else (result or [])
            postings.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        postings_fetched = len(postings)

        for raw in postings:
            posting_number = raw.get("posting_number")
            if not posting_number:
                continue
            products = raw.get("products") or []
            ozon_warehouse_id = (
                (raw.get("delivery_method") or {}).get("warehouse_id")
                or raw.get("warehouse_id")
            )
            warehouse = _find_warehouse_by_ozon_id(conn, ozon_warehouse_id)

            for line_no, p in enumerate(products):
                existing = conn.execute(
                    "SELECT id FROM ozon_postings WHERE posting_number = ? AND line_no = ?",
                    (posting_number, line_no),
                ).fetchone()
                if existing:
                    continue

                ozon_sku = p.get("sku")
                offer_id = p.get("offer_id")
                quantity = int(p.get("quantity") or 1)
                product_id = _find_or_create_product(conn, ozon_sku, offer_id, name_hint=offer_id or str(ozon_sku))

                posting_cur = conn.execute(
                    """INSERT INTO ozon_postings
                       (posting_number, line_no, ozon_sku, offer_id, ozon_warehouse_id, product_id,
                        warehouse_id, quantity, status, stock_deducted, raw_json, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?)""",
                    (posting_number, line_no, ozon_sku, offer_id, ozon_warehouse_id, product_id,
                     warehouse["id"] if warehouse else None, quantity,
                     1 if warehouse else 0, json.dumps(raw, ensure_ascii=False), now_iso(), now_iso()),
                )
                posting_row_id = posting_cur.lastrowid

                if warehouse:
                    _add_movement(
                        conn, product_id, warehouse["id"], MovementType.SALE, -quantity,
                        comment=f"Отправление Ozon {posting_number}",
                    )
                    movements_created += 1
                else:
                    log_lines.append(
                        f"Отправление {posting_number}: склад Ozon id={ozon_warehouse_id} не сопоставлен "
                        f"ни с одним вашим складом — остаток не списан, добавьте склад на странице «Склады»."
                    )
        conn.commit()
    except OzonApiError as e:
        conn.rollback()
        log_lines.append(f"Не удалось получить отправления FBS: {e}")
        conn.close()
        return {"status": "error", "postings_fetched": 0, "movements_created": 0, "message": "\n".join(log_lines)}

    # Сверка отмен — только среди отправлений, у которых остаток числится
    # списанным, за последые 30 дней (Ozon хранит /list за период, не по ID).
    #
    # /v4/posting/fbs/list отдаёт максимум 100 штук за раз (см. правку в
    # ozon_client.py после /ozon-diagnostics) — при большом обороте за 30
    # дней отправлений может быть больше, поэтому листаем страницами через
    # offset, пока Ozon не перестанет отдавать полную страницу (с разумным
    # потолком в 20 страниц = 2000 отправлений, чтобы не уйти в бесконечный
    # цикл при неожиданном ответе).
    cancelled_reversed = 0
    try:
        since = (dt.datetime.utcnow() - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        to = dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        postings = []
        offset = 0
        page_size = 100
        for _ in range(20):
            result = client.list_postings(since, to, limit=page_size, offset=offset)
            page = result.get("postings", []) if isinstance(result, dict) else (result or [])
            postings.extend(page)
            if len(page) < page_size:
                break
            offset += page_size
        status_by_number = {p.get("posting_number"): p.get("status") for p in postings}

        tracked = conn.execute(
            "SELECT * FROM ozon_postings WHERE stock_deducted = 1"
        ).fetchall()
        for order in tracked:
            new_status = status_by_number.get(order["posting_number"])
            if new_status and new_status in OZON_CANCEL_STATUSES and order["status"] not in OZON_CANCEL_STATUSES:
                conn.execute(
                    "UPDATE ozon_postings SET status = ?, stock_deducted = 0, updated_at = ? WHERE id = ?",
                    (new_status, now_iso(), order["id"]),
                )
                _add_movement(
                    conn, order["product_id"], order["warehouse_id"], MovementType.SALE_REVERSAL,
                    order["quantity"], comment=f"Отмена отправления Ozon {order['posting_number']}",
                )
                cancelled_reversed += 1
                movements_created += 1
            elif new_status and new_status != order["status"]:
                conn.execute(
                    "UPDATE ozon_postings SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, now_iso(), order["id"]),
                )
        conn.commit()
    except OzonApiError as e:
        log_lines.append(f"Не удалось сверить статусы/отмены: {e}")

    conn.close()
    return {
        "status": "error" if any("получить отправления" in line for line in log_lines) else (
            "warning" if log_lines else "success"
        ),
        "postings_fetched": postings_fetched,
        "movements_created": movements_created,
        "cancelled_reversed": cancelled_reversed,
        "message": "\n".join(log_lines) if log_lines else None,
    }


# -------------------------------------------------------------- FBO-поставки
def refresh_supplies(client: OzonClient | None = None) -> dict:
    """Подтягивает список поставок FBO и их состав из Ozon (только читает —
    ничего не грузит на остаток, это делает отдельная кнопка «Загрузить
    поставку», см. load_supply ниже). Безопасно вызывать повторно: уже
    известные поставки (по supply_order_id) обновляют только статус/состав,
    признак loaded не трогается.

    ПРАВКА (после /ozon-diagnostics, «в поставке нет ни одной позиции»):
    bundle_id раньше искался только как ПРЯМОЕ поле объекта поставки
    (info["bundle_id"] / info["supply_id"]). В официальной схеме
    SupplyOrderGetResponse у одной заявки на поставку (order) может быть
    НЕСКОЛЬКО фактических поставок в разные кластеры/склады — они, по
    документации, лежат вложенным списком (обычно под ключом "supplies"),
    и именно у элемента ЭТОГО списка есть свой supply_id/bundle_id, а не у
    самой заявки. Добавлен разбор с проверкой такого вложенного списка —
    но точное имя ключа и то, что там реально приходит, живым запросом не
    подтверждено (сеть до api-seller.ozon.ru недоступна). Если после этой
    правки состав по-прежнему не подтягивается — на /ozon-diagnostics
    добавлен сырой, необработанный ответ Ozon по обоим методам
    (get_supply_orders_info_raw, get_supply_bundle_raw), там будет видно
    точную структуру, и это можно будет поправить прицельно, без гадания."""
    client = client or OzonClient()
    conn = get_conn()
    discovered = 0
    errors: list[str] = []
    try:
        order_ids = client.list_supply_orders()
        infos = client.get_supply_orders_info(order_ids) if order_ids else []

        for info in infos:
            supply_order_id = str(
                info.get("supply_order_id") or info.get("order_id") or info.get("id") or ""
            )
            if not supply_order_id:
                continue
            status = info.get("state") or info.get("status")
            bundle_id = info.get("bundle_id") or info.get("supply_id")
            if not bundle_id:
                # Вложенный список фактических поставок заявки — см. правку
                # в докстринге выше. Перебираем правдоподобные имена ключа
                # и по возможности статус тоже берём из первой поставки,
                # если у самой заявки его не было.
                nested = info.get("supplies") or info.get("supply") or info.get("supply_orders") or []
                if isinstance(nested, dict):
                    nested = [nested]
                for sub in nested:
                    if not isinstance(sub, dict):
                        continue
                    bundle_id = sub.get("bundle_id") or sub.get("supply_id") or sub.get("id")
                    status = status or sub.get("state") or sub.get("status")
                    if bundle_id:
                        break

            existing = conn.execute(
                "SELECT * FROM ozon_supplies WHERE supply_order_id = ?", (supply_order_id,)
            ).fetchone()
            if existing:
                supply_id = existing["id"]
                conn.execute(
                    "UPDATE ozon_supplies SET status = ?, raw_json = ?, updated_at = ? WHERE id = ?",
                    (status, json.dumps(info, ensure_ascii=False), now_iso(), supply_id),
                )
            else:
                cur = conn.execute(
                    """INSERT INTO ozon_supplies
                       (supply_order_id, status, loaded, raw_json, created_at, updated_at)
                       VALUES (?, ?, 0, ?, ?, ?)""",
                    (supply_order_id, status, json.dumps(info, ensure_ascii=False), now_iso(), now_iso()),
                )
                supply_id = cur.lastrowid
                discovered += 1

            # Состав поставки — только если ещё не загружена (после загрузки
            # состав фиксирован тем, что уже перенесено на остаток, менять
            # его задним числом опасно — при необходимости корректировка
            # делается вручную через «Движения», как и везде в приложении).
            already_loaded = existing and existing["loaded"]
            if bundle_id and not already_loaded:
                items = client.get_supply_bundle([bundle_id])
                conn.execute("DELETE FROM ozon_supply_items WHERE supply_id = ?", (supply_id,))
                for it in items:
                    ozon_sku = it.get("sku")
                    offer_id = it.get("offer_id")
                    quantity = int(it.get("quantity") or 0)
                    name_hint = it.get("name")
                    product_id = _find_or_create_product(conn, ozon_sku, offer_id, name_hint)
                    conn.execute(
                        """INSERT INTO ozon_supply_items
                           (supply_id, ozon_sku, offer_id, name_hint, quantity, product_id)
                           VALUES (?, ?, ?, ?, ?, ?)""",
                        (supply_id, ozon_sku, offer_id, name_hint, quantity, product_id),
                    )
        conn.commit()
    except OzonApiError as e:
        conn.rollback()
        errors.append(f"Не удалось обновить список поставок FBO: {e}")
    finally:
        conn.close()
    return {"discovered": discovered, "errors": errors}


def load_supply(supply_id: int, user_id: int) -> dict:
    """Загрузка поставки: одной операцией переносит весь её состав с
    ФФ-источника (is_ozon_fbo_source) на склад «Ozon FBO». Нельзя выполнить
    дважды для одной и той же поставки (см. ozon_supplies.loaded)."""
    conn = get_conn()
    try:
        supply = conn.execute("SELECT * FROM ozon_supplies WHERE id = ?", (supply_id,)).fetchone()
        if not supply:
            return {"ok": False, "error": "Поставка не найдена"}
        if supply["loaded"]:
            return {"ok": False, "error": "Эта поставка уже была загружена ранее"}

        source_warehouse_id = get_ozon_fbo_source_warehouse_id(conn)
        if not source_warehouse_id:
            return {
                "ok": False,
                "error": "Не отмечен ФФ-источник поставок Ozon FBO — отметьте его на странице «Склады» "
                         "(галочка у нужного ФФ) и повторите.",
            }
        fbo_warehouse_id = get_or_create_ozon_fbo_warehouse(conn)

        items = conn.execute(
            "SELECT * FROM ozon_supply_items WHERE supply_id = ?", (supply_id,)
        ).fetchall()
        if not items:
            return {"ok": False, "error": "В поставке нет ни одной позиции — нечего загружать"}

        moved = 0
        for item in items:
            if not item["product_id"] or not item["quantity"]:
                continue
            out_id = _add_movement(
                conn, item["product_id"], source_warehouse_id, MovementType.TRANSFER_OUT,
                -item["quantity"], comment=f"Поставка Ozon FBO #{supply['supply_order_id']}",
            )
            in_id = _add_movement(
                conn, item["product_id"], fbo_warehouse_id, MovementType.TRANSFER_IN,
                item["quantity"], comment=f"Поставка Ozon FBO #{supply['supply_order_id']}",
                related_movement_id=out_id,
            )
            conn.execute("UPDATE stock_movements SET related_movement_id = ? WHERE id = ?", (in_id, out_id))
            moved += 1

        conn.execute(
            "UPDATE ozon_supplies SET loaded = 1, loaded_at = ?, loaded_by_id = ?, updated_at = ? WHERE id = ?",
            (now_iso(), user_id, now_iso(), supply_id),
        )
        conn.commit()
        return {"ok": True, "items_moved": moved}
    except Exception as e:
        conn.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        conn.close()
