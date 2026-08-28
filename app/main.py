"""Точка входа приложения: Flask-роуты, авторизация, фоновая синхронизация с WB."""
import csv
import datetime as dt
import io
import sqlite3
import threading
import time

from flask import Flask, request, session, redirect, url_for, render_template, g, Response

from app.config import SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, SYNC_INTERVAL_MINUTES
from app.database import get_conn, init_db, now_iso
from app.models import MovementType, MovementSource
from app.auth import hash_password, verify_password, get_current_user, login_required, can_edit, is_admin
from app.sync import (
    sync_once, get_stock_table, get_stock_by_ff, get_product_totals, get_current_stock,
    get_stock_locations, CANCEL_STATUSES, WB_STATUS_CANCEL_VALUES, reconcile_all_orders,
    undo_history_backfill,
)
from app.wb_client import WBClient, WBApiError
from app.analytics import (
    get_period_stats, get_daily_series, get_velocity_table, get_product_ranking,
    get_ff_comparison, get_movements_journal, get_filter_options, get_cancellations_table,
)

app = Flask(__name__)
app.secret_key = SECRET_KEY


MSK_OFFSET = dt.timedelta(hours=3)  # Москва — фиксированный сдвиг от UTC, без перехода на летнее время


def dtfmt(value, fmt="%d.%m.%Y %H:%M"):
    """В базе все даты хранятся в UTC (см. now_iso в database.py). Для показа
    пользователю переводим в московское время — иначе даты и группировка
    «по дням» на странице «Аналитика» будут расходиться с тем, что человек
    физически видит на часах."""
    if not value:
        return "—"
    try:
        return (dt.datetime.fromisoformat(value) + MSK_OFFSET).strftime(fmt)
    except (ValueError, TypeError):
        return value


app.jinja_env.filters["dtfmt"] = dtfmt


@app.before_request
def load_db():
    g.db = get_conn()


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.context_processor
def inject_user():
    return {"user": get_current_user()}


# --------------------------------------------------------------------- auth
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        return render_template("login.html", error=None)
    username = request.form["username"]
    password = request.form["password"]
    user = g.db.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not user or not verify_password(password, user["password_hash"]):
        return render_template("login.html", error="Неверный логин или пароль")
    session["user_id"] = user["id"]
    return redirect(url_for("dashboard"))


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------- dashboard
@app.route("/")
@login_required
def dashboard():
    ff_groups = get_stock_by_ff(g.db)
    product_totals = get_product_totals(g.db)
    grand_total = sum(t["quantity"] for t in product_totals)
    last_run = g.db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return render_template(
        "dashboard.html", ff_groups=ff_groups, product_totals=product_totals,
        grand_total=grand_total, last_run=last_run,
    )


@app.route("/sync/run-now", methods=["POST"])
@login_required
def sync_run_now():
    result = sync_once(WBClient())
    if result["status"] == "error":
        return redirect(url_for("dashboard", error=f"Ошибка синхронизации: {result['message']}"))
    return redirect(url_for("dashboard", ok="Синхронизация выполнена"))


# ---------------------------------------------------------------- аналитика
def _analytics_params():
    """Общие параметры фильтра для /analytics и /analytics/export.csv —
    по умолчанию последние 30 дней (по МСК) и окно скорости продаж 14 дней."""
    today = (dt.datetime.utcnow() + MSK_OFFSET).date()
    default_from = (today - dt.timedelta(days=30)).isoformat()
    date_from = request.args.get("date_from") or default_from
    date_to = request.args.get("date_to") or today.isoformat()
    ff_id = request.args.get("ff_id") or None
    product_id = request.args.get("product_id") or None
    ff_id = int(ff_id) if ff_id else None
    product_id = int(product_id) if product_id else None
    try:
        velocity_window = int(request.args.get("velocity_window") or 14)
    except ValueError:
        velocity_window = 14
    return date_from, date_to, ff_id, product_id, velocity_window


@app.route("/analytics")
@login_required
def analytics_page():
    date_from, date_to, ff_id, product_id, velocity_window = _analytics_params()
    period_stats = get_period_stats(g.db, date_from, date_to, ff_id, product_id)
    daily_series = get_daily_series(g.db, date_from, date_to, ff_id, product_id)
    velocity = get_velocity_table(g.db, velocity_window, ff_id, product_id)
    ranking = get_product_ranking(g.db, date_from, date_to)
    ff_comparison = get_ff_comparison(g.db, date_from, date_to)
    cancellations = get_cancellations_table(g.db, date_from, date_to, ff_id, product_id)
    journal = get_movements_journal(g.db, date_from, date_to, ff_id, product_id)
    filters = get_filter_options(g.db)

    max_daily = max([abs(d["net_sold"]) for d in daily_series], default=0) or 1
    period_totals = {
        "income": sum(r["income_qty"] for r in period_stats),
        "sale": sum(r["sale_qty"] for r in period_stats),
        "reversal": sum(r["reversal_qty"] for r in period_stats),
        "writeoff": sum(r["writeoff_qty"] for r in period_stats),
        "net_sold": sum(r["net_sold"] for r in period_stats),
    }
    cancellations_total = sum(r["cancelled_qty"] for r in cancellations)

    return render_template(
        "analytics.html",
        date_from=date_from, date_to=date_to, ff_id=ff_id, product_id=product_id,
        velocity_window=velocity_window,
        period_stats=period_stats, period_totals=period_totals,
        daily_series=daily_series, max_daily=max_daily,
        velocity=velocity, ranking=ranking, ff_comparison=ff_comparison,
        cancellations=cancellations, cancellations_total=cancellations_total,
        journal=journal, ff_list=filters["ff_list"], products=filters["products"],
    )


