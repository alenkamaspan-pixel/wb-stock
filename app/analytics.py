"""
Запросы для страницы «Аналитика».

Всё, как и на дашборде, считается на лету по истории движений
(stock_movements) — отдельных таблиц для аналитики заводить не нужно,
это тот же принцип: журнал движений — единственный источник правды.

Важный нюанс с датами: в базе все даты хранятся в UTC (см. now_iso() в
database.py), а бизнес ведётся по московскому времени. Здесь везде, где
считаем «по дням» или сравниваем с датой, сдвигаем время на +3 часа перед
группировкой — иначе продажа в 23:30 по Москве могла бы задним числом
попасть в статистику следующего дня.

Точность отчётов «на дату» назад во времени ограничена тем, что часть
прихода вносилась не день в день, а задним числом одной записью — это
известное ограничение, отдельно обозначено в шаблоне страницы.
"""
import sqlite3

MSK_SHIFT_SQL = "datetime(m.created_at, '+3 hours')"


def _date_bounds(date_from: str, date_to: str) -> tuple[str, str]:
    """Принимает даты в формате YYYY-MM-DD (как из <input type=date>) и
    возвращает границы в UTC для сравнения со строками created_at.
    Диапазон в терминах московского времени переводим обратно в UTC
    (-3 часа), чтобы использовать со «спрятанным» created_at без сдвига
    в WHERE (сдвигаем сравниваемые границы, а не каждую строку — быстрее).

    28.08.2026: нашли баг (Алёна заметила, что «Журнал движений» и весь
    остальной раздел «Аналитика» не показывают вообще ничего за текущие
    сутки, начиная примерно с 3 часов ночи по Москве). Причина — SQLite
    datetime(...) всегда возвращает границу с ПРОБЕЛОМ между датой и
    временем ('2026-08-28 20:59:59'), а created_at у нас пишется через
    Python datetime.isoformat() с буквой 'T' ('2026-08-28T11:17:03', см.
    now_iso() в database.py). При сравнении строк 'T' (0x54) больше
    пробела (0x20), поэтому ЛЮБАЯ запись за ту же календарную дату, что и
    верхняя граница, считалась "больше" границы и вылетала из BETWEEN —
    независимо от времени суток. Из-за этого «сегодня» в UTC (то есть
    начиная с 00:00 UTC = 03:00 по Москве) всегда пропадало из выдачи.
    Остатки WB это не затрагивало — там дат не фильтруют (см. /wb-orders-log,
    dashboard), пострадала только «Аналитика». Чиним, приводя формат границы
    к тому же 'T'-разделителю, что и у created_at — REPLACE(..., ' ', 'T')."""
    return (
        f"REPLACE(datetime('{date_from} 00:00:00', '-3 hours'), ' ', 'T')",
        f"REPLACE(datetime('{date_to} 23:59:59', '-3 hours'), ' ', 'T')",
    )


def get_period_stats(
    conn: sqlite3.Connection, date_from: str, date_to: str,
    ff_id=None, product_id=None,
) -> list[sqlite3.Row]:
    """Приход/продажи/отмены/списания за период, сгруппированные по
    товару и ФФ. net_sold = продано минус отменённое — это и есть
    'сколько реально продано' за вычетом отмен в том же периоде."""
    lo, hi = _date_bounds(date_from, date_to)
    sql = f"""
        SELECT p.id AS product_id, p.sku, p.name AS product_name,
               f.id AS ff_id, f.name AS ff_name,
               SUM(CASE WHEN m.movement_type = 'income' THEN m.delta ELSE 0 END) AS income_qty,
               SUM(CASE WHEN m.movement_type = 'sale' THEN -m.delta ELSE 0 END) AS sale_qty,
               SUM(CASE WHEN m.movement_type = 'sale_reversal' THEN m.delta ELSE 0 END) AS reversal_qty,
               SUM(CASE WHEN m.movement_type = 'writeoff' THEN -m.delta ELSE 0 END) AS writeoff_qty
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        WHERE m.created_at BETWEEN {lo} AND {hi}
          {"AND f.id = :ff_id" if ff_id else ""}
          {"AND p.id = :product_id" if product_id else ""}
        GROUP BY p.id, f.id
        ORDER BY p.name, f.name
    """
    params = {}
    if ff_id:
        params["ff_id"] = ff_id
    if product_id:
        params["product_id"] = product_id
    rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["net_sold"] = d["sale_qty"] - d["reversal_qty"]
        d["cancel_rate"] = (d["reversal_qty"] / d["sale_qty"]) if d["sale_qty"] else None
        result.append(d)
    return result


