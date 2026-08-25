"""
Клиент для Wildberries API.

ВАЖНО перед боевым запуском: WB периодически меняет форматы методов API.
Прежде чем подключать реальный токен, сверьте пути и формат тела запроса
с актуальным Swagger на https://dev.wildberries.ru/en/swagger/orders-fbs и
https://openapi.wildberries.ru/marketplace/api/en/ — все обращения к сети
в этом файле нарочно собраны в одном месте, чтобы такую сверку было легко
сделать за 10 минут, не копаясь по всему проекту.

Используются два разных токена (можно выпустить один токен сразу с двумя
скоупами в личном кабинете: Настройки → Доступ к API):
  - Marketplace API — склады, заказы/сборочные задания FBS (только чтение)
  - Statistics API  — фактические продажи/возвраты, для сверки (только чтение)

Приложение НИЧЕГО не пишет обратно в WB — только читает заказы и статусы.
Управление тем, что видят покупатели на карточке товара (доступность,
остатки на витрине), полностью остаётся на стороне каждого склада
самостоятельно, вне этого приложения. Поэтому токен для него достаточно
выпускать с галочкой «Только на чтение».
"""
import json
import time
from typing import Any

import httpx

from app.config import (
    WB_MARKETPLACE_TOKEN, WB_STATISTICS_TOKEN,
    WB_MARKETPLACE_BASE_URL, WB_STATISTICS_BASE_URL,
)


class WBApiError(Exception):
    pass


class WBClient:
    def __init__(
        self,
        marketplace_token: str = WB_MARKETPLACE_TOKEN,
        statistics_token: str = WB_STATISTICS_TOKEN,
        marketplace_base: str = WB_MARKETPLACE_BASE_URL,
        statistics_base: str = WB_STATISTICS_BASE_URL,
        timeout: float = 30.0,
        max_retries: int = 3,
    ):
        self.marketplace_token = marketplace_token
        self.statistics_token = statistics_token
        self.marketplace_base = marketplace_base.rstrip("/")
        self.statistics_base = statistics_base.rstrip("/")
        self.timeout = timeout
        self.max_retries = max_retries

    # ---------------------------------------------------------------- utils
    def _request(self, method: str, url: str, token: str, **kwargs) -> Any:
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = token
        last_error = None
        for attempt in range(1, self.max_retries + 1):
            try:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.request(method, url, headers=headers, **kwargs)
                if resp.status_code == 429:
                    # Превышен лимит запросов — ждём и пробуем снова
                    wait = 2 ** attempt
                    time.sleep(wait)
                    continue
                resp.raise_for_status()
                if not resp.content:
                    return None
                return resp.json()
            except httpx.HTTPStatusError as e:
                last_error = WBApiError(
                    f"{method} {url} -> {e.response.status_code}: {e.response.text[:500]}"
                )
                # 4xx (кроме 429) — повторять бессмысленно, это ошибка запроса
                if 400 <= e.response.status_code < 500 and e.response.status_code != 429:
                    raise last_error
                time.sleep(1.5 * attempt)
            except httpx.HTTPError as e:
                last_error = WBApiError(f"{method} {url} -> сетевая ошибка: {e}")
                time.sleep(1.5 * attempt)
        raise last_error or WBApiError(f"{method} {url} -> не удалось выполнить запрос")

    # ------------------------------------------------------- Marketplace API
    def get_warehouses(self) -> list[dict]:
        """Список складов продавца, зарегистрированных в WB."""
        url = f"{self.marketplace_base}/api/v3/warehouses"
        data = self._request("GET", url, self.marketplace_token)
        return data or []

    def get_new_orders(self) -> list[dict]:
        """Новые сборочные задания (заказы), ещё не взятые в работу."""
        url = f"{self.marketplace_base}/api/v3/orders/new"
        data = self._request("GET", url, self.marketplace_token)
        return (data or {}).get("orders", [])

    def get_orders_status(self, order_ids: list[int]) -> list[dict]:
        """Статусы заданий по списку ID заказов. У WB это поле supplierStatus
        (new/confirm/complete/cancel) — общего поля "status" в ответе нет."""
        if not order_ids:
            return []
        url = f"{self.marketplace_base}/api/v3/orders/status"
        data = self._request("POST", url, self.marketplace_token, json={"orders": order_ids})
        return (data or {}).get("orders", [])

    def get_orders(self, limit: int = 1000, next_cursor: int = 0) -> dict:
        """Общий список заказов (для дозагрузки истории)."""
        url = f"{self.marketplace_base}/api/v3/orders"
        params = {"limit": limit, "next": next_cursor}
        return self._request("GET", url, self.marketplace_token, params=params) or {}

    # ------------------------------------------------------- Statistics API
    def get_sales(self, date_from: str) -> list[dict]:
        """
        Фактические продажи/возвраты для сверки (dateFrom в формате
        YYYY-MM-DDTHH:MM:SS). Используется как контрольная сверка,
        а не основной источник списания остатков (там данные приходят с
        задержкой до нескольких часов).
        """
        url = f"{self.statistics_base}/api/v1/supplier/sales"
        params = {"dateFrom": date_from}
        data = self._request("GET", url, self.statistics_token, params=params)
        return data or []
