"""
Проверка алиасов товаров (слияние карточек WB) — фича от 28.08.2026, запрос
Алёны: артикул 1454601004 ("9690-2 карта") — та же физическая позиция, что
и "Шейвер 1.4" (CR-9690), поэтому его продажи по WB должны списываться
СРАЗУ с остатка CR-9690, без своего отдельного остатка. Действует только на
заказы, которые появятся ПОСЛЕ добавления алиаса.

Проверяем:
  1) sync._resolve_alias / _find_or_create_product возвращают id целевого
     товара, если barcode/nmId — известный алиас (и НЕ создают новый товар);
  2) без алиаса поведение прежнее (создаётся/находится обычный товар);
  3) целый цикл sync_once(): заказ по алиасному barcode списывает остаток
     ИМЕННО с целевого товара, а не заводит отдельный;
  4) HTTP-маршруты /products/aliases/new и /products/aliases/<id>/delete —
     работают только для админа, обычный пользователь получает редирект;
  5) после удаления алиаса новый заказ по тому же barcode снова заводит
     свой отдельный товар (как было бы без алиаса вообще).

Запуск: python3 tests/test_product_aliases.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_product_aliases.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso  # noqa: E402
from app.auth import hash_password  # noqa: E402
from app.sync import _resolve_alias, _find_or_create_product, get_current_stock  # noqa: E402
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


init_db()
conn = get_conn()
conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад МСК', 1, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES "
    "('CR-9690', 100, 'BC-9690', 'Шейвер 1.4 CR-9690', ?)", (now_iso(),),
)
target_id = conn.execute("SELECT id FROM products WHERE sku='CR-9690'").fetchone()["id"]
conn.commit()
conn.close()

# --- 1) без алиаса: обычное поведение, новый товар создаётся
conn = get_conn()
check("Без алиаса: _resolve_alias вернул None", _resolve_alias(conn, 200, "BC-ALIAS") is None)
new_id = _find_or_create_product(conn, 200, "BC-ALIAS", name_hint="9690-2 карта")
conn.commit()
check("Без алиаса: создан ОТДЕЛЬНЫЙ товар (не совпадает с target_id)", new_id != target_id)
products_count_before = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
check("Без алиаса: в базе теперь 2 товара", products_count_before == 2)
conn.close()

# убираем товар-заготовку, созданный только что, чтобы не мешал следующим проверкам
conn = get_conn()
conn.execute("DELETE FROM products WHERE id = ?", (new_id,))
conn.commit()
conn.close()

# --- 2) добавляем алиас (напрямую в базу — HTTP-маршрут проверяется ниже отдельно)
conn = get_conn()
conn.execute(
    "INSERT INTO product_aliases (alias_barcode, alias_nm_id, target_product_id, comment, created_at) "
    "VALUES ('BC-ALIAS', 200, ?, '9690-2 карта -> CR-9690', ?)", (target_id, now_iso()),
)
conn.commit()

check("С алиасом по barcode: _resolve_alias вернул target_id", _resolve_alias(conn, None, "BC-ALIAS") == target_id)
check("С алиасом по nmId: _resolve_alias вернул target_id", _resolve_alias(conn, 200, None) == target_id)
check("С алиасом по обоим полям сразу: тоже target_id", _resolve_alias(conn, 200, "BC-ALIAS") == target_id)

resolved_id = _find_or_create_product(conn, 200, "BC-ALIAS", name_hint="9690-2 карта")
conn.commit()
check("_find_or_create_product с алиасом вернул id ЦЕЛЕВОГО товара", resolved_id == target_id)
products_count_after = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
check("С алиасом: НЕ создан новый товар (в базе всё ещё 1 товар)", products_count_after == 1)
conn.close()


# --- 3) целый цикл sync_once(): заказ по алиасному barcode списывается с target
class MockWBClient:
    def get_new_orders(self):
        return [{"id": 7000000001, "nmId": 200, "skus": ["BC-ALIAS"], "warehouseId": 1}]

    def get_orders_status(self, order_ids):
        return [{"id": oid, "supplierStatus": "new", "wbStatus": None} for oid in order_ids]


conn = get_conn()
stock_before = get_current_stock(conn, target_id, warehouse_id)
conn.close()
check("Остаток CR-9690 до продажи по алиасу = 0", stock_before == 0)

from app.sync import sync_once  # noqa: E402

result = sync_once(MockWBClient())
check("sync_once() отработал успешно", result["status"] == "success")

conn = get_conn()
stock_after = get_current_stock(conn, target_id, warehouse_id)
check("Остаток CR-9690 списан на 1 (продажа по алиасной карточке ушла на целевой товар)", stock_after == -1)
order_row = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='7000000001'").fetchone()
check("Заказ сохранён с product_id = ЦЕЛЕВОЙ товар (не отдельный)", order_row["product_id"] == target_id)
products_count_final = conn.execute("SELECT COUNT(*) AS c FROM products").fetchone()["c"]
check("После полного цикла синхронизации по-прежнему только 1 товар в базе", products_count_final == 1)
conn.close()


# --- 4) HTTP-маршруты: права доступа
app = main_module.app
app.testing = True

with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    resp = client.post(
        "/products/aliases/new",
        data={"alias_barcode": "BC-ALIAS-2", "alias_nm_id": "201", "target_product_id": str(target_id),
              "comment": "тест"},
        follow_redirects=True,
    )
    check("Админ: добавление алиаса — 200 OK", resp.status_code == 200)

conn = get_conn()
alias_row = conn.execute("SELECT * FROM product_aliases WHERE alias_barcode='BC-ALIAS-2'").fetchone()
check("Алиас реально создан в базе через HTTP", alias_row is not None)
alias_id = alias_row["id"]
conn.close()

conn = get_conn()
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES ('manager2', ?, 'manager', ?)",
    (hash_password("pass2"), now_iso()),
)
conn.commit()
conn.close()

with app.test_client() as client:
    client.post("/login", data={"username": "manager2", "password": "pass2"}, follow_redirects=True)
    resp = client.post(
        "/products/aliases/new",
        data={"alias_barcode": "BC-ALIAS-3", "alias_nm_id": "202", "target_product_id": str(target_id)},
        follow_redirects=True,
    )
    check("Обычный менеджер: НЕ может добавить алиас (нет прав)", "Недостаточно прав" in resp.get_data(as_text=True))

conn = get_conn()
check(
    "Алиас от менеджера НЕ создан",
    conn.execute("SELECT id FROM product_aliases WHERE alias_barcode='BC-ALIAS-3'").fetchone() is None,
)
conn.close()

with app.test_client() as client:
    client.post("/login", data={"username": "manager2", "password": "pass2"}, follow_redirects=True)
    resp = client.post(f"/products/aliases/{alias_id}/delete", follow_redirects=True)
    check("Обычный менеджер: НЕ может удалить алиас (нет прав)", "Недостаточно прав" in resp.get_data(as_text=True))

conn = get_conn()
check("Алиас НЕ удалён менеджером — всё ещё в базе", conn.execute(
    "SELECT id FROM product_aliases WHERE id = ?", (alias_id,)).fetchone() is not None)
conn.close()

# --- 5) удаление алиаса админом — новые заказы по этому barcode снова заводят свой товар
with app.test_client() as client:
    client.post("/login", data={"username": "admin", "password": "admin"}, follow_redirects=True)
    resp = client.post(f"/products/aliases/{alias_id}/delete", follow_redirects=True)
    check("Админ: удаление алиаса — 200 OK", resp.status_code == 200)

conn = get_conn()
check("Алиас удалён из базы", conn.execute(
    "SELECT id FROM product_aliases WHERE id = ?", (alias_id,)).fetchone() is None)
after_delete_id = _find_or_create_product(conn, 201, "BC-ALIAS-2", name_hint="повторно отдельная карточка")
conn.commit()
check("После удаления алиаса: снова заводится ОТДЕЛЬНЫЙ товар", after_delete_id != target_id)
conn.close()

print()
if failed:
    print(f"ПРОВАЛЕНО ПРОВЕРОК: {failed} (успешно: {passed})")
    sys.exit(1)
print(f"Все проверки алиасов товаров пройдены успешно ({passed}).")
