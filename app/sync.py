"""
Бизнес-логика синхронизации с WB.

Приложение работает только на чтение — оно НЕ отправляет остатки обратно в
WB. Тем, что видят покупатели на карточке (доступность товара), занимается
каждый склад самостоятельно, вне этого приложения. Здесь только считаем
свою собственную картину: сколько где лежит, по данным о приходе/
перемещениях (вносите вручную) и о заказах/отменах (подтягиваем из WB).

Логика по умолчанию (можно скорректировать под себя после первого теста на
реальных данных):

1. Новый заказ (сборочное задание) появился в WB -> считаем, что товар уже
   "зарезервирован на отгрузку", и сразу списываем его с остатка того
   склада, с которого WB просит собрать заказ, в НАШЕЙ базе. Это самый
   безопасный момент для списания: как только заказ висит в "новых" — его
   нельзя продать ещё раз.
2. Если заказ отменяется (клиент отменил / брак при сборке и т.п.) —
   списание отменяется, товар возвращается на остаток — тоже только у нас.

Отдельно от этого файла в README описано, как позже добавить сверку с
Statistics API (/supplier/sales) — она не меняет остаток автоматически, а
только сигнализирует о расхождениях, т.к. эти данные приходят с задержкой.
"""
import datetime as dt
import json
import sqlite3

from app.database import get_conn, now_iso
from app.models import MovementType, MovementSource
from app.wb_client import WBClient, WBApiError

CANCEL_STATUSES = {"cancel", "canceled", "cancelled", "declined", "reject"}

# Отмену со стороны supplierStatus (выше) видно только если ПРОДАВЕЦ сам что-то
# сделал с заказом. 27.08.2026 выяснилось на реальных заказах Алёны: если
# заказ отменяет КЛИЕНТ до того, как продавец успел его подтвердить/собрать,
# supplierStatus так и остаётся 'new' навсегда — реальная отмена видна только
# в отдельном поле wbStatus. Именно поэтому 131 реальная отмена в кабинете WB
# Partners не давала вообще ни одной отмены в этом приложении.
#
# Подтверждено реальными данными: "declined_by_client". Остальные значения
# добавлены по аналогии (документация WB по этому полю нигде не даёт
# исчерпывающего списка) — если появится ещё не учтённое значение, страница
# /wb-diagnostics покажет его сырым текстом в колонке wbStatus, и его можно
# будет дописать сюда.
WB_STATUS_CANCEL_VALUES = {
    "declined_by_client", "canceled", "cancelled", "canceled_by_client", "cancelled_by_client",
}

# Сколько ID заказов отправлять в одном запросе /orders/status. Точный лимит
# WB не задокументирован нигде, где я могла его проверить, поэтому берём
# заведомо небольшую пачку — так один слишком большой запрос не может
# положить всю проверку статусов сразу (см. README про историю багов).
STATUS_CHECK_BATCH_SIZE = 200

# Защита от бесконечного цикла при догрузке истории заказов (backfill_order_history),
# если WB когда-нибудь вернёт курсор пагинации, который не двигается с места.
BACKFILL_MAX_PAGES = 500


def get_current_stock(conn: sqlite3.Connection, product_id: int, warehouse_id: int) -> int:
    row = conn.execute(
        "SELECT COALESCE(SUM(delta), 0) AS total FROM stock_movements "
        "WHERE product_id = ? AND warehouse_id = ?",
        (product_id, warehouse_id),
    ).fetchone()
    return int(row["total"] or 0)


