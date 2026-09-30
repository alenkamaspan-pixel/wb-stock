"""
Точечные слияния карточек, найденные Алёной на живом сайте УЖЕ ПОСЛЕ
большой чистки дублей от 28-30.09.2026 (их не было в исходном списке
_DUPLICATE_PRODUCT_GROUPS) — механизм _merge_additional_found_products,
список _ADDITIONAL_MERGES.

Проверяем на двух реальных находках:
  1) «Машинка кинг KP-2116» — канонической была "битая" карточка WB (её
     name/sku по ошибке стали просто числом nm_id/barcode), дубль — с Ozon
     SKU и заглушкой "KP-2116." в имени. При слиянии переносим ozon_sku и
     историю на карточку с WB-идентификаторами (она нужна для автосопостав-
     ления заказов WB FBS) и ЧИНИМ ей имя/sku на нормальные.
  2) «6 Шейвер CR-1325» — канонической уже была нормальная карточка WB,
     дубль — заглушка "шейвер сг-1325" с Ozon SKU. Переносим только Ozon SKU
     и историю, имя/sku канонической карточки не трогаем (final_name/
     final_sku = None в спецификации).

А также — что каждая запись в списке отмечается СВОИМ ОТДЕЛЬНЫМ именем в
schema_migrations, поэтому обе применяются независимо (в частности: если
"выполненной" отмечена только одна из них, вторая всё равно сработает).

Запуск: python3 tests/test_merge_kp2116.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

TEST_DB = "/tmp/wb_stock_test_merge_kp2116.db"
if os.path.exists(TEST_DB):
    os.remove(TEST_DB)
os.environ["DATABASE_PATH"] = TEST_DB
os.environ["SECRET_KEY"] = "test"

from app.database import init_db, get_conn, now_iso, _merge_additional_found_products  # noqa: E402

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


init_db()  # первый вызов _migrate() уже отметил обе точечные миграции
           # выполненными (карточек из сценария ещё не существовало) —
           # снимаем отметку обеих, чтобы проверить сам механизм на
           # подготовленных данных.
conn = get_conn()
conn.execute(
    "DELETE FROM schema_migrations WHERE name LIKE '2026_09_30_merge_extra_%'"
)

conn.execute(
    "INSERT INTO warehouses (name, wb_warehouse_id, is_active, created_at) VALUES ('Склад тест', 1, 1, ?)",
    (now_iso(),),
)
warehouse_id = conn.execute("SELECT id FROM warehouses WHERE wb_warehouse_id=1").fetchone()["id"]

# --- сценарий 1: KP-2116 (каноническую нужно ещё и переименовать) ---
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES "
    "('2057295527987', 1657611777, '2057295527987', '1657611777', ?)", (now_iso(),),
)
kp_canonical_id = conn.execute("SELECT id FROM products WHERE nm_id=1657611777").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, ozon_sku, name, created_at) VALUES "
    "('KP-2116.', 5762682150, 'KP-2116.', ?)", (now_iso(),),
)
kp_duplicate_id = conn.execute("SELECT id FROM products WHERE ozon_sku=5762682150").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 12, 'manual', ?)",
    (kp_duplicate_id, warehouse_id, now_iso()),
)

# --- сценарий 2: CR-1325 (каноническая карточка уже в порядке) ---
conn.execute(
    "INSERT INTO products (sku, nm_id, barcode, name, created_at) VALUES "
    "('6 Шейвер CR-1325', 1535320580, '2056158461826', '6 Шейвер CR-1325', ?)", (now_iso(),),
)
cr_canonical_id = conn.execute("SELECT id FROM products WHERE nm_id=1535320580").fetchone()["id"]
conn.execute(
    "INSERT INTO products (sku, ozon_sku, name, created_at) VALUES "
    "('шейвер сг-1325', 5799938415, 'шейвер сг-1325', ?)", (now_iso(),),
)
cr_duplicate_id = conn.execute("SELECT id FROM products WHERE ozon_sku=5799938415").fetchone()["id"]
conn.execute(
    "INSERT INTO stock_movements (product_id, warehouse_id, movement_type, delta, source, created_at) "
    "VALUES (?, ?, 'income', 7, 'manual', ?)",
    (cr_duplicate_id, warehouse_id, now_iso()),
)
conn.commit()
conn.close()

# --- запускаем сам механизм ---
conn = get_conn()
_merge_additional_found_products(conn)
conn.close()

conn = get_conn()

kp_canonical = conn.execute("SELECT * FROM products WHERE id=?", (kp_canonical_id,)).fetchone()
kp_duplicate = conn.execute("SELECT * FROM products WHERE id=?", (kp_duplicate_id,)).fetchone()
check("KP-2116: имя канонической карточки исправлено", kp_canonical["name"] == "Машинка кинг KP-2116")
check("KP-2116: sku канонической карточки исправлен", kp_canonical["sku"] == "KP-2116")
check("KP-2116: nm_id/штрихкод не потеряны",
      kp_canonical["nm_id"] == 1657611777 and kp_canonical["barcode"] == "2057295527987")
check("KP-2116: ozon_sku перенесён на каноническую карточку", kp_canonical["ozon_sku"] == 5762682150)
check("KP-2116: у дубля ozon_sku снят", kp_duplicate["ozon_sku"] is None)
check("KP-2116: дубль помечен неактивным и переименован с пометкой «слито»",
      kp_duplicate["is_active"] == 0 and "СЛИТО" in kp_duplicate["name"])
kp_movement = conn.execute("SELECT * FROM stock_movements WHERE warehouse_id=? AND delta=12", (warehouse_id,)).fetchone()
check("KP-2116: движение переехало на каноническую карточку", kp_movement["product_id"] == kp_canonical_id)

cr_canonical = conn.execute("SELECT * FROM products WHERE id=?", (cr_canonical_id,)).fetchone()
cr_duplicate = conn.execute("SELECT * FROM products WHERE id=?", (cr_duplicate_id,)).fetchone()
check("CR-1325: имя канонической карточки НЕ тронуто (уже было в порядке)",
      cr_canonical["name"] == "6 Шейвер CR-1325")
check("CR-1325: sku канонической карточки НЕ тронут", cr_canonical["sku"] == "6 Шейвер CR-1325")
check("CR-1325: ozon_sku перенесён на каноническую карточку", cr_canonical["ozon_sku"] == 5799938415)
check("CR-1325: у дубля ozon_sku снят", cr_duplicate["ozon_sku"] is None)
check("CR-1325: дубль помечен неактивным и переименован с пометкой «слито»",
      cr_duplicate["is_active"] == 0 and "СЛИТО" in cr_duplicate["name"])
cr_movement = conn.execute("SELECT * FROM stock_movements WHERE warehouse_id=? AND delta=7", (warehouse_id,)).fetchone()
check("CR-1325: движение переехало на каноническую карточку", cr_movement["product_id"] == cr_canonical_id)

conn.close()

# --- идемпотентность: повторный запуск ничего не портит ---
conn = get_conn()
names_before = {
    kp_canonical_id: conn.execute("SELECT name FROM products WHERE id=?", (kp_canonical_id,)).fetchone()["name"],
    cr_canonical_id: conn.execute("SELECT name FROM products WHERE id=?", (cr_canonical_id,)).fetchone()["name"],
}
_merge_additional_found_products(conn)  # обе записи уже отмечены выполненными
conn.close()
conn = get_conn()
check("Повторный запуск: имя KP-2116 не изменилось повторно",
      conn.execute("SELECT name FROM products WHERE id=?", (kp_canonical_id,)).fetchone()["name"]
      == names_before[kp_canonical_id])
check("Повторный запуск: имя CR-1325 не изменилось повторно",
      conn.execute("SELECT name FROM products WHERE id=?", (cr_canonical_id,)).fetchone()["name"]
      == names_before[cr_canonical_id])
conn.close()

print()
print(f"Итого: {passed} успешно, {failed} провалено.")
if failed:
    sys.exit(1)
print("Все проверки точечных слияний (KP-2116, CR-1325) пройдены успешно.")
