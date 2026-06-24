"""
Обработчики команд и inline-кнопок Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    ForceReply,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import editor, messages, publisher, queue as action_queue, state, transcribe
from .config import TG_ALLOWED_CHAT_IDS, category_label

log = logging.getLogger(__name__)

router = Router()


# ============ FSM States ============
class EditFlow(StatesGroup):
    waiting_for_edit_text = State()
    waiting_for_rejection_reason = State()


# ============ Access guard ============
def _is_allowed(message_or_query) -> bool:
    chat_id = (
        message_or_query.from_user.id
        if message_or_query.from_user
        else None
    )
    if chat_id is None:
        return False
    if not TG_ALLOWED_CHAT_IDS:
        # Если whitelist пуст - пропускаем всех (для локальной отладки).
        # На проде whitelist обязателен.
        return True
    return chat_id in TG_ALLOWED_CHAT_IDS


# ============ Keyboards ============
# TG callback_data лимит: 64 байта. Самый длинный префикс "publish:" = 8 байт.
# Безопасный slug = 64 - 8 - 1(запас) = 55 символов.
# Если slug длиннее - обрезаем. _resolve_slug() на приёме найдёт полный slug
# по префиксу в bot_state.
_CB_SLUG_MAX = 55


def _safe_cb_slug(slug: str) -> str:
    """Обрезает slug до безопасной длины для TG callback_data."""
    return slug[:_CB_SLUG_MAX] if len(slug) > _CB_SLUG_MAX else slug


def _resolve_slug(raw: str) -> str:
    """Восстанавливает полный slug из (возможно обрезанного) callback_data.

    Если raw найден точно в state.reviews - возвращает как есть.
    Иначе ищет единственный slug, начинающийся с raw. Если нашли ровно один -
    возвращаем его. Иначе возвращаем raw (пусть downstream покажет «не найдено»).
    """
    reviews = state.load().get("reviews", {})
    if raw in reviews:
        return raw
    candidates = [s for s in reviews if s.startswith(raw)]
    if len(candidates) == 1:
        return candidates[0]
    if len(candidates) > 1:
        log.warning("_resolve_slug: %d кандидатов для prefix=%r, беру первый: %s",
                     len(candidates), raw, candidates[0])
        return candidates[0]
    return raw


def review_keyboard(slug: str) -> InlineKeyboardMarkup:
    cb = _safe_cb_slug(slug)
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{cb}")],
        [InlineKeyboardButton(text="✏️ Правки", callback_data=f"edit:{cb}")],
        [InlineKeyboardButton(text="🗑 Отклонить", callback_data=f"reject:{cb}")],
    ])


# ============ Commands ============
@router.message(CommandStart())
async def on_start(message: Message):
    if not _is_allowed(message):
        await message.answer(messages.access_denied(), parse_mode="HTML")
        return
    await message.answer(messages.help_text(), parse_mode="HTML")


@router.message(Command("help"))
async def on_help(message: Message):
    if not _is_allowed(message):
        await message.answer(messages.access_denied(), parse_mode="HTML")
        return
    await message.answer(messages.help_text(), parse_mode="HTML")


@router.message(Command("pending"))
async def on_pending(message: Message):
    if not _is_allowed(message):
        await message.answer(messages.access_denied(), parse_mode="HTML")
        return
    pending = state.list_pending()
    if not pending:
        await message.answer("📭 Нет статей, ожидающих ревью.")
        return
    lines = ["📋 <b>Ожидают ревью:</b>\n"]
    for slug, review in pending:
        cat = category_label(review.get("category", ""))
        title = review.get("title", slug)
        v = review.get("current_version", "?")
        lines.append(f"• [{cat}] {title} (v{v})")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("chatid"))
async def on_chatid(message: Message):
    """Для отладки. Любой пользователь может узнать свой chat_id."""
    await message.answer(f"Ваш chat_id: <code>{message.from_user.id}</code>", parse_mode="HTML")


# ============ Callback: ✏️ Правки ============
@router.callback_query(F.data.startswith("edit:"))
async def on_edit_pressed(query: CallbackQuery, fsm: FSMContext):
    # Сразу гасим спиннер на кнопке. Если что-то ниже упадёт, юзер хотя бы
    # не будет смотреть в бесконечную загрузку до 30-секундного таймаута.
    try:
        await query.answer()
    except Exception:
        log.exception("Edit flow: не смог ответить на callback")

    if not _is_allowed(query):
        try:
            await query.answer("⛔ Доступ запрещён", show_alert=True)
        except Exception:
            pass
        return

    slug = _resolve_slug(query.data.removeprefix("edit:"))
    review = state.get_review(slug)
    if not review:
        await query.message.answer(
            "❓ Статья не найдена в базе бота. Возможно, бот её ещё не "
            "регистрировал (state мог сброситься при редеплое). "
            "Попроси команду переотправить уведомление."
        )
        log.warning(
            "Edit flow: review для slug=%s не найден (callback от user=%s)",
            slug, query.from_user.id if query.from_user else "?",
        )
        return

    try:
        await fsm.set_state(EditFlow.waiting_for_edit_text)
        await fsm.update_data(slug=slug)
        log.info(
            "Edit flow started: user=%s slug=%s title=%r",
            query.from_user.id if query.from_user else "?", slug,
            review.get("title", slug)[:60],
        )
        # ForceReply гарантирует что юзер ответит reply'ем на это сообщение.
        # Если FSM-state потеряется при редеплое, мы восстановим slug из
        # reply_to_message.text по маркеру [edit:slug] в конце.
        await query.message.answer(
            messages.asking_for_edit(title=review.get("title", slug), slug=slug),
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True),
        )
    except Exception as exc:
        log.exception("Edit flow: ошибка при отправке prompt slug=%s", slug)
        try:
            await query.message.answer(
                f"❌ Не удалось открыть форму правок: <code>{type(exc).__name__}</code>. "
                "Команда уже видит ошибку в логе.",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(EditFlow.waiting_for_edit_text, F.voice)
async def on_voice_edit(message: Message, fsm: FSMContext):
    """Голосовое сообщение → транскрипция через Groq → применение как правка."""
    if not _is_allowed(message):
        return
    await message.answer("🎤 Распознаю голосовое...")

    bot = message.bot
    file = await bot.get_file(message.voice.file_id)
    file_bytes = await bot.download_file(file.file_path)

    text = transcribe.transcribe_voice_bytes(file_bytes.read(), filename="voice.ogg")
    if not text:
        await message.answer(
            "❌ Не удалось распознать голосовое.\n"
            "Попробуйте отправить текстом или повторите запись."
        )
        return

    await message.answer(messages.voice_transcribed(text), parse_mode="HTML")
    await _process_edit(message, fsm, edit_text=text)


@router.message(EditFlow.waiting_for_edit_text, F.text & ~F.text.startswith("/"))
async def on_text_edit(message: Message, fsm: FSMContext):
    if not _is_allowed(message):
        return
    edit_text = (message.text or "").strip()
    if not edit_text:
        await message.answer("Пустое сообщение. Опишите правку текстом или голосом.")
        return
    await _process_edit(message, fsm, edit_text=edit_text)


async def _process_edit(message: Message, fsm: FSMContext, *, edit_text: str):
    """
    Применяет правку. Marker из reply_to_message приоритетнее FSM, чтобы
    защититься от stale FSM (юзер мог нажать другую кнопку между постановкой
    state и отправкой ответа).
    """
    data = await fsm.get_data()
    fsm_slug = data.get("slug")
    await fsm.clear()
    marker = _parent_marker(message)

    # Если юзер ответил на reject-prompt пока FSM в edit-state — перенаправляем
    if marker and marker[0] == "reject":
        slug = marker[1]
        review = state.get_review(slug)
        if not review:
            log.warning("Edit flow→reject redirect: review для slug=%s не найден", slug)
            return
        log.info(
            "Edit flow: получен ответ на reject-prompt (slug из marker=%s, "
            "FSM был в edit-state). Применяю как отклонение.",
            slug,
        )
        reason = (message.text or "").strip()
        if reason == "-":
            reason = None
        action_queue.remove_publish(slug)
        state.set_status(slug, "rejected", rejection_reason=reason)
        await message.answer(
            messages.rejected(title=review.get("title", slug), reason=reason),
            parse_mode="HTML",
        )
        return

    # Marker edit приоритетнее FSM (защита от stale FSM)
    if marker and marker[0] == "edit":
        slug = marker[1]
        if fsm_slug and fsm_slug != slug:
            log.warning(
                "Edit flow: FSM slug=%s, но marker=%s. Использую marker.",
                fsm_slug, slug,
            )
    else:
        slug = fsm_slug

    if not slug:
        log.warning(
            "Edit flow: FSM пуст и reply_to_message без маркера для user=%s",
            message.from_user.id if message.from_user else "?",
        )
        await message.answer(
            "Сессия правки потерялась. Нажмите ✏️ Правки заново "
            "и ответьте reply'ем на сообщение бота с описанием правки."
        )
        return

    review = state.get_review(slug)
    if not review:
        log.warning("Edit flow: review для slug=%s не найден в state", slug)
        await message.answer("Статья пропала из архива. Странно. Свяжитесь с командой.")
        return

    log.info(
        "Edit flow: применяю правку slug=%s edit_text=%r",
        slug, edit_text[:120],
    )
    await _run_edit(message, slug=slug, review=review, edit_text=edit_text)


# ============ Callback: ✅ Опубликовать ============
@router.callback_query(F.data.startswith("publish:"))
async def on_publish_pressed(query: CallbackQuery):
    if not _is_allowed(query):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    slug = _resolve_slug(query.data.removeprefix("publish:"))
    review = state.get_review(slug)
    if not review:
        await query.answer("Статья не найдена", show_alert=True)
        return

    if review.get("status") == "published":
        await query.answer("Уже опубликовано", show_alert=True)
        return

    title = review.get("title", slug)
    current_version = review.get("current_version", "2.0")

    # Очередь по scheduler_active убрана 19 мая 2026 (миграция на Timeweb):
    # scheduler в отдельном systemd-юните, git push его не убивает.
    # Публикуем сразу — мгновенный отклик через test.pravo.shop (локальные файлы).

    await query.answer()
    progress_msg = await query.message.answer("⏳ Публикую…")

    # Тяжёлая операция (вызовы fal.ai + Cloudinary + git push) - в отдельном потоке,
    # чтобы не блокировать polling.
    result = await asyncio.to_thread(
        publisher.publish, slug=slug, version=current_version,
    )

    try:
        await progress_msg.delete()
    except Exception:
        pass

    if not result.success:
        await query.message.answer(
            f"❌ <b>Не удалось опубликовать «{title}»</b>\n\n"
            f"Ошибка: <code>{(result.error or 'неизвестная')[:500]}</code>\n\n"
            "Статья осталась в drafts/, можно повторить попытку.",
            parse_mode="HTML",
        )
        return

    # Wordstat-частоту достаём из bot_state (она там запоминалась когда watcher
    # увидел драфт - см. bot/main.py). Если нет - просто не показываем.
    wordstat_line = ""
    review_after = state.get_review(slug) or {}
    wordstat_main = review_after.get("wordstat_main")
    if wordstat_main is not None:
        formatted = f"{int(wordstat_main):,}".replace(",", " ")
        wordstat_line = f"\n📊 Wordstat: {formatted}/мес"

    await query.message.answer(
        f"✅ <b>Опубликовано: «{title}»</b>\n\n"
        f"🔗 <a href=\"{result.public_url}\">{result.public_url}</a>"
        f"{wordstat_line}",
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


# ============ Callback: 🗑 Отклонить ============
@router.callback_query(F.data.startswith("reject:"))
async def on_reject_pressed(query: CallbackQuery, fsm: FSMContext):
    try:
        await query.answer()
    except Exception:
        log.exception("Reject flow: не смог ответить на callback")

    if not _is_allowed(query):
        try:
            await query.answer("⛔ Доступ запрещён", show_alert=True)
        except Exception:
            pass
        return

    slug = _resolve_slug(query.data.removeprefix("reject:"))
    review = state.get_review(slug)
    if not review:
        await query.message.answer(
            "❓ Статья не найдена в базе бота. Возможно, бот её ещё не "
            "регистрировал (state мог сброситься при редеплое). "
            "Попроси команду переотправить уведомление."
        )
        log.warning(
            "Reject flow: review для slug=%s не найден (callback от user=%s)",
            slug, query.from_user.id if query.from_user else "?",
        )
        return

    try:
        await fsm.set_state(EditFlow.waiting_for_rejection_reason)
        await fsm.update_data(slug=slug)
        log.info(
            "Reject flow started: user=%s slug=%s",
            query.from_user.id if query.from_user else "?", slug,
        )
        await query.message.answer(
            messages.asking_for_rejection_reason(title=review.get("title", slug), slug=slug),
            parse_mode="HTML",
            reply_markup=ForceReply(selective=True),
        )
    except Exception as exc:
        log.exception("Reject flow: ошибка при отправке prompt slug=%s", slug)
        try:
            await query.message.answer(
                f"❌ Не удалось открыть форму отклонения: <code>{type(exc).__name__}</code>. "
                "Команда уже видит ошибку в логе.",
                parse_mode="HTML",
            )
        except Exception:
            pass


@router.message(EditFlow.waiting_for_rejection_reason, F.text)
async def on_rejection_reason(message: Message, fsm: FSMContext):
    if not _is_allowed(message):
        return
    data = await fsm.get_data()
    fsm_slug = data.get("slug")
    await fsm.clear()

    # Marker из reply_to_message приоритетнее FSM. Это защищает от случая
    # когда юзер нажал "Правки" для одной статьи, потом "Отклонить" для
    # другой, и теперь отвечает на ПЕРВЫЙ prompt — FSM показывает второй
    # slug, но reply сделан на первое сообщение (с маркером первого slug).
    marker = _parent_marker(message)
    if marker and marker[0] == "edit":
        # Юзер ответил на edit-prompt пока FSM в reject-state.
        # Перенаправляем как edit для правильного slug из маркера.
        slug = marker[1]
        review = state.get_review(slug)
        if not review:
            log.warning("Reject flow→edit redirect: review для slug=%s не найден", slug)
            return
        log.info(
            "Reject flow: получен ответ на edit-prompt (slug из marker=%s, "
            "FSM был в reject-state). Применяю как правку.",
            slug,
        )
        edit_text = (message.text or "").strip()
        if not edit_text:
            await message.answer("Пустое сообщение. Опишите правку.")
            return
        await _run_edit(message, slug=slug, review=review, edit_text=edit_text)
        return

    if marker and marker[0] == "reject":
        # Маркер reject есть: используем slug из него (если отличается от FSM
        # — значит FSM был от другого нажатия, marker точнее).
        if marker[1] != fsm_slug:
            log.warning(
                "Reject flow: FSM slug=%s, но marker=%s. Использую marker.",
                fsm_slug, marker[1],
            )
        slug = marker[1]
    else:
        slug = fsm_slug

    if not slug:
        log.warning(
            "Reject flow: FSM data пуст и нет marker для user=%s",
            message.from_user.id if message.from_user else "?",
        )
        await message.answer(
            "Сессия отклонения потерялась. Нажмите 🗑 Отклонить заново."
        )
        return
    review = state.get_review(slug)
    if not review:
        log.warning("Reject flow: review для slug=%s не найден", slug)
        return
    reason = message.text.strip()
    if reason == "-":
        reason = None
    # Если статья была в очереди на publish — убираем (заказчик передумал).
    action_queue.remove_publish(slug)
    state.set_status(slug, "rejected", rejection_reason=reason)
    await message.answer(
        messages.rejected(title=review.get("title", slug), reason=reason),
        parse_mode="HTML",
    )


# ============ Fallback по reply_to_message (если FSM потерян) ============
# Маркер вида "↩️ edit:slug-name" или "↩️ reject:slug-name" в конце сообщения
# бота. При редеплое контейнера FSM-state стирается (data/.fsm_state.json не
# в гите, новый контейнер его не имеет). Fallback восстанавливает slug из
# текста сообщения, на которое юзер ответил reply'ем.
#
# ВАЖНО: эти handler'ы зарегистрированы ПОСЛЕ FSM-handlers выше. Aiogram
# идёт по handler'ам в порядке регистрации и берёт первый который match'ит
# фильтры. Если FSM активен — сначала отрабатывает FSM-handler, до сюда
# не дойдёт. Сюда падают только сообщения без активного FSM-state.
_MARKER_RE = re.compile(r"↩️\s*(edit|reject):([a-z0-9\-]+)", re.IGNORECASE)


def _extract_marker(text: str | None) -> tuple[str, str] | None:
    if not text:
        return None
    m = _MARKER_RE.search(text)
    if not m:
        return None
    return m.group(1).lower(), m.group(2)


def _parent_marker(message: Message) -> tuple[str, str] | None:
    parent = message.reply_to_message
    if not parent:
        return None
    return _extract_marker(parent.text)


async def _run_edit(message: Message, *, slug: str, review: dict, edit_text: str):
    """
    Общая часть для запуска edit-pipeline. Используется и FSM-handler'ами
    (через _process_edit), и fallback по reply_to_message.

    Если apply_edit упал с timeout И scheduler активен — это известный
    конфликт двух claude процессов через shared ~/.claude.json
    (несмотря на изоляцию HOME в editor.py). В этом случае запускаем
    background-task которая дождётся освобождения lock-а и повторит
    правку автоматически. Заказчику сразу отвечаем «применю когда слот
    закроется».
    """
    progress_msg = await message.answer(messages.edit_in_progress(), parse_mode="HTML")
    result = await asyncio.to_thread(
        editor.apply_edit,
        slug=slug,
        current_version=review.get("current_version", "2.0"),
        versions=review.get("versions", ["2.0"]),
        edit_text=edit_text,
    )
    try:
        await progress_msg.delete()
    except Exception:
        pass

    if not result.success or result.new_version is None:
        # Спецслучай: edit упал + активный scheduler → правка в очередь.
        # Покрываем И timeout, И exit-ошибку claude (код N) — обе вероятнее
        # всего вызваны конфликтом двух параллельных claude (overloaded/
        # rate-limit/RAM при активном слоте). editor.py уже сделал ретраи;
        # раз не помогло и слот занят — ждём его освобождения и повторяем.
        err = result.error or ""
        is_timeout = "не ответил за" in err
        is_claude_error = "вернул ошибку (код" in err
        scheduler_active, lock_age = action_queue.is_scheduler_active()
        if (is_timeout or is_claude_error) and scheduler_active:
            eta_sec = action_queue.estimate_eta_sec()
            eta_min = max(1, (eta_sec + 59) // 60)
            log.warning(
                "Edit timeout + scheduler active → ставлю в фоновую очередь "
                "slug=%s lock_age=%ds eta=%dmin",
                slug, int(lock_age), eta_min,
            )
            await message.answer(
                f"📋 Сейчас scheduler пишет другую статью (идёт {int(lock_age)//60} мин).\n"
                f"Правку применю автоматически как только он закончит — "
                f"примерно через {eta_min} мин.\n\n"
                f"Можешь не следить — пришлю результат сюда.",
                parse_mode="HTML",
            )
            # Background task — живёт пока бот работает. Если бот рестартнётся
            # за это время, правка потеряется (заказчику надо повторить).
            # В норме рестарт бота редкий, так что приемлемо.
            asyncio.create_task(_run_edit_when_scheduler_free(
                chat_id=message.chat.id,
                slug=slug, review=review, edit_text=edit_text,
            ))
            return

        log.error(
            "Edit run: claude вернул ошибку slug=%s error=%r",
            slug, (result.error or "")[:200],
        )
        await message.answer(
            messages.edit_failed(result.error or "неизвестная ошибка"),
            parse_mode="HTML",
        )
        return

    log.info("Edit run: правка применена slug=%s new_version=%s chars=%s",
             slug, result.new_version, result.char_count)
    state.add_edit(slug, new_version=result.new_version, edit_text=edit_text)
    token = state.get_preview_token_from_state()
    await message.answer(
        messages.edit_applied(
            slug=slug,
            new_version=result.new_version,
            summary=result.summary,
            char_count=result.char_count or 0,
            prev_char_count=None,
            token=token,
            uniqueness_pct=None,
            fact_warnings=None,
        ),
        parse_mode="HTML",
        reply_markup=review_keyboard(slug),
        disable_web_page_preview=False,
    )


async def _run_edit_when_scheduler_free(
    *, chat_id: int, slug: str, review: dict, edit_text: str,
) -> None:
    """
    Ждёт пока scheduler освободит lock, потом применяет правку и шлёт
    результат в чат. Запускается из _run_edit когда обычный apply_edit
    упал по timeout во время активного слота.

    Поллим каждые 30 сек, максимум 2 часа (защита от вечного зависания
    lock-а — если scheduler залип >2 ч, лучше отдать ошибку заказчику).
    """
    from aiogram import Bot
    from .config import TG_BOT_TOKEN

    deadline = asyncio.get_event_loop().time() + 7200  # 2 часа
    waited = 0
    while True:
        active, _ = action_queue.is_scheduler_active()
        if not active:
            break
        if asyncio.get_event_loop().time() > deadline:
            log.error(
                "Edit-queue: scheduler не освободил lock за 2 часа, "
                "отменяю отложенную правку slug=%s", slug,
            )
            try:
                bot = Bot(token=TG_BOT_TOKEN)
                await bot.send_message(
                    chat_id,
                    f"❌ Отложенная правка для «{review.get('title', slug)}» "
                    f"отменена: scheduler не освободил блокировку за 2 часа.\n"
                    f"Попробуй применить правку заново.",
                )
                await bot.session.close()
            except Exception:
                log.exception("Edit-queue: не смог отправить timeout-уведомление")
            return
        await asyncio.sleep(30)
        waited += 30

    log.info("Edit-queue: scheduler освободил lock (ждали %dс), применяю правку slug=%s",
             waited, slug)

    # Повторно применяем (теперь conflict-а нет)
    result = await asyncio.to_thread(
        editor.apply_edit,
        slug=slug,
        current_version=review.get("current_version", "2.0"),
        versions=review.get("versions", ["2.0"]),
        edit_text=edit_text,
    )

    bot = Bot(token=TG_BOT_TOKEN)
    try:
        if not result.success or result.new_version is None:
            log.error("Edit-queue: повторное применение упало slug=%s err=%r",
                      slug, (result.error or "")[:200])
            await bot.send_message(
                chat_id,
                f"❌ Отложенная правка для «{review.get('title', slug)}» не применилась.\n"
                f"Ошибка: <code>{(result.error or 'неизвестная')[:300]}</code>",
                parse_mode="HTML",
            )
            return

        log.info("Edit-queue: правка применена slug=%s new_version=%s",
                 slug, result.new_version)
        state.add_edit(slug, new_version=result.new_version, edit_text=edit_text)
        token = state.get_preview_token_from_state()
        await bot.send_message(
            chat_id,
            messages.edit_applied(
                slug=slug,
                new_version=result.new_version,
                summary=result.summary,
                char_count=result.char_count or 0,
                prev_char_count=None,
                token=token,
                uniqueness_pct=None,
                fact_warnings=None,
            ),
            parse_mode="HTML",
            reply_markup=review_keyboard(slug),
            disable_web_page_preview=False,
        )
    finally:
        await bot.session.close()


@router.message(F.text, F.reply_to_message, ~F.text.startswith("/"))
async def on_text_reply_fallback(message: Message, fsm: FSMContext):
    """Если FSM пуст (был редеплой), но юзер ответил reply'ем на наш prompt
    с маркером — восстанавливаем slug из текста parent message."""
    if not _is_allowed(message):
        return
    marker = _parent_marker(message)
    if not marker:
        return
    action, slug = marker
    await fsm.clear()

    review = state.get_review(slug)
    if not review:
        log.warning("Fallback: review для slug=%s не найден", slug)
        await message.answer(
            "Статья не найдена в архиве. Возможно она уже опубликована или отклонена."
        )
        return

    if action == "edit":
        log.info(
            "Fallback edit (FSM был потерян): user=%s slug=%s text=%r",
            message.from_user.id if message.from_user else "?",
            slug, (message.text or "")[:120],
        )
        edit_text = (message.text or "").strip()
        if not edit_text:
            await message.answer("Пустое сообщение. Опишите правку текстом или голосом.")
            return
        await _run_edit(message, slug=slug, review=review, edit_text=edit_text)
        return

    if action == "reject":
        log.info("Fallback reject (FSM был потерян): user=%s slug=%s",
                 message.from_user.id if message.from_user else "?", slug)
        reason = message.text.strip()
        if reason == "-":
            reason = None
        action_queue.remove_publish(slug)
        state.set_status(slug, "rejected", rejection_reason=reason)
        await message.answer(
            messages.rejected(title=review.get("title", slug), reason=reason),
            parse_mode="HTML",
        )
        return


@router.message(F.voice, F.reply_to_message)
async def on_voice_reply_fallback(message: Message, fsm: FSMContext):
    """То же для голосовых правок."""
    if not _is_allowed(message):
        return
    marker = _parent_marker(message)
    if not marker:
        return
    action, slug = marker
    if action != "edit":
        return  # отклонение голосом не поддерживаем — нужна причина текстом

    await fsm.clear()
    review = state.get_review(slug)
    if not review:
        log.warning("Fallback voice: review для slug=%s не найден", slug)
        await message.answer("Статья не найдена в архиве.")
        return

    log.info("Fallback voice edit (FSM потерян): user=%s slug=%s",
             message.from_user.id if message.from_user else "?", slug)
    await message.answer("🎤 Распознаю голосовое...")
    bot = message.bot
    file = await bot.get_file(message.voice.file_id)
    file_bytes = await bot.download_file(file.file_path)
    text = transcribe.transcribe_voice_bytes(file_bytes.read(), filename="voice.ogg")
    if not text:
        await message.answer(
            "❌ Не удалось распознать голосовое.\n"
            "Попробуйте отправить текстом или повторите запись."
        )
        return
    await message.answer(messages.voice_transcribed(text), parse_mode="HTML")
    await _run_edit(message, slug=slug, review=review, edit_text=text)


# ============ Управление планом публикаций (количество и ротация по типам) ============
# Заказчик меняет план прямо в боте:
#   /plan                     - показать текущий план
#   /setplan физ юр взыск новости   - задать, напр. /setplan 3 3 3 1
# Меняет ARTICLES_PER_DAY и ROTATION_ORDER в .env. Scheduler читает .env каждый
# слот (systemd EnvironmentFile), поэтому перезапуск НЕ нужен.
_PLAN_CATS = [("fiz", "физ"), ("yur", "юр"), ("vzysk", "взыск"), ("news", "новости")]
_PLAN_MAX_TOTAL = 12  # максимум слотов в сутки (потолок /setplan)
_TIMER_PATH = "/etc/systemd/system/liquidator-scheduler.timer"
_TIMER_BASE_MINUTE = 24  # первый слот в 00:24 (как исторически)


def _env_path() -> Path:
    return Path(__file__).resolve().parents[1] / ".env"


def _generate_timer_points(n: int) -> list[str]:
    """N равномерных OnCalendar-точек в сутках, старт 00:24."""
    points = []
    for i in range(n):
        total_min = (_TIMER_BASE_MINUTE + round(i * 1440 / n)) % 1440
        points.append(f"{total_min // 60:02d}:{total_min % 60:02d}:00")
    return sorted(points)


def _update_systemd_timer(n: int) -> tuple[bool, str]:
    """Перезаписывает systemd timer с N равномерными точками и делает daemon-reload.

    Использует `sudo tee` (appuser имеет passwordless sudo для systemctl).
    Возвращает (ok, detail) — detail = список точек или текст ошибки.
    """
    import subprocess as _sp
    points = _generate_timer_points(n)
    interval_min = round(1440 / n)
    content = "\n".join([
        "[Unit]",
        f"Description=Run Liquidator scheduler every {interval_min} min ({n} triggers/day)",
        "",
        "[Timer]",
        *[f"OnCalendar=*-*-* {p}" for p in points],
        "Persistent=false",
        "",
        "[Install]",
        "WantedBy=timers.target",
        "",
    ])
    try:
        r = _sp.run(["sudo", "tee", _TIMER_PATH],
                    input=content, capture_output=True, text=True, timeout=10)
        if r.returncode != 0:
            return False, f"tee: {r.stderr.strip()[:200]}"
        r2 = _sp.run(["sudo", "systemctl", "daemon-reload"],
                     capture_output=True, text=True, timeout=15)
        if r2.returncode != 0:
            return False, f"daemon-reload: {r2.stderr.strip()[:200]}"
        r3 = _sp.run(["sudo", "systemctl", "restart", "liquidator-scheduler.timer"],
                     capture_output=True, text=True, timeout=15)
        if r3.returncode != 0:
            return False, f"restart timer: {r3.stderr.strip()[:200]}"
        return True, ", ".join(points)
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def _read_env_raw() -> dict:
    data: dict[str, str] = {}
    p = _env_path()
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Z_][A-Z0-9_]*)=(.*)$", line)
            if m:
                data[m.group(1)] = m.group(2)
    return data


def _write_env(updates: dict) -> None:
    """Атомарно обновляет/добавляет ключи в .env, остальные строки сохраняет."""
    p = _env_path()
    lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    seen = set()
    out = []
    for line in lines:
        m = re.match(r"^([A-Z_][A-Z0-9_]*)=", line)
        if m and m.group(1) in updates:
            out.append(f"{m.group(1)}={updates[m.group(1)]}")
            seen.add(m.group(1))
        else:
            out.append(line)
    for k, v in updates.items():
        if k not in seen:
            out.append(f"{k}={v}")
    tmp = p.parent / ".env.tmp"
    tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
    tmp.replace(p)


def _build_rotation(counts: dict) -> list:
    """Round-robin: типы чередуются (новость не уходит в самый конец дня)."""
    pools = dict(counts)
    cats = [c for c, _ in _PLAN_CATS]
    order = []
    while any(pools.get(c, 0) > 0 for c in cats):
        for c in cats:
            if pools.get(c, 0) > 0:
                order.append(c)
                pools[c] -= 1
    return order


def _format_plan(counts: dict, total: int) -> str:
    parts = [f"   • {category_label(c)}: <b>{counts.get(c, 0)}</b>" for c, _ in _PLAN_CATS]
    return f"📊 <b>План публикаций: {total} статей/день</b>\n" + "\n".join(parts)


@router.message(Command("plan"))
async def on_plan(message: Message, fsm: FSMContext):
    if not _is_allowed(message):
        await message.answer(messages.access_denied(), parse_mode="HTML")
        return
    await fsm.clear()
    env = _read_env_raw()
    rotation = [c.strip() for c in env.get("ROTATION_ORDER", "").split(",") if c.strip()]
    counts = {c: rotation.count(c) for c, _ in _PLAN_CATS}
    total = sum(counts.values())
    await message.answer(
        _format_plan(counts, total)
        + "\n\nИзменить: <code>/setplan физ юр взыск новости</code>\n"
        "Например: <code>/setplan 3 3 3 1</code>",
        parse_mode="HTML",
    )


@router.message(Command("setplan"))
async def on_setplan(message: Message, fsm: FSMContext):
    if not _is_allowed(message):
        await message.answer(messages.access_denied(), parse_mode="HTML")
        return
    await fsm.clear()
    args = (message.text or "").split()[1:]
    usage = (
        "❌ Формат: <code>/setplan физ юр взыск новости</code>\n"
        "Например: <code>/setplan 3 3 3 1</code> — 3 физ, 3 юр, 3 взыск, 1 новость в день."
    )
    if len(args) != 4:
        await message.answer(usage, parse_mode="HTML")
        return
    try:
        nums = [int(x) for x in args]
    except ValueError:
        await message.answer(usage, parse_mode="HTML")
        return
    if any(n < 0 for n in nums):
        await message.answer("❌ Числа не могут быть отрицательными.", parse_mode="HTML")
        return
    counts = {c: nums[i] for i, (c, _) in enumerate(_PLAN_CATS)}
    total = sum(nums)
    if total < 1:
        await message.answer("❌ Нужна хотя бы 1 статья в день.", parse_mode="HTML")
        return
    if total > _PLAN_MAX_TOTAL:
        await message.answer(
            f"❌ Максимум {_PLAN_MAX_TOTAL} статей в день (столько слотов генерации). "
            f"Вы запросили {total}.",
            parse_mode="HTML",
        )
        return
    rotation = _build_rotation(counts)
    try:
        _write_env({"ARTICLES_PER_DAY": str(total), "ROTATION_ORDER": ",".join(rotation)})
    except Exception as exc:  # noqa: BLE001
        log.exception("setplan: ошибка записи .env")
        await message.answer(f"❌ Не удалось сохранить: {exc}", parse_mode="HTML")
        return

    # Пересоздаём systemd timer: N равномерных точек в сутках (интервал = 24h/N).
    timer_ok, timer_detail = _update_systemd_timer(total)
    interval_min = round(1440 / total)
    if timer_ok:
        timer_msg = (f"\n\n🕐 Расписание слотов обновлено: {total} раз в день "
                     f"(каждые ~{interval_min} мин).\nТочки: {timer_detail}")
        log.info("setplan: timer обновлён (%d слотов, интервал ~%d мин)", total, interval_min)
    else:
        timer_msg = f"\n\n⚠️ Расписание слотов НЕ обновлено (осталось старое): {timer_detail}"
        log.error("setplan: timer update failed: %s", timer_detail)

    log.info("setplan: %s by user=%s", counts,
             message.from_user.id if message.from_user else "?")
    warn = ""
    if counts.get("news", 0) > 1:
        warn = ("\n\n⚠️ Новостей больше 1/день: проверяемых свежих новостей в пуле "
                "мало, в отдельные дни новостных статей может выйти меньше.")
    await message.answer(
        "✅ План обновлён.\n\n"
        + _format_plan(counts, total)
        + timer_msg
        + warn,
        parse_mode="HTML",
    )
