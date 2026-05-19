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


def _metric_emoji(*, value: int | float, target: float, lower_is_better: bool) -> str:
    """Возвращает ✅ если значение в норме, ⚠ если хуже целевого."""
    if value is None:
        return ""
    if lower_is_better:
        return "✅" if value <= target else "⚠"
    return "✅" if value >= target else "⚠"


def new_draft_notification(
    *,
    slug: str,
    category: str,
    title: str,
    version: str,
    char_count: int,
    token: str,
    predicted_spam: int | None = None,
    predicted_uniqueness: int | None = None,
    predicted_ai: int | None = None,
    customer_risks: list[str] | None = None,
    wordstat_main: int | None = None,
    wordstat_total: int | None = None,
) -> str:
    """
    Уведомление о новом драфте.

    Включает:
    - Прогноз text.ru-метрик (заспам, уникальность, AI-detector) с эмодзи-статусом
    - Список рисков на языке заказчика (только если есть)
    - Wordstat-частоты ключа (если посчитаны)
    - Ссылку на превью

    Прогнозы откалиброваны под локальные эвристики (точность ±5-7%) - реальные
    метрики text.ru недоступны без API.
    """
    cat = category_label(category)
    url = preview_url(slug, token, version)
    chars_str = f"{char_count:,}".replace(",", " ")

    # === Блок прогнозов ===
    metrics_block = ""
    if predicted_spam is not None or predicted_ai is not None or predicted_uniqueness is not None:
        lines = ["", "📊 <b>Прогноз метрик:</b>"]
        if predicted_spam is not None:
            emoji = _metric_emoji(value=predicted_spam, target=50, lower_is_better=True)
            lines.append(f"   Заспам:       ~{predicted_spam}% {emoji} (цель ≤50%)")
        if predicted_uniqueness is not None:
            emoji = _metric_emoji(value=predicted_uniqueness, target=85, lower_is_better=False)
            lines.append(f"   Уникальность: ~{predicted_uniqueness}% {emoji} (цель ≥85%)")
        if predicted_ai is not None:
            emoji = _metric_emoji(value=predicted_ai, target=10, lower_is_better=True)
            lines.append(f"   AI-detector:  ~{predicted_ai}% {emoji} (цель ≤10%)")
        metrics_block = "\n".join(lines)

    # === Блок рисков (только если есть) ===
    risks_block = ""
    if customer_risks:
        risk_lines = [f"   • {html.escape(r)}" for r in customer_risks]
        risks_block = "\n\n⚠ <b>Возможные риски:</b>\n" + "\n".join(risk_lines)

    # === Wordstat (опционально) ===
    wordstat_block = ""
    if wordstat_main is not None:
        formatted_main = f"{wordstat_main:,}".replace(",", " ")
        line = f"\n\n📈 <b>Wordstat (главный ключ):</b> {formatted_main}/мес"
        if wordstat_total is not None and wordstat_total > wordstat_main:
            formatted_total = f"{wordstat_total:,}".replace(",", " ")
            line += f" (с вторичными: {formatted_total}/мес)"
        wordstat_block = line

    return (
        "🆕 <b>Новая статья на ревью</b>\n\n"
        f"<b>[{html.escape(cat)}]</b> {html.escape(title)}\n"
        f"📏 {chars_str} знаков"
        + (("\n" + metrics_block) if metrics_block else "")
        + risks_block
        + wordstat_block
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
        f"🔗 <a href=\"{html.escape(public_url)}\">{html.escape(public_url)}</a>"
    )


def rejected(*, title: str, reason: str | None) -> str:
    reason_part = f"\n\n<b>Причина:</b> {html.escape(reason)}" if reason else ""
    return f"🗑 <b>Статья отклонена</b>\n\n{html.escape(title)}{reason_part}"


# Маркеры в конце prompt-сообщений: позволяют восстановить slug из
# message.reply_to_message.text если FSM-state потерян (например, контейнер
# был редеплоен между нажатием кнопки и отправкой ответа). Видимы в чате
# как тонкий моноспейс — UX это не ломает, а надёжность даёт.

def edit_marker(slug: str) -> str:
    return f"\n\n<code>↩️ edit:{html.escape(slug)}</code>"


def reject_marker(slug: str) -> str:
    return f"\n\n<code>↩️ reject:{html.escape(slug)}</code>"


def asking_for_edit(*, title: str, slug: str) -> str:
    return (
        f"✏️ <b>Правки для статьи «{html.escape(title)}»</b>\n\n"
        "Опишите что изменить - текстом или голосовым. "
        "Например: <i>«убери блок про ИП», «перепиши абзац про сроки мягче», "
        "«замени слово X на Y во втором разделе»</i>.\n\n"
        "Когда готово — <b>ответьте reply'ем на это сообщение</b>."
        + edit_marker(slug)
    )


def asking_for_rejection_reason(*, title: str, slug: str) -> str:
    return (
        f"🗑 <b>Отклонение статьи «{html.escape(title)}»</b>\n\n"
        "Напишите коротко почему отклоняете - чтобы команда могла учесть на будущее. "
        "Если без причины - отправьте «-».\n\n"
        "<b>Ответьте reply'ем на это сообщение.</b>"
        + reject_marker(slug)
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
