"""Точка входа приложения: Flask-роуты, авторизация, фоновая синхронизация с WB."""
import datetime as dt
import threading
import time

from flask import Flask, request, session, redirect, url_for, render_template, g

from app.config import SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, SYNC_INTERVAL_MINUTES
from app.database import get_conn, init_db, now_iso
from app.models import MovementType, MovementSource
from app.auth import hash_password, verify_password, get_current_user, login_required, can_edit, is_admin
from app.sync import sync_once, get_stock_table, get_current_stock, push_single
from app.wb_client import WBClient, WBApiError

app = Flask(__name__)
app.secret_key = SECRET_KEY


def dtfmt(value, fmt="%d.%m.%Y %H:%M"):
    if not value:
        return "—"
    try:
        return dt.datetime.fromisoformat(value).strftime(fmt)
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
    rows = get_stock_table(g.db)
    last_run = g.db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return render_template("dashboard.html", rows=rows, last_run=last_run)


@app.route("/sync/run-now", methods=["POST"])
@login_required
def sync_run_now():
    result = sync_once(WBClient())
    if result["status"] == "error":
        return redirect(url_for("dashboard", error=f"Ошибка синхронизации: {result['message']}"))
    return redirect(url_for("dashboard", ok="Синхронизация выполнена"))


# ---------------------------------------------------------------- movements
@app.route("/movements")
@login_required
def movements_page():
    movements = g.db.execute(
        """
        SELECT m.*, p.name AS product_name, w.name AS warehouse_name, u.username AS created_by_username
        FROM stock_movements m
        LEFT JOIN products p ON p.id = m.product_id
        LEFT JOIN warehouses w ON w.id = m.warehouse_id
        LEFT JOIN users u ON u.id = m.created_by_id
        ORDER BY m.created_at DESC, m.id DESC
        LIMIT 300
        """
    ).fetchall()
    products = g.db.execute("SELECT * FROM products ORDER BY name").fetchall()
    warehouses = g.db.execute(
        "SELECT * FROM warehouses WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    user = get_current_user()
    return render_template(
        "movements.html", movements=movements, products=products,
        warehouses=warehouses, can_edit=can_edit(user),
    )


@app.route("/movements/income", methods=["POST"])
@login_required
def movement_income():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    product_id = int(request.form["product_id"])
    warehouse_id = int(request.form["warehouse_id"])
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
    push_single(g.db, WBClient(), product_id, warehouse_id)
    return redirect(url_for("movements_page", ok="Приход добавлен"))


@app.route("/movements/writeoff", methods=["POST"])
@login_required
def movement_writeoff():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    product_id = int(request.form["product_id"])
    warehouse_id = int(request.form["warehouse_id"])
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
    push_single(g.db, WBClient(), product_id, warehouse_id)
    return redirect(url_for("movements_page", ok="Списание добавлено"))


@app.route("/movements/transfer", methods=["POST"])
@login_required
def movement_transfer():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("movements_page", error="Недостаточно прав"))
    product_id = int(request.form["product_id"])
    from_warehouse_id = int(request.form["from_warehouse_id"])
    to_warehouse_id = int(request.form["to_warehouse_id"])
    quantity = abs(int(request.form["quantity"]))
    comment = request.form.get("comment") or None

    if from_warehouse_id == to_warehouse_id:
        return redirect(url_for("movements_page", error="Склады отправления и назначения совпадают"))

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
    push_single(g.db, WBClient(), product_id, from_warehouse_id)
    push_single(g.db, WBClient(), product_id, to_warehouse_id)
    return redirect(url_for("movements_page", ok="Перемещение выполнено"))


# ----------------------------------------------------------------- products
@app.route("/products")
@login_required
def products_page():
    products = g.db.execute("SELECT * FROM products ORDER BY name").fetchall()
    return render_template("products.html", products=products, can_edit=can_edit(get_current_user()))


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


# --------------------------------------------------------------- warehouses
@app.route("/warehouses")
@login_required
def warehouses_page():
    warehouses = g.db.execute("SELECT * FROM warehouses ORDER BY name").fetchall()
    return render_template("warehouses.html", warehouses=warehouses, can_edit=can_edit(get_current_user()))


@app.route("/warehouses/new", methods=["POST"])
@login_required
def warehouse_new():
    user = get_current_user()
    if not can_edit(user):
        return redirect(url_for("warehouses_page", error="Недостаточно прав"))
    name = request.form["name"].strip()
    wb_warehouse_id = request.form.get("wb_warehouse_id", "").strip()
    is_synced = request.form.get("is_synced_to_wb", "1") == "1"
    try:
        g.db.execute(
            "INSERT INTO warehouses (name, wb_warehouse_id, is_synced_to_wb, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?)",
            (name, int(wb_warehouse_id) if wb_warehouse_id else None, 1 if is_synced else 0, now_iso()),
        )
        g.db.commit()
    except Exception as e:
        return redirect(url_for("warehouses_page", error=f"Не удалось добавить склад: {e}"))
    return redirect(url_for("warehouses_page", ok="Склад добавлен"))


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
            "INSERT INTO warehouses (name, wb_warehouse_id, is_synced_to_wb, is_active, created_at) "
            "VALUES (?, ?, 1, 1, ?)",
            (w.get("name", f"Склад WB {wb_id}"), wb_id, now_iso()),
        )
        added += 1
    g.db.commit()
    return redirect(url_for("warehouses_page", ok=f"Импортировано складов: {added}"))


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