def get_stock_table(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Текущие остатки по всем товарам и складам (плоский список, без группировки по ФФ)."""
    return conn.execute(
        """
        SELECT p.id AS product_id, p.sku, p.name,
               w.id AS warehouse_id, w.name AS warehouse_name,
               COALESCE(SUM(m.delta), 0) AS quantity
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        GROUP BY p.id, w.id
        ORDER BY p.name, w.name
        """
    ).fetchall()


def get_product_totals(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Итого по каждому товару (артикулу) сразу по ВСЕМ складам и ФФ —
    для верхнего сводного блока дашборда."""
    return conn.execute(
        """
        SELECT p.id AS product_id, p.sku, p.name,
               COALESCE(SUM(m.delta), 0) AS quantity
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        GROUP BY p.id
        ORDER BY p.name
        """
    ).fetchall()


def get_stock_by_ff(conn: sqlite3.Connection, as_of: str | None = None) -> list[dict]:
    """Остатки, сгруппированные по фулфилмент-центрам — для дашборда и для
    раздела «Остатки на дату» в аналитике.

    Важно: ФФ здесь — это единственный физический уровень, на котором
    остаток вообще что-то значит. Склады WB внутри одного ФФ — виртуальные
    ярлыки самого WB для маршрутизации заказов, физически весь товар лежит
    в одном месте (см. обсуждение с Алёной 2026-08-26). Поэтому здесь
    намеренно НЕТ разбивки по складам внутри ФФ — только итог по ФФ и по
    товару внутри него. Списание по конкретному заказу по-прежнему
    учитывается через конкретный склад WB (это нужно для сопоставления
    заказов), но как только дело доходит до отображения остатка — всё
    сворачивается на уровень ФФ.

    as_of: дата в формате YYYY-MM-DD (московское время, конец дня) — если
    указана, считает остаток НЕ на сейчас, а на конец этого дня (сумма всех
    движений с датой не позже этого момента). По умолчанию (None) — текущий
    остаток на сейчас, как и раньше.

    Возвращает список групп в порядке: сначала привязанные ФФ (по алфавиту),
    в конце — склады без привязки к ФФ (это самостоятельные физические
    места, каждый — своя группа). Каждая группа:
      {
        "ff_id": int | None,
        "ff_name": str,
        "ff_total": int,                  # сумма по ВСЕМ товарам сразу в рамках ФФ
        "totals": [{"product_id", "sku", "name", "quantity"}, ...],  # итого по товару
      }
    """
    where_clause = ""
    params: dict = {}
    if as_of:
        # конец дня по Москве -> в UTC для сравнения со строками created_at
        where_clause = "WHERE m.created_at <= datetime(:as_of || ' 23:59:59', '-3 hours')"
        params["as_of"] = as_of

    flat = conn.execute(
        f"""
        SELECT p.id AS product_id, p.sku, p.name AS product_name,
               f.id AS ff_id, f.name AS ff_name,
               COALESCE(SUM(m.delta), 0) AS quantity
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        {where_clause}
        GROUP BY p.id, f.id
        ORDER BY (f.name IS NULL), f.name, p.name
        """,
        params,
    ).fetchall()

    groups: dict = {}
    order: list = []
    for row in flat:
        key = row["ff_id"]  # None — склад(ы) без привязки к ФФ
        if key not in groups:
            groups[key] = {
                "ff_id": key,
                "ff_name": row["ff_name"] or "Без ФФ (внутренние или ещё не привязанные склады)",
                "_totals_map": {},
            }
            order.append(key)
        group = groups[key]
        totals_map = group["_totals_map"]
        if row["product_id"] not in totals_map:
            totals_map[row["product_id"]] = {
                "product_id": row["product_id"], "sku": row["sku"],
                "name": row["product_name"], "quantity": 0,
            }
        totals_map[row["product_id"]]["quantity"] += row["quantity"]

    result = []
    for key in order:
        group = groups[key]
        group["totals"] = sorted(group["_totals_map"].values(), key=lambda t: t["name"])
        group["ff_total"] = sum(t["quantity"] for t in group["totals"])
        del group["_totals_map"]
        result.append(group)
    return result


def get_stock_locations(conn: sqlite3.Connection) -> list[dict]:
    """Единый список мест хранения для форм прихода/списания/перемещения.

    Один физический ФФ — это ОДНО место, даже если внутри него несколько
    виртуальных складов WB — поэтому в формах выбирается ФФ целиком, а не
    конкретный склад внутри него. Под капотом движение по-прежнему пишется
    против конкретной строки warehouses (это требование схемы и нужно для
    заказов WB), но для ФФ мы всегда берём один и тот же «канонический»
    склад этого ФФ — какой именно, пользователю не нужно ни видеть, ни
    выбирать, потому что физически это всё равно одно и то же место.

    Склады без привязки к ФФ — самостоятельные физические места, показаны
    как есть, по одному.
    """
    result = []
    ffs = conn.execute(
        "SELECT * FROM fulfillment_centers WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    for ff in ffs:
        canonical = conn.execute(
            "SELECT id FROM warehouses WHERE fulfillment_center_id = ? AND is_active = 1 "
            "ORDER BY id LIMIT 1",
            (ff["id"],),
        ).fetchone()
        if canonical:
            result.append({
                "key": f"ff:{ff['id']}", "label": f"ФФ «{ff['name']}»",
                "warehouse_id": canonical["id"], "ff_id": ff["id"],
            })
    standalone = conn.execute(
        "SELECT * FROM warehouses WHERE is_active = 1 AND fulfillment_center_id IS NULL ORDER BY name"
    ).fetchall()
    for w in standalone:
        result.append({
            "key": f"wh:{w['id']}", "label": w["name"],
            "warehouse_id": w["id"], "ff_id": None,
        })
    return result


def _find_or_create_product(conn: sqlite3.Connection, nm_id, barcode, name_hint: str) -> int:
    product = None
    if barcode:
        product = conn.execute("SELECT * FROM products WHERE barcode = ?", (barcode,)).fetchone()
    if not product and nm_id:
        product = conn.execute("SELECT * FROM products WHERE nm_id = ?", (nm_id,)).fetchone()
    if product:
        return product["id"]

    # Неизвестный товар пришёл из WB — создаём заготовку, чтобы ничего не потерять,
    # но её стоит донастроить (задать нормальный SKU) на странице «Товары».
    sku = barcode or (f"wb-{nm_id}" if nm_id else f"unknown-{dt.datetime.utcnow().timestamp()}")
    cur = conn.execute(
        "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES (?, ?, ?, ?, ?)",
        (sku, nm_id, barcode, name_hint or sku, now_iso()),
    )
    return cur.lastrowid


def _find_warehouse_by_wb_id(conn: sqlite3.Connection, wb_warehouse_id):
    if not wb_warehouse_id:
        return None
    return conn.execute(
        "SELECT * FROM warehouses WHERE wb_warehouse_id = ?", (wb_warehouse_id,)
    ).fetchone()


def _add_movement(conn, product_id, warehouse_id, movement_type, delta, source,
                   wb_order_row_id=None, comment=None, created_by_id=None, related_movement_id=None):
    conn.execute(
        """INSERT INTO stock_movements
           (product_id, warehouse_id, movement_type, delta, source, related_movement_id,
            wb_order_id, comment, created_by_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, warehouse_id, movement_type, delta, source, related_movement_id,
         wb_order_row_id, comment, created_by_id, now_iso()),
    )


def _is_cancelled(supplier_status, wb_status) -> bool:
    """Отмена — если её видно ЛИБО в supplierStatus (продавец сам отменил/
    отклонил), ЛИБО в wbStatus (клиент отменил, а supplierStatus мог и не
    поменяться — см. комментарий у WB_STATUS_CANCEL_VALUES)."""
    return supplier_status in CANCEL_STATUSES or wb_status in WB_STATUS_CANCEL_VALUES


def _parse_order_identity(raw: dict):
    """Достаёт из сырого объекта заказа WB общие поля — тот же формат что у
    /orders/new, что и у общего /orders (используется в Этапе 1 обычной
    синхронизации и в backfill_order_history)."""
    wb_order_id = str(raw.get("orderId") or raw.get("id"))
    nm_id = raw.get("nmId")
    barcode = raw.get("skus", [None])[0] if raw.get("skus") else raw.get("barcode")
    wb_warehouse_id = raw.get("warehouseId")
    return wb_order_id, nm_id, barcode, wb_warehouse_id


def sync_once(client: WBClient | None = None) -> dict:
    """Один цикл синхронизации. Открывает собственное соединение с БД —
    можно безопасно вызывать и из фонового потока, и из обработчика запроса."""
    client = client or WBClient()
    conn = get_conn()

    started_at = now_iso()
    cur = conn.execute(
        "INSERT INTO sync_runs (started_at, status) VALUES (?, 'running')", (started_at,)
    )
    run_id = cur.lastrowid
    conn.commit()  # фиксируем run сразу отдельной транзакцией, чтобы её не смыло rollback'ом ниже

    log_lines: list[str] = []
    orders_fetched = 0
    movements_created = 0

    # ------------------------------------------------------------- Этап 1
    # Новые заказы -> сразу списываем остаток. Всё-или-ничего В ПРЕДЕЛАХ
    # этого этапа (если что-то пошло не так на середине — откатываем только
    # его), но НЕЗАВИСИМО от этапа 2: раньше оба этапа были одной большой
    # транзакцией, и сбой при проверке статусов (этап 2) откатывал уже
    # обработанные новые заказы тоже — этим объяснялись случаи, когда после
    # синхронизации "заказов получено" росло, а по факту ничего не менялось.
    stage1_failed = False
    try:
        new_orders = client.get_new_orders()
        orders_fetched = len(new_orders)

        for raw in new_orders:
            wb_order_id = str(raw.get("orderId") or raw.get("id"))
            existing = conn.execute(
                "SELECT id FROM wb_orders WHERE wb_order_id = ?", (wb_order_id,)
            ).fetchone()
            if existing:
                continue  # уже видели этот заказ

            nm_id = raw.get("nmId")
            barcode = raw.get("skus", [None])[0] if raw.get("skus") else raw.get("barcode")
            wb_warehouse_id = raw.get("warehouseId")
            quantity = 1  # в FBS-заказе WB одна позиция = одна единица товара

            product_id = _find_or_create_product(conn, nm_id, barcode, name_hint=str(nm_id or barcode))
            warehouse = _find_warehouse_by_wb_id(conn, wb_warehouse_id)

            order_cur = conn.execute(
                """INSERT INTO wb_orders
                   (wb_order_id, nm_id, barcode, wb_warehouse_id, product_id, warehouse_id,
                    quantity, status, order_date, stock_deducted, raw_json, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, 'new', ?, ?, ?, ?, ?)""",
                (wb_order_id, nm_id, barcode, wb_warehouse_id, product_id,
                 warehouse["id"] if warehouse else None, quantity, now_iso(),
                 1 if warehouse else 0, json.dumps(raw, ensure_ascii=False), now_iso(), now_iso()),
            )
            order_row_id = order_cur.lastrowid

            if warehouse:
                _add_movement(
                    conn, product_id, warehouse["id"], MovementType.SALE, -quantity,
                    MovementSource.WB_SYNC, wb_order_row_id=order_row_id,
                    comment=f"Заказ WB {wb_order_id}",
                )
                movements_created += 1
            else:
                log_lines.append(
                    f"Заказ {wb_order_id}: склад WB id={wb_warehouse_id} не сопоставлен ни с одним "
                    f"вашим складом — остаток не списан, добавьте склад на странице «Склады»."
                )
        conn.commit()
    except WBApiError as e:
        conn.rollback()
        stage1_failed = True
        log_lines.append(f"Не удалось получить новые заказы: {e}")

    # ------------------------------------------------------------- Этап 2
    # Обновляем статусы отслеживаемых заказов, реагируем на отмены — пачками,
    # а не всеми заказами разом: пока определение статуса было сломано (см.
    # README/историю), почти все заказы за всё время застряли в статусе
    # "new", и один огромный запрос на все сразу мог упираться в лимиты WB и
    # рушить всю проверку. Сбой одной пачки не должен мешать остальным.
    stage2_had_errors = False
    try:
        tracked = conn.execute(
            "SELECT * FROM wb_orders WHERE status NOT IN ('complete', 'cancel', 'canceled', "
            "'cancelled', 'declined', 'reject')"
        ).fetchall()
        tracked_by_wb_id = {o["wb_order_id"]: o for o in tracked}
        ids = [int(o["wb_order_id"]) for o in tracked if str(o["wb_order_id"]).isdigit()]

        for batch_start in range(0, len(ids), STATUS_CHECK_BATCH_SIZE):
            batch_ids = ids[batch_start:batch_start + STATUS_CHECK_BATCH_SIZE]
            try:
                statuses = client.get_orders_status(batch_ids)
            except WBApiError as e:
                stage2_had_errors = True
                log_lines.append(
                    f"Не удалось проверить статусы пачки заказов "
                    f"({batch_start + 1}–{batch_start + len(batch_ids)} из {len(ids)}): {e}"
                )
                continue  # эта пачка не удалась — идём дальше, не бросаем всё

            # WB отдаёт статус сборочного задания сразу в двух полях:
            # supplierStatus (new/confirm/complete/cancel — управляется
            # продавцом) и wbStatus (управляется самим WB/клиентом). Раньше
            # здесь читался только supplierStatus — из-за этого отмены
            # клиентом, случившиеся до реакции продавца, не были видны вообще
            # (см. WB_STATUS_CANCEL_VALUES выше и диагностику 27.08.2026).
            wb_data_map = {str(s.get("id")): s for s in statuses}

            for wb_order_id in (str(x) for x in batch_ids):
                order = tracked_by_wb_id.get(wb_order_id)
                if not order:
                    continue
                wb_data = wb_data_map.get(wb_order_id)
                if not wb_data:
                    continue
                supplier_status = wb_data.get("supplierStatus")
                wb_status = wb_data.get("wbStatus")
                cancelled_now = _is_cancelled(supplier_status, wb_status)

                # Наш статус: если это отмена (по любому из двух полей WB) —
                # фиксируем как 'cancel', даже если supplierStatus формально
                # остался прежним. Иначе — как раньше, отражаем supplierStatus.
                new_status = "cancel" if cancelled_now else (supplier_status or order["status"])
                status_changed = new_status != order["status"]
                wb_status_changed = wb_status != order["wb_status"]
                if not status_changed and not wb_status_changed:
                    continue
                conn.execute(
                    "UPDATE wb_orders SET status = ?, wb_status = ?, updated_at = ? WHERE id = ?",
                    (new_status, wb_status, now_iso(), order["id"]),
                )

                if cancelled_now and order["stock_deducted"] and order["warehouse_id"]:
                    _add_movement(
                        conn, order["product_id"], order["warehouse_id"], MovementType.SALE_REVERSAL,
                        order["quantity"], MovementSource.WB_SYNC, wb_order_row_id=order["id"],
                        comment=f"Отмена заказа WB {order['wb_order_id']}",
                    )
                    conn.execute("UPDATE wb_orders SET stock_deducted = 0 WHERE id = ?", (order["id"],))
                    movements_created += 1
            conn.commit()  # фиксируем прогресс после каждой пачки — сбой следующей не откатит эту
    except Exception as e:
        # Непредвиденная (не WBApiError) ошибка на этапе статусов — не должна
        # ронять всю синхронизацию целиком, только эту часть.
        conn.rollback()
        stage2_had_errors = True
        log_lines.append(f"Непредвиденная ошибка при проверке статусов заказов: {e}")

    if stage1_failed:
        overall_status = "error"
    elif stage2_had_errors:
        overall_status = "warning"
    else:
        overall_status = "success"

    conn.execute(
        """UPDATE sync_runs
           SET status = ?, orders_fetched = ?, movements_created = ?,
               message = ?, finished_at = ?
           WHERE id = ?""",
        (overall_status, orders_fetched, movements_created,
         "\n".join(log_lines) if log_lines else None, now_iso(), run_id),
    )
    conn.commit()
    conn.close()
    return {
        "status": overall_status, "orders_fetched": orders_fetched,
        "movements_created": movements_created,
        "message": "\n".join(log_lines) if log_lines else None,
    }


def reconcile_all_orders(client: WBClient | None = None) -> dict:
    """Разовая (можно запускать и повторно) сверка ВСЕХ заказов в нашей базе
    с их актуальным статусом в WB — включая уже помеченные 'complete', то
    есть даже те, что обычный sync_once() больше не трогает вовсе (Этап 2
    там проверяет только «незавершённые» по нашему status).

    Понадобилась из-за найденной 27.08.2026 ошибки: заказ, отменённый
    клиентом ДО реакции продавца, у WB остаётся с supplierStatus='new' — то
    есть по прежней логике выглядел как обычный незавершённый заказ, и его
    stock_deducted никогда не обнулялся. Эта функция один раз проходит по
    ВСЕЙ истории заказов и возвращает остаток там, где отмена подтверждается
    полем wbStatus (см. WB_STATUS_CANCEL_VALUES), но у нас всё ещё числится
    списание.

    Ничего не отправляет обратно в WB — только читает статусы и, где нужно,
    добавляет движение-возврат в НАШЕЙ базе (как обычная отмена при
    синхронизации). Безопасно запускать повторно: уже возвращённые заказы
    (stock_deducted уже 0) при повторном обнаружении отмены не трогаются
    второй раз, обновляются только их status/wb_status.
    """
    client = client or WBClient()
    conn = get_conn()

    all_orders = conn.execute("SELECT * FROM wb_orders").fetchall()
    numeric_orders = [o for o in all_orders if str(o["wb_order_id"]).isdigit()]
    skipped_non_digit = len(all_orders) - len(numeric_orders)
    by_wb_id = {o["wb_order_id"]: o for o in numeric_orders}
    ids = [int(o["wb_order_id"]) for o in numeric_orders]

    checked = 0
    fixed = []
    errors = []

    try:
        for batch_start in range(0, len(ids), STATUS_CHECK_BATCH_SIZE):
            batch_ids = ids[batch_start:batch_start + STATUS_CHECK_BATCH_SIZE]
            try:
                statuses = client.get_orders_status(batch_ids)
            except WBApiError as e:
                errors.append(
                    f"Пачка {batch_start + 1}–{batch_start + len(batch_ids)} из {len(ids)}: {e}"
                )
                continue

            wb_data_map = {str(s.get("id")): s for s in statuses}

            for wb_order_id in (str(x) for x in batch_ids):
                order = by_wb_id.get(wb_order_id)
                if not order:
                    continue
                wb_data = wb_data_map.get(wb_order_id)
                if not wb_data:
                    continue
                checked += 1
                supplier_status = wb_data.get("supplierStatus")
                wb_status = wb_data.get("wbStatus")
                cancelled_now = _is_cancelled(supplier_status, wb_status)

                new_status = "cancel" if cancelled_now else (supplier_status or order["status"])
                needs_update = new_status != order["status"] or wb_status != order["wb_status"]

                reversed_now = False
                if cancelled_now and order["stock_deducted"] and order["warehouse_id"]:
                    _add_movement(
                        conn, order["product_id"], order["warehouse_id"], MovementType.SALE_REVERSAL,
                        order["quantity"], MovementSource.WB_SYNC, wb_order_row_id=order["id"],
                        comment=f"Отмена заказа WB {order['wb_order_id']} (найдено разовой сверкой)",
                    )
                    conn.execute("UPDATE wb_orders SET stock_deducted = 0 WHERE id = ?", (order["id"],))
                    reversed_now = True

                if needs_update or reversed_now:
                    conn.execute(
                        "UPDATE wb_orders SET status = ?, wb_status = ?, updated_at = ? WHERE id = ?",
                        (new_status, wb_status, now_iso(), order["id"]),
                    )

                if reversed_now:
                    fixed.append({
                        "wb_order_id": order["wb_order_id"],
                        "product_id": order["product_id"],
                        "warehouse_id": order["warehouse_id"],
                        "quantity": order["quantity"],
                        "old_status": order["status"],
                        "wb_status": wb_status,
                    })
            conn.commit()  # фиксируем прогресс после каждой пачки, как и в sync_once
    finally:
        conn.close()

    return {
        "checked": checked,
        "skipped_non_digit": skipped_non_digit,
        "fixed": fixed,
        "errors": errors,
    }


def backfill_order_history(client: WBClient | None = None) -> dict:
    """Догружает историю заказов через общий метод WB — `/api/v3/orders`
    (все заказы продавца, постранично), в отличие от `/orders/new`, который
    отдаёт только «ещё не взятые в работу». Нужна из-за находки 27.08.2026:
    заказ, отменённый клиентом быстрее, чем раз в SYNC_INTERVAL_MINUTES,
    успевает пропасть из «новых» ещё до того, как обычная синхронизация его
    увидит — на реальных 131 отмене Алёны так пропало 126 (96%), их не было
    в нашей базе вообще ни в каком виде.

    Для каждого найденного в общем списке заказа, которого ещё нет в нашей
    базе, сразу проверяется его АКТУАЛЬНЫЙ статус (supplierStatus/wbStatus,
    та же логика, что и в остальном коде):
      - если заказ уже отменён — просто фиксируется как отменённый, остаток
        не трогается вообще (раз заказ отменили — считаем, что его как бы
        не было: списывать и сразу же возвращать бессмысленно);
      - если заказ активный/выполнен — списывается остаток, ровно как это
        сделала бы обычная синхронизация, если бы увидела заказ вовремя.

    Безопасно запускать повторно (и после обрыва на середине, например по
    таймауту): уже известные заказы просто пропускаются, прогресс
    сохраняется постранично (commit после каждой страницы).
    """
    client = client or WBClient()
    conn = get_conn()

    discovered = 0
    added_active = 0
    added_cancelled = 0
    skipped_no_warehouse = 0
    errors = []

    try:
        cursor = 0
        for page_num in range(BACKFILL_MAX_PAGES):
            try:
                page = client.get_orders(limit=1000, next_cursor=cursor)
            except WBApiError as e:
                errors.append(f"Страница {page_num + 1} (next={cursor}): {e}")
                break

            page_orders = page.get("orders", [])
            if not page_orders:
                break

            # Отбираем только то, чего у нас ещё нет — остальное уже видели
            # либо обычной синхронизацией, либо предыдущим запуском этой же догрузки.
            new_by_wb_id = {}
            for raw in page_orders:
                wb_order_id, *_ = _parse_order_identity(raw)
                if not wb_order_id or wb_order_id == "None":
                    continue
                if wb_order_id in new_by_wb_id:
                    continue  # дубль внутри той же страницы ответа WB
                existing = conn.execute(
                    "SELECT id FROM wb_orders WHERE wb_order_id = ?", (wb_order_id,)
                ).fetchone()
                if existing:
                    continue
                new_by_wb_id[wb_order_id] = raw

            discovered += len(new_by_wb_id)

            if new_by_wb_id:
                numeric_ids = [int(wb_id) for wb_id in new_by_wb_id if wb_id.isdigit()]
                wb_status_by_id = {}
                for batch_start in range(0, len(numeric_ids), STATUS_CHECK_BATCH_SIZE):
                    batch_ids = numeric_ids[batch_start:batch_start + STATUS_CHECK_BATCH_SIZE]
                    try:
                        statuses = client.get_orders_status(batch_ids)
                        for s in statuses:
                            wb_status_by_id[str(s.get("id"))] = s
                    except WBApiError as e:
                        errors.append(f"Статусы новых заказов (страница {page_num + 1}): {e}")

                for wb_order_id, raw in new_by_wb_id.items():
                    _, nm_id, barcode, wb_warehouse_id = _parse_order_identity(raw)
                    quantity = 1
                    wb_data = wb_status_by_id.get(wb_order_id)
                    supplier_status = wb_data.get("supplierStatus") if wb_data else None
                    wb_status = wb_data.get("wbStatus") if wb_data else None
                    cancelled = _is_cancelled(supplier_status, wb_status)

                    product_id = _find_or_create_product(conn, nm_id, barcode, name_hint=str(nm_id or barcode))
                    warehouse = _find_warehouse_by_wb_id(conn, wb_warehouse_id)

                    if cancelled:
                        status, stock_deducted = "cancel", 0
                    else:
                        status, stock_deducted = (supplier_status or "new"), (1 if warehouse else 0)

                    order_cur = conn.execute(
                        """INSERT INTO wb_orders
                           (wb_order_id, nm_id, barcode, wb_warehouse_id, product_id, warehouse_id,
                            quantity, status, wb_status, order_date, stock_deducted, raw_json,
                            created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (wb_order_id, nm_id, barcode, wb_warehouse_id, product_id,
                         warehouse["id"] if warehouse else None, quantity, status, wb_status,
                         now_iso(), stock_deducted, json.dumps(raw, ensure_ascii=False),
                         now_iso(), now_iso()),
                    )
                    order_row_id = order_cur.lastrowid

                    if cancelled:
                        added_cancelled += 1
                    elif warehouse:
                        _add_movement(
                            conn, product_id, warehouse["id"], MovementType.SALE, -quantity,
                            MovementSource.WB_SYNC, wb_order_row_id=order_row_id,
                            comment=f"Заказ WB {wb_order_id} (найден догрузкой истории)",
                        )
                        added_active += 1
                    else:
                        skipped_no_warehouse += 1

                conn.commit()  # фиксируем прогресс после каждой страницы — можно спокойно прерваться

            next_cursor = page.get("next")
            if next_cursor is None or next_cursor == cursor:
                break
            cursor = next_cursor
        else:
            errors.append(
                f"Достигнут предел в {BACKFILL_MAX_PAGES} страниц — возможно, догрузили не всю "
                f"историю, можно безопасно запустить ещё раз."
            )
    finally:
        conn.close()

    return {
        "discovered": discovered,
        "added_active": added_active,
        "added_cancelled": added_cancelled,
        "skipped_no_warehouse": skipped_no_warehouse,
        "errors": errors,
    }


BACKFILL_MOVEMENT_MARKER = "(найден догрузкой истории)"


def undo_history_backfill(marker: str = BACKFILL_MOVEMENT_MARKER) -> dict:
    """АВАРИЙНАЯ отмена последствий ошибки в backfill_order_history()
    (27.08.2026): та функция предполагала, что общий метод WB
    (`/api/v3/orders`) отдаёт только заказы из «рабочего» периода — на деле
    он отдаёт ВСЮ историю заказов магазина на WB за всё время, включая те,
    что были задолго до этого приложения. Код списывал остаток по КАЖДОМУ
    найденному активному заказу, как будто это только что случившаяся
    продажа — из-за этого остаток по нескольким товарам массово ушёл в
    минус.

    Отменяет ровно то, что создал этот сбойный запуск, и ничего больше:
    все движения, созданные им, однозначно помечены комментарием
    `marker` (see backfill_order_history) — удаляем их и сами строки
    wb_orders, к которым они относятся, восстанавливая остаток к состоянию
    до бага. «Отменённые» заказы из того же запуска (без движения — на
    остаток и так не повлияли) этой функцией НЕ трогаются: они безвредны,
    их можно спокойно почистить отдельно, без спешки.
    """
    conn = get_conn()
    try:
        tainted_orders = conn.execute(
            "SELECT DISTINCT wb_order_id FROM stock_movements "
            "WHERE comment LIKE ? AND wb_order_id IS NOT NULL",
            (f"%{marker}%",),
        ).fetchall()
        tainted_order_ids = [row["wb_order_id"] for row in tainted_orders]

        movements_deleted = conn.execute(
            "SELECT COUNT(*) AS c FROM stock_movements WHERE comment LIKE ?", (f"%{marker}%",)
        ).fetchone()["c"]

        conn.execute("DELETE FROM stock_movements WHERE comment LIKE ?", (f"%{marker}%",))

        orders_deleted = 0
        if tainted_order_ids:
            placeholders = ",".join("?" for _ in tainted_order_ids)
            orders_deleted = conn.execute(
                f"SELECT COUNT(*) AS c FROM wb_orders WHERE id IN ({placeholders})",
                tainted_order_ids,
            ).fetchone()["c"]
            conn.execute(f"DELETE FROM wb_orders WHERE id IN ({placeholders})", tainted_order_ids)

        conn.commit()
        return {"movements_deleted": movements_deleted, "orders_deleted": orders_deleted}
    finally:
        conn.close()
