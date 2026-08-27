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

# Сколько ID заказов отправлять в одном запросе /orders/status. Точный лимит
# WB не задокументирован нигде, где я могла его проверить, поэтому берём
# заведомо небольшую пачку — так один слишком большой запрос не может
# положить всю проверку статусов сразу (см. README про историю багов).
STATUS_CHECK_BATCH_SIZE = 200


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

            # WB отдаёт статус сборочного задания в поле supplierStatus
            # (new/confirm/complete/cancel) — поля "status" в ответе нет вообще,
            # из-за чего смена статуса раньше не замечалась ни разу.
            status_map = {str(s.get("id")): s.get("supplierStatus") for s in statuses}

            for wb_order_id in (str(x) for x in batch_ids):
                order = tracked_by_wb_id.get(wb_order_id)
                if not order:
                    continue
                new_status = status_map.get(wb_order_id)
                if not new_status or new_status == order["status"]:
                    continue
                conn.execute(
                    "UPDATE wb_orders SET status = ?, updated_at = ? WHERE id = ?",
                    (new_status, now_iso(), order["id"]),
                )

                if new_status in CANCEL_STATUSES and order["stock_deducted"] and order["warehouse_id"]:
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