@app.route("/analytics/export.csv")
@login_required
def analytics_export_csv():
    date_from, date_to, ff_id, product_id, _ = _analytics_params()
    rows = get_movements_journal(g.db, date_from, date_to, ff_id, product_id, limit=100000)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Дата", "Товар", "Артикул", "Склад", "ФФ", "Тип", "Кол-во", "Источник", "Кто внёс", "Комментарий"])
    for m in rows:
        writer.writerow([
            dtfmt(m["created_at"]), m["product_name"] or "", m["product_sku"] or "",
            m["warehouse_name"] or "", m["ff_name"] or "", m["movement_type"], m["delta"],
            "WB" if m["source"] == "wb_sync" else "вручную",
            m["created_by_username"] or "", m["comment"] or "",
        ])
    output = buf.getvalue()
    return Response(
        "﻿" + output, mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=analytics_{date_from}_{date_to}.csv"},
    )


# ---------------------------------------------------------------- movements
@app.route("/movements")
@login_required
def movements_page():
    # Только ручные операции (приход/списание/перемещение) — заказы и отмены
    # WB здесь больше не показываются, чтобы не путать ручные операции с
    # автоматическими; для них есть отдельный раздел «Заказы WB».
    movements = g.db.execute(
        """
        SELECT m.*, p.name AS product_name, w.name AS warehouse_name,
               f.id AS ff_id, f.name AS ff_name, u.username AS created_by_username
        FROM stock_movements m
        LEFT JOIN products p ON p.id = m.product_id
        LEFT JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        LEFT JOIN users u ON u.id = m.created_by_id
        WHERE m.source = 'manual'
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT 300
        """
    ).fetchall()
    products = g.db.execute("SELECT * FROM products ORDER BY name").fetchall()
    locations = get_stock_locations(g.db)
    user = get_current_user()
    return render_template(
        "movements.html", movements=movements, products=products,
        locations=locations, can_edit=can_edit(user),
    )


@app.route("/wb-orders-log")
@login_required
def wb_orders_log_page():
    """Отдельный раздел: только события по заказам WB (списание при новом
    заказе, возврат при отмене) — какой склад/ФФ и когда. Живёт отдельно от
    «Движений», чтобы не путать автоматические события WB с ручными
    операциями (приход/перемещение/списание)."""
    events = g.db.execute(
        """
        SELECT m.*, p.name AS product_name, p.sku AS product_sku,
               w.name AS warehouse_name, f.id AS ff_id, f.name AS ff_name
        FROM stock_movements m
        LEFT JOIN products p ON p.id = m.product_id
        LEFT JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        WHERE m.source = 'wb_sync'
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT 300
        """
    ).fetchall()
    return render_template("wb_orders_log.html", events=events)


TERMINAL_STATUSES = ("complete", "cancel", "canceled", "cancelled", "declined", "reject")


@app.route("/wb-diagnostics")
@login_required
def wb_diagnostics_page():
    """Технический раздел только для чтения: проверяет за один проход сразу
    несколько версий того, почему реальные отмены WB (см. кабинет WB
    Partners) не появляются в нашей системе, вместо того чтобы гонять
    гипотезы по одной через скриншоты. Ничего не пишет ни в нашу базу, ни
    тем более обратно в WB.

    Доступен только администратору — это отладочный инструмент, а не
    рабочая страница склада."""
    user = get_current_user()
    if not is_admin(user):
        return redirect(url_for("dashboard", error="Недостаточно прав"))

    placeholders = ",".join("?" for _ in TERMINAL_STATUSES)

    # --- 1) Разбивка всех заказов в НАШЕЙ базе по статусам, как они сейчас
    # хранятся — если WB реально использует другую строку для отмены, чем
    # зашито в CANCEL_STATUSES (sync.py), она будет видна здесь как
    # "нормальный", незавершённый статус, который никогда не считается отменой.
    status_counts = g.db.execute(
        "SELECT status, COUNT(*) AS c FROM wb_orders GROUP BY status ORDER BY c DESC"
    ).fetchall()

    tracked = g.db.execute(
        f"SELECT * FROM wb_orders WHERE status NOT IN ({placeholders})",
        TERMINAL_STATUSES,
    ).fetchall()

    # --- 2) Заказы, чей ID не является чисто числовым — sync.py фильтрует их
    # через str(...).isdigit() и НИКОГДА не отправляет на проверку статуса в
    # WB, то есть их отмена физически не может быть замечена.
    non_digit_orders = [o for o in tracked if not str(o["wb_order_id"]).isdigit()]

    # --- 3) Отслеживаемые (ещё не завершённые, по нашим данным) заказы, у
    # которых нет привязанного склада и/или списание не проводилось — даже
    # если WB пришлёт по ним статус "отмена", код sync.py молча ничего не
    # сделает, потому что условие `stock_deducted and warehouse_id` не
    # выполнится.
    gating_issues = [
        o for o in tracked if not o["warehouse_id"] or not o["stock_deducted"]
    ]

    # --- 4) Точечная проверка конкретных ID заказов — берём ID прямо из
    # кабинета WB Partners (например, из вкладки «Отменённые») и смотрим
    # СЫРОЙ ответ WB API по каждому, целиком, а не только supplierStatus —
    # чтобы увидеть, нет ли там отдельного поля вроде wbStatus, которое мы
    # сейчас нигде не читаем.
    order_ids_raw = request.args.get("order_ids", "").strip()
    probe_results = []
    probe_error = None
    if order_ids_raw:
        requested_ids = [x.strip() for x in order_ids_raw.replace(",", " ").split() if x.strip()]
        numeric_ids = [int(x) for x in requested_ids if x.isdigit()]
        wb_by_id = {}
        try:
            if numeric_ids:
                wb_statuses = WBClient().get_orders_status(numeric_ids)
                wb_by_id = {str(s.get("id")): s for s in wb_statuses}
        except WBApiError as e:
            probe_error = str(e)

        for order_id in requested_ids:
            local = g.db.execute(
                "SELECT * FROM wb_orders WHERE wb_order_id = ?", (order_id,)
            ).fetchone()
            raw = wb_by_id.get(order_id)
            raw_items = None
            if raw is not None:
                raw_items = [
                    {"key": k, "value": v, "looks_like_status": "status" in k.lower()}
                    for k, v in raw.items()
                ]
            probe_results.append({
                "order_id": order_id,
                "found_in_wb_response": raw is not None,
                "in_our_db": local is not None,
                "local_status": local["status"] if local else None,
                "local_warehouse_id": local["warehouse_id"] if local else None,
                "local_stock_deducted": local["stock_deducted"] if local else None,
                "raw_items": raw_items,
            })

    return render_template(
        "wb_diagnostics.html",
        status_counts=status_counts,
        non_digit_orders=non_digit_orders,
        gating_issues=gating_issues,
        order_ids_raw=order_ids_raw,
        probe_results=probe_results,
        probe_error=probe_error,
        cancel_statuses=sorted(CANCEL_STATUSES),
        wb_status_cancel_values=sorted(WB_STATUS_CANCEL_VALUES),
    )


