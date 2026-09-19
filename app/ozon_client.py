"""
Клиент для Ozon Seller API.

ОБНОВЛЕНО 18.09.2026 (вторая итерация): первая версия этого файла ссылалась
на методы, которые Ozon к моменту реального запуска уже отключил — Алёна
поймала это через /ozon-diagnostics и live-кнопки на странице «Ozon» (три
живые ошибки: `POST /v1/warehouse/list` -> "obsolete method cannot be used",
`POST /v3/posting/fbs/unfulfilled/list` -> "mismatch between cutoff &
delivery date", `POST /v2/supply-order/list` -> 404). Пути ниже заменены на
актуальные по состоянию на 18.09.2026, сверенные напрямую с официальным
разделом методов на docs.ozon.ru/api/seller/ (список разделов «Работа со
складами FBS и rFBS», «Обработка заказов FBS и rFBS», «Доставка FBO»):
  - склады:        /v1/warehouse/list       -> /v2/warehouse/list
  - необработанные: /v3/posting/fbs/unfulfilled/list -> /v4/…
  - список отправлений: /v3/posting/fbs/list -> /v4/… (тот же паттерн
    устаревания, что и у unfulfilled — на всякий случай заменено заранее)
  - поставки FBO:   /v2/supply-order/list    -> /v3/supply-order/list
                     /v2/supply-order/get    -> /v3/supply-order/get
  - состав поставки: /v1/supply-order/bundle остался тем же путём, но
    оказался POST, а не GET (в первой версии был ошибочно вызван как GET)

ВАЖНО: сами тела запросов (особенно новые поля фильтра у /v4/…/unfulfilled и
точная структура фильтра/пагинации у /v3/supply-order/list) по-прежнему НЕ
сверены байт-в-байт с живым ответом — песочница не имеет сетевого доступа к
api-seller.ozon.ru. То, что ниже — лучшее приближение по докам и по прошлой
ошибке (Ozon явно требовал непустой диапазон cutoff_from/cutoff_to для
unfulfilled-фильтра — теперь он заполняется всегда, широким окном). Если
после этой правки /ozon-diagnostics или live-кнопки покажут новую ошибку —
это следующая, более точная подсказка от самого Ozon о том, какое поле
называется иначе; путь метода к этому моменту уже должен быть верным.

Используются два значения из личного кабинета (Настройки → Seller API):
  - Client-Id — числовой идентификатор кабинета
  - Api-Key   — сам ключ

Приложение (как и для WB) НИЧЕГО не пишет обратно в Ozon — только читает
заказы FBS, поставки FBO и список складов. Управление тем, что видят
покупатели на карточке товара, остаётся вне этого приложения.
"""
import datetime as dt
import time
from typing import Any

import httpx

from app.config import OZON_CLIENT_ID, OZON_API_KEY, OZON_BASE_URL


class OzonApiError(Exception):
    pass


