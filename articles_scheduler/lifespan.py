"""
Интеграция articles_scheduler в lifecycle FastAPI.

Подключается в main.py через @app.on_event("startup") / "shutdown" - те же
обработчики, в которых уже стартует Telegram-бот.

Поведение:
- Если SCHEDULER_ENABLED != true (по умолчанию false) - не стартуем,
  логируем и выходим. Это безопасный дефолт: даже если случайно
  задеплоить с этим модулем без env, scheduler молчит.
- Если стартуем - APScheduler в asyncio-режиме с одним job'ом
  `run_one_article` по интервалу SCHEDULER_INTERVAL_MINUTES.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger("scheduler.lifespan")

_scheduler = None  # type: ignore[var-annotated]


def _is_truthy(val: str | None) -> bool:
    return (val or "").strip().lower() in ("1", "true", "yes", "on")


def start_articles_scheduler() -> None:
    """Стартует APScheduler. Безопасно вызывать повторно - вернётся без действий."""
    global _scheduler

    if not _is_truthy(os.getenv("SCHEDULER_ENABLED")):
        log.info("Articles scheduler отключён (SCHEDULER_ENABLED != true)")
        return

    if _scheduler is not None:
        log.warning("Articles scheduler уже запущен, пропуск")
        return

    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.interval import IntervalTrigger
    except ImportError:
        log.exception("Articles scheduler: apscheduler не установлен, пропуск")
        return

    try:
        from articles_scheduler.runner import run_one_article
    except Exception:
        log.exception("Articles scheduler: не смог импортировать runner, пропуск")
        return

    interval = int(os.getenv("SCHEDULER_INTERVAL_MINUTES", "144"))
    tz = os.getenv("SCHEDULER_TZ", "Europe/Moscow")
    articles_per_day = int(os.getenv("ARTICLES_PER_DAY", "1"))
    rotation = os.getenv("ROTATION_ORDER", "fiz,yur,vzysk,news")

    scheduler = AsyncIOScheduler(timezone=tz)
    scheduler.add_job(
        run_one_article,
        IntervalTrigger(minutes=interval),
        id="articles_one_tick",
        max_instances=1,        # никогда не запускать параллельно
        coalesce=True,          # если пропустили несколько слотов - выполнить один раз
        misfire_grace_time=300, # допустимая задержка 5 минут
        replace_existing=True,
    )
    scheduler.start()
    _scheduler = scheduler

    log.info(
        "Articles scheduler стартовал: интервал %d мин, tz=%s, лимит %d/день, ротация %s",
        interval, tz, articles_per_day, rotation,
    )


async def stop_articles_scheduler() -> None:
    """Корректно останавливает scheduler. Без ожидания текущих job'ов."""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:
        log.exception("Articles scheduler: ошибка при shutdown")
    _scheduler = None
    log.info("Articles scheduler остановлен")
