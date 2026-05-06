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

from . import handlers, messages, publisher, queue as action_queue, state, watcher
from .config import (
    BOT_WATCH_INTERVAL_SEC,
    DATA_DIR,
    TG_ALLOWED_CHAT_IDS,
    TG_BOT_TOKEN,
    validate_config,
)
from .fsm_storage import JsonFileStorage

# Как часто проверять очередь на готовность к выполнению.
# 20s = быстро отреагировать когда scheduler закончил, но не сжигать CPU.
QUEUE_CHECK_INTERVAL_SEC = 20


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


async def queue_loop(bot: Bot):
    """
    Фоновая задача: обрабатывает отложенные publish-действия из очереди
    после того как scheduler закончил слот (LOCK_FILE снят).

    Важно: pop+publish делается ПО ОДНОМУ за тик. После каждой публикации
    публикатор делает git push, что триггерит redeploy Cloud Apps. Контейнер
    может перезапуститься — оставшиеся в очереди элементы переживут это,
    потому что queue хранится на диске. После перезапуска новый бот
    подхватит очередь и продолжит.
    """
    while True:
        try:
            await _queue_iteration(bot)
        except Exception as e:
            log.exception("queue_loop error: %s", e)
        await asyncio.sleep(QUEUE_CHECK_INTERVAL_SEC)


async def _queue_iteration(bot: Bot):
    active, _age = action_queue.is_scheduler_active()
    if active:
        return

    # ВАЖНО: pop_next ДО publisher.publish.
    # Это меняет bot_state.json на диске (удаляет элемент из pending_actions).
    # Когда publisher.publish ниже сделает git commit+push, в коммит уйдёт
    # bot_state.json с уже уменьшенной очередью. После редеплоя Cloud Apps
    # новый контейнер прочитает bot_state.json и НЕ будет повторно
    # публиковать тот же slug.
    #
    # Если publisher упадёт по флакающей причине (network, fal.ai, OOM на
    # генерации картинки) — возвращаем item обратно в очередь с инкрементом
    # счётчика attempts. После 3 попыток отказываемся и сообщаем заказчику.
    item = action_queue.pop_next()
    if item is None:
        return

    action = item.get("action")
    if action != "publish":
        log.warning("Неизвестный action в очереди: %r — пропускаю", action)
        return

    slug = item.get("slug")
    version = item.get("version")
    chat_id = item.get("chat_id")
    if not slug:
        log.warning("publish-задача без slug: %r — пропускаю", item)
        return

    review = state.get_review(slug)
    if not review:
        log.warning("publish-задача для несуществующего review: %s — пропускаю", slug)
        return
    if review.get("status") == "published":
        log.info("publish %s уже выполнен (status=published) — пропускаю", slug)
        return
    if review.get("status") == "rejected":
        log.info("publish %s отклонён за время очереди — пропускаю", slug)
        return

    title = review.get("title", slug)
    log.info("Queue: запускаю отложенный publish slug=%s", slug)

    if chat_id:
        try:
            await bot.send_message(
                chat_id=chat_id,
                text=(f"▶️ Очередь: начинаю публикацию <b>«{title}»</b>…\n"
                      "30-60 секунд."),
                parse_mode="HTML",
            )
        except Exception as exc:
            log.warning("Не смог отправить сообщение о старте очереди: %s", exc)

    result = await asyncio.to_thread(
        publisher.publish, slug=slug, version=version,
    )

    # Лимит автоматических retry для одной задачи. Сетевые/OOM-сбои —
    # верни в очередь, заказчик не должен жать кнопку из-за них. После
    # MAX_PUBLISH_ATTEMPTS отдаём заказчику решение (нажать ещё раз / разобрать).
    MAX_PUBLISH_ATTEMPTS = 3

    if not result.success:
        attempts = int(item.get("attempts", 0)) + 1
        if attempts < MAX_PUBLISH_ATTEMPTS:
            log.warning(
                "publish %s упал на attempt=%d/%d (error=%r) — возвращаю в очередь",
                slug, attempts, MAX_PUBLISH_ATTEMPTS,
                (result.error or "")[:200],
            )
            requeued = dict(item)
            requeued["attempts"] = attempts
            requeued["last_error"] = (result.error or "")[:300]
            # Не используем enqueue_publish (он дедупит) — пишем напрямую
            # как retry с сохранением chat_id и версии.
            state.add_pending_action(requeued)
            if chat_id:
                try:
                    await bot.send_message(
                        chat_id=chat_id,
                        text=(
                            f"⚠️ <b>«{title}»</b>: попытка {attempts}/{MAX_PUBLISH_ATTEMPTS} "
                            f"не удалась.\n\nПовторю автоматически через ~30 сек.\n\n"
                            f"<code>{(result.error or 'неизвестная')[:300]}</code>"
                        ),
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    log.warning("Не смог отправить retry-уведомление: %s", exc)
            return

        # Исчерпали лимит попыток — оставляем заказчику ручное решение
        log.error(
            "publish %s провалился после %d попыток, бросаю",
            slug, MAX_PUBLISH_ATTEMPTS,
        )
        if chat_id:
            try:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        f"❌ <b>Не удалось опубликовать «{title}»</b>\n\n"
                        f"После {MAX_PUBLISH_ATTEMPTS} попыток. "
                        f"Последняя ошибка: <code>{(result.error or 'неизвестная')[:500]}</code>\n\n"
                        "Статья осталась в drafts/. Нажмите ✅ Опубликовать ещё раз "
                        "если хотите попробовать снова."
                    ),
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.warning("Не смог отправить final-failure уведомление: %s", exc)
        return

    if not chat_id:
        return

    try:

        cover_line = ""
        if result.cover_url:
            cover_line = f"\n🖼 <a href=\"{result.cover_url}\">Обложка</a>"

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>Опубликовано из очереди: «{title}»</b>\n\n"
                f"🔗 <a href=\"{result.public_url}\">{result.public_url}</a>"
                f"{cover_line}"
            ),
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
    except Exception as exc:
        log.warning("Не смог отправить итоговое сообщение очереди: %s", exc)