def get_daily_series(
    conn: sqlite3.Connection, date_from: str, date_to: str,
    ff_id=None, product_id=None,
) -> list[dict]:
    """Продажи и приход по дням (московское время) — для графика тренда."""
    lo, hi = _date_bounds(date_from, date_to)
    sql = f"""
        SELECT date({MSK_SHIFT_SQL}) AS day,
               SUM(CASE WHEN m.movement_type = 'income' THEN m.delta ELSE 0 END) AS income_qty,
               SUM(CASE WHEN m.movement_type = 'sale' THEN -m.delta ELSE 0 END) AS sale_qty,
               SUM(CASE WHEN m.movement_type = 'sale_reversal' THEN m.delta ELSE 0 END) AS reversal_qty
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        WHERE m.created_at BETWEEN {lo} AND {hi}
          {"AND f.id = :ff_id" if ff_id else ""}
          {"AND p.id = :product_id" if product_id else ""}
        GROUP BY day
        ORDER BY day
    """
    params = {}
    if ff_id:
        params["ff_id"] = ff_id
    if product_id:
        params["product_id"] = product_id
    rows = conn.execute(sql, params).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["net_sold"] = d["sale_qty"] - d["reversal_qty"]
        result.append(d)
    return result


def get_velocity_table(
    conn: sqlite3.Connection, window_days: int, ff_id=None, product_id=None,
) -> list[dict]:
    """Темп продаж и сколько дней осталось — по каждому товару и ФФ, за
    последние window_days дней (от текущего момента, московское время)."""
    sql_window = f"""
        SELECT p.id AS product_id, p.sku, p.name AS product_name,
               f.id AS ff_id, f.name AS ff_name,
               SUM(CASE WHEN m.movement_type = 'sale' THEN -m.delta ELSE 0 END) AS sale_qty,
               SUM(CASE WHEN m.movement_type = 'sale_reversal' THEN m.delta ELSE 0 END) AS reversal_qty
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        WHERE {MSK_SHIFT_SQL} >= datetime('now', '+3 hours', ? || ' days')
          {"AND f.id = :ff_id" if ff_id else ""}
          {"AND p.id = :product_id" if product_id else ""}
        GROUP BY p.id, f.id
    """
    sql_window = sql_window.replace("?", ":window")
    params = {"window": -window_days}
    if ff_id:
        params["ff_id"] = ff_id
    if product_id:
        params["product_id"] = product_id
    sold_rows = {(r["product_id"], r["ff_id"]): r for r in conn.execute(sql_window, params).fetchall()}

    # текущий остаток по товару и ФФ (без ограничения по времени — "сейчас")
    stock_sql = f"""
        SELECT p.id AS product_id, p.sku, p.name AS product_name,
               f.id AS ff_id, f.name AS ff_name,
               COALESCE(SUM(m.delta), 0) AS stock
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        WHERE 1=1
          {"AND f.id = :ff_id" if ff_id else ""}
          {"AND p.id = :product_id" if product_id else ""}
        GROUP BY p.id, f.id
    """
    stock_params = {}
    if ff_id:
        stock_params["ff_id"] = ff_id
    if product_id:
        stock_params["product_id"] = product_id
    stock_rows = conn.execute(stock_sql, stock_params).fetchall()

    result = []
    for r in stock_rows:
        key = (r["product_id"], r["ff_id"])
        sold_row = sold_rows.get(key)
        sale_qty = sold_row["sale_qty"] if sold_row else 0
        reversal_qty = sold_row["reversal_qty"] if sold_row else 0
        net_sold = sale_qty - reversal_qty
        daily_rate = net_sold / window_days if window_days else 0
        stock = r["stock"]
        if daily_rate > 0:
            days_left = stock / daily_rate
        else:
            days_left = None
        result.append({
            "product_id": r["product_id"], "sku": r["sku"], "product_name": r["product_name"],
            "ff_id": r["ff_id"], "ff_name": r["ff_name"] or "Без ФФ",
            "stock": stock, "net_sold": net_sold, "daily_rate": daily_rate, "days_left": days_left,
        })
    result.sort(key=lambda x: (x["ff_name"], x["product_name"]))
    return result


