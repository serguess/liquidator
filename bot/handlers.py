"""
Обработчики команд и inline-кнопок Telegram.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from . import editor, messages, state, transcribe
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
    await query.message.answer(
        messages.asking_for_edit(title=review.get("title", slug)),
        parse_mode="HTML",
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
    data = await fsm.get_data()
    slug = data.get("slug")
    await fsm.clear()

    if not slug:
        await message.answer("Сессия правки потерялась. Нажмите ✏️ Правки заново.")
        return

    review = state.get_review(slug)
    if not review:
        await message.answer("Статья пропала из архива. Странно. Свяжитесь с командой.")
        return

    progress_msg = await message.answer(messages.edit_in_progress(), parse_mode="HTML")

    # Тяжёлый вызов Claude Code - в отдельном потоке, чтобы не блокировать polling.
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
        await message.answer(
            messages.edit_failed(result.error or "неизвестная ошибка"),
            parse_mode="HTML",
        )
        return

    # Обновляем state.
    prev_char_count = None  # Можно посчитать из предыдущей версии если надо.
    state.add_edit(slug, new_version=result.new_version, edit_text=edit_text)

    token = state.get_preview_token_from_state()
    await message.answer(
        messages.edit_applied(
            slug=slug,
            new_version=result.new_version,
            summary=result.summary,
            char_count=result.char_count or 0,
            prev_char_count=prev_char_count,
            token=token,
            uniqueness_pct=None,  # text.ru подключим во второй итерации
            fact_warnings=None,
        ),
        parse_mode="HTML",
        reply_markup=review_keyboard(slug),
        disable_web_page_preview=False,
    )


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

    # Публикация подключается во втором этапе. Пока заглушка.
    await query.answer()
    await query.message.answer(
        "🚧 Публикация ещё в разработке.\n\n"
        "Скоро статья будет автоматически переноситься из drafts/ в articles/, "
        "обновлять articles.json и sitemap.xml, и пинговать IndexNow."
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
    await query.message.answer(
        messages.asking_for_rejection_reason(title=review.get("title", slug)),
        parse_mode="HTML",
    )
    await query.answer()


@router.message(EditFlow.waiting_for_rejection_reason, F.text)
async def on_rejection_reason(message: Message, fsm: FSMContext):
    if not _is_allowed(message):
        return
    data = await fsm.get_data()
    slug = data.get("slug")
    await fsm.clear()
    if not slug:
        await message.answer("Сессия отклонения потерялась.")
        return
    review = state.get_review(slug)
    if not review:
        return
    reason = message.text.strip()
    if reason == "-":
        reason = None
    state.set_status(slug, "rejected", rejection_reason=reason)
    await message.answer(
        messages.rejected(title=review.get("title", slug), reason=reason),
        parse_mode="HTML",
    )
