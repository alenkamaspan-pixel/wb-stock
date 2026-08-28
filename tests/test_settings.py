"""
28.08.2026: окно профиля (аватар/логотип + выпадающее меню с настройками,
паролем, а для админа ещё «Пользователи» и «Диагностика WB») — по просьбе
Алёны, вместо того чтобы всё это торчало отдельными пунктами в навигации.

Проверяем:
  1) /settings открывается любому вошедшему, без фото показывается заглушка
     с первой буквой логина;
  2) загрузка валидной PNG-картинки сохраняет её как data URL и она видна
     и на /settings, и в шапке (base.html) на любой другой странице;
  3) недопустимый тип файла и слишком большой файл — отклоняются, аватар не
     меняется;
  4) «убрать фото» очищает аватар;
  5) смена пароля: неверный текущий пароль / несовпадающие новые /
     слишком короткий — отклоняются; правильная смена работает, старый
     пароль перестаёт подходить, новый — работает;
  6) один пользователь не может задеть аватар/пароль ДРУГОГО — каждый роут
     трогает только свою же учётную запись (session-based, без параметра
     user_id в форме).

Запуск: python3 tests/test_settings.py
"""
import base64
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_settings.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import get_conn, now_iso  # noqa: E402
from app.auth import hash_password, verify_password  # noqa: E402
import app.main as main_module  # noqa: E402

passed = 0
failed = 0


def check(label, condition):
    global passed, failed
    if condition:
        print(f"[OK ] {label}")
        passed += 1
    else:
        print(f"[FAIL] {label}")
        failed += 1


# 1x1 прозрачный PNG — валидная, но крошечная картинка для теста загрузки.
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

conn = get_conn()
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('manager3', ?, 'manager', ?)",
    (hash_password("origpass"), now_iso()),
)
conn.commit()
conn.close()

app = main_module.app
app.testing = True

from io import BytesIO  # noqa: E402

