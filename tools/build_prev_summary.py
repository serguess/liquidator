"""
Сборка короткого summary последней статьи в категории — для антишаблонности.

Раньше: писатель (агент 4) и архитектор (агент 3) читали ПОЛНЫЙ текст
последней статьи в категории, чтобы «не повторять формулу финала и H2».
Это 5-15k токенов на каждый прогон, при том что нужны только:
  — первое предложение вступления
  — список H2 (порядок и формулировки)
  — формула финального абзаца
  — main_keyword

Теперь: runner перед запуском Claude вызывает этот скрипт. Скрипт
извлекает 5-6 строк из последней статьи и пишет в data/_prev_summary_{cat}.json.
Агенты 3 и 4 читают этот лёгкий JSON.

Источник «последней статьи»:
  1. articles/{cat}/*.html — опубликованные (приоритет).
  2. drafts/{slug}/article.html — драфты с meta.category=cat (если опубликованных нет).
Сортировка по mtime, берём самую свежую.

Запуск:
    python -m tools.build_prev_summary --category fiz
    python -m tools.build_prev_summary --category fiz --json
    python -m tools.build_prev_summary --all  # сразу по всем категориям

Вывод:
    data/_prev_summary_{cat}.json — на каждую категорию свой файл.
    Если в категории ничего нет — файл записывается с {"prev_article": null}.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
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

CATEGORIES = ["fiz", "yur", "vzysk", "news"]

# Регексы для парсинга HTML
H1_RX = re.compile(r"<h1[^>]*>(.*?)</h1>", re.DOTALL | re.IGNORECASE)
H2_RX = re.compile(r"<h2[^>]*>(.*?)</h2>", re.DOTALL | re.IGNORECASE)
P_RX = re.compile(r"<p[^>]*>(.*?)</p>", re.DOTALL | re.IGNORECASE)
TAG_RX = re.compile(r"<[^>]+>")
FAQ_HEADERS = {"частые вопросы", "часто задаваемые вопросы", "faq", "вопросы и ответы"}


def _strip_tags(html: str) -> str:
    text = TAG_RX.sub("", html)
    return re.sub(r"\s+", " ", text).strip()


def _first_sentence(text: str, max_chars: int = 200) -> str:
    """Первое предложение из текста, обрезка по точке/восклицанию/вопросу."""
    if not text:
        return ""
    text = text.strip()
    m = re.search(r"^(.{20,}?[.!?])(\s|$)", text)
    if m:
        s = m.group(1).strip()
    else:
        s = text[:max_chars].rsplit(" ", 1)[0]
    return s[:max_chars]


def _extract_h2_list(html: str, exclude_faq: bool = True) -> list[str]:
    """Все H2 из тела статьи. FAQ-блок исключаем (он не про антишаблонность)."""
    h2s = []
    for m in H2_RX.findall(html):
        clean = _strip_tags(m)
        if not clean:
            continue
        if exclude_faq and clean.lower().strip() in FAQ_HEADERS:
            # FAQ найден — обрываем (всё что после — вопросы FAQ)
            break
        h2s.append(clean)
    return h2s


def _extract_intro_and_final(html: str) -> tuple[str, str]:
    """
    Возвращает (intro_first_sentence, final_paragraph_formula).
    intro — первое предложение первого <p> до первого <h2>.
    final — последний абзац текста перед FAQ (или перед концом article__body).
    """
    # Берём только тело статьи, если есть article__body
    body_m = re.search(
        r'<article[^>]*class="[^"]*article__body[^"]*"[^>]*>(.*?)</article>',
        html, re.DOTALL | re.IGNORECASE,
    )
    body = body_m.group(1) if body_m else html

    # Intro: первый «значимый» <p>. Сначала ищем до первого <h2> (классический
    # формат: лид перед первым H2). Если там нет — берём первый <p> после первого
    # <h2> (формат когда H2 идёт сразу после обложки без лида).
    first_h2_pos = len(body)
    h2_match = re.search(r"<h2[^>]*>", body, re.IGNORECASE)
    if h2_match:
        first_h2_pos = h2_match.start()

    intro = ""
    for m in P_RX.findall(body[:first_h2_pos]):
        clean = _strip_tags(m)
        if clean and len(clean) > 30:
            intro = _first_sentence(clean)
            break

    if not intro:
        # Fallback: первый <p> после первого <h2>
        for m in P_RX.findall(body[first_h2_pos:]):
            clean = _strip_tags(m)
            if clean and len(clean) > 30:
                intro = _first_sentence(clean)
                break

    # Final: ищем последний <p> до FAQ-заголовка (или до конца)
    faq_pos = len(body)
    for m in re.finditer(r"<h2[^>]*>(.*?)</h2>", body, re.DOTALL | re.IGNORECASE):
        h_text = _strip_tags(m.group(1)).lower().strip()
        if h_text in FAQ_HEADERS:
            faq_pos = m.start()
            break
    pre_faq = body[:faq_pos]
    paras = [_strip_tags(m) for m in P_RX.findall(pre_faq)]
    paras = [p for p in paras if p and len(p) > 30]
    final = _first_sentence(paras[-1], max_chars=240) if paras else ""

    return intro, final


def _classify_final_formula(final_paragraph: str) -> str:
    """
    Грубая классификация формулы финала — чтобы писатель явно отстраивался.
    Простая эвристика по ключевым маркерам.
    """
    if not final_paragraph:
        return "unknown"
    f = final_paragraph.lower()
    if any(k in f for k in ["принимает суд", "решает суд", "окончательное решение"]):
        return "судья_решает"
    if any(k in f for k in ["обещ", "гарант", "если кто-то"]):
        return "предостережение_от_обещаний"
    if any(k in f for k in ["лучше", "рекоменд", "стоит обратиться", "имеет смысл"]):
        return "рекомендация_к_консультации"
    if any(k in f for k in ["важно знать", "помните", "учитывайте"]):
        return "напоминание_о_важности"
    if any(k in f for k in ["в нашей практике", "по нашему опыту", "мы видим"]):
        return "авторская_статистика"
    return "иное"


def _read_meta_for_article(article_path: Path) -> dict:
    """
    Пытается найти meta.json рядом со статьёй:
      - drafts/{slug}/article.html → drafts/{slug}/meta.json
      - articles/{cat}/{slug}.html → articles/{cat}/{slug}.meta.json
    Возвращает dict (может быть пустой).
    """
    parent = article_path.parent
    candidates = [
        parent / "meta.json",
        article_path.with_suffix(".meta.json"),
        parent / f"{article_path.stem}.meta.json",
    ]
    for c in candidates:
        if c.exists():
            try:
                return json.loads(c.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
    return {}


def find_latest_article(category: str, exclude_slug: str | None = None) -> Path | None:
    """
    Возвращает Path до самой свежей по mtime article.html в указанной категории.
    Приоритет: articles/{cat}/*.html > drafts/{slug}/article.html (с meta.category=cat).
    exclude_slug — пропустить указанный slug (не сравнивать статью саму с собой).
    """
    candidates: list[tuple[Path, float]] = []

    # 1. articles/{cat}/*.html
    cat_dir = ARTICLES_DIR / category
    if cat_dir.exists():
        for p in cat_dir.glob("*.html"):
            if p.name.endswith(".bak"):
                continue
            if exclude_slug and p.stem == exclude_slug:
                continue
            candidates.append((p, p.stat().st_mtime))

    # 2. drafts/{slug}/article.html — только из той же категории
    if DRAFTS_DIR.exists():
        for slug_dir in DRAFTS_DIR.iterdir():
            if not slug_dir.is_dir() or slug_dir.name.startswith("_"):
                continue
            if exclude_slug and slug_dir.name == exclude_slug:
                continue
            article = slug_dir / "article.html"
            if not article.exists():
                continue
            meta = _read_meta_for_article(article)
            if meta.get("category") != category:
                continue
            candidates.append((article, article.stat().st_mtime))

    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    return candidates[0][0]


def build_summary(category: str, exclude_slug: str | None = None) -> dict:
    """
    Возвращает summary последней статьи категории или {prev_article: null}.
    """
    article_path = find_latest_article(category, exclude_slug=exclude_slug)
    if not article_path:
        return {"category": category, "prev_article": None}

    try:
        html = article_path.read_text(encoding="utf-8")
    except OSError as exc:
        return {"category": category, "prev_article": None, "error": str(exc)}

    meta = _read_meta_for_article(article_path)
    intro, final = _extract_intro_and_final(html)
    h2_list = _extract_h2_list(html, exclude_faq=True)

    # slug определяем по имени файла или папки
    if article_path.name == "article.html":
        slug = article_path.parent.name
    else:
        slug = article_path.stem

    return {
        "category": category,
        "prev_article": {
            "slug": slug,
            "source": str(article_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
            "main_keyword": meta.get("main_keyword") or "",
            "title": meta.get("title") or "",
            "intro_first_sentence": intro,
            "h2_list": h2_list[:10],  # берём первые 10 H2 — больше не показываем
            "h2_count": len(h2_list),
            "final_paragraph_excerpt": final,
            "final_paragraph_formula": _classify_final_formula(final),
            "writer_route": meta.get("writer_route") or "",
            "intent": meta.get("intent") or "",
            "text_chars": meta.get("text_chars"),
        },
    }


def write_summary(category: str, exclude_slug: str | None = None) -> dict:
    """
    Строит summary и пишет в data/_prev_summary_{cat}.json.
    Возвращает dict с результатом для логов.
    """
    summary = build_summary(category, exclude_slug=exclude_slug)
    DATA_DIR.mkdir(exist_ok=True)
    out_path = DATA_DIR / f"_prev_summary_{category}.json"
    out_path.write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return {
        "ok": True,
        "category": category,
        "out_path": str(out_path),
        "has_prev": summary["prev_article"] is not None,
        "prev_slug": (summary.get("prev_article") or {}).get("slug"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Лёгкий summary последней статьи категории — для антишаблонности."
    )
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--category", choices=CATEGORIES,
                   help="Одна категория")
    g.add_argument("--all", action="store_true",
                   help="Сразу по всем (fiz, yur, vzysk, news)")
    parser.add_argument("--exclude-slug",
                        help="Пропустить указанный slug (не сравнивать статью с самой собой)")
    parser.add_argument("--json", action="store_true",
                        help="Вывод результата в JSON")
    args = parser.parse_args()

    cats = [args.category] if args.category else CATEGORIES
    results = []
    for cat in cats:
        res = write_summary(cat, exclude_slug=args.exclude_slug)
        results.append(res)

    if args.json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    else:
        for r in results:
            mark = "✓" if r["has_prev"] else "○"
            slug = r["prev_slug"] or "(нет статей в категории)"
            print(f"{mark} {r['category']}: prev={slug} → {r['out_path']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
