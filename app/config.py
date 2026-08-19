"""Загрузка настроек приложения из переменных окружения (.env)."""
import os
from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


# Путь к файлу SQLite-базы. На Railway/Render примонтируйте постоянный диск
# и укажите путь на нём (например, /data/wb_stock.db), иначе при каждом
# передеплое база будет обнуляться — подробности в README.
DATABASE_PATH = _get("DATABASE_PATH", "./wb_stock.db")

WB_MARKETPLACE_TOKEN = _get("WB_MARKETPLACE_TOKEN")
WB_STATISTICS_TOKEN = _get("WB_STATISTICS_TOKEN")
WB_MARKETPLACE_BASE_URL = _get("WB_MARKETPLACE_BASE_URL", "https://marketplace-api.wildberries.ru")
WB_STATISTICS_BASE_URL = _get("WB_STATISTICS_BASE_URL", "https://statistics-api.wildberries.ru")

SYNC_INTERVAL_MINUTES = int(_get("SYNC_INTERVAL_MINUTES", "20"))

SECRET_KEY = _get("SECRET_KEY", "dev-only-insecure-key")

ADMIN_USERNAME = _get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = _get("ADMIN_PASSWORD", "admin")