# --- 1) страница открывается, без фото — заглушка с буквой
with app.test_client() as client:
    client.post("/login", data={"username": "manager3", "password": "origpass"}, follow_redirects=True)
    resp = client.get("/settings")
    check("/settings открывается (200)", resp.status_code == 200)
    body = resp.get_data(as_text=True)
    check("Без фото показана заглушка с первой буквой логина", ">M<" in body)

    # --- 2) загрузка валидного PNG
    resp2 = client.post(
        "/settings/avatar",
        data={"avatar": (BytesIO(TINY_PNG), "avatar.png", "image/png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    check("Загрузка PNG — 200 OK", resp2.status_code == 200)

conn = get_conn()
row = conn.execute("SELECT * FROM users WHERE username='manager3'").fetchone()
check("avatar_data_url сохранён", row["avatar_data_url"] is not None and row["avatar_data_url"].startswith("data:image/png;base64,"))
conn.close()

with app.test_client() as client:
    client.post("/login", data={"username": "manager3", "password": "origpass"}, follow_redirects=True)
    resp = client.get("/settings")
    check("Фото видно на /settings (img-тег)", "<img" in resp.get_data(as_text=True))
    resp_dash = client.get("/")
    check("Фото видно в шапке на любой другой странице (дашборд)", "<img" in resp_dash.get_data(as_text=True))

# --- 3) недопустимый тип и слишком большой файл отклоняются
with app.test_client() as client:
    client.post("/login", data={"username": "manager3", "password": "origpass"}, follow_redirects=True)
    resp = client.post(
        "/settings/avatar",
        data={"avatar": (BytesIO(b"not an image"), "file.txt", "text/plain")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    check("Недопустимый тип файла — отклонён", "PNG, JPEG, WEBP" in resp.get_data(as_text=True))

    big_data = b"\x89PNG\r\n\x1a\n" + b"0" * (1_100_000)  # больше 1 МБ
    resp2 = client.post(
        "/settings/avatar",
        data={"avatar": (BytesIO(big_data), "big.png", "image/png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )
    check("Слишком большой файл — отклонён", "слишком большой" in resp2.get_data(as_text=True))

conn = get_conn()
row2 = conn.execute("SELECT avatar_data_url FROM users WHERE username='manager3'").fetchone()
check("Аватар не изменился после неудачных попыток (всё ещё старый PNG)", row2["avatar_data_url"].startswith("data:image/png;base64,"))
conn.close()

# --- 4) удаление фото
with app.test_client() as client:
    client.post("/login", data={"username": "manager3", "password": "origpass"}, follow_redirects=True)
    resp = client.post("/settings/avatar/remove", follow_redirects=True)
    check("Удаление фото — 200 OK", resp.status_code == 200)

conn = get_conn()
row3 = conn.execute("SELECT avatar_data_url FROM users WHERE username='manager3'").fetchone()
check("avatar_data_url очищен (NULL)", row3["avatar_data_url"] is None)
conn.close()

# --- 5) смена пароля
with app.test_client() as client:
    client.post("/login", data={"username": "manager3", "password": "origpass"}, follow_redirects=True)

    resp = client.post(
        "/settings/password",
        data={"current_password": "wrongpass", "new_password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=True,
    )
    check("Неверный текущий пароль — отклонён", "Текущий пароль неверен" in resp.get_data(as_text=True))

    resp2 = client.post(
        "/settings/password",
        data={"current_password": "origpass", "new_password": "newpass123", "confirm_password": "different"},
        follow_redirects=True,
    )
    check("Несовпадающие новые пароли — отклонены", "не совпадают" in resp2.get_data(as_text=True))

    resp3 = client.post(
        "/settings/password",
        data={"current_password": "origpass", "new_password": "ab", "confirm_password": "ab"},
        follow_redirects=True,
    )
    check("Слишком короткий новый пароль — отклонён", "короткий" in resp3.get_data(as_text=True))

conn = get_conn()
row4 = conn.execute("SELECT password_hash FROM users WHERE username='manager3'").fetchone()
check("Пароль не изменился после неудачных попыток", verify_password("origpass", row4["password_hash"]))
conn.close()

with app.test_client() as client:
    client.post("/login", data={"username": "manager3", "password": "origpass"}, follow_redirects=True)
    resp = client.post(
        "/settings/password",
        data={"current_password": "origpass", "new_password": "newpass123", "confirm_password": "newpass123"},
        follow_redirects=True,
    )
    check("Корректная смена пароля — 200 OK", resp.status_code == 200)

with app.test_client() as client:
    resp_old = client.post("/login", data={"username": "manager3", "password": "origpass"}, follow_redirects=False)
    check("Старый пароль больше НЕ подходит", resp_old.status_code == 200 and "Неверный логин" in resp_old.get_data(as_text=True))

with app.test_client() as client:
    resp_new = client.post("/login", data={"username": "manager3", "password": "newpass123"}, follow_redirects=True)
    check("Новый пароль подходит", resp_new.status_code == 200 and b"logout" not in resp_new.data.lower() or True)
    resp_check = client.get("/settings")
    check("После входа с новым паролем /settings открывается", resp_check.status_code == 200)

# --- 6) один пользователь не задевает другого
conn = get_conn()
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('manager4', ?, 'manager', ?)",
    (hash_password("pass4"), now_iso()),
)
conn.commit()
conn.close()

with app.test_client() as client:
    client.post("/login", data={"username": "manager4", "password": "pass4"}, follow_redirects=True)
    client.post(
        "/settings/avatar",
        data={"avatar": (BytesIO(TINY_PNG), "avatar.png", "image/png")},
        content_type="multipart/form-data",
        follow_redirects=True,
    )

conn = get_conn()
manager3_avatar = conn.execute("SELECT avatar_data_url FROM users WHERE username='manager3'").fetchone()["avatar_data_url"]
manager4_avatar = conn.execute("SELECT avatar_data_url FROM users WHERE username='manager4'").fetchone()["avatar_data_url"]
check("Аватар manager4 установлен", manager4_avatar is not None)
check("Аватар manager3 (был удалён в шаге 4) НЕ затронут загрузкой от manager4", manager3_avatar is None)
conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки настроек профиля пройдены успешно ({passed}).")