class OzonClient:
    def __init__(
        self,
        client_id: str = OZON_CLIENT_ID,
        api_key: str = OZON_API_KEY,
        base_url: str = OZON_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.client_id = client_id
        self.api_key = api_key
        self.base = base_url.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    # ---------------------------------------------------------------- utils
    def _post(self, path: str, json_body: dict | None = None) -> Any:
        return self._request("POST", path, json_body)

    def _get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, None, params=params)

    def _request(self, method: str, path: str, json_body: dict | None, **kwargs) -> Any:
        url = f"{self.base}{path}"
        headers = {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(method, url, headers=headers, json=json_body, **kwargs)
                if resp.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                resp.raise_for_status()
                if not resp.content:
                    return None
                return resp.json()
            except httpx.HTTPStatusError as e:
                last_error = OzonApiError(
                    f"{method} {path} -> {e.response.status_code}: {e.response.text[:500]}"
                )
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    raise last_error
                time.sleep(1.5 * attempt)
            except httpx.HTTPError as e:
                last_error = OzonApiError(f"{method} {path} -> сетевая ошибка: {e}")
                time.sleep(1.5 * attempt)
        raise last_error or OzonApiError(f"{method} {path} -> не удалось выполнить запрос")

    # ------------------------------------------------------------- склады
    def get_fbs_warehouses(self) -> list[dict]:
        """Склады FBS/rFBS продавца (не путать со складами FBO — те через
        кластеры, здесь не нужны: поставки FBO у нас идут на единый
        виртуальный склад «Ozon FBO», см. app/ozon_sync.py).

        Было /v1/warehouse/list — живой ответ Ozon: 400 "obsolete method
        cannot be used". Заменено на /v2/warehouse/list (актуальный метод
        того же раздела «Работа со складами FBS и rFBS» в официальных
        доках), тело запроса пустое, как и было у v1.

        СЕДЬМАЯ ПРАВКА (после /ozon-diagnostics): метод отвечал без ошибки
        (200 OK), но с пустым списком [], хотя в личном кабинете Ozon у
        аккаунта минимум 2 активных FBS-склада (ФБС москва, ФБС ЕКБ). Это
        не ошибка вида «Ozon подсказал точное поле» — тут Ozon ничего не
        подсказывает, а просто отвечает 200 с другой структурой, чем мы
        ожидаем, и разбор молча даёт []. Единственный жёстко зашитый ключ
        был "result" как список — теперь разбор терпим к нескольким
        вероятным формам ответа (result как словарь с вложенным списком
        под "warehouses"/"search"/"items", либо эти же ключи прямо на
        верхнем уровне без обёртки "result"). Если ни один из этих
        вариантов не тот — на /ozon-diagnostics теперь дополнительно виден
        и сырой, необработанный JSON-ответ Ozon по этому методу, так что
        точное имя поля можно будет увидеть напрямую, а не гадать снова."""
        data = self._post("/v2/warehouse/list", {})
        result = (data or {}).get("result", data or {})
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            return result.get("warehouses") or result.get("search") or result.get("items") or []
        return []

    def get_fbs_warehouses_raw(self) -> Any:
        """Тот же вызов, что get_fbs_warehouses, но БЕЗ разбора ответа —
        только для /ozon-diagnostics, чтобы увидеть точную структуру,
        которую реально прислал Ozon (см. СЕДЬМАЯ ПРАВКА выше)."""
        return self._post("/v2/warehouse/list", {})

    # --------------------------------------------------------- FBS-заказы
    def get_unfulfilled_postings(self, limit: int = 100, offset: int = 0) -> dict:
        """Отправления FBS, ещё не собранные — аналог WB /orders/new, тот же
        самый безопасный момент для списания остатка.

        Было /v3/posting/fbs/unfulfilled/list с пустым filter={} — живой
        ответ Ozon: 400 "the mismatch between cutoff & delivery date" (Ozon
        не смог сам вывести согласованный диапазон дат из пустого фильтра).
        Заменено на /v4/… (v3 в этом разделе официально отключён — по
        независимым источникам, отключение было ещё 01.06.2026) и filter
        теперь всегда содержит непустой cutoff_from/cutoff_to — широкое
        окно вместо пустого объекта, чтобы условие на сервере не ловило
        рассинхрон дат.

        ВТОРАЯ ПРАВКА (после /ozon-diagnostics): live-ответ показал ещё
        одну ошибку — "Limit: value must be inside range (0, 100]". Была
        default=1000, Ozon v4 разрешает максимум 100 за раз — default
        снижен, и любое переданное значение на всякий случай подрезается,
        чтобы случайный limit>100 не уронил синхронизацию снова.

        ДЕСЯТАЯ ПРАВКА (после того, как Алёна показала скриншот из личного
        кабинета Ozon с 4 реальными заказами «ожидают сборки», а наша
        синхронизация написала «отправлений 0»): это тот же класс бага,
        что был со складами (СЕДЬМАЯ ПРАВКА в get_fbs_warehouses) — Ozon,
        похоже, отвечает 200 OK, но с другой структурой, чем мы ждём
        (единственный жёстко зашитый ключ был "result" как объект с полем
        "postings"), и разбор молча даёт пусто без всякой ошибки. Как и
        там, разбор ответа сделан терпимым к нескольким вероятным формам,
        и на /ozon-diagnostics добавлен сырой, необработанный ответ этого
        метода — если и расширенный разбор не попадёт в точку, точная
        структура будет видна напрямую."""
        limit = max(1, min(int(limit), 100))
        now = dt.datetime.utcnow()
        cutoff_from = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cutoff_to = (now + dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "dir": "asc",
            "filter": {
                "cutoff_from": cutoff_from,
                "cutoff_to": cutoff_to,
            },
            "limit": limit,
            "offset": offset,
            "with": {"analytics_data": False, "financial_data": False},
        }
        data = self._post("/v4/posting/fbs/unfulfilled/list", body)
        result = (data or {}).get("result", data or {})
        if isinstance(result, dict):
            if "postings" not in result:
                # Явных альтернативных имён поля в доках не нашлось (в
                # отличие от warehouses/orders) — на всякий случай тоже
                # проверяем пару правдоподобных вариантов, а не только
                # "postings", прежде чем сдаться на пустой список.
                alt = result.get("items") or result.get("orders")
                if alt is not None:
                    result = {**result, "postings": alt}
            return result
        if isinstance(result, list):
            return {"postings": result}
        return {}

    def get_unfulfilled_postings_raw(self, limit: int = 100, offset: int = 0) -> Any:
        """Тот же вызов, что get_unfulfilled_postings, но БЕЗ разбора ответа —
        только для /ozon-diagnostics (см. get_fbs_warehouses_raw выше)."""
        limit = max(1, min(int(limit), 100))
        now = dt.datetime.utcnow()
        cutoff_from = (now - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        cutoff_to = (now + dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
        body = {
            "dir": "asc",
            "filter": {"cutoff_from": cutoff_from, "cutoff_to": cutoff_to},
            "limit": limit,
            "offset": offset,
            "with": {"analytics_data": False, "financial_data": False},
        }
        return self._post("/v4/posting/fbs/unfulfilled/list", body)

    def list_postings(
        self, since_iso: str, to_iso: str, limit: int = 100, offset: int = 0,
        status: str | None = None,
    ) -> dict:
        """Общий список отправлений FBS за период (для сверки статусов/отмен
        и для разовой догрузки истории — аналог WB /orders + /orders/status
        в одном месте, т.к. Ozon отдаёт статус сразу в самом списке).

        Было /v3/posting/fbs/list — не проверено вживую (до этого не
        дошли), но тот же раздел и тот же паттерн устаревания, что и у
        unfulfilled/list выше, поэтому заменено на /v4/… заранее. Заодно
        снижен default limit (1000 -> 100) по аналогии с unfulfilled/list
        выше — у v4-методов этого раздела похоже общий потолок в 100."""
        limit = max(1, min(int(limit), 100))
        filt: dict = {"since": since_iso, "to": to_iso}
        if status:
            filt["status"] = status
        body = {
            "dir": "asc",
            "filter": filt,
            "limit": limit,
            "offset": offset,
            "with": {"analytics_data": False, "financial_data": False},
        }
        data = self._post("/v4/posting/fbs/list", body)
        return (data or {}).get("result", {})

    # -------------------------------------------------------- FBO-поставки
    def list_supply_orders(self, states: list[str] | None = None, limit: int = 100) -> list:
        """ID поставок FBO (по умолчанию — все текущие состояния; передайте
        states, если понадобится сузить, например только подтверждённые).

        Было /v2/supply-order/list — живой ответ Ozon: 404 page not found.
        Заменено на /v3/supply-order/list (актуальный метод раздела
        «Доставка FBO» в официальных доках). Название поля с ID поставок в
        ответе v3 не подтверждено живым запросом — на случай, если Ozon
        переименовал его (или стал сразу отдавать список объектов вместо
        списка чисел), разбор ответа сделан терпимым к обоим вариантам.

        ВТОРАЯ ПРАВКА (после /ozon-diagnostics): live-ответ показал ошибку
        "SupplyOrderListRequest.Limit: value must be inside range [1, 100]"
        — то есть Ozon ждёт поле limit ПРЯМО в теле запроса, а не вложенным
        в paging, как было раньше (тогда до сервера доходил limit=0 по
        умолчанию, отсюда и ошибка). Исправлено на плоскую структуру.

        ТРЕТЬЯ ПРАВКА (после /ozon-diagnostics): следующий live-ответ —
        "SupplyOrderListRequest.SortBy: value must not be in list [0]".
        Это protobuf-enum поле sort_by, и 0 — его запрещённое значение по
        умолчанию (когда поле вообще не передано, Ozon сам подставляет 0).
        Названия конкретных допустимых значений enum'а нигде подтвердить не
        удалось (сеть до api-seller.ozon.ru по-прежнему недоступна), поэтому
        передаю просто следующее по порядку значение (1) — это стандартный
        способ обойти "нельзя 0" у protobuf-enum'ов, когда не известно
        точное имя. Если и это не то значение — правильный ответ будет
        видно прямо в следующей ошибке от /ozon-diagnostics (Ozon обычно
        подсказывает допустимый диапазон/список в самом сообщении).

        ЧЕТВЁРТАЯ ПРАВКА (после /ozon-diagnostics): sort_by=1 эту ошибку
        снял, но вылезла следующая — "SupplyOrderListRequest.Filter: value
        is required". То есть filter обязателен даже при отсутствии states
        (раньше он просто не передавался вовсе) — теперь всегда шлём хотя
        бы пустой объект {}.

        ПЯТАЯ ПРАВКА (после /ozon-diagnostics): пустого filter={} тоже
        оказалось мало — live-ответ: "invalid
        SupplyOrderListRequest_Filter.States: value must contain at least
        1 item(s)". То есть states — обязательный непустой список, даже
        если нужны вообще все поставки. Точные допустимые значения этого
        enum'а нигде подтвердить не удалось (сеть до api-seller.ozon.ru
        недоступна, dev.ozon.ru блокирует автоматический доступ, а
        docs.ozon.ru в браузере не отдала схему метода), поэтому как и с
        sort_by передала просто диапазон небольших целых чисел, включив
        для надёжности и 0.

        ШЕСТАЯ ПРАВКА (после /ozon-diagnostics): live-ответ показал, что
        0 в этом enum'е тоже запрещён — "SupplyOrderListRequest_Filter.
        States[0]: value must not be in list [0]" (то есть именно первый
        элемент списка, 0, забракован — как и в sort_by, 0 здесь тоже
        служебное значение "не задано"). Убрала 0 из списка, оставила
        диапазон 1..9. Названия конкретных состояний по-прежнему не
        подтверждены — если и этого диапазона окажется мало или он
        неточен, следующая ошибка из /ozon-diagnostics это покажет."""
        limit = max(1, min(int(limit), 100))
        body: dict = {
            "limit": limit,
            "sort_by": 1,
            "filter": {"states": states if states else [1, 2, 3, 4, 5, 6, 7, 8, 9]},
        }
        data = self._post("/v3/supply-order/list", body)
        result = (data or {}).get("result", data or {})
        raw_ids = (
            result.get("supply_order_id")
            if isinstance(result, dict) else None
        )
        if raw_ids is None and isinstance(result, dict):
            raw_ids = result.get("supply_order_ids") or result.get("order_ids") or result.get("orders")
        if raw_ids is None:
            raw_ids = []
        # Каждый элемент может оказаться либо просто числом (id), либо
        # целым объектом поставки (тогда берём из него id).
        ids = []
        for item in raw_ids:
            if isinstance(item, dict):
                ids.append(item.get("supply_order_id") or item.get("order_id") or item.get("id"))
            else:
                ids.append(item)
        return [i for i in ids if i is not None]

    def get_supply_orders_info(self, order_ids: list[int]) -> list[dict]:
        """Детали по списку поставок FBO (статус, склад назначения и т.п.).

        Было /v2/supply-order/get — заменено на /v3/supply-order/get вслед
        за /v3/supply-order/list выше (тот же раздел, та же версия API)."""
        if not order_ids:
            return []
        data = self._post("/v3/supply-order/get", {"order_ids": order_ids})
        result = (data or {}).get("result", data or {})
        if isinstance(result, dict):
            return result.get("orders") or result.get("supply_orders") or []
        return result if isinstance(result, list) else []

    def get_supply_orders_info_raw(self, order_ids: list[int]) -> Any:
        """Тот же вызов, что get_supply_orders_info, но БЕЗ разбора ответа —
        только для /ozon-diagnostics (см. get_fbs_warehouses_raw выше)."""
        if not order_ids:
            return None
        return self._post("/v3/supply-order/get", {"order_ids": order_ids})

    def get_supply_bundle(self, bundle_ids: list[str]) -> list[dict]:
        """Состав поставки (товары и количества) по bundle_id — bundle_id
        берётся из ответа get_supply_orders_info (поле каждой поставки,
        см. комментарий в ozon_sync.py на случай, если фактическое имя поля
        в живом ответе окажется другим — сверьте через /ozon-diagnostics).

        Путь /v1/supply-order/bundle остался тем же, но в официальных доках
        это POST с телом, а не GET с query-параметрами, как было раньше —
        исправлено (Ozon мог просто игнорировать query-параметры GET и
        отдавать пустой/некорректный ответ, из-за чего эта часть могла
        молча не работать даже без явной ошибки).

        ВОСЬМАЯ ПРАВКА (после /ozon-diagnostics): live-ответ — "invalid
        GetSupplyOrderBundleRequest.Limit: value must be inside range
        (0, 100]". Как и у /v3/supply-order/list раньше, поле limit
        обязательно и не имеет безопасного значения по умолчанию (0 —
        вне допустимого диапазона). Добавлено limit=100 (по одной
        поставке за раз нам заведомо не нужно больше 100 товарных строк;
        если у Ozon окажется постраничная выдача и товаров больше —
        следующая ошибка/пустой хвост это покажет)."""
        if not bundle_ids:
            return []
        data = self._post("/v1/supply-order/bundle", {"bundle_ids": bundle_ids, "limit": 100})
        result = (data or {}).get("result", data or {})
        if isinstance(result, dict):
            return result.get("items", [])
        return result if isinstance(result, list) else []

    def get_supply_bundle_raw(self, bundle_ids: list[str]) -> Any:
        """Тот же вызов, что get_supply_bundle, но БЕЗ разбора ответа —
        только для /ozon-diagnostics (см. get_fbs_warehouses_raw выше)."""
        if not bundle_ids:
            return None
        return self._post("/v1/supply-order/bundle", {"bundle_ids": bundle_ids, "limit": 100})
