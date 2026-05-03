"""
Yandex Wordstat через Yandex Cloud AI Studio Search API v2.

Используется агентом 1 (семантика) при подборе темы для проверки частотности
ключевых слов и фильтрации низкочастотных тем (<100 запросов/мес).

Также читается ботом для показа частотности в Telegram-уведомлениях.

ENV переменные:
    YANDEX_CLOUD_API_KEY    - API-ключ от сервисного аккаунта в AI Studio (ОБЯЗАТЕЛЬНО)
    YANDEX_CLOUD_FOLDER_ID  - ID папки в Yandex Cloud (ОБЯЗАТЕЛЬНО)
    WORDSTAT_REGION_CODE    - geoId региона (по умолчанию 225 = Россия)
    WORDSTAT_CACHE_TTL_DAYS - срок жизни кэша (по умолчанию 30 дней;
                              Wordstat обновляется раз в месяц, чаще нет смысла)

Кэш: data/wordstat_cache.json. Хранит для каждого ключа:
    - frequency: общее число показов в месяц
    - top_requests: до 10 связанных запросов с частотами
    - cached_at: дата кэширования

Запуск как скрипта:
    python -m tools.wordstat "банкротство физического лица"
    python -m tools.wordstat --batch "ключ1" "ключ2" "ключ3"
"""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

# Загружаем .env из корня проекта - иначе при запуске как `python -m tools.wordstat`
# переменные окружения окажутся пустыми. На сервере ENV подставляет Cloud Apps,
# load_dotenv() просто ничего не найдёт и тихо пропустит.
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

log = logging.getLogger("wordstat")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = PROJECT_ROOT / "data" / "wordstat_cache.json"

# Yandex Cloud Search API v2 - Wordstat
API_BASE = "https://searchapi.api.cloud.yandex.net/v2/wordstat"
TOP_REQUESTS_URL = f"{API_BASE}/topRequests"

DEFAULT_REGION = int(os.getenv("WORDSTAT_REGION_CODE", "225"))  # 225 = Россия
CACHE_TTL_DAYS = int(os.getenv("WORDSTAT_CACHE_TTL_DAYS", "30"))
REQUEST_TIMEOUT_SEC = 15
RPS_DELAY_SEC = 0.2  # ~5 RPS, чтобы не превысить лимит API


@dataclass
class WordstatResult:
    keyword: str
    frequency: int                     # число показов в месяц по основному запросу
    top_requests: list[dict]           # [{"phrase": "...", "count": N}, ...]
    region: int
    cached: bool                       # True если из кэша
    cached_at: Optional[str] = None    # ISO дата
    error: Optional[str] = None        # текст ошибки, если запрос упал

    @property
    def is_low_frequency(self) -> bool:
        """Низкочастотная тема: <100 запросов/мес. Используется агентом 1 для фильтра."""
        return self.frequency < 100


# ============ КЭШ ============

