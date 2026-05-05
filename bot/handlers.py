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
def review_keyboard(slug: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Опубликовать", callback_data=f"publish:{slug}")],
        [InlineKeyboardButton(text="✏️ Правки", callback_data=f"edit:{slug}")],
        [InlineKeyboardButton(text="🗑 Отклонить", callback_data=f"reject:{slug}")],
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
    if not _is_allowed(query):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    slug = query.data.removeprefix("edit:")
    review = state.get_review(slug)
    if not review:
        await query.answer("Статья не найдена", show_alert=True)
        return

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
    await query.answer()


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


@router.message(EditFlow.waiting_for_edit_text, F.text)
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

    slug = query.data.removeprefix("publish:")
    review = state.get_review(slug)
    if not review:
        await query.answer("Статья не найдена", show_alert=True)
        return

    if review.get("status") == "published":
        await query.answer("Уже опубликовано", show_alert=True)
        return

    title = review.get("title", slug)
    current_version = review.get("current_version", "2.0")

    # Если scheduler сейчас пишет статью — публикация немедленно сделала бы
    # git push, что триггерит redeploy Cloud Apps и убьёт работающий
    # контейнер вместе с ещё-не-закоммиченным draft'ом текущего слота.
    # Поэтому ставим в очередь, queue_processor исполнит после снятия lock.
    scheduler_active, lock_age = action_queue.is_scheduler_active()
    if scheduler_active:
        eta_sec = action_queue.estimate_eta_sec()
        eta_min = max(1, (eta_sec + 59) // 60)
        action_queue.enqueue_publish(
            slug=slug, version=current_version,
            chat_id=query.message.chat.id,
            reply_to_message_id=query.message.message_id,
        )
        queue_pos = sum(
            1 for item in action_queue.peek()
            if item.get("action") == "publish"
        )
        await query.answer(
            f"📋 В очереди (#{queue_pos}). Опубликую через ~{eta_min} мин.",
            show_alert=True,
        )
        await query.message.answer(
            f"📋 <b>«{title}»</b> поставлено в очередь на публикацию.\n\n"
            f"Сейчас scheduler пишет другую статью (идёт уже {int(lock_age)//60} мин). "
            f"Опубликую как только он закончит — примерно через {eta_min} мин.\n\n"
            f"Позиция в очереди: #{queue_pos}",
            parse_mode="HTML",
        )
        return

    await query.answer()
    progress_msg = await query.message.answer(
        "📤 Публикую: генерирую обложку, переношу файлы, обновляю индексы и пушу в репо…\n"
        "Это займёт 30-60 секунд."
    )

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

    cover_line = ""
    if result.cover_url:
        cover_line = f"\n🖼 <a href=\"{result.cover_url}\">Обложка</a>"

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
        f"{cover_line}{wordstat_line}\n\n"
        "Статья перенесена в articles/, drafts/ заархивирован, "
        "articles.json и sitemap.xml обновлены, изменения запушены в main.",
        parse_mode="HTML",
        disable_web_page_preview=False,
    )


# ============ Callback: 🗑 Отклонить ============
@router.callback_query(F.data.startswith("reject:"))
async def on_reject_pressed(query: CallbackQuery, fsm: FSMContext):
    if not _is_allowed(query):
        await query.answer("⛔ Доступ запрещён", show_alert=True)
        return

    slug = query.data.removeprefix("reject:")
    review = state.get_review(slug)
    if not review:
        await query.answer("Статья не найдена", show_alert=True)
        return

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
    await query.answer()


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


@router.message(F.text, F.reply_to_message)
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
