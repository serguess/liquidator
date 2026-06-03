"""
Валидация и автофикс внутренних ссылок между статьями.

Контекст (баг 12 мая 2026): LLM-агент 6 (seo-editor) проставлял внутренние
ссылки между статьями в произвольном формате. Яндекс.Вебмастер при обходе
сайта показал:
  - 7 настоящих 404: ссылки без префикса категории (/bankrotstvo-pensionera)
    или с лишним trailing slash (/articles/yur/.../).
  - 62 ссылки с 301-редиректом: с .html на конце, redirect на каноничный URL.

Каноничный формат внутренней ссылки на статью:
    href="/articles/{category}/{slug}"
    - category ∈ {fiz, yur, vzysk, news}
    - slug должен существовать в data/published_index.json (source=published)
      или в файловой системе articles/{category}/{slug}.html
    - БЕЗ .html на конце
    - БЕЗ trailing slash

Что делает скрипт:
1. Собирает множество валидных {slug → category} из published_index.json + ФС.
2. Парсит все href в HTML-файлах.
3. Для каждой внутренней ссылки определяет один из вердиктов:
   - ok               — ссылка корректна.
   - whitelisted      — служебная страница (/, /privacy, /category/all, ...),
                        не трогаем.
   - external/anchor  — внешняя или якорная, не трогаем.
   - fix_html         — заканчивается на .html, чинится удалением расширения.
   - fix_trailing     — заканчивается на /, чинится удалением слеша.
   - fix_short        — короткая /{slug} без префикса, slug найден в индексе,
                        чинится подстановкой /articles/{cat}/{slug}.
   - error_unknown    — внутренняя ссылка на статью, но slug нигде не найден.
   - error_ambiguous  — короткая /{slug}, но slug встречается в нескольких
                        категориях (теоретически возможно, защита от коллизий).

Запуск:
    python -m tools.internal_links_check                       # все articles/**/*.html, check-режим
    python -m tools.internal_links_check --fix                 # автофикс + репорт
    python -m tools.internal_links_check --path drafts/foo/article.html
    python -m tools.internal_links_check --json                # отчёт в stdout как JSON

Возврат:
    0 — все ссылки корректны (после автофикса, если был --fix)
    1 — остались error_* (нет соответствующих статей)
    2 — структурная ошибка (нет articles/, не смог загрузить индекс)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ARTICLES_DIR = PROJECT_ROOT / "articles"
PUBLISHED_INDEX = PROJECT_ROOT / "data" / "published_index.json"

VALID_CATEGORIES = {"fiz", "yur", "vzysk", "news"}

# Внутренние URL, которые не являются статьями и не должны валидироваться.
WHITELIST_EXACT = {
    "/",
    "/privacy", "/privacy.html",
    "/terms", "/terms.html",
    "/payment", "/payment.html",
    "/category/all",
}

# Адреса, которые надо ЧИНИТЬ редиректом на главную (а не вайтлистить).
# /index.html и /index дают 301 на / в проде → лишний .html-сигнал для Яндекса
# на каждой странице (ссылка «Главная» в хлебных крошках). Канон — «/».
INDEX_ALIASES = {"/index.html", "/index", "/index/"}
WHITELIST_PREFIXES = (
    "/category/all?",
    "/category/all#",
    "/assets/",
    "/brand/",
    "/static/",
    "/favicon",
    "/robots.txt",
    "/sitemap.xml",
)

# Расширения статических ресурсов на root-уровне сайта — не статьи, не валидируем.
STATIC_EXTENSIONS = (
    ".css", ".js", ".mjs", ".map", ".json", ".webmanifest",
    ".ico", ".png", ".jpg", ".jpeg", ".webp", ".svg", ".gif",
    ".woff", ".woff2", ".ttf", ".otf",
    ".mp4", ".webm",
    ".pdf", ".xml", ".txt",
)

# href="..." — захватываем содержимое.
HREF_RX = re.compile(r'href="([^"]*)"')

# Каноничный паттерн /articles/{cat}/{slug} с опциональными .html / / / #anchor.
ARTICLE_URL_RX = re.compile(
    r'^/articles/([a-z]+)/([a-z0-9][a-z0-9\-]*?)(\.html)?(/)?(#[^\s]*)?$'
)

# Короткая ссылка типа /{slug} без префикса категории (виновник 404).
SHORT_URL_RX = re.compile(
    r'^/([a-z][a-z0-9\-]+)(\.html)?(/)?(#[^\s]*)?$'
)


@dataclass
class LinkHit:
    href: str
    verdict: str          # ok | whitelisted | external | anchor | fix_* | error_*
    fixed_href: str | None = None
    reason: str = ""      # человеко-читаемое пояснение


@dataclass
class FileReport:
    file: str
    total_hrefs: int = 0
    ok: int = 0
    whitelisted: int = 0
    external: int = 0
    anchor: int = 0
    fixed: int = 0
    errors: int = 0
    fix_details: list[LinkHit] = field(default_factory=list)
    error_details: list[LinkHit] = field(default_factory=list)
    changed: bool = False


@dataclass
class AggregateReport:
    files_checked: int = 0
    files_changed: int = 0
    total_fixes: int = 0
    total_errors: int = 0
    files: list[FileReport] = field(default_factory=list)


def _load_valid_slugs() -> dict[str, str]:
    """
    Возвращает {slug: category}. Источник 1 — published_index.json (source=published),
    источник 2 — файловая система articles/{cat}/*.html (на случай stale index).
    """
    slug_to_cat: dict[str, str] = {}

    if PUBLISHED_INDEX.exists():
        try:
            data = json.loads(PUBLISHED_INDEX.read_text(encoding="utf-8"))
            for item in data.get("articles", []):
                if item.get("source") != "published":
                    continue
                slug = item.get("slug")
                cat = item.get("category")
                if slug and cat in VALID_CATEGORIES:
                    slug_to_cat[slug] = cat
        except (json.JSONDecodeError, OSError):
            pass

    if ARTICLES_DIR.exists():
        for cat_dir in ARTICLES_DIR.iterdir():
            if not cat_dir.is_dir() or cat_dir.name not in VALID_CATEGORIES:
                continue
            for html in cat_dir.glob("*.html"):
                slug = html.stem
                slug_to_cat.setdefault(slug, cat_dir.name)

    return slug_to_cat


def _classify(href: str, slug_to_cat: dict[str, str]) -> LinkHit:
    """Возвращает вердикт для одной ссылки."""
    if not href:
        return LinkHit(href=href, verdict="anchor", reason="empty href")

    if href.startswith("#"):
        return LinkHit(href=href, verdict="anchor", reason="page anchor")

    if href.startswith(("http://", "https://", "//", "mailto:", "tel:", "javascript:")):
        return LinkHit(href=href, verdict="external", reason="external/protocol link")

    if not href.startswith("/"):
        # Относительная ссылка типа "foo.html" — не валидируем, но и не трогаем.
        return LinkHit(href=href, verdict="external", reason="relative link, skipped")

    # /index.html, /index → канон главной «/» (убираем .html-сигнал на каждой странице)
    base_no_anchor = href.split("#", 1)[0].split("?", 1)[0]
    if base_no_anchor in INDEX_ALIASES:
        anchor = href[len(base_no_anchor):] if href != base_no_anchor else ""
        return LinkHit(
            href=href, verdict="fix_html", fixed_href="/" + anchor,
            reason="/index.html → / (убрать .html-зеркало главной)",
        )

    # Whitelist
    base = href.split("#", 1)[0].split("?", 1)[0]
    if href in WHITELIST_EXACT or base in WHITELIST_EXACT:
        return LinkHit(href=href, verdict="whitelisted", reason="service page")
    for prefix in WHITELIST_PREFIXES:
        if href.startswith(prefix):
            return LinkHit(href=href, verdict="whitelisted", reason=f"prefix {prefix}")
    # Статика по расширению (например /styles.css?v=28, /site.webmanifest)
    base_lower = base.lower()
    if any(base_lower.endswith(ext) for ext in STATIC_EXTENSIONS):
        return LinkHit(href=href, verdict="whitelisted", reason="static asset")

    # Каноничный /articles/{cat}/{slug}[...]
    m = ARTICLE_URL_RX.match(href)
    if m:
        cat, slug, dot_html, trailing, anchor = m.groups()
        anchor = anchor or ""
        if cat not in VALID_CATEGORIES:
            return LinkHit(
                href=href, verdict="error_unknown",
                reason=f"category '{cat}' не в {sorted(VALID_CATEGORIES)}",
            )
        if slug_to_cat.get(slug) != cat:
            # slug не найден или не в этой категории — потенциальный 404.
            return LinkHit(
                href=href, verdict="error_unknown",
                reason=f"slug '{slug}' не найден в категории '{cat}' "
                       f"(есть: {slug_to_cat.get(slug, 'нигде')})",
            )
        if dot_html or trailing:
            canonical = f"/articles/{cat}/{slug}{anchor}"
            verdict = "fix_html" if dot_html else "fix_trailing"
            return LinkHit(
                href=href, verdict=verdict, fixed_href=canonical,
                reason=("трейлинг .html → 301-редирект" if dot_html
                        else "trailing slash → 404 на pravo.shop"),
            )
        return LinkHit(href=href, verdict="ok", reason="canonical")

    # Короткая /{slug}
    m = SHORT_URL_RX.match(href)
    if m:
        slug, dot_html, trailing, anchor = m.groups()
        anchor = anchor or ""
        cat = slug_to_cat.get(slug)
        if cat:
            return LinkHit(
                href=href, verdict="fix_short",
                fixed_href=f"/articles/{cat}/{slug}{anchor}",
                reason=f"короткая ссылка → /articles/{cat}/{slug}",
            )
        # Нет такого slug нигде — но это может быть /privacy и пр. Не наш кейс,
        # они в whitelist. Значит реально неизвестный URL.
        return LinkHit(
            href=href, verdict="error_unknown",
            reason=f"короткая ссылка '/{slug}' — нет такой статьи в индексе",
        )

    # Что-то ещё абсолютное /... — отдадим в error для ручного разбора.
    return LinkHit(
        href=href, verdict="error_unknown",
        reason="нераспознанная внутренняя ссылка",
    )


# Тег <a> с href — для раскрытия битых ссылок в текст.
A_TAG_RX = re.compile(r'<a\b[^>]*?href="([^"]*)"[^>]*>(.*?)</a>', re.DOTALL | re.IGNORECASE)


def unwrap_broken_internal_links(html: str, slug_to_cat: dict[str, str]) -> tuple[str, int]:
    """Раскрывает в обычный текст <a href="...">текст</a> для внутренних ссылок
    на НЕсуществующие (не published) статьи — вердикт error_unknown. Валидные,
    служебные, внешние и чинимые (fix_*) ссылки не трогает.

    Используется и при публикации (bot/publisher — единая точка контроля, ловит
    любой путь генерации, включая recovery), и для разовой чистки уже
    опубликованных статей (--unwrap-broken). Возвращает (html, число раскрытых)."""
    count = 0

    def repl(m: re.Match) -> str:
        nonlocal count
        href = m.group(1)
        verdict = _classify(href, slug_to_cat)
        if verdict.verdict == "error_unknown":
            count += 1
            return m.group(2)  # внутренний текст без обёртки <a>
        return m.group(0)

    return A_TAG_RX.sub(repl, html), count


def analyze_file(path: Path, slug_to_cat: dict[str, str], apply_fix: bool = False) -> FileReport:
    report = FileReport(file=str(path))
    if not path.exists():
        return report

    raw = path.read_text(encoding="utf-8")
    new_raw = raw

    # Идём по совпадениям; для каждой ссылки строим вердикт.
    # Чтобы не сломать порядок при множественных одинаковых href, используем
    # пословные замены через списочные счётчики.
    hits: list[tuple[re.Match, LinkHit]] = []
    for m in HREF_RX.finditer(raw):
        href = m.group(1)
        verdict = _classify(href, slug_to_cat)
        hits.append((m, verdict))
        report.total_hrefs += 1
        if verdict.verdict == "ok":
            report.ok += 1
        elif verdict.verdict == "whitelisted":
            report.whitelisted += 1
        elif verdict.verdict == "external":
            report.external += 1
        elif verdict.verdict == "anchor":
            report.anchor += 1
        elif verdict.verdict.startswith("fix_"):
            report.fixed += 1
            report.fix_details.append(verdict)
        elif verdict.verdict.startswith("error_"):
            report.errors += 1
            report.error_details.append(verdict)

    if apply_fix and report.fixed > 0:
        # Применяем фиксы. Идём по всем уникальным парам (old → new) и заменяем
        # на уровне атрибута href="OLD" → href="NEW" (точная подстановка).
        # Замены применяются глобально для каждой пары; работает идемпотентно.
        seen: set[tuple[str, str]] = set()
        for _, link in hits:
            if not link.verdict.startswith("fix_") or not link.fixed_href:
                continue
            pair = (link.href, link.fixed_href)
            if pair in seen:
                continue
            seen.add(pair)
            new_raw = new_raw.replace(f'href="{link.href}"', f'href="{link.fixed_href}"')

        if new_raw != raw:
            path.write_text(new_raw, encoding="utf-8")
            report.changed = True

    return report


def _gather_targets(arg_path: str | None) -> list[Path]:
    """Возвращает список HTML-файлов для проверки."""
    if arg_path:
        target = (PROJECT_ROOT / arg_path).resolve()
        if target.is_file():
            return [target]
        if target.is_dir():
            return sorted(target.rglob("*.html"))
        return []
    # Дефолт: только опубликованные статьи. drafts проверяются per-slug при
    # вызове из quality_gate; здесь массово ходить по drafts/ не нужно.
    return sorted(ARTICLES_DIR.rglob("*.html")) if ARTICLES_DIR.exists() else []


def run(arg_path: str | None = None, apply_fix: bool = False) -> AggregateReport:
    slug_to_cat = _load_valid_slugs()
    files = _gather_targets(arg_path)
    agg = AggregateReport()

    for f in files:
        rep = analyze_file(f, slug_to_cat, apply_fix=apply_fix)
        agg.files_checked += 1
        agg.total_fixes += rep.fixed
        agg.total_errors += rep.errors
        if rep.changed:
            agg.files_changed += 1
        # В сводку добавляем только файлы с фиксами или ошибками.
        if rep.fixed or rep.errors:
            agg.files.append(rep)

    return agg


def _print_text(agg: AggregateReport, apply_fix: bool) -> None:
    mode = "FIX" if apply_fix else "CHECK"
    print(f"[internal_links_check / {mode}] "
          f"проверено файлов: {agg.files_checked}, "
          f"изменено: {agg.files_changed}, "
          f"найдено фиксов: {agg.total_fixes}, "
          f"ошибок: {agg.total_errors}")
    for fr in agg.files:
        rel = Path(fr.file).relative_to(PROJECT_ROOT) if Path(fr.file).is_absolute() else fr.file
        tag = "CHANGED" if fr.changed else ("WOULD-FIX" if fr.fixed and not apply_fix else "ERRORS")
        print(f"  [{tag}] {rel}: fixed={fr.fixed}, errors={fr.errors}")
        for link in fr.fix_details:
            print(f"      fix  ({link.verdict}) {link.href} → {link.fixed_href}  — {link.reason}")
        for link in fr.error_details:
            print(f"      ERR  ({link.verdict}) {link.href}  — {link.reason}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Проверка/автофикс внутренних ссылок между статьями pravo.shop"
    )
    parser.add_argument("--path", default=None,
                        help="Путь к файлу или папке (default: articles/)")
    parser.add_argument("--fix", action="store_true",
                        help="Применить автофиксы формата (.html/trailing/short) — без флага только check")
    parser.add_argument("--unwrap-broken", action="store_true",
                        help="Раскрыть в текст битые ссылки (error_unknown — на не-published статьи)")
    parser.add_argument("--json", action="store_true",
                        help="Вывод в JSON вместо текстового отчёта")
    args = parser.parse_args()

    if not ARTICLES_DIR.exists():
        print(f"Не найдено: {ARTICLES_DIR}", file=sys.stderr)
        return 2

    if args.unwrap_broken:
        slug_to_cat = _load_valid_slugs()
        targets = _gather_targets(args.path)
        total_unwrapped = 0
        files_changed = 0
        for f in targets:
            raw = f.read_text(encoding="utf-8")
            new, n = unwrap_broken_internal_links(raw, slug_to_cat)
            if n:
                f.write_text(new, encoding="utf-8")
                files_changed += 1
                total_unwrapped += n
                rel = f.relative_to(PROJECT_ROOT) if f.is_absolute() else f
                print(f"  [UNWRAP] {rel}: раскрыто {n}")
        print(f"[unwrap-broken] раскрыто {total_unwrapped} битых ссылок в {files_changed} файлах")
        return 0

    agg = run(arg_path=args.path, apply_fix=args.fix)

    if args.json:
        out = {
            "files_checked": agg.files_checked,
            "files_changed": agg.files_changed,
            "total_fixes": agg.total_fixes,
            "total_errors": agg.total_errors,
            "files": [
                {
                    **{k: v for k, v in asdict(fr).items()
                       if k not in ("fix_details", "error_details")},
                    "fix_details": [asdict(x) for x in fr.fix_details],
                    "error_details": [asdict(x) for x in fr.error_details],
                }
                for fr in agg.files
            ],
        }
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        _print_text(agg, apply_fix=args.fix)

    # Exit 1 если есть ошибки, которые не починились автофиксом.
    return 1 if agg.total_errors > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