def _read_cache() -> dict:
    if not CACHE_PATH.exists():
        return {}
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_cache(cache: dict) -> None:
    CACHE_PATH.parent.mkdir(exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _cache_key(keyword: str, region: int) -> str:
    return f"{region}:{keyword.strip().lower()}"


def _is_cache_fresh(cached_at_iso: str) -> bool:
    try:
        cached = datetime.fromisoformat(cached_at_iso)
    except ValueError:
        return False
    return (datetime.now() - cached) < timedelta(days=CACHE_TTL_DAYS)


# ============ API ============

def _api_credentials() -> tuple[Optional[str], Optional[str]]:
    api_key = os.getenv("YANDEX_CLOUD_API_KEY", "").strip()
    folder_id = os.getenv("YANDEX_CLOUD_FOLDER_ID", "").strip()
    return api_key or None, folder_id or None


def _api_request(keyword: str, region: int) -> Optional[dict]:
    """
    Вызывает /v2/wordstat/topRequests и возвращает сырой JSON-ответ.
    None при любой ошибке (логируется).
    """
    api_key, folder_id = _api_credentials()
    if not api_key or not folder_id:
        log.warning("YANDEX_CLOUD_API_KEY или YANDEX_CLOUD_FOLDER_ID не заданы")
        return None

    try:
        import httpx
    except ImportError:
        log.exception("httpx не установлен")
        return None

    payload = {
        "folderId": folder_id,
        "phrase": keyword,
        "geoId": [region],
    }
    headers = {
        "Authorization": f"Api-Key {api_key}",
        "Content-Type": "application/json",
    }

    try:
        resp = httpx.post(
            TOP_REQUESTS_URL,
            json=payload,
            headers=headers,
            timeout=REQUEST_TIMEOUT_SEC,
        )
    except Exception as exc:
        log.exception("Wordstat API: сеть не отвечает (%s)", exc)
        return None

    if resp.status_code != 200:
        log.warning(
            "Wordstat API: status=%d body=%s",
            resp.status_code, resp.text[:300],
        )
        return None

    try:
        return resp.json()
    except Exception:
        log.exception("Wordstat API: невалидный JSON в ответе")
        return None


def _parse_response(raw: dict, keyword: str, region: int) -> WordstatResult:
    """
    Парсит ответ Yandex Cloud Search API v2.
    Структура (упрощённо):
    {
      "topRequests": [{"phrase": "...", "count": "1234"}, ...],
      "totalCount": "5678"   # или count первого элемента
    }
    Числа в proto приходят как строки (int64-quirk).
    """
    if not raw:
        return WordstatResult(
            keyword=keyword, frequency=0, top_requests=[], region=region,
            cached=False, error="empty_response",
        )

    # Общая частотность по фразе
    frequency = 0
    for field in ("totalCount", "count", "shows"):
        if field in raw:
            try:
                frequency = int(str(raw[field]))
                break
            except (TypeError, ValueError):
                pass

    # Top requests - список похожих запросов
    top_raw = raw.get("topRequests") or raw.get("items") or []
    top_requests = []
    for item in top_raw[:10]:
        try:
            phrase = item.get("phrase") or item.get("text") or ""
            count = int(str(item.get("count") or item.get("shows") or 0))
            if phrase:
                top_requests.append({"phrase": phrase, "count": count})
        except Exception:
            continue

    # Если frequency=0, но есть top_requests - возможно API вернул только похожие
    # без общей частоты. Тогда берём максимум из top_requests как оценку.
    if frequency == 0 and top_requests:
        # Часто первый top-запрос совпадает с самой фразой = это и есть основная частота
        for item in top_requests:
            if item["phrase"].lower().strip() == keyword.lower().strip():
                frequency = item["count"]
                break
        if frequency == 0:
            frequency = max((r["count"] for r in top_requests), default=0)

    return WordstatResult(
        keyword=keyword,
        frequency=frequency,
        top_requests=top_requests,
        region=region,
        cached=False,
        cached_at=datetime.now().isoformat(timespec="seconds"),
    )


# ============ ОСНОВНАЯ ФУНКЦИЯ ============

def get_frequency(keyword: str, region: int = DEFAULT_REGION,
                  use_cache: bool = True) -> WordstatResult:
    """
    Главный публичный метод. Сначала смотрит в кэш (если разрешено),
    при промахе - дёргает API, складывает результат в кэш.

    Возвращает WordstatResult всегда, даже при ошибке - тогда frequency=0
    и error содержит причину. Код-вызывающий должен проверять result.error
    перед принятием решений.
    """
    keyword = (keyword or "").strip()
    if not keyword:
        return WordstatResult(
            keyword="", frequency=0, top_requests=[], region=region,
            cached=False, error="empty_keyword",
        )

    # Кэш
    cache = _read_cache() if use_cache else {}
    key = _cache_key(keyword, region)
    if use_cache and key in cache:
        entry = cache[key]
        cached_at = entry.get("cached_at", "")
        if cached_at and _is_cache_fresh(cached_at):
            return WordstatResult(
                keyword=keyword,
                frequency=entry.get("frequency", 0),
                top_requests=entry.get("top_requests", []),
                region=region,
                cached=True,
                cached_at=cached_at,
            )

    # API
    raw = _api_request(keyword, region)
    if raw is None:
        return WordstatResult(
            keyword=keyword, frequency=0, top_requests=[], region=region,
            cached=False, error="api_request_failed",
        )

    result = _parse_response(raw, keyword=keyword, region=region)

    # Сохраняем в кэш только успешные ответы
    if not result.error:
        cache[key] = {
            "frequency": result.frequency,
            "top_requests": result.top_requests,
            "cached_at": result.cached_at,
        }
        try:
            _write_cache(cache)
        except OSError:
            log.exception("Не смог записать wordstat cache")

    return result


def get_frequencies_batch(keywords: list[str],
                          region: int = DEFAULT_REGION) -> list[WordstatResult]:
    """
    Прогон списка ключей. Между запросами пауза для соблюдения rate-limit (5 RPS).
    Кэш используется по умолчанию.
    """
    results = []
    for i, kw in enumerate(keywords):
        if i > 0:
            time.sleep(RPS_DELAY_SEC)
        results.append(get_frequency(kw, region=region))
    return results


def summarize_for_meta(main_keyword: str, secondary_keywords: list[str],
                       region: int = DEFAULT_REGION) -> dict:
    """
    Краткая сводка для записи в brief.json / meta.json.
    Возвращает структуру:
    {
      "frequency_main": 1234,
      "frequency_secondary": [...],
      "frequency_total": 5678,
      "is_low_frequency": false,
      "checked_at": "2026-05-03T..."
    }
    """
    main_res = get_frequency(main_keyword, region=region)
    sec_results = get_frequencies_batch(secondary_keywords, region=region)

    sec_freqs = [
        {"keyword": r.keyword, "frequency": r.frequency}
        for r in sec_results
    ]
    total = main_res.frequency + sum(r.frequency for r in sec_results)

    return {
        "frequency_main": main_res.frequency,
        "frequency_secondary": sec_freqs,
        "frequency_total": total,
        "is_low_frequency": main_res.frequency < 100 and total < 200,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "region": region,
        "main_error": main_res.error,
    }


# ============ CLI ============

def _cli():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    args = sys.argv[1:]
    if not args:
        print("Usage:")
        print("  python -m tools.wordstat 'банкротство физического лица'")
        print("  python -m tools.wordstat --batch 'ключ1' 'ключ2' 'ключ3'")
        print("  python -m tools.wordstat --summary 'главный ключ' 'второй' 'третий'")
        sys.exit(1)

    if args[0] == "--batch":
        results = get_frequencies_batch(args[1:])
        for r in results:
            print(json.dumps(asdict(r), ensure_ascii=False))
    elif args[0] == "--summary":
        if len(args) < 2:
            print("--summary требует минимум один ключ", file=sys.stderr)
            sys.exit(1)
        summary = summarize_for_meta(args[1], args[2:])
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        result = get_frequency(args[0])
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    _cli()
