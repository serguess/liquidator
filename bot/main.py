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

from pathlib import Path

from . import (
    handlers, messages, notified_sentinel, publisher,
    queue as action_queue, state, watcher,
)
from .config import (
    BATCH_DELIVERY_HOUR,
    BATCH_MAX_PER_DAY,
    BATCH_DELIVERY_INTERVAL_SEC,
    BATCH_DELIVERY_START_AT,
    BATCH_DELIVERY_TZ,
    BOT_WATCH_INTERVAL_SEC,
    DATA_DIR,
    DRAFTS_DIR,
    TG_ALLOWED_CHAT_IDS,
    TG_BOT_TOKEN,
    validate_config,
)
from .fsm_storage import JsonFileStorage

# Как часто проверять очередь на готовность к выполнению.
# 20s = быстро отреагировать когда scheduler закончил, но не сжигать CPU.
QUEUE_CHECK_INTERVAL_SEC = 2


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
                        "Можно попробовать ещё раз — нажмите ✅ Опубликовать."
                    ),
                    parse_mode="HTML",
                )
            except Exception as exc:
                log.warning("Не смог отправить final-failure уведомление: %s", exc)
        return

    if not chat_id:
        return

    try:

        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"✅ <b>Опубликовано из очереди: «{title}»</b>\n\n"
                f"🔗 <a href=\"{result.public_url}\">{result.public_url}</a>"
            ),
            parse_mode="HTML",
            disable_web_page_preview=False,
        )
    except Exception as exc:
        log.warning("Не смог отправить итоговое сообщение очереди: %s", exc)


def _batch_delivery_started() -> bool:
    """Проверяет: наступил ли BATCH_DELIVERY_START_AT.

    До этого момента — старая логика (статья → моментальное уведомление).
    После — статьи копятся в pending_batch и доставляются batch'ем каждый
    день в BATCH_DELIVERY_HOUR МСК.
    """
    if not BATCH_DELIVERY_START_AT:
        return False
    try:
        from datetime import datetime
        start = datetime.fromisoformat(BATCH_DELIVERY_START_AT)
    except ValueError:
        log.error("BATCH_DELIVERY_START_AT %r невалиден (нужен ISO-формат, "
                  "напр. 2026-05-14T10:00). Batch-режим выключен.",
                  BATCH_DELIVERY_START_AT)
        return False
    from datetime import datetime
    return datetime.now() >= start


async def _send_review_notification(bot: Bot, draft: dict, token: str) -> list[int]:
    """Шлёт уведомление о готовом review-draft'е во все chat_id из whitelist.

    Используется и при моментальной отправке (до BATCH_DELIVERY_START_AT),
    и в batch-цикле (по одной статье за раз с интервалом).

    Возвращает список chat_id, куда сообщение реально ушло. Если пусто —
    sentinel создавать НЕ надо (повторим попытку на следующем тике).
    """
    text = messages.new_draft_notification(
        slug=draft["slug"],
        category=draft["category"],
        title=draft["title"],
        version=draft["version"],
        char_count=draft["char_count"],
        token=token,
        predicted_spam=draft.get("predicted_spam"),
        predicted_uniqueness=draft.get("predicted_uniqueness"),
        predicted_ai=draft.get("predicted_ai"),
        customer_risks=draft.get("customer_risks") or [],
        wordstat_main=draft.get("wordstat_main"),
        wordstat_total=draft.get("wordstat_total"),
    )

    sent_to: list[int] = []
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
            sent_to.append(chat_id)
        except Exception as e:
            log.error("Не смог отправить уведомление %s: %s", chat_id, e)
    return sent_to


def _mark_sentinel_after_send(draft: dict, sent_to: list[int]) -> None:
    """Создаёт `.notified` sentinel в папке драфта после успешной отправки.
    Без этого после редеплоя контейнера watcher повторно увидит draft как
    «новый» и пошлёт второе уведомление."""
    if not sent_to:
        return
    draft_dir = DRAFTS_DIR / draft["slug"]
    notified_sentinel.mark_notified(
        draft_dir,
        chat_ids=sent_to,
        version=draft.get("version", ""),
        title=draft.get("title", ""),
    )


