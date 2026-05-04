"""
Детерминированный рендеринг boilerplate-блоков статьи.

Заказчик зафиксировал: дисклеймер, CTA, блок «Об авторе», JSON-LD,
шапка/подвал/breadcrumbs во всех статьях ИДЕНТИЧНЫ. LLM их пишет каждый
раз заново — это лишние токены и периодические рассинхроны (опечатка в
имени CSS-класса, лишний пробел в JSON-LD, забытый data-source). Скрипт
делает это ровно так как заказчик утвердил, без вариативности.

Что делает (по порядку):
  1. Читает body.html (или body.md) — то, что написал агент 6 (только содержание:
     лид + body с H2/H3 + FAQ). В body допустимы placeholder-комментарии:
       <!--BP:CTA-TOP-->     — место для верхнего CTA-овала
       <!--BP:CTA-MID-->     — место для inline CTA в середине
       <!--BP:CTA-BOTTOM-->  — место для финального CTA-овала
       <!--BP:DISCLAIMER-->  — место для дисклеймера перед финальным CTA
  2. Читает meta.json — берёт title, description, h1, lead, slug, category,
     topic_action, faq, date_published, og_image и др.
  3. Опционально читает research.json — берёт первый required_disclaimer
     если в meta.json его нет.
  4. Подставляет CTA, дисклеймер в body. Оборачивает body полным HTML-каркасом
     (head с метатегами, JSON-LD x3, header, breadcrumbs, head-блок статьи,
     cover, body, aside «Об авторе», related, footer, related-loader script).
  5. Пишет drafts/{slug}/article.html.

Запуск:
    python -m tools.inject_boilerplate drafts/{slug}/
    python -m tools.inject_boilerplate drafts/{slug}/ --body body.html --out article.html
    python -m tools.inject_boilerplate drafts/{slug}/ --check  # только валидация без записи

Exit:
    0 — успешно
    1 — отсутствуют обязательные поля meta.json
    2 — файлы не найдены
"""

from __future__ import annotations

import argparse
import html as html_module
import json
import re
import sys
from datetime import datetime, date
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE_ORIGIN = "https://pravo.shop"
CSS_VERSION = "24"

CATEGORY_LABELS = {
    "fiz": "Физические лица",
    "yur": "Юридические лица",
    "vzysk": "Взыскание задолженности",
    "news": "Новости",
}

CATEGORY_TAGS = {
    "fiz": "Физические лица",
    "yur": "Юридические лица",
    "vzysk": "Взыскание",
    "news": "Новости",
}

DEFAULT_DISCLAIMER = (
    "Материал носит информационный характер и не является юридической консультацией. "
    "Каждое дело индивидуально: применимость процедуры и её результат зависят от "
    "конкретных обстоятельств. Перед обращением за процедурой рекомендуется "
    "консультация с практикующим юристом или арбитражным управляющим. "
    "Использование материалов сайта возможно только с активной ссылкой на pravo.shop как на источник."
)

AUTHOR_ORG_NAME = "ООО «ЛИКВИДАТОР»"
AUTHOR_ORG_ROLE = "Компания по списанию и взысканию задолженности, опыт более 10 лет"
AUTHOR_ORG_BIO = (
    "Завершено более 800 дел о банкротстве. Списано более 1 млрд. рублей, "
    "взыскано более 500 млн. рублей. Бесплатная консультация в день обращения."
)
AUTHOR_ORG_DESCRIPTION = (
    "Юридическая компания: банкротство физических и юридических лиц, "
    "взыскание задолженности. Опыт более 10 лет, более 800 завершённых дел."
)

REQUIRED_META_FIELDS = ["slug", "category", "title", "description", "h1", "topic_action"]

PLACEHOLDER_RX = {
    "cta_top": "<!--BP:CTA-TOP-->",
    "cta_mid": "<!--BP:CTA-MID-->",
    "cta_bottom": "<!--BP:CTA-BOTTOM-->",
    "disclaimer": "<!--BP:DISCLAIMER-->",
}


# ============ Утилиты ============

