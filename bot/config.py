"""
Конфиг бота. Все секреты - из переменных окружения.

Обязательные:
    TG_BOT_TOKEN          - токен от @BotFather
    TG_ALLOWED_CHAT_IDS   - chat_id заказчика (через запятую если несколько),
                            числа из @userinfobot

Опциональные:
    BOT_PREVIEW_TOKEN     - произвольная строка для подписи preview-ссылок.
                            Если не задана - сгенерирована при первом старте
                            и сохранена в data/bot_state.json.
    GROQ_API_KEY          - для транскрипции голосовых через Whisper.
                            Если не задан - голосовые игнорируются.
    TEXTRU_USER_KEY       - для проверки уникальности.
                            Если не задан - метрика не показывается.
    PUBLIC_BASE_URL       - публичный адрес сайта (https://pravo.shop).
                            Используется в preview-ссылках в TG.
                            По умолчанию: https://pravo.shop
    BOT_WATCH_INTERVAL    - секунды между сканированиями drafts/.
                            По умолчанию: 60

Claude Code:
    Бот ожидает что бинарник `claude` доступен в PATH.
    Авторизация Claude Code должна быть выполнена заранее (через
    ANTHROPIC_API_KEY в env или через скопированный ~/.claude.json).
"""

from __future__ import annotations

import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

# Загружаем .env из корня проекта (bankrotstvo/.env) ДО чтения os.getenv ниже.
# Иначе при импорте config из bot/main.py переменные окажутся пустыми.
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
load_dotenv(_PROJECT_ROOT / ".env")

# Корень проекта (bankrotstvo/).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = PROJECT_ROOT / "drafts"
ARTICLES_DIR = PROJECT_ROOT / "articles"
DATA_DIR = PROJECT_ROOT / "data"
STATE_FILE = DATA_DIR / "bot_state.json"


def _env_str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_chat_ids(name: str) -> list[int]:
    """Парсит '123,456,789' → [123, 456, 789]. Пустые/невалидные пропускает."""
    raw = _env_str(name)
    if not raw:
        return []
    out = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(int(part))
        except ValueError:
            continue
    return out


# === Telegram ===
TG_BOT_TOKEN = _env_str("TG_BOT_TOKEN")
TG_ALLOWED_CHAT_IDS: list[int] = _env_chat_ids("TG_ALLOWED_CHAT_IDS")

# === Внешние API (опциональные) ===
GROQ_API_KEY = _env_str("GROQ_API_KEY")
TEXTRU_USER_KEY = _env_str("TEXTRU_USER_KEY")

# === Сайт ===
PUBLIC_BASE_URL = _env_str("PUBLIC_BASE_URL", "https://pravo.shop").rstrip("/")

# === Watcher ===
BOT_WATCH_INTERVAL_SEC = _env_int("BOT_WATCH_INTERVAL", 60)

# === Preview-токен ===
# Если не задан в env - генерим один раз и кладём в state. Так после рестарта
# ссылки остаются валидными.
_PREVIEW_TOKEN_FROM_ENV = _env_str("BOT_PREVIEW_TOKEN")


def get_preview_token(state_token: str | None = None) -> str:
    """
    Приоритет: env → state → новый случайный.
    state_token = тот, что лежит в data/bot_state.json (если уже сгенерили).
    """
    if _PREVIEW_TOKEN_FROM_ENV:
        return _PREVIEW_TOKEN_FROM_ENV
    if state_token:
        return state_token
    return secrets.token_urlsafe(24)


# === Категории: технический id → отображаемое имя ===
# Зафиксировано заказчиком (фидбек): в видимом тексте полные названия.
CATEGORY_LABELS = {
    "fiz": "Физические лица",
    "yur": "Юридические лица",
    "vzysk": "Взыскание задолженности",
    "news": "Новости",
}


def category_label(cat: str) -> str:
    return CATEGORY_LABELS.get(cat, cat)


# === Validation на старте ===
def validate_config() -> list[str]:
    """Возвращает список ошибок конфига. Пустой список = всё ок."""
    errors = []
    if not TG_BOT_TOKEN:
        errors.append("TG_BOT_TOKEN не задан (env переменная). Получите токен у @BotFather.")
    if not TG_ALLOWED_CHAT_IDS:
        errors.append(
            "TG_ALLOWED_CHAT_IDS не задан. Узнайте chat_id у @userinfobot и положите в env."
        )
    if not DRAFTS_DIR.exists():
        errors.append(f"Папка drafts/ не найдена: {DRAFTS_DIR}")
    return errors