@app.route("/wb-diagnostics/reconcile", methods=["POST"])
@login_required
def wb_diagnostics_reconcile():
    """Разовая (можно запускать и повторно) сверка ВСЕХ заказов с актуальным
    статусом WB — включая уже 'complete' — чтобы вернуть остаток по заказам,
    отменённым клиентом, но незамеченным из-за того, что supplierStatus у
    них не менялся (см. диагностику 27.08.2026 и sync.reconcile_all_orders).
    В отличие от обычной синхронизации, эта операция меняет остатки задним
    числом, поэтому требует явного текстового подтверждения — как сброс
    остатков на странице «Пользователи»."""
    user = get_current_user()
    if not is_admin(user):
        return redirect(url_for("dashboard", error="Недостаточно прав"))
    confirm_text = request.form.get("confirm_text", "").strip()
    if confirm_text != "ПЕРЕСЧИТАТЬ":
        return redirect(url_for(
            "wb_diagnostics_page",
            error="Для подтверждения нужно ввести слово ПЕРЕСЧИТАТЬ (заглавными буквами) — ничего не изменено",
        ))
    try:
        report = reconcile_all_orders(WBClient())
    except Exception as e:
        return redirect(url_for("wb_diagnostics_page", error=f"Не удалось выполнить пересчёт: {e}"))

    fixed_detailed = []
    for item in report["fixed"]:
        product = g.db.execute(
            "SELECT name, sku FROM products WHERE id = ?", (item["product_id"],)
        ).fetchone()
        warehouse = g.db.execute(
            "SELECT name FROM warehouses WHERE id = ?", (item["warehouse_id"],)
        ).fetchone()
        fixed_detailed.append({
            **item,
            "product_name": product["name"] if product else "—",
            "product_sku": product["sku"] if product else "—",
            "warehouse_name": warehouse["name"] if warehouse else "—",
        })

    return render_template(
        "wb_reconcile_result.html",
        checked=report["checked"],
        skipped_non_digit=report["skipped_non_digit"],
        errors=report["errors"],
        fixed=fixed_detailed,
    )


@app.route("/wb-diagnostics/backfill-history", methods=["POST"])
@login_required
def wb_diagnostics_backfill_history():
    """ОТКЛЮЧЕНО 27.08.2026: эта функция ошибочно предполагала, что общий
    метод WB (`/api/v3/orders`) отдаёт только заказы из «рабочего» периода —
    на деле он отдаёт ВСЮ историю заказов магазина за всё время (годы), и
    код списывал остаток по каждому найденному активному заказу как по
    только что случившейся продаже — это увело остаток по нескольким
    товарам в глубокий минус. Кнопка убрана со страницы диагностики; этот
    маршрут оставлен только чтобы старая (уже загруженная в браузере)
    страница с формой не могла случайно вызвать sync.backfill_order_history()
    повторно. См. sync.undo_history_backfill() для отмены уже нанесённого
    ущерба."""
    return redirect(url_for(
        "wb_diagnostics_page",
        error="Догрузка истории отключена — она ошибочно списывала остаток по многолетним старым "
              "заказам. Ничего не выполнено. Используйте «Отменить последствия догрузки истории» ниже.",
    ))


@app.route("/wb-diagnostics/undo-backfill", methods=["POST"])
@login_required
def wb_diagnostics_undo_backfill():
    """Аварийная отмена последствий отключённой выше догрузки истории —
    см. sync.undo_history_backfill(). Удаляет ровно то, что создал сбойный
    запуск (движения с характерной пометкой в комментарии и связанные с
    ними строки wb_orders), возвращая остаток к состоянию до бага."""
    user = get_current_user()
    if not is_admin(user):
        return redirect(url_for("dashboard", error="Недостаточно прав"))
    confirm_text = request.form.get("confirm_text", "").strip()
    if confirm_text != "ОТМЕНИТЬ":
        return redirect(url_for(
            "wb_diagnostics_page",
            error="Для подтверждения нужно ввести слово ОТМЕНИТЬ (заглавными буквами) — ничего не изменено",
        ))
    try:
        report = undo_history_backfill()
    except Exception as e:
        return redirect(url_for("wb_diagnostics_page", error=f"Не удалось отменить: {e}"))

    return render_template("wb_undo_backfill_result.html", **report)