def _esc(text: str) -> str:
    """HTML-escape для текста, который пойдёт в атрибуты или контент."""
    if text is None:
        return ""
    return html_module.escape(str(text), quote=True)


def _esc_attr(text: str) -> str:
    """То же что _esc, но смысловая обёртка для атрибутов."""
    return _esc(text)


def _today_iso() -> str:
    return date.today().isoformat()


def _human_date_ru(iso: str | None) -> str:
    """2026-05-04 → «4 мая 2026»."""
    if not iso:
        iso = _today_iso()
    months = ["января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря"]
    try:
        d = datetime.fromisoformat(iso[:10]).date()
    except ValueError:
        return iso
    return f"{d.day} {months[d.month - 1]} {d.year}"


def _read_minutes(text_chars: int | None) -> int:
    """Грубая прикидка: ~1500 знаков = 1 минута чтения."""
    if not text_chars or text_chars <= 0:
        return 5
    minutes = max(1, round(text_chars / 1500))
    return minutes


def _canonical_url(category: str, slug: str) -> str:
    return f"{SITE_ORIGIN}/articles/{category}/{slug}"


def _og_image_path(slug: str) -> str:
    return f"/assets/articles/{slug}.jpg"


def _short_bc(title: str, max_len: int = 60) -> str:
    """Короткая последняя крошка из title. Берём до первого ':' или режем по длине."""
    if not title:
        return ""
    if ":" in title:
        return title.split(":", 1)[0].strip()
    if len(title) <= max_len:
        return title
    return title[:max_len].rsplit(" ", 1)[0] + "…"