def get_product_ranking(
    conn: sqlite3.Connection, date_from: str, date_to: str, limit: int = 10,
) -> dict:
    """Топ и антитоп товаров по продажам (нетто) за период — для раздела
    'Обзор'. Возвращает {"top": [...], "slow": [...]}."""
    stats = get_period_stats(conn, date_from, date_to)
    by_product: dict = {}
    for row in stats:
        pid = row["product_id"]
        if pid not in by_product:
            by_product[pid] = {"product_id": pid, "sku": row["sku"], "name": row["product_name"], "net_sold": 0}
        by_product[pid]["net_sold"] += row["net_sold"]
    items = sorted(by_product.values(), key=lambda x: x["net_sold"], reverse=True)
    return {"top": items[:limit], "slow": list(reversed(items[-limit:])) if items else []}


def get_ff_comparison(conn: sqlite3.Connection, date_from: str, date_to: str) -> list[dict]:
    """Сравнение ФФ по скорости продаж (нетто) за период — сумма по всем товарам."""
    stats = get_period_stats(conn, date_from, date_to)
    by_ff: dict = {}
    for row in stats:
        key = row["ff_id"]
        if key not in by_ff:
            by_ff[key] = {"ff_id": key, "ff_name": row["ff_name"] or "Без ФФ", "net_sold": 0, "sale_qty": 0, "reversal_qty": 0}
        by_ff[key]["net_sold"] += row["net_sold"]
        by_ff[key]["sale_qty"] += row["sale_qty"]
        by_ff[key]["reversal_qty"] += row["reversal_qty"]
    result = list(by_ff.values())
    for r in result:
        r["cancel_rate"] = (r["reversal_qty"] / r["sale_qty"]) if r["sale_qty"] else None
    result.sort(key=lambda x: x["net_sold"], reverse=True)
    return result


def get_cancellations_table(
    conn: sqlite3.Connection, date_from: str, date_to: str,
    ff_id=None, product_id=None,
) -> list[dict]:
    """Отменённые заказы за период — по товару и конкретному складу (не ФФ
    целиком), чтобы было видно, на каком складе и какого артикула больше
    всего отмен. movement_type = 'sale_reversal', delta там уже положительный
    (возврат остатка), поэтому сумма delta и есть отменённое количество."""
    lo, hi = _date_bounds(date_from, date_to)
    sql = f"""
        SELECT p.id AS product_id, p.sku, p.name AS product_name,
               w.id AS warehouse_id, w.name AS warehouse_name,
               f.id AS ff_id, f.name AS ff_name,
               SUM(m.delta) AS cancelled_qty,
               COUNT(*) AS cancellations_count
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        WHERE m.movement_type = 'sale_reversal'
          AND m.created_at BETWEEN {lo} AND {hi}
          {"AND f.id = :ff_id" if ff_id else ""}
          {"AND p.id = :product_id" if product_id else ""}
        GROUP BY p.id, w.id
        ORDER BY cancelled_qty DESC
    """
    params = {}
    if ff_id:
        params["ff_id"] = ff_id
    if product_id:
        params["product_id"] = product_id
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def get_movements_journal(
    conn: sqlite3.Connection, date_from: str = None, date_to: str = None,
    ff_id=None, product_id=None, limit: int = 300,
) -> list[sqlite3.Row]:
    """Полная лента движений (плюс/минус) с фильтрами — для раздела
    'Журнал'. В отличие от страницы «Движения» (там правят/вносят записи),
    здесь только просмотр, зато с фильтрами по датам/ФФ/товару."""
    where = ["1=1"]
    params = {}
    if date_from and date_to:
        lo, hi = _date_bounds(date_from, date_to)
        where.append(f"m.created_at BETWEEN {lo} AND {hi}")
    if ff_id:
        where.append("f.id = :ff_id")
        params["ff_id"] = ff_id
    if product_id:
        where.append("p.id = :product_id")
        params["product_id"] = product_id
    params["limit"] = limit

    sql = f"""
        SELECT m.*, p.name AS product_name, p.sku AS product_sku,
               w.name AS warehouse_name, f.id AS ff_id, f.name AS ff_name,
               u.username AS created_by_username
        FROM stock_movements m
        JOIN products p ON p.id = m.product_id
        JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        LEFT JOIN users u ON u.id = m.created_by_id
        WHERE {' AND '.join(where)}
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT :limit
    """
    return conn.execute(sql, params).fetchall()


def get_filter_options(conn: sqlite3.Connection) -> dict:
    """Списки для выпадающих фильтров (ФФ и товары)."""
    ff_list = conn.execute("SELECT * FROM fulfillment_centers ORDER BY name").fetchall()
    products = conn.execute("SELECT * FROM products ORDER BY name").fetchall()
    return {"ff_list": ff_list, "products": products}