def _resolve_location(conn, location_key: str):
    """'ff:3' / 'wh:5' -> id канонического склада для записи движения.
    Один физический ФФ — одно место хранения, поэтому в формах выбирается
    ФФ целиком, а не конкретный виртуальный склад WB внутри него."""
    for loc in get_stock_locations(conn):
        if loc["key"] == location_key:
            return loc["warehouse_id"]
    return None


@app.route("/movements/income", methods=["POST"])
@login_required
def movement_income():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    product_id = int(request.form["product_id"])
    warehouse_id = _resolve_location(g.db, request.form["location"])
    if warehouse_id is None:
        return redirect(url_for("movements_page", error="Выбранное место хранения не найдено"))
    quantity = int(request.form["quantity"])
    comment = request.form.get("comment") or None
    g.db.execute(
        """INSERT INTO stock_movements
           (product_id, warehouse_id, movement_type, delta, source, comment, created_by_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, warehouse_id, MovementType.INCOME, quantity, MovementSource.MANUAL,
         comment, user["id"], now_iso()),
    )
    g.db.commit()
    return redirect(url_for("movements_page", ok="Приход добавлен"))


@app.route("/movements/writeoff", methods=["POST"])
@login_required
def movement_writeoff():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    product_id = int(request.form["product_id"])
    warehouse_id = _resolve_location(g.db, request.form["location"])
    if warehouse_id is None:
        return redirect(url_for("movements_page", error="Выбранное место хранения не найдено"))
    quantity = abs(int(request.form["quantity"]))
    comment = request.form["comment"]
    g.db.execute(
        """INSERT INTO stock_movements
           (product_id, warehouse_id, movement_type, delta, source, comment, created_by_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, warehouse_id, MovementType.WRITEOFF, -quantity, MovementSource.MANUAL,
         comment, user["id"], now_iso()),
    )
    g.db.commit()
    return redirect(url_for("movements_page", ok="Списание добавлено"))