async def _watch_iteration(bot: Bot):
    new_drafts = watcher.scan_for_new_drafts()
    if not new_drafts:
        return

    batch_mode = _batch_delivery_started()
    log.info("Найдено новых драфтов: %d (batch_mode=%s)", len(new_drafts), batch_mode)
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

        if batch_mode:
            # BATCH-режим: статья КОПИТСЯ до ближайшего 10:00 МСК. Тогда
            # _batch_delivery_iteration выгребет её вместе с остальными
            # pending_batch и отправит подряд (с интервалом). Sentinel НЕ
            # создаём пока не отправлено: при рестарте бота watcher всё равно
            # пропустит draft потому что он уже в state.reviews (см.
            # state.known_slugs в watcher.scan_for_new_drafts).
            state.mark_pending_batch(draft["slug"])
            log.info("Draft %s помечен pending_batch (отправится в ближайшем %02d:00 МСК)",
                     draft["slug"], BATCH_DELIVERY_HOUR)
            continue

        # INSTANT-режим (до BATCH_DELIVERY_START_AT) — старое поведение.
        sent_to = await _send_review_notification(bot, draft, token)
        _mark_sentinel_after_send(draft, sent_to)


# ============ BATCH DELIVERY (с 14 мая 2026) ============
# Раз в час проверяем: «сейчас BATCH_DELIVERY_HOUR МСК И сегодня batch ещё не
# отправлялся» → выгребаем все pending_batch reviews и шлём подряд с интервалом
# BATCH_DELIVERY_INTERVAL_SEC (защита от TG flood-limit).
#
# Идемпотентность: last_batch_date в bot_state.json гарантирует «один batch в
# сутки». Если бот рестартует в 10:05 — увидит что сегодня batch уже был, не
# повторит. Если бот был выключен в 10:00 (например, упал в обновлении) и
# поднялся в 11:00 — увидит что сегодня batch НЕ был, и отправит на ближайшем
# тике (catch-up: лучше отправить с задержкой, чем потерять).

BATCH_CHECK_INTERVAL_SEC = 300  # 5 мин — достаточно частая проверка


