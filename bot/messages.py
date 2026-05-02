"""
Шаблоны сообщений в Telegram.

Используем HTML-форматирование (parse_mode="HTML"). Markdown в TG капризнее
с экранированием, HTML удобнее для вёрстки кнопок и ссылок.
"""

from __future__ import annotations

import html

from .config import PUBLIC_BASE_URL, category_label


def preview_url(slug: str, token: str, version: str | None = None) -> str:
    base = f"{PUBLIC_BASE_URL}/p/{slug}?t={token}"
    if version:
        base += f"&v={version}"
    return base


def new_draft_notification(
    *,
    slug: str,
    category: str,
    title: str,
    version: str,
    char_count: int,
    token: str,
    uniqueness_pct: float | None = None,
) -> str:
    cat = category_label(category)
    url = preview_url(slug, token, version)
    uniq_line = ""
    if uniqueness_pct is not None:
        uniq_line = f"\n<b>Уникальность:</b> {uniqueness_pct:.0f}% (text.ru)"

    return (
        "📰 <b>Новая статья на ревью</b>\n\n"
        f"<b>Тема:</b> {html.escape(title)}\n"
        f"<b>Категория:</b> {html.escape(cat)}\n"
        f"<b>Длина:</b> {char_count:,} знаков".replace(",", " ")
        + uniq_line
        + f"\n\n🔗 <a href=\"{url}\">Прочитать статью</a>"
    )


def edit_applied(
    *,
    slug: str,
    new_version: str,
    summary: str,
    char_count: int,
    prev_char_count: int | None,
    token: str,
    uniqueness_pct: float | None = None,
    fact_warnings: list[str] | None = None,
) -> str:
    """Сообщение после применения правки."""
    url = preview_url(slug, token, new_version)
    summary_html = html.escape(summary).replace("\n", "\n")

    delta = ""
    if prev_char_count is not None:
        diff = char_count - prev_char_count
        sign = "+" if diff >= 0 else ""
        delta = f" ({sign}{diff:,})".replace(",", " ")

    uniq_line = ""
    if uniqueness_pct is not None:
        uniq_line = f"\n<b>Уникальность:</b> {uniqueness_pct:.0f}%"

    warnings = ""
    if fact_warnings:
        items = "\n".join(f"  • {html.escape(w)}" for w in fact_warnings)
        warnings = f"\n\n⚠️ <b>Замечания по фактам:</b>\n{items}"

    return (
        f"✏️ <b>Версия {html.escape(new_version)} готова</b>\n\n"
        f"<b>Что изменено:</b>\n{summary_html}\n\n"
        f"<b>Длина:</b> {char_count:,} знаков{delta}".replace(",", " ")
        + uniq_line
        + warnings
        + f"\n\n🔗 <a href=\"{url}\">Прочитать версию {html.escape(new_version)}</a>"
    )


def published(*, title: str, public_url: str) -> str:
    return (
        f"✅ <b>Опубликовано</b>\n\n"
        f"{html.escape(title)}\n"
        f"🔗 <a href=\"{html.escape(public_url)}\">{html.escape(public_url)}</a>\n\n"
        "Запросы на индексацию отправлены в Яндекс."
    )


def rejected(*, title: str, reason: str | None) -> str:
    reason_part = f"\n\n<b>Причина:</b> {html.escape(reason)}" if reason else ""
    return f"🗑 <b>Статья отклонена</b>\n\n{html.escape(title)}{reason_part}"


def asking_for_edit(*, title: str) -> str:
    return (
        f"✏️ <b>Правки для статьи «{html.escape(title)}»</b>\n\n"
        "Опишите что изменить - текстом или голосовым. "
        "Например: <i>«убери блок про ИП», «перепиши абзац про сроки мягче», "
        "«замени слово X на Y во втором разделе»</i>.\n\n"
        "Когда готово — отправьте сообщение."
    )


def asking_for_rejection_reason(*, title: str) -> str:
    return (
        f"🗑 <b>Отклонение статьи «{html.escape(title)}»</b>\n\n"
        "Напишите коротко почему отклоняете - чтобы команда могла учесть на будущее. "
        "Если без причины - отправьте «-»."
    )


def edit_in_progress() -> str:
    return "⏳ Применяю правку, это займёт ~30 секунд..."


def edit_failed(error: str) -> str:
    return (
        "❌ <b>Не удалось применить правку</b>\n\n"
        f"<code>{html.escape(error)}</code>\n\n"
        "Попробуйте переформулировать или напишите по-другому."
    )


def voice_transcribed(text: str) -> str:
    return (
        "🎤 <b>Распознал голосовое:</b>\n\n"
        f"<i>{html.escape(text)}</i>\n\n"
        "Применяю как правку..."
    )


def access_denied() -> str:
    return (
        "⛔ Доступ запрещён.\n\n"
        "Этот бот настроен на конкретный chat. "
        "Если вы заказчик и попали сюда впервые - "
        "пришлите ваш chat_id администратору."
    )


def help_text() -> str:
    return (
        "<b>Что умеет этот бот</b>\n\n"
        "1. Когда команда готовит новую статью, бот присылает уведомление "
        "со ссылкой на статью и тремя кнопками:\n"
        "   • <b>✅ Опубликовать</b> - выложить на сайт.\n"
        "   • <b>✏️ Правки</b> - попросить изменения (текстом или голосом).\n"
        "   • <b>🗑 Отклонить</b> - не публиковать, отдать на переделку.\n\n"
        "2. После правки бот через ~30 секунд присылает новую версию. "
        "Можно править сколько угодно раз.\n\n"
        "3. Голосовые сообщения распознаются автоматически.\n\n"
        "Команды:\n"
        "/pending - показать статьи, ожидающие ревью\n"
        "/help - эта справка"
    )
