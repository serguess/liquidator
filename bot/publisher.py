"""
Публикатор статей: drafts/ → articles/ + Cloudinary обложка + индексы + git push.

Вызывается из bot/handlers.py при нажатии «✅ Опубликовать».

Логика:
    1) Прочитать выбранную версию HTML из drafts/{slug}/.
    2) Прочитать meta.json (категория, title, description).
    3) Через tools.image_gen сгенерировать обложку → загрузить в Cloudinary.
       Если упало - используем дефолтную (CSS-фон тёмного цвета на сайте).
    4) Заменить в HTML плейсхолдер обложки на Cloudinary URL.
    5) Скопировать файл в articles/{category}/{slug}.html.
    6) Обновить articles.json - добавить карточку в начало списка.
    7) Обновить sitemap.xml - добавить URL.
    8) Опционально: IndexNow ping (Яндекс/Bing).
    9) Удалить drafts/{slug}/ (чтобы не копился).
   10) git add + commit + push через SSH (origin переключён в entrypoint).
   11) Если push упал - откатить локальные файлы (атомарность).
   12) Записать в bot state статус "published".

Возвращает PublishResult с success/error и URL опубликованной статьи.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Optional

from . import state
from .config import (
    ARTICLES_DIR,
    DRAFTS_DIR,
    PROJECT_ROOT,
    PUBLIC_BASE_URL,
)

log = logging.getLogger("publisher")

ARTICLES_JSON_PATH = PROJECT_ROOT / "articles.json"
SITEMAP_PATH = PROJECT_ROOT / "sitemap.xml"
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GIT_AUTHOR_NAME = os.getenv("GIT_AUTHOR_NAME", "Liquidator Publisher")
GIT_AUTHOR_EMAIL = os.getenv("GIT_AUTHOR_EMAIL", "scheduler@pravo.shop")
INDEXNOW_KEY = os.getenv("INDEXNOW_KEY", "").strip()
INDEXNOW_HOST = os.getenv("INDEXNOW_HOST", "pravo.shop")


# Метки категорий для articles.json (формат, который уже на сайте: «Банкротство физических лиц»).
# В config.py CATEGORY_LABELS хранит более короткие версии («Физические лица») - они для
# хлебных крошек в article.html. Для карточек articles.json заказчик использовал длинные.
ARTICLES_JSON_CATEGORY_LABELS = {
    "fiz": "Банкротство физических лиц",
    "yur": "Банкротство юридических лиц",
    "vzysk": "Взыскание задолженности",
    "news": "Новости",
}


@dataclass
class PublishResult:
    success: bool
    slug: str
    error: Optional[str] = None
    public_url: Optional[str] = None
    cover_url: Optional[str] = None
    git_pushed: bool = False


# ============ ВЫБОР ВЕРСИИ ============

def _resolve_source_html(slug: str, version: Optional[str] = None) -> Optional[Path]:
    """
    Какой файл публикуем:
      1. Если version указан и есть drafts/{slug}/versions/v{version}.html - его.
      2. Иначе drafts/{slug}/article-v2.html (последняя после ИИ-конвейера).
      3. Иначе drafts/{slug}/article.html.
    """
    folder = DRAFTS_DIR / slug
    if not folder.exists():
        return None
    if version:
        candidate = folder / "versions" / f"v{version}.html"
        if candidate.exists():
            return candidate
    for fname in ("article-v2.html", "article.html"):
        candidate = folder / fname
        if candidate.exists():
            return candidate
    return None


def _read_meta(slug: str) -> dict:
    meta_path = DRAFTS_DIR / slug / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


# ============ ОБЛОЖКА ============

def _cover_image_for(
    slug: str,
    title: str,
    category: str,
    image_prompt: Optional[str] = None,
) -> Optional[str]:
    """
    Генерирует обложку через fal.ai → Cloudinary. None при ошибке.

    image_prompt - заранее подготовленный промпт под конкретную статью
    (генерирует агент 6 при написании, кладёт в meta.json как поле
    `image_prompt`). Если None или пусто - image_gen падает на шаблонный
    промпт по категории.

    Импорт внутри функции, чтобы publisher.py загружался без cloudinary/fal-client
    в окружениях, где их нет (локальная отладка некоторых сценариев).
    """
    try:
        from tools.image_gen import generate_and_upload_cover
    except ImportError:
        log.warning("tools.image_gen не доступен (нет cloudinary/fal-client?)")
        return None

    try:
        return generate_and_upload_cover(
            slug=slug, title=title, category=category, image_prompt=image_prompt,
        )
    except Exception:
        log.exception("Ошибка генерации обложки для %s", slug)
        return None


COVER_BLOCK_RX = re.compile(
    r'<div\s+class="article__cover"[^>]*>\s*</div>',
    re.IGNORECASE,
)


def _inject_cover_into_html(html: str, cover_url: Optional[str]) -> str:
    """
    Подставляет cover_url в `<div class="article__cover" style="background-image:url('...')"></div>`.
    Если уже есть style с background-image - заменяет URL внутри. Если нет блока -
    оставляет HTML как есть.
    Если cover_url=None - оставляет блок пустым (CSS даст тёмный фон-заглушку).
    """
    if not cover_url:
        return html

    # Заменяем любой существующий cover-блок целиком на новый с правильным URL.
    new_block = (
        f'<div class="article__cover" '
        f'style="background-image:url(\'{cover_url}\')"></div>'
    )
    new_html, n = COVER_BLOCK_RX.subn(new_block, html, count=1)
    if n > 0:
        return new_html

    # Если в HTML нет совсем такого блока (агент 6 не вставил), добавим перед <article class="article__body">.
    body_marker = '<article class="article__body">'
    if body_marker in html:
        return html.replace(body_marker, new_block + "\n      " + body_marker, 1)

    # Совсем не нашли куда вставить - оставляем как есть.
    return html


# ============ ARTICLES.JSON ============

# Дата для карточки: «12 мая 2026» (русские месяцы)
RU_MONTHS = {
    1: "января", 2: "февраля", 3: "марта", 4: "апреля", 5: "мая", 6: "июня",
    7: "июля", 8: "августа", 9: "сентября", 10: "октября", 11: "ноября", 12: "декабря",
}


def _ru_date(d: date) -> str:
    return f"{d.day} {RU_MONTHS[d.month]} {d.year}"


def _estimate_read_time(text_chars: Optional[int]) -> str:
    """5000 знаков ≈ 5 мин чтения. По умолчанию 5 мин."""
    if not text_chars or text_chars < 1000:
        return "5 мин."
    minutes = max(1, round(text_chars / 1500))
    return f"{minutes} мин."


def _update_articles_json(
    slug: str,
    category: str,
    title: str,
    description: str,
    cover_url: Optional[str],
    text_chars: Optional[int],
    tone: str = "b",
) -> None:
    """
    Добавляет карточку статьи в начало списка articles[]. Если slug уже есть -
    обновляет (иначе будут дубли при повторной публикации).
    """
    if ARTICLES_JSON_PATH.exists():
        try:
            data = json.loads(ARTICLES_JSON_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("articles.json битый, начинаю с нуля")
            data = {"generated": "", "articles": []}
    else:
        data = {"generated": "", "articles": []}

    today = date.today()
    card = {
        "slug": slug,
        "cat": category,
        "catLabel": ARTICLES_JSON_CATEGORY_LABELS.get(category, category),
        "title": title,
        "desc": description or "",
        "date": _ru_date(today),
        "dateIso": today.isoformat(),
        "read": _estimate_read_time(text_chars),
        "tone": tone,
        # Если cover_url=None, фронт может использовать дефолтный шаблон. Кладём пустую строку.
        "img": cover_url or "",
        "url": f"articles/{category}/{slug}",
    }

    articles = data.get("articles", [])
    # Удаляем старую запись с тем же slug (если переиздание)
    articles = [a for a in articles if a.get("slug") != slug]
    # Добавляем новую в начало
    articles.insert(0, card)
    data["articles"] = articles
    data["generated"] = today.isoformat()

    ARTICLES_JSON_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


# ============ SITEMAP.XML ============

# Шаблон одной записи. Простой: один url-блок добавляется перед закрывающим </urlset>.
SITEMAP_URL_TEMPLATE = (
    '  <url>\n'
    '    <loc>{loc}</loc>\n'
    '    <lastmod>{lastmod}</lastmod>\n'
    '    <changefreq>monthly</changefreq>\n'
    '    <priority>0.7</priority>\n'
    '  </url>\n'
)


def _update_sitemap(slug: str, category: str) -> None:
    """
    Добавляет новую <url> запись в sitemap.xml перед </urlset>.
    Если запись уже есть для этого URL - обновляет lastmod.
    """
    loc = f"/articles/{category}/{slug}"
    today_iso = date.today().isoformat()

    if not SITEMAP_PATH.exists():
        log.warning("sitemap.xml не найден, пропускаю обновление")
        return

    content = SITEMAP_PATH.read_text(encoding="utf-8")

    # Если запись уже есть - заменяем lastmod внутри неё.
    existing_pattern = re.compile(
        rf"(<url>\s*<loc>{re.escape(loc)}</loc>\s*<lastmod>)([^<]+)(</lastmod>)",
        re.MULTILINE,
    )
    new_content, n = existing_pattern.subn(rf"\g<1>{today_iso}\g<3>", content, count=1)
    if n > 0:
        SITEMAP_PATH.write_text(new_content, encoding="utf-8")
        return

    # Иначе добавляем новую запись перед </urlset>
    new_entry = SITEMAP_URL_TEMPLATE.format(loc=loc, lastmod=today_iso)
    if "</urlset>" in content:
        content = content.replace("</urlset>", new_entry + "</urlset>", 1)
    else:
        content = content.rstrip() + "\n" + new_entry
    SITEMAP_PATH.write_text(content, encoding="utf-8")


# ============ INDEXNOW ============

def _indexnow_ping(public_url: str) -> bool:
    """
    Уведомляет Яндекс/Bing о новом URL через IndexNow API. Бесплатно, не требует
    регистрации - только ключ-файл в корне сайта (любая случайная строка).
    Если INDEXNOW_KEY не задан - тихо пропускаем.
    """
    if not INDEXNOW_KEY:
        return False
    try:
        import httpx
        # IndexNow принимает простой GET-запрос
        resp = httpx.get(
            "https://yandex.com/indexnow",
            params={"url": public_url, "key": INDEXNOW_KEY},
            timeout=10,
        )
        log.info("IndexNow ping: %s status=%d", public_url, resp.status_code)
        return resp.status_code in (200, 202)
    except Exception:
        log.exception("IndexNow ping упал: %s", public_url)
        return False


# ============ GIT ============

def _git_env() -> dict:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL,
    }


GITHUB_REPO = os.getenv("GITHUB_REPO", "triyul22/liquidator")


def _git_remote_url() -> Optional[str]:
    """HTTPS URL с PAT для push. None если GIT_PUSH_TOKEN не задан."""
    token = os.getenv("GIT_PUSH_TOKEN", "").strip()
    if not token:
        return None
    return f"https://x-access-token:{token}@github.com/{GITHUB_REPO}.git"


def _mask_token(text: str) -> str:
    token = os.getenv("GIT_PUSH_TOKEN", "").strip()
    if token and token in text:
        return text.replace(token, "***")
    return text


def _git_commit_and_push(slug: str, category: str) -> dict:
    cwd = str(PROJECT_ROOT)
    env = _git_env()

    paths_to_add = [
        f"articles/{category}/{slug}.html",
        "articles.json",
        "sitemap.xml",
        "data/bot_state.json",
        f"drafts/{slug}/",  # удалённый каталог тоже надо закоммитить
    ]
    subprocess.run(["git", "add", "--", *paths_to_add], cwd=cwd, env=env,
                   check=False, capture_output=True)

    commit_msg = f"publish: {slug} ({category})"
    commit_res = subprocess.run(
        ["git", "commit", "-m", commit_msg],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    combined = (commit_res.stdout or "") + (commit_res.stderr or "")
    if "nothing to commit" in combined:
        return {"committed": False, "pushed": False, "reason": "nothing_to_commit"}
    if commit_res.returncode != 0:
        return {"committed": False, "pushed": False,
                "reason": "commit_failed", "stderr": combined[-300:]}

    remote_url = _git_remote_url()
    if not remote_url:
        return {"committed": True, "pushed": False, "reason": "no_token"}

    push_res = subprocess.run(
        ["git", "push", remote_url, GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    if push_res.returncode != 0:
        return {"committed": True, "pushed": False,
                "reason": "push_failed",
                "stderr": _mask_token((push_res.stderr or "")[-300:])}
    return {"committed": True, "pushed": True}


# ============ ОСНОВНАЯ ФУНКЦИЯ ============

def publish(slug: str, version: Optional[str] = None) -> PublishResult:
    """
    Главная функция публикации. См. docstring модуля.
    Атомарность: если git push упал - откатываем локальные изменения, чтобы
    в следующий раз можно было повторить с чистого листа.
    """
    log.info("Публикация: slug=%s version=%s", slug, version)

    source = _resolve_source_html(slug, version=version)
    if not source:
        return PublishResult(
            success=False, slug=slug,
            error=f"Не найден исходный HTML в drafts/{slug}/",
        )

    meta = _read_meta(slug)
    category = meta.get("category") or _guess_category_from_review(slug)
    if not category or category not in ARTICLES_JSON_CATEGORY_LABELS:
        return PublishResult(
            success=False, slug=slug,
            error=f"Неизвестная категория в meta.json: {category!r}",
        )

    title = meta.get("title") or meta.get("h1") or slug
    description = meta.get("description") or ""
    image_prompt = meta.get("image_prompt") or None
    text_chars = meta.get("text_chars")
    tone = (meta.get("tone") or "b").lower()
    if tone not in ("a", "b", "c", "d"):
        tone = "b"

    # Готовим целевые пути для возможного отката
    target_path = ARTICLES_DIR / category / f"{slug}.html"
    target_path.parent.mkdir(parents=True, exist_ok=True)
    drafts_path = DRAFTS_DIR / slug

    # Бэкапы существующих файлов для отката
    articles_json_backup = ARTICLES_JSON_PATH.read_text(encoding="utf-8") if ARTICLES_JSON_PATH.exists() else None
    sitemap_backup = SITEMAP_PATH.read_text(encoding="utf-8") if SITEMAP_PATH.exists() else None
    target_existed = target_path.exists()
    target_backup = target_path.read_text(encoding="utf-8") if target_existed else None

    cover_url: Optional[str] = None
    drafts_archive_path: Optional[Path] = None

    try:
        # 1. Сгенерировать обложку
        cover_url = _cover_image_for(
            slug=slug, title=title, category=category, image_prompt=image_prompt,
        )

        # 2. Прочитать HTML, подставить обложку
        html = source.read_text(encoding="utf-8")
        html = _inject_cover_into_html(html, cover_url)

        # 3. Записать в articles/{category}/{slug}.html
        target_path.write_text(html, encoding="utf-8")

        # 4. articles.json
        _update_articles_json(
            slug=slug, category=category, title=title, description=description,
            cover_url=cover_url, text_chars=text_chars, tone=tone,
        )

        # 5. sitemap.xml
        _update_sitemap(slug=slug, category=category)

        # 6. Удалить drafts/{slug}/ (или архивировать).
        # Архивируем в drafts/_archive/YYYY-MM/{slug}/ - занимает мало, но есть история.
        if drafts_path.exists():
            archive_root = DRAFTS_DIR / "_archive" / date.today().strftime("%Y-%m")
            archive_root.mkdir(parents=True, exist_ok=True)
            drafts_archive_path = archive_root / slug
            if drafts_archive_path.exists():
                shutil.rmtree(drafts_archive_path)
            shutil.move(str(drafts_path), str(drafts_archive_path))

        # 7. Обновить state бота ДО git push, чтобы попало в коммит
        public_url = f"{PUBLIC_BASE_URL}/articles/{category}/{slug}"
        state.set_status(
            slug, "published",
            published_url=public_url,
            published_at=datetime.now().isoformat(timespec="seconds"),
            articles_path=f"articles/{category}/{slug}.html",
        )

        # 8. git commit + push
        git_result = _git_commit_and_push(slug=slug, category=category)
        git_pushed = git_result.get("pushed", False)

        if not git_pushed:
            # Откат локальных изменений (атомарность):
            # вернуть articles/, articles.json, sitemap.xml, восстановить drafts/.
            log.warning("git push не удался: %s. Откатываю локальные изменения.", git_result)
            _rollback(
                target_path=target_path, target_existed=target_existed, target_backup=target_backup,
                articles_json_backup=articles_json_backup, sitemap_backup=sitemap_backup,
                drafts_path=drafts_path, drafts_archive_path=drafts_archive_path,
            )
            # state бота тоже откатываем на pending_review
            state.set_status(
                slug, "pending_review",
                published_url=None, published_at=None, articles_path=None,
            )
            return PublishResult(
                success=False, slug=slug,
                error=f"git push не удался: {git_result.get('reason')} {git_result.get('stderr', '')}",
                cover_url=cover_url,
            )

        # 9. IndexNow ping (не критично, ошибка не отменяет публикацию)
        _indexnow_ping(public_url)

        log.info("Опубликовано: %s → %s", slug, public_url)
        return PublishResult(
            success=True, slug=slug,
            public_url=public_url, cover_url=cover_url, git_pushed=True,
        )

    except Exception as exc:
        log.exception("Публикация упала: %s", slug)
        # Откат всего, что успели сделать
        _rollback(
            target_path=target_path, target_existed=target_existed, target_backup=target_backup,
            articles_json_backup=articles_json_backup, sitemap_backup=sitemap_backup,
            drafts_path=drafts_path, drafts_archive_path=drafts_archive_path,
        )
        return PublishResult(success=False, slug=slug, error=str(exc), cover_url=cover_url)


def _rollback(
    *,
    target_path: Path, target_existed: bool, target_backup: Optional[str],
    articles_json_backup: Optional[str], sitemap_backup: Optional[str],
    drafts_path: Path, drafts_archive_path: Optional[Path],
) -> None:
    """Восстанавливает файлы в состояние до попытки публикации."""
    try:
        if target_existed and target_backup is not None:
            target_path.write_text(target_backup, encoding="utf-8")
        elif target_path.exists():
            target_path.unlink()
    except Exception:
        log.exception("rollback target_path")

    try:
        if articles_json_backup is not None:
            ARTICLES_JSON_PATH.write_text(articles_json_backup, encoding="utf-8")
    except Exception:
        log.exception("rollback articles.json")

    try:
        if sitemap_backup is not None:
            SITEMAP_PATH.write_text(sitemap_backup, encoding="utf-8")
    except Exception:
        log.exception("rollback sitemap.xml")

    try:
        if drafts_archive_path and drafts_archive_path.exists() and not drafts_path.exists():
            shutil.move(str(drafts_archive_path), str(drafts_path))
    except Exception:
        log.exception("rollback drafts move-back")


def _guess_category_from_review(slug: str) -> Optional[str]:
    """Если в meta.json нет category - пробуем достать из bot state."""
    review = state.get_review(slug) or {}
    return review.get("category")
