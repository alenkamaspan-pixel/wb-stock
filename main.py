"""Точка входа приложения: Flask-роуты, авторизация, фоновая синхронизация с WB."""
import datetime as dt
import sqlite3
import threading
import time

from flask import Flask, request, session, redirect, url_for, render_template, g

from app.config import SECRET_KEY, ADMIN_USERNAME, ADMIN_PASSWORD, SYNC_INTERVAL_MINUTES
from app.database import get_conn, init_db, now_iso
from app.models import MovementType, MovementSource
from app.auth import hash_password, verify_password, get_current_user, login_required, can_edit, is_admin
from app.sync import sync_once, get_stock_table, get_stock_by_ff, get_current_stock
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
    ff_groups = get_stock_by_ff(g.db)
    last_run = g.db.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
    return render_template("dashboard.html", ff_groups=ff_groups, last_run=last_run)


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


# --------------------------------------------------------------- warehouses
@app.route("/warehouses")
@login_required
def warehouses_page():
    warehouses = g.db.execute(
        """
        SELECT w.*, f.name AS ff_name
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
