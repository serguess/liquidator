"""
Лёгкий индекс опубликованных и готовых статей. Без полного текста.

Зачем: архитектору (агент 3) и критику (агент 8) нужно знать ЧТО уже
написано, чтобы:
- ставить правильные внутренние ссылки (без галлюцинаций slug-ов);
- не повторять структуру H2/тему/угол;
- видеть какие смежные темы уже покрыты.

При этом читать полные тексты статей — слишком дорого по токенам
(15-20 статей по 7000 знаков = 100k+ знаков на каждый запуск архитектора).
Поэтому индекс содержит только метаданные:

- slug, category, url
- title, h1, main_keyword, secondary_keywords
- h2_topics — список заголовков H2 (формулировки, без текста блоков)
- intent (cold/warm/hot), writer_route (A/B)
- char_count, published_at
- tags

Размер на 100 статей ~30-50 KB JSON — это <10k токенов даже на большом каталоге.

Источники:
- `articles/{cat}/*.html` — опубликованные статьи (берём meta из соседнего .meta.json
  или парсим из HTML), извлекаем H2.
- `drafts/{slug}/meta.json` + `drafts/{slug}/article.html` — драфты,
  одобренные ботом (status=approved/published в bot_state.json).

Запуск:
    python -m tools.build_published_index
    python -m tools.build_published_index --include-drafts  # включить драфты pending_review
    python -m tools.build_published_index --json            # вывод в stdout, не в файл

Выход: `data/published_index.json`
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = PROJECT_ROOT / "articles"
DRAFTS_DIR = PROJECT_ROOT / "drafts"
DATA_DIR = PROJECT_ROOT / "data"
INDEX_PATH = DATA_DIR / "published_index.json"
BOT_STATE_PATH = DATA_DIR / "bot_state.json"

# === Извлечение H2 из HTML/MD ===

H2_HTML_RX = re.compile(r"<h2\b[^>]*>(.*?)</h2>", re.DOTALL | re.IGNORECASE)
H1_HTML_RX = re.compile(r"<h1\b[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
TITLE_HTML_RX = re.compile(r"<title\b[^>]*>(.*?)</title>", re.DOTALL | re.IGNORECASE)
TAG_RX = re.compile(r"<[^>]+>")


def _extract_text(rx: re.Pattern, html: str) -> str:
    """Достаёт текст первого совпадения regex, убирает теги и схлопывает пробелы."""
    m = rx.search(html)
    if not m:
        return ""
    text = TAG_RX.sub(" ", m.group(1)).strip()
    return re.sub(r"\s+", " ", text)


def _keyword_from_title(text: str) -> str:
    """Главный ключ из заголовка: часть до первого двоеточия (H1 у нас вида
    «Ключ: уточнение»). Нормализацию в слова делает сам cannibalization_check."""
    if not text:
        return ""
    head = re.split(r"[:|]", text, maxsplit=1)[0].strip()
    return head or text.strip()


def _extract_h2_from_html(html: str, limit: int = 12) -> list[str]:
    """Возвращает список H2-формулировок. Ограничиваем чтобы индекс не разбух."""
    titles = []
    for m in H2_HTML_RX.finditer(html):
        text = TAG_RX.sub("", m.group(1)).strip()
        text = re.sub(r"\s+", " ", text)
        if text and text not in titles:
            titles.append(text[:120])
        if len(titles) >= limit:
            break
    return titles


def _extract_h2_from_md(md: str, limit: int = 12) -> list[str]:
    titles = []
    for line in md.splitlines():
        if line.startswith("## "):
            text = line[3:].strip()
            if text and text not in titles:
                titles.append(text[:120])
            if len(titles) >= limit:
                break
    return titles


def _read_meta(meta_path: Path) -> dict:
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _read_brief(brief_path: Path) -> dict:
    """brief.json — там есть intent и writer_route, которых может не быть в meta."""
    return _read_meta(brief_path)  # та же логика чтения


def _count_chars_html(html: str) -> int:
    """Грубая оценка длины авторского текста (без head, script, blockquote)."""
    cleaned = re.sub(r"<head\b[^>]*>.*?</head>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<script\b[^>]*>.*?</script>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<style\b[^>]*>.*?</style>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = re.sub(r"<blockquote\b[^>]*>.*?</blockquote>", " ", cleaned, flags=re.DOTALL | re.IGNORECASE)
    cleaned = TAG_RX.sub(" ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return len(cleaned)


def _build_entry(slug: str, category: str, html_path: Path,
                 source: str = "published") -> dict | None:
    """Собирает запись индекса для одной статьи."""
    if not html_path.exists():
        return None

    folder = html_path.parent
    # Для articles/{cat}/{slug}.html meta лежит рядом, для drafts — в той же папке
    if source == "published":
        meta_path = html_path.with_suffix(".meta.json")
        brief_path = None  # для опубликованных brief обычно нет
    else:
        meta_path = folder / "meta.json"
        brief_path = folder / "brief.json"

    meta = _read_meta(meta_path)
    brief = _read_brief(brief_path) if brief_path else {}

    try:
        html = html_path.read_text(encoding="utf-8")
    except OSError:
        return None

    h2_topics = _extract_h2_from_html(html)
    char_count = meta.get("text_chars") or _count_chars_html(html)

    # У опубликованных статей рядом нет meta.json/brief.json (drafts подчищаются),
    # поэтому title/h1/ключи в meta пустые → cannibalization_check их не видит
    # (запись с пустым main_keyword отбрасывается). Достаём из самого HTML.
    html_title = _extract_text(TITLE_HTML_RX, html)
    html_h1 = _extract_text(H1_HTML_RX, html)
    title = meta.get("title") or brief.get("title") or html_title or ""
    h1 = meta.get("h1") or brief.get("h1") or html_h1 or ""
    main_keyword = (meta.get("main_keyword") or brief.get("main_keyword")
                    or _keyword_from_title(h1 or title) or "")
    # secondary НЕ заполняем из h2_topics: длинные H2-фразы раздувают keys_set
    # и занижают индекс Жаккара в cannibalization_check. H2 остаётся отдельным полем.
    secondary_keywords = (meta.get("secondary_keywords")
                          or brief.get("secondary_keywords") or [])

    if source == "published":
        url = f"/articles/{category}/{slug}.html"
    else:
        url = None  # драфт ещё не имеет публичного URL

    entry = {
        "slug": slug,
        "category": category,
        "source": source,  # 'published' | 'draft_approved' | 'draft_pending'
        "url": url,
        "title": title,
        "h1": h1,
        "main_keyword": main_keyword,
        "secondary_keywords": secondary_keywords,
        "intent": brief.get("intent") or meta.get("intent") or "",
        "writer_route": brief.get("writer_route") or meta.get("writer_route") or "",
        "h2_topics": h2_topics,
        "char_count": char_count,
        "published_at": meta.get("published_at"),
        "tags": meta.get("tags") or [],
    }
    return entry


def _bot_review_status() -> dict[str, str]:
    """Читает bot_state.json и возвращает {slug: status}."""
    if not BOT_STATE_PATH.exists():
        return {}
    try:
        state = json.loads(BOT_STATE_PATH.read_text(encoding="utf-8"))
        return {
            slug: data.get("status", "")
            for slug, data in (state.get("reviews") or {}).items()
        }
    except (json.JSONDecodeError, OSError):
        return {}


def build_index(include_drafts: bool = True) -> dict:
    articles: list[dict] = []

    # 1. Опубликованные на сайте
    if ARTICLES_DIR.exists():
        for cat_dir in ARTICLES_DIR.iterdir():
            if not cat_dir.is_dir():
                continue
            category = cat_dir.name
            for html_file in cat_dir.glob("*.html"):
                slug = html_file.stem
                entry = _build_entry(slug, category, html_file, source="published")
                if entry:
                    articles.append(entry)

    # 2. Драфты (опционально, по умолчанию включаем — архитектору полезно знать что в работе)
    bot_status = _bot_review_status()
    if include_drafts and DRAFTS_DIR.exists():
        for slug_dir in DRAFTS_DIR.iterdir():
            if not slug_dir.is_dir() or slug_dir.name.startswith("_"):
                continue
            slug = slug_dir.name
            # Берём article-v2.html если есть, иначе article.html
            html_path = slug_dir / "article-v2.html"
            if not html_path.exists():
                html_path = slug_dir / "article.html"
            if not html_path.exists():
                continue

            meta = _read_meta(slug_dir / "meta.json")
            category = meta.get("category", "fiz")

            status = bot_status.get(slug, "")
            source = "draft_approved" if status in ("approved", "published") \
                else ("draft_pending" if status == "pending_review" else "draft_local")

            entry = _build_entry(slug, category, html_path, source=source)
            if entry:
                articles.append(entry)

    # Сортировка: published → draft_approved → draft_pending → draft_local
    source_order = {"published": 0, "draft_approved": 1, "draft_pending": 2, "draft_local": 3}
    articles.sort(key=lambda a: (source_order.get(a["source"], 9), a.get("category", ""), a["slug"]))

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total": len(articles),
        "by_source": {
            src: sum(1 for a in articles if a["source"] == src)
            for src in ("published", "draft_approved", "draft_pending", "draft_local")
        },
        "by_category": {
            cat: sum(1 for a in articles if a["category"] == cat)
            for cat in {a["category"] for a in articles}
        },
        "articles": articles,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Сборщик лёгкого индекса опубликованных статей")
    parser.add_argument("--include-drafts", action="store_true", default=True,
                        help="Включать ли драфты (по умолчанию да — архитектору нужно знать про статьи в работе)")
    parser.add_argument("--no-drafts", dest="include_drafts", action="store_false",
                        help="Только опубликованные на сайте")
    parser.add_argument("--json", action="store_true",
                        help="Печатать JSON в stdout вместо записи в data/published_index.json")
    args = parser.parse_args()

    index = build_index(include_drafts=args.include_drafts)

    if args.json:
        print(json.dumps(index, ensure_ascii=False, indent=2))
    else:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        INDEX_PATH.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"OK: индекс {len(index['articles'])} статей записан в {INDEX_PATH.relative_to(PROJECT_ROOT)}")
        print(f"  По источникам: {index['by_source']}")
        print(f"  По категориям: {index['by_category']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
