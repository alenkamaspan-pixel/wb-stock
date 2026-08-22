"""Простая авторизация по сессии (cookie). Несколько человек — свои логин/пароль."""
import sqlite3
from functools import wraps

from flask import session, redirect, url_for, g
from werkzeug.security import generate_password_hash, check_password_hash


def hash_password(password: str) -> str:
    return generate_password_hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return check_password_hash(password_hash, password)


def get_current_user() -> sqlite3.Row | None:
    """Текущий пользователь из сессии. Кэшируется на время запроса в flask.g.
    Использует общее на весь запрос соединение g.db (открывается в main.load_db)."""
    if "user" in g:
        return g.user
    user_id = session.get("user_id")
    if not user_id:
        g.user = None
        return None
    g.user = g.db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return g.user


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not get_current_user():
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def can_edit(user: sqlite3.Row) -> bool:
    return user is not None and user["role"] in ("admin", "manager")


def is_admin(user: sqlite3.Row) -> bool:
    return user is not None and user["role"] == "admin"