@app.route("/movements/transfer", methods=["POST"])
@login_required
def movement_transfer():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    product_id = int(request.form["product_id"])
    from_location = request.form["from_location"]
    to_location = request.form["to_location"]
    quantity = abs(int(request.form["quantity"]))
    comment = request.form.get("comment") or None

    if from_location == to_location:
        return redirect(url_for("movements_page", error="Место отправления и назначения совпадают"))

    from_warehouse_id = _resolve_location(g.db, from_location)
    to_warehouse_id = _resolve_location(g.db, to_location)
    if from_warehouse_id is None or to_warehouse_id is None:
        return redirect(url_for("movements_page", error="Выбранное место хранения не найдено"))

    out_cur = g.db.execute(
        """INSERT INTO stock_movements
           (product_id, warehouse_id, movement_type, delta, source, comment, created_by_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, from_warehouse_id, MovementType.TRANSFER_OUT, -quantity, MovementSource.MANUAL,
         comment, user["id"], now_iso()),
    )
    out_id = out_cur.lastrowid
    in_cur = g.db.execute(
        """INSERT INTO stock_movements
           (product_id, warehouse_id, movement_type, delta, source, comment, created_by_id,
            related_movement_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (product_id, to_warehouse_id, MovementType.TRANSFER_IN, quantity, MovementSource.MANUAL,
         comment, user["id"], out_id, now_iso()),
    )
    in_id = in_cur.lastrowid
    g.db.execute("UPDATE stock_movements SET related_movement_id = ? WHERE id = ?", (in_id, out_id))
    g.db.commit()
    return redirect(url_for("movements_page", ok="Перемещение выполнено"))


@app.route("/movements/<int:movement_id>/edit", methods=["GET"])
@login_required
def movement_edit_form(movement_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    m = g.db.execute("SELECT * FROM stock_movements WHERE id = ?", (movement_id,)).fetchone()
    if not m:
        return redirect(url_for("movements_page", error="Движение не найдено"))
    if m["source"] != MovementSource.MANUAL:
        return redirect(url_for(
            "movements_page",
            error="Движения, созданные синхронизацией с WB, менять нельзя — они привязаны к заказу.",
        ))
    if m["movement_type"] in (MovementType.TRANSFER_OUT, MovementType.TRANSFER_IN):
        return redirect(url_for(
            "movements_page",
            error="Перемещение нельзя редактировать напрямую — удалите его (обе стороны удалятся "
                  "вместе) и внесите заново с нужным количеством.",
        ))
    products = g.db.execute("SELECT * FROM products ORDER BY name").fetchall()
    warehouses = g.db.execute("SELECT * FROM warehouses WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template("movement_edit.html", m=m, products=products, warehouses=warehouses)


@app.route("/movements/<int:movement_id>/edit", methods=["POST"])
@login_required
def movement_edit(movement_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    m = g.db.execute("SELECT * FROM stock_movements WHERE id = ?", (movement_id,)).fetchone()
    if not m:
        return redirect(url_for("movements_page", error="Движение не найдено"))
    if m["source"] != MovementSource.MANUAL or m["movement_type"] in (
        MovementType.TRANSFER_OUT, MovementType.TRANSFER_IN,
    ):
        return redirect(url_for("movements_page", error="Это движение менять нельзя"))

    product_id = int(request.form["product_id"])
    warehouse_id = int(request.form["warehouse_id"])
    quantity = abs(int(request.form["quantity"]))
    comment = request.form.get("comment") or None
    # Направление (приход/списание) сохраняем таким же, каким было — меняем
    # только количество, товар и склад, а не тип движения.
    sign = 1 if m["delta"] >= 0 else -1
    delta = sign * quantity

    try:
        g.db.execute(
            "UPDATE stock_movements SET product_id = ?, warehouse_id = ?, delta = ?, comment = ? "
            "WHERE id = ?",
            (product_id, warehouse_id, delta, comment, movement_id),
        )
        g.db.commit()
    except sqlite3.IntegrityError as e:
        return redirect(url_for(
            "movement_edit_form", movement_id=movement_id,
            error=f"Не удалось сохранить: {e}",
        ))
    return redirect(url_for("movements_page", ok="Движение изменено"))


@app.route("/movements/<int:movement_id>/delete", methods=["POST"])
@login_required
def movement_delete(movement_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    m = g.db.execute("SELECT * FROM stock_movements WHERE id = ?", (movement_id,)).fetchone()
    if not m:
        return redirect(url_for("movements_page", error="Движение не найдено"))
    if m["source"] != MovementSource.MANUAL:
        return redirect(url_for(
            "movements_page",
            error="Движения, созданные синхронизацией с WB, удалять нельзя — они привязаны к заказу.",
        ))

    if m["movement_type"] in (MovementType.TRANSFER_OUT, MovementType.TRANSFER_IN):
        # Перемещение — это две связанные строки (списание с одного склада и
        # приход на другой). Удаляем всегда обе вместе, чтобы не оставить
        # "половинку" перемещения, которая испортит остаток на одном складе.
        related_id = m["related_movement_id"]
        g.db.execute("DELETE FROM stock_movements WHERE id = ?", (movement_id,))
        if related_id:
            g.db.execute("DELETE FROM stock_movements WHERE id = ?", (related_id,))
    else:
        g.db.execute("DELETE FROM stock_movements WHERE id = ?", (movement_id,))
    g.db.commit()
    return redirect(url_for("movements_page", ok="Движение удалено"))


# ----------------------------------------------------------------- products
@app.route("/products")
@login_required
def products_page():
    products = g.db.execute("SELECT * FROM products ORDER BY name").fetchall()
    aliases = g.db.execute(
        """
        SELECT a.*, p.name AS target_name, p.sku AS target_sku
        FROM product_aliases a
        JOIN products p ON p.id = a.target_product_id
        ORDER BY a.created_at DESC
        """
    ).fetchall()
    return render_template(
        "products.html", products=products, aliases=aliases,
        can_edit=can_edit(get_current_user()), is_admin=is_admin(get_current_user()),
    )


@app.route("/products/new", methods=["POST"])
@login_required
def product_new():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("products_page", error="Недостаточно прав"))
    sku = request.form["sku"].strip()
    name = request.form["name"].strip()
    nm_id = request.form.get("nm_id", "").strip()
    barcode = request.form.get("barcode", "").strip()
    try:
        g.db.execute(
            "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES (?, ?, ?, ?, ?)",
            (sku, int(nm_id) if nm_id else None, barcode or None, name, now_iso()),
        )
        g.db.commit()
    except Exception as e:
        return redirect(url_for("products_page", error=f"Не удалось добавить товар: {e}"))
    return redirect(url_for("products_page", ok="Товар добавлен"))


@app.route("/products/<int:product_id>/edit", methods=["GET"])
@login_required
def product_edit_form(product_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("products_page", error="Недостаточно прав"))
    product = g.db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        return redirect(url_for("products_page", error="Товар не найден"))
    return render_template("product_edit.html", product=product)


@app.route("/products/<int:product_id>/edit", methods=["POST"])
@login_required
def product_edit(product_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("products_page", error="Недостаточно прав"))
    sku = request.form["sku"].strip()
    name = request.form["name"].strip()
    nm_id = request.form.get("nm_id", "").strip()
    barcode = request.form.get("barcode", "").strip()
    try:
        g.db.execute(
            "UPDATE products SET sku = ?, name = ?, nm_id = ?, barcode = ? WHERE id = ?",
            (sku, name, int(nm_id) if nm_id else None, barcode or None, product_id),
        )
        g.db.commit()
    except sqlite3.IntegrityError:
        return redirect(url_for(
            "product_edit_form", product_id=product_id,
            error="Такой SKU, nmId или штрихкод уже используется другим товаром",
        ))
    return redirect(url_for("products_page", ok="Товар изменён"))


@app.route("/products/<int:product_id>/delete", methods=["POST"])
@login_required
def product_delete(product_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("products_page", error="Недостаточно прав"))
    try:
        g.db.execute("DELETE FROM products WHERE id = ?", (product_id,))
        g.db.commit()
    except sqlite3.IntegrityError:
        return redirect(url_for(
            "products_page",
            error="Нельзя удалить: по этому товару уже есть движения или заказы. "
                  "Если он больше не нужен — просто переименуйте его через «Изменить».",
        ))
    return redirect(url_for("products_page", ok="Товар удалён"))


# --------------------------------------------------------- алиасы товаров
# 28.08.2026: карточка WB (barcode/nmId), которая физически — тот же товар,
# что и другой, уже заведённый у нас продукт. После добавления алиаса её
# будущие продажи по WB списываются сразу с остатка целевого товара, у самой
# карточки-алиаса отдельный остаток больше не ведётся. Ничего из прошлой
# истории движений не трогает — действует только на новые заказы вперёд.
# Меняет то, как считается остаток при синхронизации — поэтому только админ.
@app.route("/products/aliases/new", methods=["POST"])
@login_required
def product_alias_new():
    user = get_current_user()
    if not is_admin(user):
        return redirect(url_for("products_page", error="Недостаточно прав"))
    alias_barcode = request.form.get("alias_barcode", "").strip()
    alias_nm_id = request.form.get("alias_nm_id", "").strip()
    target_product_id = request.form.get("target_product_id", "").strip()
    comment = request.form.get("comment", "").strip()
    if not alias_barcode and not alias_nm_id:
        return redirect(url_for("products_page", error="Укажите штрихкод и/или nmId алиаса"))
    if not target_product_id:
        return redirect(url_for("products_page", error="Выберите целевой товар"))
    try:
        g.db.execute(
            "INSERT INTO product_aliases (alias_barcode, alias_nm_id, target_product_id, comment, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                alias_barcode or None, int(alias_nm_id) if alias_nm_id else None,
                int(target_product_id), comment or None, now_iso(),
            ),
        )
        g.db.commit()
    except sqlite3.IntegrityError:
        return redirect(url_for(
            "products_page",
            error="Такой штрихкод или nmId уже используется другим алиасом (или самим товаром)",
        ))
    return redirect(url_for("products_page", ok="Алиас добавлен"))


@app.route("/products/aliases/<int:alias_id>/delete", methods=["POST"])
@login_required
def product_alias_delete(alias_id):
    user = get_current_user()
    if not is_admin(user):
        return redirect(url_for("products_page", error="Недостаточно прав"))
    g.db.execute("DELETE FROM product_aliases WHERE id = ?", (alias_id,))
    g.db.commit()
    return redirect(url_for("products_page", ok="Алиас удалён"))


# --------------------------------------------------------------- warehouses
@app.route("/warehouses")
@login_required
def warehouses_page():
    warehouses = g.db.execute(
        """
        SELECT w.*, f.id AS ff_id, f.name AS ff_name
        FROM warehouses w
        LEFT JOIN fulfillment_centers f ON f.id = w.fulfillment_center_id
        ORDER BY w.name
        """
    ).fetchall()
    ff_list = g.db.execute("SELECT * FROM fulfillment_centers WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template(
        "warehouses.html", warehouses=warehouses, ff_list=ff_list, can_edit=can_edit(get_current_user()),
    )


@app.route("/warehouses/new", methods=["POST"])
@login_required
def warehouse_new():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("warehouses_page", error="Недостаточно прав"))
    name = request.form["name"].strip()
    wb_warehouse_id = request.form.get("wb_warehouse_id", "").strip()
    fulfillment_center_id = request.form.get("fulfillment_center_id", "").strip()
    try:
        g.db.execute(
            "INSERT INTO warehouses (name, wb_warehouse_id, fulfillment_center_id, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (
                name, int(wb_warehouse_id) if wb_warehouse_id else None,
                int(fulfillment_center_id) if fulfillment_center_id else None, now_iso(),
            ),
        )
        g.db.commit()
    except Exception as e:
        return redirect(url_for("warehouses_page", error=f"Не удалось добавить склад: {e}"))
    return redirect(url_for("warehouses_page", ok="Склад добавлен"))


@app.route("/warehouses/<int:warehouse_id>/edit", methods=["GET"])
@login_required
def warehouse_edit_form(warehouse_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("warehouses_page", error="Недостаточно прав"))
    warehouse = g.db.execute("SELECT * FROM warehouses WHERE id = ?", (warehouse_id,)).fetchone()
    if not warehouse:
        return redirect(url_for("warehouses_page", error="Склад не найден"))
    ff_list = g.db.execute("SELECT * FROM fulfillment_centers WHERE is_active = 1 ORDER BY name").fetchall()
    return render_template("warehouse_edit.html", warehouse=warehouse, ff_list=ff_list)


@app.route("/warehouses/<int:warehouse_id>/edit", methods=["POST"])
@login_required
def warehouse_edit(warehouse_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("warehouses_page", error="Недостаточно прав"))
    name = request.form["name"].strip()
    wb_warehouse_id = request.form.get("wb_warehouse_id", "").strip()
    fulfillment_center_id = request.form.get("fulfillment_center_id", "").strip()
    is_active = 1 if request.form.get("is_active") == "1" else 0
    try:
        g.db.execute(
            "UPDATE warehouses SET name = ?, wb_warehouse_id = ?, fulfillment_center_id = ?, "
            "is_active = ? WHERE id = ?",
            (
                name, int(wb_warehouse_id) if wb_warehouse_id else None,
                int(fulfillment_center_id) if fulfillment_center_id else None, is_active, warehouse_id,
            ),
        )
        g.db.commit()
    except sqlite3.IntegrityError:
        return redirect(url_for(
            "warehouse_edit_form", warehouse_id=warehouse_id,
            error="Такой ID склада в WB уже используется другим складом",
        ))
    return redirect(url_for("warehouses_page", ok="Склад изменён"))


@app.route("/warehouses/<int:warehouse_id>/delete", methods=["POST"])
@login_required
def warehouse_delete(warehouse_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("warehouses_page", error="Недостаточно прав"))
    try:
        g.db.execute("DELETE FROM warehouses WHERE id = ?", (warehouse_id,))
        g.db.commit()
    except sqlite3.IntegrityError:
        return redirect(url_for(
            "warehouses_page",
            error="Нельзя удалить: по этому складу уже есть движения или заказы. "
                  "Если он больше не используется — отметьте его неактивным через «Изменить».",
        ))
    return redirect(url_for("warehouses_page", ok="Склад удалён"))


@app.route("/warehouses/import-from-wb", methods=["POST"])
@login_required
def warehouses_import():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("warehouses_page", error="Недостаточно прав"))
    try:
        wb_warehouses = WBClient().get_warehouses()
    except WBApiError as e:
        return redirect(url_for("warehouses_page", error=f"Не удалось получить склады из WB: {e}"))

    added = 0
    for w in wb_warehouses:
        wb_id = w.get("id")
        if not wb_id:
            continue
        exists = g.db.execute(
            "SELECT id FROM warehouses WHERE wb_warehouse_id = ?", (wb_id,)
        ).fetchone()
        if exists:
            continue
        g.db.execute(
            "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES (?, ?, 1, ?)",
            (w.get("name", f"Склад WB {wb_id}"), wb_id, now_iso()),
        )
        added += 1
    g.db.commit()
    return redirect(url_for("warehouses_page", ok=f"Импортировано складов: {added}"))


