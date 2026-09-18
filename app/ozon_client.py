"""
Клиент для Ozon Seller API.

ВАЖНО ПЕРЕД БОЕВЫМ ЗАПУСКОМ, серьёзнее, чем обычно: в отличие от wb_client.py,
пути и поля ниже НЕ сверены с живым ответом Ozon — песочница, в которой
писался этот код, не имеет сетевого доступа к api-seller.ozon.ru (закрыто
политикой прокси), поэтому проверить реальный JSON не удалось. Пути методов и
названия полей взяты из официальной документации и нескольких независимых
open-source клиентов (актуальных на 18.09.2026) и, по всем источникам,
согласуются друг с другом — но Ozon, как и WB, периодически меняет форматы.

Поэтому первым делом после того, как переменные окружения (OZON_CLIENT_ID,
OZON_API_KEY) появятся на Railway — зайдите на страницу «Ozon» → «Диагностика»
и нажмите «Тестовый запрос» (см. app/main.py, /ozon-diagnostics): она делает
по одному живому вызову каждого метода ниже и показывает сырой ответ Ozon как
есть. Сверьте его с этим файлом, прежде чем полагаться на автосинхронизацию
для реальных остатков — ровно как рекомендовано делать с WB в wb_client.py.

Используются два значения из личного кабинета (Настройки → Seller API):
  - Client-Id — числовой идентификатор кабинета
  - Api-Key   — сам ключ

Приложение (как и для WB) НИЧЕГО не пишет обратно в Ozon — только читает
заказы FBS, поставки FBO и список складов. Управление тем, что видят
покупатели на карточке товара, остаётся вне этого приложения.
"""
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
        виртуальный склад «Ozon FBO», см. app/ozon_sync.py)."""
        data = self._post("/v1/warehouse/list")
        return (data or {}).get("result", [])

    # --------------------------------------------------------- FBS-заказы
    def get_unfulfilled_postings(self, limit: int = 1000, offset: int = 0) -> dict:
        """Отправления FBS, ещё не собранные — аналог WB /orders/new, тот же
        самый безопасный момент для списания остатка."""
        body = {
            "dir": "asc",
            "filter": {},
            "limit": limit,
            "offset": offset,
            "with": {"analytics_data": False, "financial_data": False},
        }
        data = self._post("/v3/posting/fbs/unfulfilled/list", body)
        return (data or {}).get("result", {})

    def list_postings(
        self, since_iso: str, to_iso: str, limit: int = 1000, offset: int = 0,
        status: str | None = None,
    ) -> dict:
        """Общий список отправлений FBS за период (для сверки статусов/отмен
        и для разовой догрузки истории — аналог WB /orders + /orders/status
        в одном месте, т.к. Ozon отдаёт статус сразу в самом списке)."""
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
        data = self._post("/v3/posting/fbs/list", body)
        return (data or {}).get("result", {})

    # -------------------------------------------------------- FBO-поставки
    def list_supply_orders(self, states: list[str] | None = None, limit: int = 100) -> list:
        """ID поставок FBO (по умолчанию — все текущие состояния; передайте
        states, если понадобится сузить, например только подтверждённые)."""
        body: dict = {"paging": {"limit": limit}}
        if states:
            body["filter"] = {"states": states}
        data = self._post("/v2/supply-order/list", body)
        return (data or {}).get("supply_order_id", [])

    def get_supply_orders_info(self, order_ids: list[int]) -> list[dict]:
        """Детали по списку поставок FBO (статус, склад назначения и т.п.)."""
        if not order_ids:
            return []
        data = self._post("/v2/supply-order/get", {"order_ids": order_ids})
        return (data or {}).get("orders", [])

    def get_supply_bundle(self, bundle_ids: list[str]) -> list[dict]:
        """Состав поставки (товары и количества) по bundle_id — bundle_id
        берётся из ответа get_supply_orders_info (поле каждой поставки,
        см. комментарий в ozon_sync.py на случай, если фактическое имя поля
        в живом ответе окажется другим — сверьте через /ozon-diagnostics)."""
        if not bundle_ids:
            return []
        data = self._get("/v1/supply-order/bundle", params={"bundle_ids": bundle_ids})
        return (data or {}).get("items", [])