def _moscow_now():
    """Текущее время в TZ из BATCH_DELIVERY_TZ (по умолчанию Europe/Moscow).
    Если zoneinfo недоступна или TZ-имя кривое — fallback на naive local time."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo  # Python 3.9+
        return datetime.now(ZoneInfo(BATCH_DELIVERY_TZ))
    except (ImportError, Exception) as e:
        log.warning("ZoneInfo(%r) недоступна (%s), fallback на naive datetime.now()",
                    BATCH_DELIVERY_TZ, e)
        return datetime.now()


async def _batch_delivery_iteration(bot: Bot) -> None:
    """Один тик batch-loop'а. Решает: пора ли слать batch?

    Условия для отправки:
    1. BATCH_DELIVERY_START_AT уже наступил.
    2. Текущий час в МСК == BATCH_DELIVERY_HOUR (10 по умолчанию).
    3. Сегодня batch ещё не отправлялся (state.last_batch_date != сегодня).
    4. Есть статьи с pending_batch=true.

    Все четыре условия выполнены → шлём все pending_batch по очереди.
    """
    if not _batch_delivery_started():
        return

    now = _moscow_now()
    today_str = now.strftime("%Y-%m-%d")

    # Защита от двойной отправки за сутки.
    if state.get_last_batch_date() == today_str:
        return

    # Час отправки: ровно BATCH_DELIVERY_HOUR (или позже, на случай catch-up
    # после простоя бота). До BATCH_DELIVERY_HOUR — ждём.
    if now.hour < BATCH_DELIVERY_HOUR:
        return

    slugs = state.pending_batch_slugs()
    if not slugs:
        log.info("Batch: нет pending_batch reviews — отметим день %s как пустой",
                 today_str)
        # Помечаем день закрытым даже если статей не было — иначе на каждом тике
        # будем логировать «нет статей».
        state.set_last_batch_date(today_str, count=0)
        return

    if BATCH_MAX_PER_DAY > 0 and len(slugs) > BATCH_MAX_PER_DAY:
        import json as _json
        def _cat(sl):
            try:
                return (_json.loads((DRAFTS_DIR / sl / "meta.json").read_text(encoding="utf-8"))
                        .get("category") or "fiz")
            except Exception:
                return "fiz"
        # Round-robin по категориям, чтобы свежие news не вытеснялись старыми
        # fiz/yur. slugs уже отсортированы по first_seen (старые раньше).
        by_cat = {}
        for sl in slugs:
            by_cat.setdefault(_cat(sl), []).append(sl)
        picked, cats = [], list(by_cat)
        while len(picked) < BATCH_MAX_PER_DAY and any(by_cat.values()):
            for c in cats:
                if by_cat[c]:
                    picked.append(by_cat[c].pop(0))
                    if len(picked) >= BATCH_MAX_PER_DAY:
                        break
        picked_set = set(picked)
        deferred = [s for s in slugs if s not in picked_set]
        slugs = [s for s in slugs if s in picked_set]
        log.info("Batch: лимит %d/день (баланс по категориям) — переношу %d: %s",
                 BATCH_MAX_PER_DAY, len(deferred), deferred)

    log.info("Batch-доставка %s: %d статей в очереди, начинаю рассылку",
             today_str, len(slugs))

    token = state.get_preview_token_from_state()
    sent_count = 0
    failed_count = 0

    for i, slug in enumerate(slugs):
        # Каждую статью собираем заново из meta.json + state, чтобы взять
        # актуальные данные (за время копления могла быть редактура от агента).
        draft = _draft_from_state_and_meta(slug)
        if draft is None:
            log.warning("Batch: draft %s не найден (state или meta пропал) — пропускаю",
                        slug)
            failed_count += 1
            # Снимаем флаг, иначе будет висеть навечно.
            state.mark_batch_sent(slug)
            continue

        sent_to = await _send_review_notification(bot, draft, token)
        if sent_to:
            _mark_sentinel_after_send(draft, sent_to)
            state.mark_batch_sent(slug)
            state.upsert_review(slug, {"batch_fail_count": 0})
            sent_count += 1
            log.info("Batch [%d/%d]: отправлено slug=%s в %s",
                     i + 1, len(slugs), slug, sent_to)
        else:
            # Не смогли отправить ни одному. Считаем неудачи: если 3+ batch'а
            # подряд не смогли доставить эту статью - снимаем pending_batch,
            # чтобы не зависала навечно (bug 17-22 мая: slug > 55 символов →
            # BUTTON_DATA_INVALID → pending навсегда). Ручной retry через
            # scripts/send_now.py.
            review = state.get_review(slug) or {}
            fail_streak = review.get("batch_fail_count", 0) + 1
            state.upsert_review(slug, {"batch_fail_count": fail_streak})
            if fail_streak >= 3:
                state.mark_batch_sent(slug)  # снимает pending_batch
                log.error(
                    "Batch [%d/%d]: slug=%s провалился %d batch'ей подряд — "
                    "СНИМАЮ pending_batch (используй scripts/send_now.py для ручной отправки)",
                    i + 1, len(slugs), slug, fail_streak,
                )
            else:
                log.error(
                    "Batch [%d/%d]: НЕ смог отправить slug=%s (попытка %d/3) — оставляю pending",
                    i + 1, len(slugs), slug, fail_streak,
                )
            failed_count += 1

        # Пауза между сообщениями: защита от TG flood-limit (30 msg/sec лимит,
        # но bots с медиа-вложениями ловят лимиты быстрее).
        if i < len(slugs) - 1:
            await asyncio.sleep(BATCH_DELIVERY_INTERVAL_SEC)

    # Фиксируем факт отправки. Даже если часть провалилась, день считаем
    # закрытым (иначе повторим всё что было отправлено успешно).
    state.set_last_batch_date(today_str, count=sent_count)
    log.info("Batch %s завершён: %d отправлено, %d ошибок",
             today_str, sent_count, failed_count)


def _draft_from_state_and_meta(slug: str) -> dict | None:
    """Восстанавливает draft-dict (формат как у watcher.scan_for_new_drafts())
    из state + meta.json. Нужно для batch-отправки: при сканировании watcher'ом
    статья уже в state, второй раз через scan_for_new_drafts() не пройдёт.
    """
    import json
    review = state.get_review(slug)
    if not review:
        return None
    folder = DRAFTS_DIR / slug
    if not folder.exists():
        return None
    meta_path = folder / "meta.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            meta = {}

    # char_count: из meta или из state (если уже считали).
    char_count = meta.get("text_chars") or review.get("char_count") or 0

    return {
        "slug": slug,
        "category": review.get("category") or meta.get("category", "fiz"),
        "title": review.get("title") or meta.get("title") or slug,
        "version": review.get("current_version", "2.0"),
        "char_count": int(char_count) if isinstance(char_count, (int, float)) else 0,
        "wordstat_main": review.get("wordstat_main") or meta.get("frequency_main"),
        "wordstat_total": review.get("wordstat_total") or meta.get("frequency_total"),
        "predicted_spam": meta.get("predicted_spam_pct"),
        "predicted_ai": meta.get("predicted_ai_pct"),
        "predicted_uniqueness": meta.get("predicted_uniqueness_pct"),
        "customer_risks": meta.get("customer_risks") or [],
    }


async def batch_loop(bot: Bot):
    """Фоновая задача: раз в BATCH_CHECK_INTERVAL_SEC проверяет, не пора ли
    слать batch. Защищена от двойной отправки через state.last_batch_date.
    """
    while True:
        try:
            await _batch_delivery_iteration(bot)
        except Exception as e:
            log.exception("batch_loop error: %s", e)
        await asyncio.sleep(BATCH_CHECK_INTERVAL_SEC)


BOOTSTRAP_DONE_FLAG = DATA_DIR / ".bootstrap_sentinel_done"


def _bootstrap_sync_drafts() -> int:
    """
    ОДНОРАЗОВАЯ миграция: при первом запуске после внедрения механизма
    sentinel'ов проходит по всем drafts/{slug}/ и для каждой папки которая
    1) НЕ имеет .notified sentinel
    2) НЕ зарегистрирована в bot_state.json:reviews
    создаёт sentinel в bootstrap-режиме (без отправки уведомления).

    После успешного запуска создаёт файл-флаг data/.bootstrap_sentinel_done,
    который коммитится в git. Все последующие старты бота bootstrap НЕ
    запускают — иначе он будет гасить уведомления о новых статьях,
    которые scheduler пишет в drafts/ между деплоями.

    Возвращает число созданных sentinel'ов. 0 если bootstrap уже выполнен.
    """
    if BOOTSTRAP_DONE_FLAG.exists():
        log.debug("Bootstrap-sentinel уже выполнен ранее (flag %s exists), пропускаю",
                  BOOTSTRAP_DONE_FLAG.name)
        return 0

    if not DRAFTS_DIR.exists():
        # Всё равно ставим флаг — миграция не нужна, и не должна повторяться
        _mark_bootstrap_done()
        return 0

    known = state.known_slugs()
    created = 0
    for sub in DRAFTS_DIR.iterdir():
        if not sub.is_dir() or sub.name.startswith("_") or sub.name.startswith("."):
            continue
        slug = sub.name
        if slug in known:
            continue  # bot_state.json уже знает — sentinel не нужен
        if notified_sentinel.is_notified(sub):
            continue  # sentinel уже есть — bot_state восстановится при первом
                       # реальном уведомлении

        # Папка есть, но ни в одном из источников не отмечена. Это значит
        # она существовала ДО внедрения sentinel-механизма — заказчик про
        # неё уже знает, повторное уведомление не нужно.
        try:
            meta_path = sub / "meta.json"
            title = ""
            if meta_path.exists():
                import json as _json
                try:
                    title = (_json.loads(meta_path.read_text(encoding="utf-8"))
                             .get("title") or "")[:120]
                except Exception:
                    pass
        except Exception:
            title = ""

        if notified_sentinel.mark_notified(sub, chat_ids=(), title=title,
                                              bootstrap=True):
            created += 1
            log.debug("Bootstrap sentinel for %s (title=%r)", slug, title[:60])

    _mark_bootstrap_done()
    return created


def _mark_bootstrap_done() -> None:
    """Создаёт файл-флаг что bootstrap выполнен. Файл коммитится."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    try:
        from datetime import datetime
        BOOTSTRAP_DONE_FLAG.write_text(
            f"bootstrap completed at {datetime.now().isoformat(timespec='seconds')}\n"
            "После этого момента bot/main.py не запускает bootstrap-sync.\n"
            "Новые статьи от scheduler'а проходят через watcher как обычные.\n",
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Не смог создать %s: %s", BOOTSTRAP_DONE_FLAG.name, exc)


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

    # FSM-alias middleware: handlers объявляют параметр `fsm: FSMContext`,
    # но aiogram 3.x по умолчанию инжектит ключом `state`. Без алиаса каждое
    # нажатие кнопок «Правки» / «Отклонить» крашится с
    # `TypeError: missing 1 required positional argument: 'fsm'`, callback.answer()
    # не успевает выполниться → TG показывает вечный спиннер на кнопке.
    # Сразу копируем data['state'] → data['fsm'] чтобы handler'ы работали
    # с тем же FSMContext под привычным именем.
    # На Cloud Apps этот middleware был зарегистрирован в main.py FastAPI-варианте.
    @dp.update.outer_middleware()
    async def _alias_fsm_to_state(handler, event, data):
        state_obj = data.get("state")
        if state_obj is not None and "fsm" not in data:
            data["fsm"] = state_obj
        return await handler(event, data)

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

    # Bootstrap-sync: для каждой папки drafts/{slug}/ которая уже существует,
    # но НЕ имеет sentinel-файла И НЕ зарегистрирована в bot_state.json —
    # создаём sentinel в bootstrap-режиме. Это значит "заказчик про эту
    # статью уже знает (или мы потеряли state), повторное уведомление
    # отправлять не нужно".
    #
    # Этот bootstrap безопасен: если по какой-то причине запись в state
    # ЕСТЬ, sentinel не нужен (watcher всё равно пропустит). Если sentinel
    # ЕСТЬ, watcher тоже пропустит. Bootstrap нужен только для папок где
    # ОБА маркера потеряны — в обычной работе таких нет.
    bootstrap_synced = _bootstrap_sync_drafts()
    if bootstrap_synced:
        log.info(
            "Bootstrap-sync: создано %d sentinel-файлов для существующих "
            "папок без записи в state. Повторных уведомлений не будет.",
            bootstrap_synced,
        )

    # Запускаем watcher, queue processor и batch-delivery параллельно с polling.
    watcher_task = asyncio.create_task(watch_loop(bot))
    queue_task = asyncio.create_task(queue_loop(bot))
    batch_task = asyncio.create_task(batch_loop(bot))

    if BATCH_DELIVERY_START_AT:
        log.info("Batch-доставка включена: start=%s, hour=%02d МСК, interval=%ds",
                 BATCH_DELIVERY_START_AT, BATCH_DELIVERY_HOUR,
                 BATCH_DELIVERY_INTERVAL_SEC)
    else:
        log.info("Batch-доставка ВЫКЛЮЧЕНА (BATCH_DELIVERY_START_AT пуст) — "
                 "статьи доставляются по мере готовности")

    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        watcher_task.cancel()
        queue_task.cancel()
        batch_task.cancel()
        await bot.session.close()


def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        log.info("Остановлено пользователем")


if __name__ == "__main__":
    main()