# ------------------------------------------------------ fulfillment centers
@app.route("/fulfillment-centers")
@login_required
def ff_page():
    ff_list = g.db.execute("SELECT * FROM fulfillment_centers ORDER BY name").fetchall()
    warehouses = g.db.execute("SELECT * FROM warehouses ORDER BY name").fetchall()
    return render_template(
        "fulfillment_centers.html", ff_list=ff_list, warehouses=warehouses,
        can_edit=can_edit(get_current_user()),
    )


@app.route("/fulfillment-centers/new", methods=["POST"])
@login_required
def ff_new():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("ff_page", error="Недостаточно прав"))
    name = request.form["name"].strip()
    try:
        g.db.execute(
            "INSERT INTO fulfillment_centers (name, is_active, created_at) VALUES (?, 1, ?)",
            (name, now_iso()),
        )
        g.db.commit()
    except Exception as e:
        return redirect(url_for("ff_page", error=f"Не удалось добавить ФФ: {e}"))
    return redirect(url_for("ff_page", ok="Фулфилмент-центр добавлен"))


@app.route("/fulfillment-centers/<int:ff_id>/edit", methods=["GET"])
@login_required
def ff_edit_form(ff_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("ff_page", error="Недостаточно прав"))
    ff = g.db.execute("SELECT * FROM fulfillment_centers WHERE id = ?", (ff_id,)).fetchone()
    if not ff:
        return redirect(url_for("ff_page", error="ФФ не найден"))
    warehouses = g.db.execute("SELECT * FROM warehouses ORDER BY name").fetchall()
    return render_template("ff_edit.html", ff=ff, warehouses=warehouses)


