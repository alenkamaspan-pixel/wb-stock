"""
Проверка чистки дублей карточек (30.09.2026, по итогам большой сверки
остатков с Алёной — одна карточка на артикул, WB-идентификаторы и Ozon SKU
на одной и той же карточке).

Проверяем:
  1) _merge_known_duplicate_products переносит недостающие идентификаторы
     (ozon_sku) и всю историю движений с карточки-дубля на каноническую;
  2) карточка-дубль после этого помечена is_active=0 и переименована с явной
     пометкой "слито" — но не удалена физически;
  3) миграция идемпотентна — повторный вызов ничего не ломает и не дублирует;
  4) is_active=0 карточки больше не предлагаются в форме движений
     (movements_page отдаёт только активные товары);
  5) окончательное удаление карточки (/products/<id>/delete) теперь реально
     удаляет товар вместе с историей движений — но только для admin и только
     с confirmed=yes; без этого — отказ, история не тронута.

Запуск: python3 tests/test_product_merge.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_product_merge.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso, _merge_known_duplicate_products  # noqa: E402
from app.auth import hash_password  # noqa: E402
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


init_db()  # первый вызов _migrate() уже отметил миграцию слияния выполненной
           # (карточек из списка ещё не существовало) — снимаем отметку, чтобы
           # протестировать саму логику слияния на подготовленных данных.
conn = get_conn()
conn.execute("DELETE FROM schema_migrations WHERE name = '2026_09_30_merge_duplicate_products'")

conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад тест', 1, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]

conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES "
    "('5 шейвер CR-1324', 1105688843, '2051833144032', '5 шейвер CR-1324', ?)", (now_iso(),),
)
canonical_id = conn.execute("SELECT id FROM products WHERE sku='5 шейвер CR-1324'").fetchone()["id"]

conn.execute(
    "INSERT INTO products (sku, ozon_sku, name, created_at) VALUES "
    "('CR-1324 OZ', 5678819890, 'Профессиональный шейвер для бритья', ?)", (now_iso(),),
)
ghost_id = conn.execute("SELECT id FROM products WHERE sku='CR-1324 OZ'").fetchone()["id"]

conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'transfer_out', -30, 'manual', ?)",
    (ghost_id, warehouse_id, now_iso()),
)
conn.execute(
    "INSERT INTO wb_orders (wb_order_id, product_id, warehouse_id, quantity, created_at, updated_at) "
    "VALUES ('order-ghost-1', ?, ?, 1, ?, ?)",
    (ghost_id, warehouse_id, now_iso(), now_iso()),
)
conn.commit()

check(
    "До слияния: у канонической карточки нет ozon_sku",
    conn.execute("SELECT ozon_sku FROM products WHERE id=?", (canonical_id,)).fetchone()["ozon_sku"] is None,
)
movements_before = conn.execute(
    "SELECT COUNT(*) AS c FROM stock_movements WHERE product_id=?", (ghost_id,)
).fetchone()["c"]
check("До слияния: у карточки-дубля есть 1 движение", movements_before == 1)
conn.close()

# --- запускаем саму миграцию слияния
conn = get_conn()
_merge_known_duplicate_products(conn)
conn.close()

conn = get_conn()
canonical = conn.execute("SELECT * FROM products WHERE id=?", (canonical_id,)).fetchone()
ghost = conn.execute("SELECT * FROM products WHERE id=?", (ghost_id,)).fetchone()

check(f"После слияния: ozon_sku перенесён на каноническую карточку, получено {canonical['ozon_sku']}",
      canonical["ozon_sku"] == 5678819890)
check("После слияния: у карточки-дубля ozon_sku снят (NULL)", ghost["ozon_sku"] is None)
check("После слияния: карточка-дубль помечена неактивной", ghost["is_active"] == 0)
check(f"После слияния: карточка-дубль переименована с пометкой «слито», получено «{ghost['name']}»",
      "СЛИТО" in ghost["name"] and canonical["name"] in ghost["name"])
check("После слияния: каноническая карточка осталась активной", canonical["is_active"] == 1)

movement = conn.execute("SELECT * FROM stock_movements WHERE warehouse_id=?", (warehouse_id,)).fetchone()
check("После слияния: движение переехало на каноническую карточку", movement["product_id"] == canonical_id)
movements_left_on_ghost = conn.execute(
    "SELECT COUNT(*) AS c FROM stock_movements WHERE product_id=?", (ghost_id,)
).fetchone()["c"]
check("После слияния: на карточке-дубле движений больше нет", movements_left_on_ghost == 0)

order = conn.execute("SELECT * FROM wb_orders WHERE wb_order_id='order-ghost-1'").fetchone()
check("После слияния: заказ тоже переехал на каноническую карточку", order["product_id"] == canonical_id)
conn.close()

# --- идемпотентность: повторный запуск ничего не портит
conn = get_conn()
name_before_second_run = conn.execute("SELECT name FROM products WHERE id=?", (ghost_id,)).fetchone()["name"]
_merge_known_duplicate_products(conn)  # уже отмечена выполненной — должна тут же выйти
conn.close()

conn = get_conn()
name_after_second_run = conn.execute("SELECT name FROM products WHERE id=?", (ghost_id,)).fetchone()["name"]
check("Повторный запуск миграции: имя карточки-дубля не изменилось повторно (не задвоилась пометка)",
      name_before_second_run == name_after_second_run)
conn.close()

# --- is_active=0 больше не предлагается в форме движений
main_module.app.config["TESTING"] = True
client = main_module.app.test_client()

conn = get_conn()
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'admin', ?)",
    ("admin_merge_test", hash_password("pass12345"), now_iso()),
)
conn.execute(
    "INSERT INTO fulfillment_centers (name, is_active, created_at) VALUES ('ФФ Тест', 1, ?)",
    (now_iso(),),
)
ff_id = conn.execute("SELECT id FROM fulfillment_centers WHERE name='ФФ Тест'").fetchone()["id"]
conn.execute("UPDATE warehouses SET fulfillment_center_id=? WHERE id=?", (ff_id, warehouse_id))
conn.commit()
conn.close()

client.post("/login", data={"username": "admin_merge_test", "password": "pass12345"})
resp = client.get("/movements")
html = resp.get_data(as_text=True)
check("Каноническая карточка есть в форме движений", canonical["name"] in html)
check("Карточка-дубль (is_active=0) НЕ показана в форме движений", ghost["name"] not in html)

# --- окончательное удаление: теперь реально работает, но только для admin с confirmed=yes
conn = get_conn()
conn.execute(
    "INSERT INTO products (sku, name, created_at) VALUES ('TO-DELETE', 'Карточка на удаление', ?)",
    (now_iso(),),
)
to_delete_id = conn.execute("SELECT id FROM products WHERE sku='TO-DELETE'").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 5, 'manual', ?)",
    (to_delete_id, warehouse_id, now_iso()),
)
conn.commit()
conn.close()

resp = client.post(f"/products/{to_delete_id}/delete", data={}, follow_redirects=True)
html = resp.get_data(as_text=True)
check("Без confirmed=yes: удаление отклонено", "не подтверждено" in html)
conn = get_conn()
still_there = conn.execute("SELECT 1 FROM products WHERE id=?", (to_delete_id,)).fetchone()
conn.close()
check("Без confirmed=yes: карточка НЕ удалена", still_there is not None)

resp = client.post(f"/products/{to_delete_id}/delete", data={"confirmed": "yes"}, follow_redirects=True)
html = resp.get_data(as_text=True)
check("С confirmed=yes (admin): удаление подтверждено сообщением", "окончательно удалён" in html)
conn = get_conn()
gone = conn.execute("SELECT 1 FROM products WHERE id=?", (to_delete_id,)).fetchone()
movements_gone = conn.execute(
    "SELECT COUNT(*) AS c FROM stock_movements WHERE product_id=?", (to_delete_id,)
).fetchone()["c"]
conn.close()
check("Карточка реально удалена из базы", gone is None)
check("Вместе с ней удалена вся история движений по ней", movements_gone == 0)

# --- обычный менеджер (не admin) не может удалить
conn = get_conn()
conn.execute(
    "INSERT INTO users (username, password_hash, role, created_at) VALUES (?, ?, 'manager', ?)",
    ("manager_merge_test", hash_password("pass12345"), now_iso()),
)
conn.execute(
    "INSERT INTO products (sku, name, created_at) VALUES ('TO-DELETE-2', 'Вторая на удаление', ?)",
    (now_iso(),),
)
to_delete_id2 = conn.execute("SELECT id FROM products WHERE sku='TO-DELETE-2'").fetchone()["id"]
conn.commit()
conn.close()

client2 = main_module.app.test_client()
client2.post("/login", data={"username": "manager_merge_test", "password": "pass12345"})
resp = client2.post(f"/products/{to_delete_id2}/delete", data={"confirmed": "yes"}, follow_redirects=True)
html = resp.get_data(as_text=True)
check("Менеджер (не admin): недостаточно прав", "Недостаточно прав" in html)
conn = get_conn()
still_there2 = conn.execute("SELECT 1 FROM products WHERE id=?", (to_delete_id2,)).fetchone()
conn.close()
check("Менеджер не смог удалить карточку", still_there2 is not None)

print()
print(f"Итого: {passed} успешно, {failed} провалено.")
if failed:
    sys.exit(1)
print("Все проверки слияния дублей и удаления карточек пройдены успешно.")
