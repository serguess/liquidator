"""
Watcher: сканирует drafts/ и находит новые драфты, которых ещё нет в bot_state.

Запускается периодически из bot/main.py (через asyncio).
Также есть CLI-режим для разовых проверок: `python -m bot.watcher`.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import notified_sentinel, state
from .config import DRAFTS_DIR


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
            continue

        current_html = _resolve_current_html(sub)
        if not current_html:
            continue

        meta = _read_meta(sub)
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