async def _watch_iteration(bot: Bot):
    new_drafts = watcher.scan_for_new_drafts()
    if not new_drafts:
        return

    log.info("Найдено новых драфтов: %d", len(new_drafts))
    token = state.get_preview_token_from_state()

    for draft in new_drafts:
        # Регистрируем в state.
        watcher.register_draft(draft)
        # Wordstat-числа сохраняем в state, чтобы потом publish-сообщение могло
        # их прочитать без повторного парсинга meta.json.
        if draft.get("wordstat_main") is not None or draft.get("wordstat_total") is not None:
            state.upsert_review(draft["slug"], {
                "wordstat_main": draft.get("wordstat_main"),
                "wordstat_total": draft.get("wordstat_total"),
            })

        # Шлём уведомление каждому в whitelist.
        text = messages.new_draft_notification(
            slug=draft["slug"],
            category=draft["category"],
            title=draft["title"],
            version=draft["version"],
            char_count=draft["char_count"],
            token=token,
            uniqueness_pct=None,  # text.ru добавим позже
            wordstat_main=draft.get("wordstat_main"),
            wordstat_total=draft.get("wordstat_total"),
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
    # JsonFileStorage вместо MemoryStorage: state переживает рестарт контейнера.
    # Cloud Apps делает редеплой при каждом push, MemoryStorage обнулялся бы
    # каждый раз — двухшаговые flow ("Правки", "Отклонить") не работали бы
    # если юзер начал диалог до редеплоя, а закончил после.
    fsm_storage = JsonFileStorage(DATA_DIR / ".fsm_state.json")
    dp = Dispatcher(storage=fsm_storage)
    dp.include_router(handlers.router)

    log.info("Бот стартует. Whitelist chat_id: %s", TG_ALLOWED_CHAT_IDS or "ПУСТО (все)")
    log.info("Watcher: интервал %d сек", BOT_WATCH_INTERVAL_SEC)

    # Очищаем data/.action_queue.json от прошлой версии очереди (если остался).
    # Сейчас очередь живёт в data/bot_state.json:pending_actions, который
    # коммитится в git и переживает редеплои Cloud Apps.
    if action_queue.cleanup_legacy_file():
        log.info("Удалён legacy-файл очереди — теперь используется bot_state.json")

    pending = action_queue.peek()
    if pending:
        log.info("При старте обнаружено %d отложенных действий — обработаю в queue_loop",
                 len(pending))

    # Запускаем watcher и queue processor параллельно с polling.
    watcher_task = asyncio.create_task(watch_loop(bot))
    queue_task = asyncio.create_task(queue_loop(bot))

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watcher_task.cancel()
        queue_task.cancel()
        await bot.session.close()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")


if __name__ == "__main__":
    main()
