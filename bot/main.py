"""
Точка входа Telegram-бота.

Запуск:
    python -m bot.main

Бот стартует с polling (long-poll) и параллельно крутит фоновую задачу,
которая каждые BOT_WATCH_INTERVAL сек сканирует drafts/ на новые статьи
и шлёт уведомления заказчику.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from dotenv import load_dotenv
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from . import handlers, messages, state, watcher
from .config import (
    BOT_WATCH_INTERVAL_SEC,
    TG_ALLOWED_CHAT_IDS,
    TG_BOT_TOKEN,
    validate_config,
)


def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    # Заглушаем особо болтливые логгеры aiogram.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


log = logging.getLogger("bot")


async def watch_loop(bot: Bot):
    """Фоновая задача: сканирует drafts/, шлёт уведомления о новых."""
    while True:
        try:
            await _watch_iteration(bot)
        except Exception as e:
            log.exception("watch_loop error: %s", e)
        await asyncio.sleep(BOT_WATCH_INTERVAL_SEC)


async def _watch_iteration(bot: Bot):
    new_drafts = watcher.scan_for_new_drafts()
    if not new_drafts:
        return

    log.info("Найдено новых драфтов: %d", len(new_drafts))
    token = state.get_preview_token_from_state()

    for draft in new_drafts:
        # Регистрируем в state.
        watcher.register_draft(draft)

        # Шлём уведомление каждому в whitelist.
        text = messages.new_draft_notification(
            slug=draft["slug"],
            category=draft["category"],
            title=draft["title"],
            version=draft["version"],
            char_count=draft["char_count"],
            token=token,
            uniqueness_pct=None,  # text.ru добавим позже
        )

        for chat_id in TG_ALLOWED_CHAT_IDS:
            try:
                msg = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=handlers.review_keyboard(draft["slug"]),
                    disable_web_page_preview=False,
                )
                # Запоминаем id сообщения для последующего edit'а.
                state.set_tg_message(
                    draft["slug"],
                    chat_id=chat_id,
                    message_id=msg.message_id,
                )
            except Exception as e:
                log.error("Не смог отправить уведомление %s: %s", chat_id, e)


async def main_async():
    load_dotenv()
    setup_logging()

    errors = validate_config()
    if errors:
        log.error("Конфиг невалиден:")
        for e in errors:
            log.error("  - %s", e)
        sys.exit(1)

    bot = Bot(
        token=TG_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(handlers.router)

    log.info("Бот стартует. Whitelist chat_id: %s", TG_ALLOWED_CHAT_IDS or "ПУСТО (все)")
    log.info("Watcher: интервал %d сек", BOT_WATCH_INTERVAL_SEC)

    # Запускаем watcher параллельно с polling.
    watcher_task = asyncio.create_task(watch_loop(bot))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watcher_task.cancel()
        await bot.session.close()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")


if __name__ == "__main__":
    main()