def _extract_first_h1(body: str) -> str | None:
    """Если в body уже есть <h1>, вернуть его текст."""
    m = re.search(r"<h1\b[^>]*>(.*?)</h1>", body, re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    return re.sub(r"<[^>]+>", "", m.group(1)).strip()


def _strip_outer_html(body: str) -> str:
    """
    Если агент случайно положил полный HTML (<!DOCTYPE>, <html>, <body>),
    выдираем только содержимое <article class="article__body">…</article>.
    Если такого тега нет — возвращаем как есть.
    """
    m = re.search(r'<article\b[^>]*class="[^"]*article__body[^"]*"[^>]*>(.*?)</article>',
                  body, re.DOTALL | re.IGNORECASE)
    if m:
        return m.group(1).strip()
    # Если есть <body>, берём всё что внутри (агент написал шире)
    m2 = re.search(r"<body\b[^>]*>(.*?)</body>", body, re.DOTALL | re.IGNORECASE)
    if m2:
        # Из body выпиливаем header/footer/main-обёртку — оставляем только article__body
        inner = m2.group(1)
        m3 = re.search(r'<article\b[^>]*class="[^"]*article__body[^"]*"[^>]*>(.*?)</article>',
                       inner, re.DOTALL | re.IGNORECASE)
        if m3:
            return m3.group(1).strip()
    return body


# ============ Рендереры блоков ============

def render_cta_hero(text: str, slug: str, position: str) -> str:
    """Большой CTA-овал. position: 'top' | 'bottom'."""
    src = f"article-{slug}-{position}"
    return (
        f'<a href="/index.html#contacts" class="article__cta--hero" '
        f'data-source="{_esc_attr(src)}">\n'
        f'  <span>{_esc(text)}</span>\n'
        f'</a>'
    )


def render_cta_inline(text: str, slug: str) -> str:
    """Inline-CTA в середине статьи."""
    src = f"article-{slug}-mid"
    return (
        f'<p><a href="/index.html#contacts" class="article__cta-inline" '
        f'data-source="{_esc_attr(src)}">'
        f'<strong>{_esc(text)} →</strong></a></p>'
    )


def render_disclaimer(text: str) -> str:
    return f'<div class="article__disclaimer">\n  {_esc(text)}\n</div>'


def render_author_aside() -> str:
    return (
        '<aside class="article__author article__author--company">\n'
        '  <div class="article__author-avatar article__author-avatar--logo" aria-hidden="true">\n'
        '    <img src="/assets/logo.svg" alt="" onerror="this.style.display=\'none\'"/>\n'
        '  </div>\n'
        '  <div class="article__author-body">\n'
        f'    <div class="article__author-name">{_esc(AUTHOR_ORG_NAME)}</div>\n'
        f'    <div class="article__author-role">{_esc(AUTHOR_ORG_ROLE)}</div>\n'
        f'    <p class="article__author-bio">{_esc(AUTHOR_ORG_BIO)}</p>\n'
        '  </div>\n'
        '</aside>'
    )


def render_breadcrumbs(category: str, breadcrumb_current: str) -> str:
    cat_label = CATEGORY_LABELS.get(category, category)
    return (
        '<nav class="article__breadcrumbs" aria-label="Хлебные крошки">\n'
        '  <a href="/index.html">Главная</a>\n'
        '  <span class="article__bc-sep">/</span>\n'
        f'  <a href="/category/{category}.html">{_esc(cat_label)}</a>\n'
        '  <span class="article__bc-sep">/</span>\n'
        f'  <span class="article__bc-current">{_esc(breadcrumb_current)}</span>\n'
        '</nav>'
    )


def render_article_head(h1: str, lead: str, date_human: str, read_minutes: int,
                        category: str) -> str:
    tag = CATEGORY_TAGS.get(category, CATEGORY_LABELS.get(category, category))
    return (
        '<div class="article__head">\n'
        f'  <span class="article__tag">{_esc(tag)}</span>\n'
        f'  <h1 class="article__title">{_esc(h1)}</h1>\n'
        f'  <p class="article__lead">{_esc(lead)}</p>\n'
        '  <div class="article__meta">\n'
        '    <span class="article__meta-item">\n'
        '      <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="3" y="5" width="18" height="16" rx="2" stroke="currentColor" stroke-width="1.6"/>'
        '<path d="M3 9h18M8 3v4M16 3v4" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>\n'
        f'      {_esc(date_human)}\n'
        '    </span>\n'
        '    <span class="article__meta-item">\n'
        '      <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="1.6"/>'
        '<path d="M12 7v5l3 2" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>\n'
        f'      {read_minutes} минут чтения\n'
        '    </span>\n'
        '  </div>\n'
        '</div>'
    )


def render_cover(slug: str) -> str:
    img = _og_image_path(slug)
    return f'<div class="article__cover" style="background-image:url(\'{img}\')"></div>'


def render_related_section(category: str, slug: str) -> str:
    return (
        '<section class="article__related">\n'
        '  <h2 class="article__related-title">Читайте также</h2>\n'
        f'  <div class="article__related-grid" id="relatedGrid" '
        f'data-cat="{_esc_attr(category)}" data-slug="{_esc_attr(slug)}" data-base="/"></div>\n'
        '</section>'
    )


def render_related_script() -> str:
    return (
        '<script>\n'
        '(function loadRelated() {\n'
        "  const grid = document.getElementById('relatedGrid');\n"
        '  if (!grid) return;\n'
        "  const base = grid.dataset.base || '';\n"
        '  const cat = grid.dataset.cat;\n'
        '  const slug = grid.dataset.slug;\n'
        "  fetch(base + 'articles.json', { cache: 'no-cache' })\n"
        '    .then(r => r.json())\n'
        '    .then(data => {\n'
        '      const same = data.articles.filter(a => a.cat === cat && a.slug !== slug).slice(0, 3);\n'
        '      const pool = same.length >= 3 ? same : same.concat(\n'
        '        data.articles.filter(a => a.slug !== slug && a.cat !== cat).slice(0, 3 - same.length)\n'
        '      );\n'
        '      grid.innerHTML = pool.map(a => `\n'
        '        <a class="related-card" href="${base}${a.url}" '
        'style="background-image:linear-gradient(180deg, rgba(10,13,18,0.1) 0%, rgba(10,13,18,0.85) 100%), url(\'${base}${a.img}\')">\n'
        '          <span class="related-card__tag">${a.catLabel}</span>\n'
        '          <h3 class="related-card__title">${a.title}</h3>\n'
        '          <div class="related-card__meta">${a.date} - ${a.read}</div>\n'
        '        </a>\n'
        "      `).join('');\n"
        '    })\n'
        "    .catch(() => { grid.style.display = 'none'; });\n"
        '})();\n'
        '</script>'
    )


def render_header() -> str:
    return (
        '<header class="header" id="header">\n'
        '  <div class="header__inner">\n'
        '    <nav class="header__nav header__nav--left">\n'
        '      <a href="/index.html#services">УСЛУГИ</a>\n'
        '      <a href="/index.html#cases">КЕЙСЫ</a>\n'
        '    </nav>\n'
        '    <a href="/index.html" class="header__brand">\n'
        '      <img src="/assets/logo.svg" alt="" class="header__logo" onerror="this.style.display=\'none\'"/>\n'
        '      <span class="header__brand-text">ЛИКВИДАТОР</span>\n'
        '    </a>\n'
        '    <nav class="header__nav header__nav--right">\n'
        '      <a href="/index.html#knowledge">БАЗА ЗНАНИЙ</a>\n'
        '      <a href="/index.html#footer">КОНТАКТЫ</a>\n'
        '    </nav>\n'
        '  </div>\n'
        '</header>'
    )


def render_footer() -> str:
    return (
        '<footer class="article-footer">\n'
        '  <div class="article-footer__inner">\n'
        '    <div class="article-footer__brand">\n'
        '      <img src="/assets/logo.svg" alt="" onerror="this.style.display=\'none\'"/>\n'
        '      <span>ЛИКВИДАТОР</span>\n'
        '    </div>\n'
        '    <p class="article-footer__law">Федеральный закон «О несостоятельности (банкротстве)» '
        'от 26.10.2002 № 127-ФЗ; Федеральный закон «Об исполнительном производстве» от 02.10.2007 № 229-ФЗ</p>\n'
        '    <p class="article-footer__copy">© ООО «ЛИКВИДАТОР». Реальные дела. Реальные результаты.</p>\n'
        '  </div>\n'
        '</footer>'
    )


# ============ JSON-LD ============

def render_article_jsonld(meta: dict) -> str:
    slug = meta["slug"]
    category = meta["category"]
    cat_label = CATEGORY_LABELS.get(category, category)
    canonical = meta.get("canonical_url") or _canonical_url(category, slug)
    img = meta.get("og_image") or _og_image_path(slug)
    date_pub = (meta.get("date_published") or meta.get("published_at") or _today_iso())[:10]
    date_mod = (meta.get("date_modified") or meta.get("updated_at") or date_pub)[:10]

    # knowsAbout зависит от категории
    knows_about = {
        "fiz": ["банкротство физических лиц", "127-ФЗ", "списание долгов", "арбитражная практика"],
        "yur": ["банкротство юридических лиц", "127-ФЗ", "ликвидация", "субсидиарная ответственность"],
        "vzysk": ["взыскание задолженности", "229-ФЗ", "исполнительное производство", "судебный приказ"],
        "news": ["банкротство", "127-ФЗ", "судебная практика", "законодательство"],
    }.get(category, ["банкротство", "взыскание задолженности", "127-ФЗ"])

    payload = {
        "@context": "https://schema.org",
        "@type": "Article",
        "headline": meta["title"],
        "description": meta["description"],
        "image": img,
        "datePublished": date_pub,
        "dateModified": date_mod,
        "author": {
            "@type": "Organization",
            "name": AUTHOR_ORG_NAME,
            "url": f"{SITE_ORIGIN}/",
            "logo": {"@type": "ImageObject", "url": "/assets/logo.svg"},
            "description": AUTHOR_ORG_DESCRIPTION,
            "knowsAbout": knows_about,
        },
        "publisher": {
            "@type": "Organization",
            "name": AUTHOR_ORG_NAME,
            "logo": {"@type": "ImageObject", "url": "/assets/logo.svg"},
        },
        "mainEntityOfPage": {"@type": "WebPage", "@id": canonical},
        "articleSection": cat_label,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return f'<script type="application/ld+json">\n{body}\n</script>'


def render_breadcrumb_jsonld(meta: dict) -> str:
    category = meta["category"]
    cat_label = CATEGORY_LABELS.get(category, category)
    bc_current = meta.get("breadcrumb_current") or _short_bc(meta["title"])
    payload = {
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": "Главная", "item": f"{SITE_ORIGIN}/"},
            {"@type": "ListItem", "position": 2, "name": cat_label,
             "item": f"{SITE_ORIGIN}/category/{category}"},
            {"@type": "ListItem", "position": 3, "name": bc_current},
        ],
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return f'<script type="application/ld+json">\n{body}\n</script>'


def render_faq_jsonld(faq: list[dict]) -> str:
    if not faq:
        return ""
    items = []
    for q in faq:
        question = q.get("question") or q.get("q") or ""
        answer = q.get("answer") or q.get("a") or ""
        if not question or not answer:
            continue
        items.append({
            "@type": "Question",
            "name": question,
            "acceptedAnswer": {"@type": "Answer", "text": answer},
        })
    if not items:
        return ""
    payload = {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        "mainEntity": items,
    }
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    return f'<script type="application/ld+json">\n{body}\n</script>'


# ============ Полный шаблон ============

def render_head(meta: dict, jsonld_blocks: list[str]) -> str:
    slug = meta["slug"]
    category = meta["category"]
    title = meta["title"]
    description = meta["description"]
    canonical = meta.get("canonical_url") or _canonical_url(category, slug)
    og_img = meta.get("og_image") or _og_image_path(slug)
    og_title = meta.get("og_title") or title
    og_desc = meta.get("og_description") or description
    robots = meta.get("robots") or "index, follow"

    fonts_url = (
        "https://fonts.googleapis.com/css2?"
        "family=Unbounded:wght@400;500;600;700;800"
        "&family=Manrope:wght@300;400;500;600;700"
        "&family=Onest:wght@400;500;600;700"
        "&family=Cormorant+Garamond:ital,wght@0,700;1,700"
        "&display=swap"
    )

    jsonld_combined = "\n\n  ".join(b for b in jsonld_blocks if b)

    return (
        '<head>\n'
        '  <meta charset="UTF-8" />\n'
        '  <meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f'  <title>{_esc(title)}</title>\n'
        f'  <meta name="description" content="{_esc_attr(description)}" />\n'
        f'  <meta name="robots" content="{_esc_attr(robots)}" />\n'
        f'  <link rel="canonical" href="{_esc_attr(canonical)}" />\n\n'
        '  <meta property="og:type" content="article" />\n'
        f'  <meta property="og:title" content="{_esc_attr(og_title)}" />\n'
        f'  <meta property="og:description" content="{_esc_attr(og_desc)}" />\n'
        f'  <meta property="og:image" content="{_esc_attr(og_img)}" />\n'
        f'  <meta property="og:url" content="{_esc_attr(canonical)}" />\n\n'
        '  <link rel="preconnect" href="https://fonts.googleapis.com">\n'
        '  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n'
        f'  <link rel="preload" href="{fonts_url}" as="style" '
        "onload=\"this.onload=null;this.rel='stylesheet'\">\n"
        f'  <noscript><link href="{fonts_url}" rel="stylesheet"></noscript>\n\n'
        f'  <link rel="stylesheet" href="/styles.css?v={CSS_VERSION}" />\n\n'
        f'  {jsonld_combined}\n'
        '</head>'
    )


def assemble_article(meta: dict, body_html: str, disclaimer_text: str) -> str:
    """
    Собирает финальный article.html из meta + body.

    body_html — содержимое <article class="article__body">…</article>:
    лид + разделы H2/H3 + FAQ. В нём допустимы placeholder-комментарии
    BP:CTA-TOP, BP:CTA-MID, BP:CTA-BOTTOM, BP:DISCLAIMER — они заменятся
    на стандартные блоки.
    """
    slug = meta["slug"]
    category = meta["category"]
    h1 = meta["h1"]
    lead = meta.get("lead") or meta.get("description") or ""
    topic_action = meta["topic_action"]
    topic_action_alt = meta.get("topic_action_alt") or f"Узнать свой вариант: оставить заявку на {topic_action}"
    cta_top_text = meta.get("cta_top_text") or f"Оставить заявку на {topic_action}"
    cta_mid_text = meta.get("cta_mid_text") or f"Оставить заявку на {topic_action}"
    cta_bottom_text = meta.get("cta_bottom_text") or topic_action_alt

    breadcrumb_current = meta.get("breadcrumb_current") or _short_bc(meta["title"])
    date_human = _human_date_ru(meta.get("date_published") or meta.get("published_at"))
    read_minutes = _read_minutes(meta.get("text_chars"))

    # 1. Подставляем CTA и дисклеймер в body
    body = _strip_outer_html(body_html)

    cta_top = render_cta_hero(cta_top_text, slug, "top")
    cta_mid = render_cta_inline(cta_mid_text, slug)
    cta_bottom = render_cta_hero(cta_bottom_text, slug, "bottom")
    disclaimer = render_disclaimer(disclaimer_text)

    body = body.replace(PLACEHOLDER_RX["cta_top"], cta_top)
    body = body.replace(PLACEHOLDER_RX["cta_mid"], cta_mid)
    body = body.replace(PLACEHOLDER_RX["cta_bottom"], cta_bottom)
    body = body.replace(PLACEHOLDER_RX["disclaimer"], disclaimer)

    # 2. Если в body НЕТ <h1> — добавляем его сами на основе article-head ниже
    # Для совместимости: даже если есть, оставляем — но article-head всё равно
    # отрендерим, дубль на странице нежелателен → вырезаем <h1> из body если был.
    body = re.sub(r"<h1\b[^>]*>.*?</h1>\s*", "", body, count=1, flags=re.DOTALL | re.IGNORECASE)

    # 3. Собираем JSON-LD
    jsonld_blocks = [
        render_article_jsonld(meta),
        render_breadcrumb_jsonld(meta),
        render_faq_jsonld(meta.get("faq") or []),
    ]

    # 4. Собираем head
    head = render_head(meta, jsonld_blocks)

    # 5. Собираем структурные блоки
    header = render_header()
    breadcrumbs = render_breadcrumbs(category, breadcrumb_current)
    article_head = render_article_head(h1, lead, date_human, read_minutes, category)
    cover = render_cover(slug)
    author_aside = render_author_aside()
    related_section = render_related_section(category, slug)
    footer = render_footer()
    related_script = render_related_script()

    # 6. Финальная сборка
    return (
        '<!DOCTYPE html>\n'
        '<html lang="ru">\n'
        f'{head}\n'
        '<body class="article-page">\n\n'
        f'  {header}\n\n'
        '  <main class="article">\n'
        '    <div class="article__container">\n'
        f'      {breadcrumbs}\n\n'
        f'      {article_head}\n\n'
        f'      {cover}\n\n'
        '      <article class="article__body">\n\n'
        f'{body.strip()}\n\n'
        '      </article>\n\n'
        f'      {author_aside}\n\n'
        f'      {related_section}\n'
        '    </div>\n'
        '  </main>\n\n'
        f'  {footer}\n\n'
        f'  {related_script}\n\n'
        '</body>\n'
        '</html>\n'
    )


# ============ I/O ============

def _validate_meta(meta: dict) -> list[str]:
    """Возвращает список отсутствующих обязательных полей."""
    missing = [f for f in REQUIRED_META_FIELDS if not meta.get(f)]
    return missing


def _load_disclaimer_text(slug_dir: Path, meta: dict) -> str:
    """
    Источник дисклеймера в порядке приоритета:
      1. meta.disclaimer_text (если заказчик переопределил)
      2. research.required_disclaimers[0]
      3. DEFAULT_DISCLAIMER (стандарт от заказчика)
    """
    if meta.get("disclaimer_text"):
        return meta["disclaimer_text"]
    research_path = slug_dir / "research.json"
    if research_path.exists():
        try:
            r = json.loads(research_path.read_text(encoding="utf-8"))
            disclaimers = r.get("required_disclaimers") or []
            for d in disclaimers:
                if isinstance(d, str) and len(d) > 30:
                    return d
        except (json.JSONDecodeError, OSError):
            pass
    return DEFAULT_DISCLAIMER


def process(slug_dir: Path, body_filename: str = "body.html",
            out_filename: str = "article.html",
            check_only: bool = False) -> dict:
    """
    Главный entry point. Возвращает dict с результатом для интеграции с runner.
    """
    if not slug_dir.exists() or not slug_dir.is_dir():
        return {"ok": False, "error": "slug_dir_not_found", "path": str(slug_dir)}

    body_path = slug_dir / body_filename
    if not body_path.exists():
        # fallback на article.html — может быть, агент 6 ещё пишет туда напрямую
        alt_path = slug_dir / "article.html"
        if alt_path.exists() and body_filename == "body.html":
            body_path = alt_path
        else:
            return {"ok": False, "error": "body_not_found",
                    "expected": [str(body_path), str(alt_path)]}

    meta_path = slug_dir / "meta.json"
    if not meta_path.exists():
        return {"ok": False, "error": "meta_not_found", "expected": str(meta_path)}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "error": "meta_invalid", "detail": str(exc)}

    missing = _validate_meta(meta)
    if missing:
        return {"ok": False, "error": "meta_missing_fields", "missing": missing}

    body_raw = body_path.read_text(encoding="utf-8")
    disclaimer_text = _load_disclaimer_text(slug_dir, meta)

    article_html = assemble_article(meta, body_raw, disclaimer_text)

    out_path = slug_dir / out_filename
    written = False
    if not check_only:
        out_path.write_text(article_html, encoding="utf-8")
        written = True

    return {
        "ok": True,
        "slug": meta["slug"],
        "category": meta["category"],
        "input_body": str(body_path),
        "output": str(out_path),
        "written": written,
        "html_chars": len(article_html),
        "body_chars": len(body_raw),
        "placeholders_substituted": {
            "cta_top": PLACEHOLDER_RX["cta_top"] in body_raw,
            "cta_mid": PLACEHOLDER_RX["cta_mid"] in body_raw,
            "cta_bottom": PLACEHOLDER_RX["cta_bottom"] in body_raw,
            "disclaimer": PLACEHOLDER_RX["disclaimer"] in body_raw,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Детерминированный рендеринг boilerplate-блоков статьи"
    )
    parser.add_argument("slug_dir", help="Путь к drafts/{slug}/")
    parser.add_argument("--body", default="body.html",
                        help="Имя файла с body (по умолчанию body.html, fallback article.html)")
    parser.add_argument("--out", default="article.html",
                        help="Куда писать готовый HTML (по умолчанию article.html)")
    parser.add_argument("--check", action="store_true",
                        help="Только валидация, без записи файла")
    parser.add_argument("--json", action="store_true",
                        help="Вывод результата в JSON")
    args = parser.parse_args()

    slug_dir = Path(args.slug_dir).resolve()
    result = process(slug_dir, body_filename=args.body, out_filename=args.out,
                     check_only=args.check)

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        if result["ok"]:
            print(f"OK: {result['slug']} → {result['output']} "
                  f"({result['html_chars']} символов)")
            ph = result["placeholders_substituted"]
            print(f"  CTA-top:    {'✓' if ph['cta_top'] else '✗ (placeholder отсутствовал в body)'}")
            print(f"  CTA-mid:    {'✓' if ph['cta_mid'] else '✗'}")
            print(f"  CTA-bottom: {'✓' if ph['cta_bottom'] else '✗'}")
            print(f"  Disclaimer: {'✓' if ph['disclaimer'] else '✗'}")
        else:
            print(f"FAIL: {result.get('error')} — {result}", file=sys.stderr)

    if not result["ok"]:
        if result.get("error") == "meta_missing_fields":
            return 1
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