@app.route("/fulfillment-centers/<int:ff_id>/edit", methods=["POST"])
@login_required
def ff_edit(ff_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("ff_page", error="Недостаточно прав"))
    ff = g.db.execute("SELECT * FROM fulfillment_centers WHERE id = ?", (ff_id,)).fetchone()
    if not ff:
        return redirect(url_for("ff_page", error="ФФ не найден"))
    name = request.form["name"].strip()
    is_active = 1 if request.form.get("is_active") == "1" else 0
    g.db.execute(
        "UPDATE fulfillment_centers SET name = ?, is_active = ? WHERE id = ?",
        (name, is_active, ff_id),
    )
    # Склады, отмеченные галочкой в форме — привязываем к этому ФФ; те, что
    # раньше были привязаны именно к нему, но галочку сняли — отвязываем.
    # Склады, привязанные к ДРУГИМ ФФ и не отмеченные здесь, не трогаем.
    selected_ids = {int(x) for x in request.form.getlist("warehouse_ids")}
    all_warehouse_ids = [row["id"] for row in g.db.execute("SELECT id FROM warehouses").fetchall()]
    for wid in all_warehouse_ids:
        if wid in selected_ids:
            g.db.execute(
                "UPDATE warehouses SET fulfillment_center_id = ? WHERE id = ?", (ff_id, wid),
            )
        else:
            g.db.execute(
                "UPDATE warehouses SET fulfillment_center_id = NULL "
                "WHERE id = ? AND fulfillment_center_id = ?",
                (wid, ff_id),
            )
    g.db.commit()
    return redirect(url_for("ff_page", ok="Фулфилмент-центр изменён"))


@app.route("/fulfillment-centers/<int:ff_id>/delete", methods=["POST"])
@login_required
def ff_delete(ff_id):
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("ff_page", error="Недостаточно прав"))
    # Привязанные склады не удаляются — просто отвязываются от этого ФФ,
    # их остатки и история движений никуда не пропадают.
    g.db.execute("UPDATE warehouses SET fulfillment_center_id = NULL WHERE fulfillment_center_id = ?", (ff_id,))
    g.db.execute("DELETE FROM fulfillment_centers WHERE id = ?", (ff_id,))
    g.db.commit()
    return redirect(url_for("ff_page", ok="Фулфилмент-центр удалён"))


