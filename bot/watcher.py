"""
Watcher: сканирует drafts/ и находит новые драфты, которых ещё нет в bot_state.

Запускается периодически из bot/main.py (через asyncio).
Также есть CLI-режим для разовых проверок: `python -m bot.watcher`.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import notified_sentinel, state
from .config import DRAFTS_DIR

# Feature flag: требовать sentinel `.pushed` перед отправкой уведомления.
# На VPS включаем (REQUIRE_PUSHED_SENTINEL=true в .env) — закрывает race
# между ready_for_review=true и реальным git push (Cloud Apps может ещё
# не подтянуть статью, заказчик получит 404 на /preview).
# На Cloud Apps до миграции оставляем выключенным — иначе обновлённый
# watcher.py не увидит sentinel в существующих драфтах и не отправит
# уведомления до следующего scheduler-слота.
REQUIRE_PUSHED_SENTINEL = os.getenv("REQUIRE_PUSHED_SENTINEL", "").strip().lower() in ("1", "true", "yes", "on")


def _read_meta(folder: Path) -> dict:
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _extract_title_from_html(html_path: Path) -> str:
    """Fallback если в meta.json нет title - вытащим из <title>."""
    try:
        text = html_path.read_text(encoding="utf-8")
    except OSError:
        return html_path.parent.name
    m = re.search(r"<title>(.+?)</title>", text, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m = re.search(r'<h1[^>]*class="[^"]*article__title[^"]*"[^>]*>(.+?)</h1>', text, re.DOTALL)
    if m:
        return re.sub(r"<[^>]+>", "", m.group(1)).strip()
    return html_path.parent.name


def _count_text_chars(html_path: Path) -> int:
    """
    Считает символы авторского текста ТАКЖЕ как quality_gate (tools/quality_checks):
    только содержимое <article>...</article>, без header/footer/sidebar/CTA/JSON-LD/FAQ-вопросов.
    Раньше считали весь body — цифра была на 2-3 тысячи больше реальной.
    """
    try:
        from tools.quality_checks import extract_author_text_from_html
    except ImportError:
        extract_author_text_from_html = None
    try:
        text = html_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    if extract_author_text_from_html:
        return len(extract_author_text_from_html(text))
    # Fallback: тот же алгоритм но без выреза footer-а — лучше чем ничего.
    text = re.sub(r"<script\b[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<style\b[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<head\b[^>]*>.*?</head>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[a-zA-Z]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return len(text.strip())


def _resolve_current_html(folder: Path) -> Path | None:
    """
    Возвращает путь к актуальной версии статьи в папке драфта.

    Приоритет: article-v2.html → article.html. Это то, что показывают заказчику.
    """
    v2 = folder / "article-v2.html"
    if v2.exists():
        return v2
    v1 = folder / "article.html"
    if v1.exists():
        return v1
    return None


def _ensure_versions_dir(folder: Path, current_html: Path) -> tuple[Path, str]:
    """
    Создаёт versions/v2.0.html если ещё нет (копия текущего article-*.html).
    Возвращает (путь к v2.0.html, "2.0").
    """
    versions_dir = folder / "versions"
    versions_dir.mkdir(exist_ok=True)
    v20 = versions_dir / "v2.0.html"
    if not v20.exists():
        v20.write_text(current_html.read_text(encoding="utf-8"), encoding="utf-8")
    return v20, "2.0"


def scan_for_new_drafts() -> list[dict]:
    """
    Возвращает список новых драфтов, которых ещё нет в bot_state.

    Каждый элемент:
    {
      "slug": ...,
      "category": ...,
      "title": ...,
      "version": "2.0",
      "char_count": ...,
      "current_html": Path,
    }
    """
    if not DRAFTS_DIR.exists():
        return []

    known = state.known_slugs()
    new_drafts = []

    for sub in sorted(DRAFTS_DIR.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_") or sub.name.startswith("."):
            continue

        slug = sub.name

        # Двойная проверка: пропускаем если ХОТЯ БЫ ОДИН источник правды
        # говорит что этот draft уже был отправлен. Если bot_state.json
        # сбросился — нас спасёт sentinel-файл в папке драфта (он
        # коммитится в git вместе с папкой). Если sentinel удалён руками —
        # нас спасёт bot_state. Чтобы повторное уведомление пришло,
        # должны исчезнуть ОБА маркера одновременно.
        if slug in known:
            continue
        if notified_sentinel.is_notified(sub):
            # Sentinel есть, но slug не в state.reviews (иначе сработал бы
            # `slug in known` выше). Это значит bot_state был сброшен после
            # уведомления — например, ручной чисткой при пересоздании драфтов.
            # В таком состоянии старое Telegram-сообщение с кнопкой
            # «Опубликовать» отвечает «Статья не найдена», потому что
            # handlers.on_publish_pressed читает state.get_review(slug) → None.
            #
            # Восстанавливаем запись в state ТИХО (без повторного уведомления),
            # чтобы кнопки в существующих сообщениях снова работали.
            # Делаем только если draft реально готов (ready_for_review=true).
            meta = _read_meta(sub)
            if meta.get("ready_for_review"):
                category = meta.get("category", "fiz")
                title = meta.get("title") or meta.get("h1") or sub.name
                try:
                    state.add_review(slug, category=category, title=title, version="2.0")
                    print(f"watcher: восстановлена запись state для {slug} "
                          f"(sentinel был, state пустой - повторного уведомления нет)")
                except Exception as exc:
                    print(f"watcher: не удалось восстановить state для {slug}: {exc}")
            continue

        current_html = _resolve_current_html(sub)
        if not current_html:
            continue

        meta = _read_meta(sub)

        # Race-condition guard: ждём пока агент 7 поставит ready_for_review=true.
        # Иначе watcher шлёт уведомление до того как агент 7 сгенерил обложку
        # и финализировал meta.json. Заказчик получает статью без cover.
        # Агент 7 пишет ready_for_review=true в самом конце своей работы.
        # Если флаг отсутствует/false - draft ещё не готов, ждём следующий тик watcher'а.
        if not meta.get("ready_for_review"):
            continue

        # Дополнительный race-guard на VPS: между ready_for_review=true и
        # реальным git push scheduler делает ещё несколько шагов (15-60 сек).
        # Если watcher отправит уведомление в это окно, заказчик кликнет на
        # /preview ссылку и получит 404 потому что Cloud Apps ещё не подтянул
        # новый коммит. Sentinel .pushed создаёт scheduler ТОЛЬКО после
        # успешного git push.
        # Управляется флагом REQUIRE_PUSHED_SENTINEL=true в .env (только VPS).
        # На Cloud Apps флаг выключен — там scheduler в одном процессе с
        # watcher, race-окно физически отсутствует.
        if REQUIRE_PUSHED_SENTINEL and not (sub / ".pushed").exists():
            continue

        category = meta.get("category", "fiz")
        title = meta.get("title") or meta.get("h1") or _extract_title_from_html(current_html)
        # Приоритет: text_chars из meta.json (его пишет quality_gate точно).
        # Fallback на _count_text_chars если meta нет.
        meta_chars = meta.get("text_chars")
        char_count = int(meta_chars) if isinstance(meta_chars, (int, float)) and meta_chars > 0 \
            else _count_text_chars(current_html)

        # Wordstat-частоты (агент 1 уже их посчитал и положил в meta.json).
        # Если их нет (API недоступен / старая статья) - просто None, бот не покажет.
        wordstat_main = meta.get("frequency_main")
        wordstat_total = meta.get("frequency_total")

        # Прогнозы text.ru-метрик и риски (заполнены quality_gate'ом).
        # Если их нет (старая статья до внедрения прогнозов) - бот покажет статью без них.
        predicted_spam = meta.get("predicted_spam_pct")
        predicted_ai = meta.get("predicted_ai_pct")
        predicted_uniqueness = meta.get("predicted_uniqueness_pct")
        customer_risks = meta.get("customer_risks") or []

        # Создаём v2.0 в versions/.
        _, version = _ensure_versions_dir(sub, current_html)

        new_drafts.append({
            "slug": slug,
            "category": category,
            "title": title,
            "version": version,
            "char_count": char_count,
            "current_html": current_html,
            "wordstat_main": wordstat_main,
            "wordstat_total": wordstat_total,
            "predicted_spam": predicted_spam,
            "predicted_ai": predicted_ai,
            "predicted_uniqueness": predicted_uniqueness,
            "customer_risks": customer_risks,
        })

    return new_drafts


def register_draft(draft: dict) -> dict:
    """Заносит драфт в bot_state как pending_review."""
    return state.add_review(
        draft["slug"],
        category=draft["category"],
        title=draft["title"],
        version=draft["version"],
    )


if __name__ == "__main__":
    # CLI-режим: показать что считаем новым.
    import sys
    found = scan_for_new_drafts()
    if not found:
        print("Новых драфтов не найдено")
        sys.exit(0)
    print(f"Найдено новых драфтов: {len(found)}")
    for d in found:
        print(f"  [{d['category']}] {d['slug']}  ({d['char_count']:,} зн)  - {d['title']}")