# ---------------------------------------------------------------------- ozon
# 28.08.2026: остатки на Ozon — отдельно от WB и пока полностью вручную, без
# подключения к Ozon API. Сознательно НЕ через stock_movements (это не
# журнал заказов/приходов WB, а просто текущее число по каждому товару,
# которое вводит человек) — чтобы не перепутать с WB-остатками и не задеть
# их логику синхронизации. История правок — в ozon_stock_log, только для
# прозрачности (кто и когда поменял), возврата назад через неё пока нет.
@app.route("/ozon")
@login_required
def ozon_page():
    rows = g.db.execute(
        """
        SELECT p.id AS product_id, p.sku, p.name,
               COALESCE(o.quantity, 0) AS quantity, o.updated_at
        FROM products p
        LEFT JOIN ozon_stock o ON o.product_id = p.id
        ORDER BY p.name
        """
    ).fetchall()
    total = sum(r["quantity"] for r in rows)
    return render_template(
        "ozon.html", rows=rows, total=total, can_edit=can_edit(get_current_user()),
    )


@app.route("/ozon/set", methods=["POST"])
@login_required
def ozon_set():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("ozon_page", error="Недостаточно прав"))
    product_id = request.form.get("product_id", "").strip()
    quantity_raw = request.form.get("quantity", "").strip()
    comment = request.form.get("comment", "").strip()
    if not product_id or quantity_raw == "":
        return redirect(url_for("ozon_page", error="Не указан товар или количество"))
    try:
        quantity = int(quantity_raw)
    except ValueError:
        return redirect(url_for("ozon_page", error="Количество должно быть целым числом"))
    if quantity < 0:
        return redirect(url_for("ozon_page", error="Количество не может быть отрицательным"))

    product_id = int(product_id)
    existing = g.db.execute("SELECT * FROM ozon_stock WHERE product_id = ?", (product_id,)).fetchone()
    old_quantity = existing["quantity"] if existing else 0

    if existing:
        g.db.execute(
            "UPDATE ozon_stock SET quantity = ?, updated_at = ?, updated_by_id = ? WHERE product_id = ?",
            (quantity, now_iso(), user["id"], product_id),
        )
    else:
        g.db.execute(
            "INSERT INTO ozon_stock (product_id, quantity, updated_at, updated_by_id) VALUES (?, ?, ?, ?)",
            (product_id, quantity, now_iso(), user["id"]),
        )
    g.db.execute(
        "INSERT INTO ozon_stock_log (product_id, old_quantity, new_quantity, comment, created_by_id, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (product_id, old_quantity, quantity, comment or None, user["id"], now_iso()),
    )
    g.db.commit()
    return redirect(url_for("ozon_page", ok="Остаток на Ozon обновлён"))


# -------------------------------------------------------------------- users
@app.route("/users")
@login_required
def users_page():
    user = get_current_user()
    if not is_admin(user):
        return redirect(url_for("dashboard", error="Недостаточно прав"))
    users = g.db.execute("SELECT * FROM users ORDER BY username").fetchall()
    return render_template("users.html", users=users)


@app.route("/users/new", methods=["POST"])
@login_required
def user_new():
    user = get_current_user()
    if not is_admin(user):
        return redirect(url_for("dashboard", error="Недостаточно прав"))
    username = request.form["username"].strip()
    password = request.form["password"]
    role = request.form.get("role", "manager")
    if g.db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
        return redirect(url_for("users_page", error="Такой логин уже существует"))
    g.db.execute(
        "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, ?, ?)",
        (username, hash_password(password), role, now_iso()),
    )
    g.db.commit()
    return redirect(url_for("users_page", ok="Пользователь добавлен"))


@app.route("/admin/reset-stock", methods=["POST"])
@login_required
def admin_reset_stock():
    """Полный сброс остатков: удаляет ВСЕ движения (и внесённые вручную, и
    созданные синхронизацией с WB) и всю историю заказов WB — чтобы начать
    учёт заново с чистого листа. Товары, склады, ФФ и пользователи не
    затрагиваются. Доступно только администратору, требует явного
    подтверждения — действие необратимо."""
    user = get_current_user()
    if not is_admin(user):
        return redirect(url_for("dashboard", error="Сбросить остатки может только администратор"))
    confirm_text = request.form.get("confirm_text", "").strip()
    if confirm_text != "СБРОСИТЬ":
        return redirect(url_for(
            "users_page",
            error="Для подтверждения нужно ввести слово СБРОСИТЬ (заглавными буквами) — ничего не удалено",
        ))
    g.db.execute("DELETE FROM stock_movements")
    g.db.execute("DELETE FROM wb_orders")
    g.db.commit()
    return redirect(url_for(
        "users_page",
        ok="Готово: все остатки, движения и история заказов WB обнулены. "
           "Товары, склады, ФФ и пользователи не тронуты — можно вносить приход заново.",
    ))


# ------------------------------------------------------------------- запуск
def bootstrap():
    """Создать таблицы и первого администратора, если базы ещё не было."""
    init_db()
    conn = get_conn()
    try:
        if conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"] == 0:
            conn.execute(
                "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
                (ADMIN_USERNAME, hash_password(ADMIN_PASSWORD), now_iso()),
            )
            conn.commit()
    finally:
        conn.close()


def _scheduler_loop():
    while True:
        time.sleep(SYNC_INTERVAL_MINUTES * 60)
        try:
            sync_once()
        except Exception:
            pass  # ошибки уже пишутся в sync_runs.message; фоновый поток не должен падать


def start_scheduler():
    thread = threading.Thread(target=_scheduler_loop, daemon=True)
    thread.start()


bootstrap()
start_scheduler()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
