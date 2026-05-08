"""
ЛИКВИДАТОР: единое приложение (статика + API).

Запуск локально:
    uvicorn main:app --reload --port 8000

Эндпоинты:
    GET  /api/health   - проверка жизни + наличия SMTP-кредов
    POST /api/lead     - приём заявки с формы → письмо на MAIL_TO
    /                  - статика из текущей директории (index.html, styles.css, ...)
"""
from __future__ import annotations

import os
import re
import sys
import time
import json
import html
import asyncio
import secrets
import smtplib
import logging
import subprocess
from datetime import datetime as _dt
from collections import defaultdict, deque
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.utils import formataddr
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import Response, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from pydantic import BaseModel, Field, field_validator

# Локально подхватываем .env (на Timeweb переменные идут из ENV приложения)
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# ============ CONFIG ============
ROOT = Path(__file__).parent

SMTP_HOST = os.getenv("SMTP_HOST", "smtp.yandex.ru")
SMTP_PORT = int(os.getenv("SMTP_PORT", "465"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")

MAIL_TO        = os.getenv("MAIL_TO", "lead@pravo.shop")
MAIL_FROM      = os.getenv("MAIL_FROM", SMTP_USER or "lead@pravo.shop")
MAIL_FROM_NAME = os.getenv("MAIL_FROM_NAME", "Сайт ЛИКВИДАТОР")

# CORS: если сайт и API на одном домене (рекомендуется) - оставь "same-origin".
# Если API будет на поддомене api.pravo.shop - поставь "https://pravo.shop".
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "same-origin")

RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "3"))

# Preview-роут для черновиков SEO-конвейера. Если переменные не заданы - роут отключён (404).
PREVIEW_USER = os.getenv("PREVIEW_USER", "")
PREVIEW_PASSWORD = os.getenv("PREVIEW_PASSWORD", "")
DRAFTS_DIR = ROOT / "drafts"
# Репо на GitHub: используется для кнопки "Править на GitHub" в /preview/.
# Формат: "owner/repo" (например, "serguess/liquidator"). Ветка - GITHUB_BRANCH (по умолчанию main).
GITHUB_REPO = os.getenv("GITHUB_REPO", "serguess/liquidator")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("liquidator")


# ============ APP ============
app = FastAPI(
    title="Liquidator",
    docs_url=None, redoc_url=None, openapi_url=None,  # прячем swagger в проде
)

if ALLOWED_ORIGIN != "same-origin":
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[ALLOWED_ORIGIN] if ALLOWED_ORIGIN != "*" else ["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["Content-Type"],
    )

# Gzip-сжатие всех ответов > 500 байт (HTML, CSS, JS, JSON, SVG сжимаются в 4-5 раз)
app.add_middleware(GZipMiddleware, minimum_size=500, compresslevel=6)


# Cache-Control заголовки для статики (кэш в браузере, чтобы повторные заходы были мгновенными)
_CACHE_IMMUTABLE_EXT = (".webp", ".jpg", ".jpeg", ".png", ".svg", ".ico",
                         ".mp4", ".webm", ".woff", ".woff2", ".ttf", ".otf")
_CACHE_MEDIUM_EXT = (".css", ".js")


@app.middleware("http")
async def _cache_headers(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path.lower()
    if path.endswith(_CACHE_IMMUTABLE_EXT):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    elif path.endswith(_CACHE_MEDIUM_EXT):
        response.headers["Cache-Control"] = "public, max-age=2592000"
    elif path.endswith(".html") or path == "/" or path.endswith("/"):
        response.headers["Cache-Control"] = "public, max-age=3600, must-revalidate"
    elif path.endswith(".json"):
        response.headers["Cache-Control"] = "public, max-age=3600"
    return response


# ============ MODELS ============
PHONE_RE = re.compile(r"^[\d\s\+\-\(\)]{10,25}$")


class LeadIn(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    phone: str = Field(..., min_length=10, max_length=25)
    source: Optional[str] = Field(default="", max_length=200)
    page_url: Optional[str] = Field(default="", max_length=500)
    page_title: Optional[str] = Field(default="", max_length=300)
    consent: bool = True
    # honeypot: скрытое поле, которое боты обычно заполняют, а люди - нет
    hp: Optional[str] = Field(default="", alias="_hp")

    model_config = {"populate_by_name": True}

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if len(v) < 2:
            raise ValueError("name too short")
        return v

    @field_validator("phone")
    @classmethod
    def _phone(cls, v: str) -> str:
        if not PHONE_RE.match(v):
            raise ValueError("invalid phone")
        digits = re.sub(r"\D", "", v)
        if len(digits) < 10:
            raise ValueError("phone too short")
        return v


# ============ RATE LIMIT (in-memory) ============
# Для одного инстанса ок. Если пойдём в несколько реплик - надо Redis.
_rate: dict[str, deque] = defaultdict(deque)


def _rate_limit_ok(ip: str) -> bool:
    now = time.time()
    q = _rate[ip]
    # чистим старые записи
    while q and now - q[0] > 3600:
        q.popleft()
    if len(q) >= RATE_LIMIT_PER_HOUR:
        return False
    q.append(now)
    return True


def _client_ip(request: Request) -> str:
    # за прокси Timeweb/Cloudflare берём первый из X-Forwarded-For
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    return request.client.host if request.client else "0.0.0.0"


# ============ MAIL ============
def _send_mail(subject: str, html_body: str, text_body: str) -> None:
    if not (SMTP_USER and SMTP_PASS):
        raise RuntimeError("SMTP_USER/SMTP_PASS не заданы в ENV")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = formataddr((MAIL_FROM_NAME, MAIL_FROM))
    msg["To"] = MAIL_TO
    msg["Reply-To"] = MAIL_TO
    msg.attach(MIMEText(text_body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))

    # 465 = SSL, 587 = STARTTLS
    if SMTP_PORT == 465:
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(MAIL_FROM, [MAIL_TO], msg.as_string())
    else:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as s:
            s.starttls()
            s.login(SMTP_USER, SMTP_PASS)
            s.sendmail(MAIL_FROM, [MAIL_TO], msg.as_string())


def _render_mail(payload: LeadIn, ip: str, ua: str, ts: str) -> tuple[str, str]:
    e = html.escape

    # Источник: метка кнопки ("1 блок (главный экран)", "Статья") + ссылка на страницу.
    # Если title страницы выглядит как "Статья: ...", показываем полное название статьи.
    label = payload.source or "Не указан"
    page_title = (payload.page_title or "").strip()
    page_url = (payload.page_url or "").strip()

    # собираем HTML-источник
    if page_url:
        anchor_text = page_title or page_url
        source_html = f'{e(label)} - <a href="{e(page_url)}">{e(anchor_text)}</a>'
    else:
        source_html = e(label)

    # собираем plain-text источник
    if page_url:
        source_text = f"{label} - {page_title or page_url} ({page_url})"
    else:
        source_text = label

    text = (
        "Новая заявка с сайта ЛИКВИДАТОР\n\n"
        f"Имя: {payload.name}\n"
        f"Телефон: {payload.phone}\n\n"
        f"Источник: {source_text}\n"
        f"Дата: {ts}\n"
        f"IP: {ip}\n"
        f"User-Agent: {ua}\n"
    )
    html_body = f"""<!doctype html>
<html><body style="font-family:-apple-system,Segoe UI,Arial,sans-serif;max-width:560px;margin:0;padding:16px;color:#222;">
  <h2 style="color:#5b6236;margin:0 0 12px;">Новая заявка с сайта</h2>
  <table cellpadding="6" style="border-collapse:collapse;font-size:15px;">
    <tr><td style="color:#888;width:120px;">Имя:</td><td><b>{e(payload.name)}</b></td></tr>
    <tr><td style="color:#888;">Телефон:</td><td><b><a href="tel:{e(payload.phone)}">{e(payload.phone)}</a></b></td></tr>
    <tr><td style="color:#888;vertical-align:top;">Источник:</td><td>{source_html}</td></tr>
    <tr><td style="color:#888;">Дата:</td><td>{ts}</td></tr>
    <tr><td style="color:#888;">IP:</td><td>{e(ip)}</td></tr>
    <tr><td style="color:#888;vertical-align:top;">User-Agent:</td><td style="font-size:12px;color:#666;">{e(ua)}</td></tr>
  </table>
</body></html>"""
    return html_body, text


# ============ SECURITY: блокируем отдачу исходников и служебных файлов ============
_BLOCKED_PATHS = {
    "/main.py", "/requirements.txt", "/BACKEND.md", "/README.md",
    "/.env", "/.env.example", "/.gitignore",
}
_BLOCKED_PREFIXES = (
    "/.git", "/.venv", "/venv", "/__pycache__", "/agent-plan",
    "/.claude", "/data", "/drafts", "/tools",  # SEO-конвейер: только через /preview
    "/articles_scheduler", "/bot",  # фоновые модули, наружу не светим
)


@app.middleware("http")
async def _block_sources(request: Request, call_next):
    p = request.url.path
    if p in _BLOCKED_PATHS or any(p.startswith(pref) for pref in _BLOCKED_PREFIXES):
        return Response(status_code=404)
    return await call_next(request)


# ============ CLEAN URLS (без .html в адресной строке) ============
# /payment.html        → 301 → /payment
# /index.html          → 301 → /
# /category/all.html   → 301 → /category/all
# /articles/fiz/x.html → 301 → /articles/fiz/x
# /payment             → внутри отдаёт payment.html (rewrite, без редиректа)
_NO_REWRITE_PREFIXES = ("/api", "/preview", "/p/", "/assets", "/favicon")


@app.middleware("http")
async def _clean_urls(request: Request, call_next):
    p = request.url.path
    method = request.method

    # Не трогаем API, превью, статику-ассеты
    if any(p.startswith(pref) for pref in _NO_REWRITE_PREFIXES):
        return await call_next(request)

    if method in ("GET", "HEAD"):
        # 1. /xxx/index.html → /xxx/
        if p.endswith("/index.html"):
            target = p[: -len("index.html")] or "/"
            qs = request.url.query
            if qs:
                target = f"{target}?{qs}"
            return RedirectResponse(url=target, status_code=301)

        # 2. /xxx.html → /xxx
        if p.endswith(".html"):
            target = p[: -len(".html")]
            qs = request.url.query
            if qs:
                target = f"{target}?{qs}"
            return RedirectResponse(url=target, status_code=301)

        # 3. /xxx без расширения → перепиши на /xxx.html, если такой файл есть
        last_seg = p.rsplit("/", 1)[-1]
        if p != "/" and last_seg and "." not in last_seg and not p.endswith("/"):
            fs_path = ROOT / p.lstrip("/")
            if not fs_path.is_file():
                html_path = ROOT / (p.lstrip("/") + ".html")
                if html_path.is_file():
                    new_path = p + ".html"
                    request.scope["path"] = new_path
                    raw_qs = request.scope.get("query_string", b"")
                    request.scope["raw_path"] = new_path.encode() + (b"?" + raw_qs if raw_qs else b"")

    return await call_next(request)


@app.middleware("http")
async def _canonical_redirect(request: Request, call_next):
    """
    Канонические 301-редиректы для устранения дублей в Яндекс.Вебмастере.

    Без них одна главная индексируется как N разных страниц - Яндекс
    размазывает SEO-вес и портит ранжирование:
    - https://www.pravo.shop/   ┐
    - https://pravo.shop//      ├─ один контент, разные URL
    - https://pravo.shop///     │
    - https://pravo.shop/index  ┘

    Приводим всё к https://pravo.shop/<path> (без www, один слэш, без /index).
    """
    if request.method not in ("GET", "HEAD"):
        return await call_next(request)

    host = (request.url.hostname or "").lower()
    path = request.url.path
    query = request.url.query

    new_host = host
    new_path = path
    needs_redirect = False

    # 1. www.* → без www
    if host.startswith("www."):
        new_host = host[4:]
        needs_redirect = True

    # 2. Множественные слэши → один (/////// → /)
    if "//" in path:
        new_path = re.sub(r"/+", "/", path)
        needs_redirect = True

    # 3. /index или /index/ → /
    if path == "/index" or path == "/index/":
        new_path = "/"
        needs_redirect = True

    if needs_redirect:
        target = f"https://{new_host}{new_path}"
        if query:
            target += f"?{query}"
        return RedirectResponse(url=target, status_code=301)

    return await call_next(request)


# ============ ROUTES ============
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "smtp_configured": bool(SMTP_USER and SMTP_PASS),
        "mail_to": MAIL_TO,
    }


@app.post("/api/lead")
async def create_lead(payload: LeadIn, request: Request):
    # 1. honeypot: бот заполнил _hp - тихо отвечаем ok, письмо не шлём
    if payload.hp:
        log.info("honeypot triggered ip=%s name=%r", _client_ip(request), payload.name)
        return {"ok": True}

    # 2. согласие на обработку ПД
    if not payload.consent:
        raise HTTPException(status_code=400, detail="Требуется согласие на обработку персональных данных")

    ip = _client_ip(request)

    # 3. rate limit
    if not _rate_limit_ok(ip):
        log.warning("rate limit ip=%s", ip)
        raise HTTPException(status_code=429, detail="Слишком много заявок. Попробуйте через час.")

    ua = request.headers.get("user-agent", "")
    ts = time.strftime("%d.%m.%Y %H:%M", time.localtime())

    html_body, text_body = _render_mail(payload, ip, ua, ts)

    try:
        _send_mail("PRAVO.SHOP - Заявка", html_body, text_body)
    except Exception:
        log.exception("send_mail failed")
        raise HTTPException(
            status_code=500,
            detail="Не удалось отправить заявку. Позвоните нам или попробуйте позже.",
        )

    log.info("lead sent name=%r phone=%r ip=%s", payload.name, payload.phone, ip)
    return {"ok": True}


# ============ PREVIEW (черновики SEO-конвейера) ============
# Доступ через Basic Auth. Логин/пароль - в PREVIEW_USER / PREVIEW_PASSWORD (ENV).
# Все ответы получают X-Robots-Tag: noindex, чтобы не попасть в поисковики.
_basic_auth = HTTPBasic(realm="LIKVIDATOR preview")


def _check_preview_auth(credentials: HTTPBasicCredentials = Depends(_basic_auth)) -> str:
    if not (PREVIEW_USER and PREVIEW_PASSWORD):
        raise HTTPException(status_code=503, detail="Preview не настроен (нет PREVIEW_USER/PREVIEW_PASSWORD)")
    user_ok = secrets.compare_digest(credentials.username.encode(), PREVIEW_USER.encode())
    pass_ok = secrets.compare_digest(credentials.password.encode(), PREVIEW_PASSWORD.encode())
    if not (user_ok and pass_ok):
        raise HTTPException(
            status_code=401,
            detail="Неверный логин или пароль",
            headers={"WWW-Authenticate": 'Basic realm="LIKVIDATOR preview"'},
        )
    return credentials.username


def _safe_slug(slug: str) -> str:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,200}", slug or ""):
        raise HTTPException(status_code=400, detail="Некорректный slug")
    return slug


def _list_drafts() -> list[dict]:
    if not DRAFTS_DIR.exists():
        return []
    items = []
    for sub in sorted(DRAFTS_DIR.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_"):
            continue
        article_v1 = sub / "article.html"
        article_v2 = sub / "article-v2.html"
        meta_path = sub / "meta.json"
        if not article_v1.exists() and not article_v2.exists():
            continue
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        # Берём mtime последней актуальной версии для сортировки.
        latest_file = article_v2 if article_v2.exists() else article_v1
        items.append({
            "slug": sub.name,
            "category": meta.get("category", "?"),
            "title": meta.get("title") or meta.get("h1") or sub.name,
            "h1": meta.get("h1", ""),
            "description": meta.get("description", ""),
            "factcheck_passed": meta.get("factcheck_passed"),
            "text_chars": meta.get("text_chars"),
            "text_words": meta.get("text_words"),
            "has_v1": article_v1.exists(),
            "has_v2": article_v2.exists(),
            "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(latest_file.stat().st_mtime)),
        })
    items.sort(key=lambda x: x["updated_at"], reverse=True)
    return items


_PREVIEW_INDEX_TPL = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>ЛИКВИДАТОР - черновики</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1100px;margin:0 auto;padding:32px 24px;color:#222;background:#fafaf7}}
  h1{{margin:0 0 8px;font-size:28px;color:#3a4118}}
  .lead{{color:#666;margin:0 0 24px;font-size:14px}}
  .lead b{{color:#b85c00}}
  table{{width:100%;border-collapse:collapse;background:#fff;border:1px solid #e6e3da;border-radius:8px;overflow:hidden}}
  th,td{{padding:12px 14px;text-align:left;border-bottom:1px solid #efece4;font-size:14px;vertical-align:top}}
  th{{background:#f3f0e7;color:#5b6236;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:.04em}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#fcfaf2}}
  .slug{{font-family:ui-monospace,Menlo,Consolas,monospace;color:#5b6236;font-size:13px}}
  .cat{{display:inline-block;padding:2px 8px;border-radius:10px;background:#eef0e0;color:#5b6236;font-size:12px;font-weight:600}}
  .ok{{color:#3a8a2a;font-weight:600}}
  .fail{{color:#b00;font-weight:600}}
  .meta{{color:#888;font-size:12px;margin-top:4px}}
  a.title{{color:#222;text-decoration:none;font-weight:600}}
  a.title:hover{{color:#5b6236;text-decoration:underline}}
  .empty{{padding:48px 24px;text-align:center;color:#888}}
</style></head>
<body>
<h1>Черновики SEO-конвейера</h1>
<p class="lead">Это превью неопубликованных статей. Видно только вам по логину. <b>Не индексируется</b> поисковиками.</p>
<div style="margin:0 0 20px;display:flex;gap:8px;flex-wrap:wrap;font-size:13px">
  <a href="/preview/topics" style="background:#0969da;border:1px solid #0969da;color:#fff;padding:6px 12px;border-radius:6px;text-decoration:none">🗺️ Карта тем (32)</a>
  <a href="{repo_url}" target="_blank" rel="noopener" style="background:#fff;border:1px solid #d0d7de;color:#24292f;padding:6px 12px;border-radius:6px;text-decoration:none">📁 Репо на GitHub</a>
  <a href="{prompts_url}" target="_blank" rel="noopener" style="background:#fff;border:1px solid #d0d7de;color:#24292f;padding:6px 12px;border-radius:6px;text-decoration:none">🤖 Промпты агентов</a>
  <a href="{readme_url}" target="_blank" rel="noopener" style="background:#fff;border:1px solid #d0d7de;color:#24292f;padding:6px 12px;border-radius:6px;text-decoration:none">📖 Инструкция</a>
  <a href="{issues_url}" target="_blank" rel="noopener" style="background:#fff;border:1px solid #d0d7de;color:#24292f;padding:6px 12px;border-radius:6px;text-decoration:none">💬 Комментарии</a>
</div>
<h2 style="margin:24px 0 12px;font-size:18px;color:#3a4118">Готовые черновики статей</h2>
{table}
</body></html>"""


@app.get("/preview", include_in_schema=False)
@app.get("/preview/", include_in_schema=False)
def preview_index(_user: str = Depends(_check_preview_auth)):
    items = _list_drafts()
    if not items:
        body = '<div class="empty">Пока нет черновиков. Запустите конвейер агентов и обновите страницу.</div>'
    else:
        rows = []
        for it in items:
            fc = it["factcheck_passed"]
            fc_html = '<span class="ok">да</span>' if fc is True else (
                '<span class="fail">нет</span>' if fc is False else '<span style="color:#999">-</span>'
            )
            chars = it["text_chars"]
            words = it["text_words"]
            size_html = f'{chars} зн. / {words} сл.' if chars and words else '-'
            e = html.escape
            slug_e = e(it["slug"])
            # Кнопки версий: v2 (актуальная) и v1 (исходная), если присутствуют.
            version_links = []
            if it["has_v2"]:
                version_links.append(
                    f'<a href="/preview/{slug_e}?v=2" '
                    'style="display:inline-block;padding:4px 10px;border-radius:6px;'
                    'background:#3a8a2a;color:#fff;text-decoration:none;font-size:12px;'
                    'font-weight:600;margin-right:6px">v2 (актуальная)</a>'
                )
            if it["has_v1"]:
                version_links.append(
                    f'<a href="/preview/{slug_e}?v=1" '
                    'style="display:inline-block;padding:4px 10px;border-radius:6px;'
                    'background:#888;color:#fff;text-decoration:none;font-size:12px">v1 (как было)</a>'
                )
            versions_html = "".join(version_links) if version_links else "-"
            # Главная ссылка ведёт на v2 (если есть), иначе на v1.
            default_v = "2" if it["has_v2"] else "1"
            rows.append(
                f'<tr>'
                f'<td><span class="cat">{e(str(it["category"]))}</span></td>'
                f'<td><a class="title" href="/preview/{slug_e}?v={default_v}">{e(it["title"])}</a>'
                f'<div class="meta slug">{slug_e}</div></td>'
                f'<td>{versions_html}</td>'
                f'<td>{size_html}</td>'
                f'<td>{fc_html}</td>'
                f'<td>{e(it["updated_at"])}</td>'
                f'</tr>'
            )
        table = (
            '<table>'
            '<thead><tr><th>Категория</th><th>Заголовок / slug</th><th>Версии</th><th>Объём</th><th>Фактчек</th><th>Обновлено</th></tr></thead>'
            f'<tbody>{"".join(rows)}</tbody>'
            '</table>'
        )
        body = table
    repo_url = f"https://github.com/{GITHUB_REPO}" if GITHUB_REPO else "#"
    prompts_url = f"{repo_url}/tree/{GITHUB_BRANCH}/.claude/agents" if GITHUB_REPO else "#"
    readme_url = f"{repo_url}#readme" if GITHUB_REPO else "#"
    issues_url = f"{repo_url}/issues/new" if GITHUB_REPO else "#"
    response = HTMLResponse(_PREVIEW_INDEX_TPL.format(
        table=body, repo_url=repo_url, prompts_url=prompts_url,
        readme_url=readme_url, issues_url=issues_url,
    ))
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


_TOPIC_MAP_DIR = DRAFTS_DIR / "_topic-map"


def _load_topic_map() -> dict:
    """Читает все JSON из drafts/_topic-map/, возвращает {category: {label, topics}}."""
    out = {}
    if not _TOPIC_MAP_DIR.exists():
        return out
    for f in sorted(_TOPIC_MAP_DIR.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            out[data["category"]] = data
        except Exception:
            continue
    return out


_TOPICS_TPL = """<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<meta name="robots" content="noindex,nofollow"/>
<title>Карта тем - ЛИКВИДАТОР</title>
<style>
  body{{font-family:-apple-system,Segoe UI,Roboto,Arial,sans-serif;max-width:1200px;margin:0 auto;padding:32px 24px;color:#222;background:#fafaf7}}
  h1{{margin:0 0 8px;font-size:28px;color:#3a4118}}
  .lead{{color:#666;margin:0 0 24px;font-size:14px;max-width:780px}}
  .lead b{{color:#b85c00}}
  .top-actions{{margin:0 0 24px;display:flex;gap:8px;flex-wrap:wrap;font-size:13px}}
  .top-actions a{{padding:6px 12px;border-radius:6px;text-decoration:none;border:1px solid #d0d7de;background:#fff;color:#24292f}}
  .top-actions a.primary{{background:#0969da;border-color:#0969da;color:#fff}}
  .filters{{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 16px}}
  .filters button{{padding:6px 14px;border-radius:18px;border:1px solid #d0d7de;background:#fff;color:#24292f;cursor:pointer;font-size:13px;font-weight:500}}
  .filters button.active{{background:#5b6236;border-color:#5b6236;color:#fff}}
  .cat-section{{margin:0 0 32px}}
  .cat-section h2{{margin:0 0 8px;font-size:20px;color:#3a4118;border-bottom:2px solid #eef0e0;padding-bottom:8px}}
  .cat-section .sub{{color:#888;font-size:13px;margin:0 0 16px}}
  .topic{{background:#fff;border:1px solid #e6e3da;border-radius:8px;padding:14px 16px;margin:0 0 10px;display:grid;grid-template-columns:1fr auto;gap:12px;align-items:start}}
  .topic.s-approved{{border-left:4px solid #3a8a2a}}
  .topic.s-rejected{{border-left:4px solid #b00;opacity:.55}}
  .topic.s-rewrite{{border-left:4px solid #b85c00}}
  .topic.s-proposed{{border-left:4px solid #d0d7de}}
  .topic h3{{margin:0 0 4px;font-size:15px;color:#222;font-weight:600}}
  .topic .id{{display:inline-block;padding:1px 7px;border-radius:4px;background:#f3f0e7;color:#5b6236;font-size:11px;font-family:ui-monospace,Menlo,Consolas,monospace;margin-right:6px}}
  .topic .desc{{color:#666;font-size:13px;margin:4px 0 8px;line-height:1.4}}
  .topic .tags{{display:flex;gap:5px;flex-wrap:wrap;font-size:11px}}
  .tag{{padding:2px 8px;border-radius:10px;background:#eef0e0;color:#5b6236;font-weight:600}}
  .tag.f-high{{background:#fee2c7;color:#a64a00}}
  .tag.f-medium{{background:#fff3d6;color:#7a5400}}
  .tag.f-low{{background:#e8e8e8;color:#666}}
  .tag.i-problem{{background:#fde4e4;color:#a30000}}
  .tag.i-solution{{background:#e2eafd;color:#0a3d9c}}
  .tag.i-commercial{{background:#d4f5dd;color:#1c6624}}
  .tag.i-informational{{background:#f0f0f0;color:#444}}
  .topic .actions{{display:flex;gap:6px;align-items:center}}
  .topic .actions a{{font-size:12px;text-decoration:none;color:#0969da;padding:4px 8px;border-radius:5px;border:1px solid #d0d7de;background:#fff;white-space:nowrap}}
  .topic .actions a:hover{{background:#f3f0e7}}
  .notes{{margin-top:8px;padding:8px 10px;background:#fffbe5;border:1px solid #f0e6a3;border-radius:5px;font-size:12px;color:#7a5400}}
  .empty{{padding:48px 24px;text-align:center;color:#888;background:#fff;border-radius:8px;border:1px dashed #d0d7de}}
  .stats{{display:flex;gap:14px;margin:0 0 20px;font-size:13px;color:#666}}
  .stats span b{{color:#3a4118;font-size:15px}}
</style></head>
<body>
<h1>🗺️ Карта тем для статей</h1>
<p class="lead">Это <b>предложения тем</b> от агента-семантика. Перед тем как запускать конвейер на 30+ статей, посмотрите глазами: попадает ли агент в вашу тематику, правильно ли видит интент клиентов и стадию воронки.</p>
<p class="lead">Чтобы одобрить тему: откройте файл темы на GitHub (кнопка «✏️ Редактировать»), измените <code>"status": "proposed"</code> на <code>"approved"</code>, добавьте комментарий в <code>"client_notes"</code>. Или создайте issue на GitHub с пометкой темы.</p>

<details style="margin:0 0 24px;background:#fff;border:1px solid #e6e3da;border-radius:8px;padding:14px 18px">
  <summary style="cursor:pointer;font-weight:600;color:#3a4118;font-size:15px;list-style:none">📖 Что означают теги у каждой темы (нажмите, чтобы развернуть)</summary>
  <div style="margin-top:14px;font-size:13px;line-height:1.55;color:#444;display:grid;grid-template-columns:1fr 1fr;gap:18px 28px">
    <div>
      <div style="font-weight:600;color:#3a4118;margin-bottom:6px">🎯 Интент (что в голове у читателя)</div>
      <div><span class="tag i-problem">problem-aware</span> — человек в проблеме («приставы списали зарплату»). Самая горячая аудитория, нужна эмпатия.</div>
      <div style="margin-top:4px"><span class="tag i-solution">solution-aware</span> — знает что есть решение, выбирает между вариантами.</div>
      <div style="margin-top:4px"><span class="tag i-commercial">commercial</span> — готов покупать услугу («сколько стоит банкротство»). Самый ценный трафик.</div>
      <div style="margin-top:4px"><span class="tag i-informational">informational</span> — просто хочет понять как работает. Прогрев на будущее.</div>
    </div>
    <div>
      <div style="font-weight:600;color:#3a4118;margin-bottom:6px">🪜 Стадия воронки (как близко к покупке)</div>
      <div><span class="tag">awareness</span> — только узнал о проблеме, гуглит общее.</div>
      <div style="margin-top:4px"><span class="tag">consideration</span> — сравнивает варианты, выбирает подход.</div>
      <div style="margin-top:4px"><span class="tag">decision</span> — готов действовать, ищет исполнителя. Сюда конверсия в лиды.</div>
    </div>
    <div>
      <div style="font-weight:600;color:#3a4118;margin-bottom:6px">📑 Формат статьи</div>
      <div><span class="tag">step-by-step</span> — пошаговая инструкция</div>
      <div style="margin-top:4px"><span class="tag">comparison</span> — сравнение вариантов («МФЦ vs суд»)</div>
      <div style="margin-top:4px"><span class="tag">case-study</span> — разбор конкретного случая</div>
      <div style="margin-top:4px"><span class="tag">law-explanation</span> — объяснение закона</div>
      <div style="margin-top:4px"><span class="tag">faq</span> — вопрос-ответ</div>
      <div style="margin-top:4px"><span class="tag">myths</span> — мифы и правда</div>
    </div>
    <div>
      <div style="font-weight:600;color:#3a4118;margin-bottom:6px">📊 Частотность (примерная оценка агента)</div>
      <div><span class="tag f-high">high</span> — тысячи запросов в месяц, приоритет</div>
      <div style="margin-top:4px"><span class="tag f-medium">medium</span> — сотни, можно делать</div>
      <div style="margin-top:4px"><span class="tag f-low">low</span> — десятки, только если стратегически важно</div>
      <div style="margin-top:8px;font-size:12px;color:#888">Примечание: грубая оценка без Wordstat. Точную частотность смотрите в Яндекс.Wordstat.</div>
    </div>
    <div style="grid-column:1 / -1">
      <div style="font-weight:600;color:#3a4118;margin-bottom:6px">🎁 Оффер (CTA в конце статьи)</div>
      <div style="display:grid;grid-template-columns:repeat(2,1fr);gap:6px 18px">
        <div><code>proverka-spisaniya</code> — бесплатная проверка, спишутся ли долги</div>
        <div><code>otmena-prikaza</code> — помощь с отменой судебного приказа</div>
        <div><code>snyatie-aresta</code> — снятие ареста со счёта</div>
        <div><code>raschet-stoimosti</code> — расчёт стоимости банкротства</div>
      </div>
    </div>
    <div style="grid-column:1 / -1;padding-top:10px;border-top:1px solid #eef0e0">
      <div style="font-weight:600;color:#3a4118;margin-bottom:6px">Зачем эти теги?</div>
      <div>Они показывают, что агент думает <b>стратегически</b>, а не пишет что попало. Если, например, все 32 темы в стадии <code>awareness</code> и ни одной <code>decision</code> — это перекос, не будет лидов. Если тема для <code>problem-aware</code> читателя, а агент пишет её в стиле сухого закона — что-то не так с тоном. Это инструмент быстрого аудита.</div>
    </div>
  </div>
</details>

<div class="top-actions">
  <a href="/preview/" class="primary">← К черновикам</a>
  <a href="{topics_folder_url}" target="_blank" rel="noopener">📁 Открыть папку тем на GitHub</a>
  <a href="{issues_url}" target="_blank" rel="noopener">💬 Создать issue с обратной связью</a>
</div>

<div class="stats">
  <span>Всего тем: <b>{total}</b></span>
  <span>Одобрено: <b>{approved}</b></span>
  <span>На переработку: <b>{rewrite}</b></span>
  <span>Отклонено: <b>{rejected}</b></span>
  <span>Ждут решения: <b>{proposed}</b></span>
</div>

<div class="filters">
  <button class="active" data-filter="all">Все категории</button>
  {filter_buttons}
</div>

{sections}

<script>
  const buttons = document.querySelectorAll('.filters button');
  const sections = document.querySelectorAll('.cat-section');
  buttons.forEach(b => b.addEventListener('click', () => {{
    buttons.forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    const f = b.dataset.filter;
    sections.forEach(s => {{
      s.style.display = (f === 'all' || s.dataset.cat === f) ? '' : 'none';
    }});
  }}));
</script>
</body></html>"""


@app.get("/preview/topics", include_in_schema=False)
def preview_topics(_user: str = Depends(_check_preview_auth)):
    tm = _load_topic_map()
    if not tm:
        body = '<div class="empty">Карта тем пока не сгенерирована.<br/>Запустите агента 1 в режиме генерации тем.</div>'
        repo_url = f"https://github.com/{GITHUB_REPO}" if GITHUB_REPO else "#"
        response = HTMLResponse(_TOPICS_TPL.format(
            sections=body, filter_buttons="", topics_folder_url=repo_url,
            issues_url=f"{repo_url}/issues/new", total=0, approved=0,
            rewrite=0, rejected=0, proposed=0,
        ))
    else:
        e = html.escape
        sections_html = []
        filter_buttons = []
        total = approved = rewrite = rejected = proposed = 0

        cat_order = ["fiz", "yur", "vzysk", "news"]
        for cat in cat_order:
            if cat not in tm:
                continue
            data = tm[cat]
            label = data.get("category_label", cat)
            topics = data.get("topics", [])
            filter_buttons.append(
                f'<button data-filter="{e(cat)}">{e(label)} ({len(topics)})</button>'
            )

            topic_blocks = []
            for t in topics:
                total += 1
                status = (t.get("status") or "proposed").lower()
                if status == "approved": approved += 1
                elif status == "rewrite": rewrite += 1
                elif status == "rejected": rejected += 1
                else: proposed += 1

                edit_url = (
                    f"https://github.com/{GITHUB_REPO}/edit/{GITHUB_BRANCH}/drafts/_topic-map/{cat}.json"
                    if GITHUB_REPO else "#"
                )
                intent = t.get("intent", "")
                intent_short = intent.split("-")[0] if "-" in intent else intent[:4]
                freq = t.get("frequency_estimate", "")
                offer = t.get("offer", "")
                notes = t.get("client_notes", "")
                notes_html = f'<div class="notes">📝 {e(notes)}</div>' if notes else ""

                topic_blocks.append(f'''
<div class="topic s-{e(status)}" data-cat="{e(cat)}">
  <div>
    <h3><span class="id">{e(t.get("id",""))}</span>{e(t.get("title",""))}</h3>
    <div class="desc">{e(t.get("description",""))}</div>
    <div class="tags">
      <span class="tag i-{e(intent_short)}">{e(intent)}</span>
      <span class="tag">{e(t.get("funnel_stage",""))}</span>
      <span class="tag">{e(t.get("article_type",""))}</span>
      <span class="tag f-{e(freq)}">частотность: {e(freq)}</span>
      <span class="tag">оффер: {e(offer)}</span>
      <span class="tag">~{e(str(t.get("expected_length_chars","")))} зн.</span>
    </div>
    <div style="margin-top:6px;font-size:12px;color:#888"><b>Главный ключ:</b> {e(t.get("main_keyword",""))}</div>
    <div style="margin-top:4px;font-size:12px;color:#888"><b>Зачем:</b> {e(t.get("rationale",""))}</div>
    {notes_html}
  </div>
  <div class="actions">
    <a href="{edit_url}" target="_blank" rel="noopener">✏️ Редактировать</a>
  </div>
</div>''')

            sections_html.append(
                f'<section class="cat-section" data-cat="{e(cat)}">'
                f'<h2>{e(label)} <span style="color:#888;font-size:14px;font-weight:400">({len(topics)} тем)</span></h2>'
                f'<p class="sub">Категория: <code>{e(cat)}</code> · файл: <code>drafts/_topic-map/{e(cat)}.json</code></p>'
                f'{"".join(topic_blocks)}'
                '</section>'
            )

        repo_url = f"https://github.com/{GITHUB_REPO}" if GITHUB_REPO else "#"
        topics_folder_url = f"{repo_url}/tree/{GITHUB_BRANCH}/drafts/_topic-map" if GITHUB_REPO else "#"
        issues_url = f"{repo_url}/issues/new" if GITHUB_REPO else "#"

        response = HTMLResponse(_TOPICS_TPL.format(
            sections="".join(sections_html),
            filter_buttons="".join(filter_buttons),
            topics_folder_url=topics_folder_url,
            issues_url=issues_url,
            total=total, approved=approved, rewrite=rewrite,
            rejected=rejected, proposed=proposed,
        ))

    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/preview/{slug}", include_in_schema=False)
def preview_slug(slug: str, v: str = "", _user: str = Depends(_check_preview_auth)):
    slug = _safe_slug(slug)
    folder = DRAFTS_DIR / slug
    article_v1 = folder / "article.html"
    article_v2 = folder / "article-v2.html"
    has_v1 = article_v1.exists()
    has_v2 = article_v2.exists()

    # Выбор версии: по умолчанию v2 (актуальная), если есть. Иначе - v1.
    requested = v.strip()
    if requested == "1" and has_v1:
        article = article_v1
        current_version = "1"
        article_filename = "article.html"
    elif requested == "2" and has_v2:
        article = article_v2
        current_version = "2"
        article_filename = "article-v2.html"
    elif has_v2:
        article = article_v2
        current_version = "2"
        article_filename = "article-v2.html"
    elif has_v1:
        article = article_v1
        current_version = "1"
        article_filename = "article.html"
    else:
        raise HTTPException(status_code=404, detail="Черновик не найден")

    raw = article.read_text(encoding="utf-8")
    # Принудительный noindex в самом HTML (на случай если кто-то скопирует исходник)
    if '<meta name="robots"' not in raw.lower():
        raw = raw.replace("<head>", '<head>\n  <meta name="robots" content="noindex,nofollow"/>', 1)

    # Плавающая панель: переключатель версий, ссылки на GitHub, кнопка назад.
    bar_buttons = []

    # Переключатель v1 / v2.
    if has_v1 and has_v2:
        if current_version == "2":
            bar_buttons.append(
                '<span style="background:#3a8a2a;color:#fff;padding:8px 14px;border-radius:6px;'
                'box-shadow:0 2px 8px rgba(0,0,0,.15);font-weight:600">'
                'Сейчас: v2 (актуальная)</span>'
            )
            bar_buttons.append(
                f'<a href="/preview/{slug}?v=1" '
                'style="background:#fff;color:#24292f;padding:8px 14px;border-radius:6px;'
                'text-decoration:none;border:1px solid #d0d7de">→ показать v1 (как было)</a>'
            )
        else:
            bar_buttons.append(
                '<span style="background:#888;color:#fff;padding:8px 14px;border-radius:6px;'
                'box-shadow:0 2px 8px rgba(0,0,0,.15);font-weight:600">'
                'Сейчас: v1 (как было)</span>'
            )
            bar_buttons.append(
                f'<a href="/preview/{slug}?v=2" '
                'style="background:#3a8a2a;color:#fff;padding:8px 14px;border-radius:6px;'
                'text-decoration:none;border:1px solid #2a6d1c;font-weight:600">→ показать v2 (актуальная)</a>'
            )

    # Ссылки на GitHub: править актуальную версию.
    if GITHUB_REPO:
        edit_article = f"https://github.com/{GITHUB_REPO}/edit/{GITHUB_BRANCH}/drafts/{slug}/{article_filename}"
        edit_meta = f"https://github.com/{GITHUB_REPO}/edit/{GITHUB_BRANCH}/drafts/{slug}/meta.json"
        bar_buttons.append(
            f'<a href="{edit_article}" target="_blank" rel="noopener" '
            'style="background:#0969da;color:#fff;padding:8px 14px;border-radius:6px;'
            'text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.15)">✏️ Править текст на GitHub</a>'
        )
        bar_buttons.append(
            f'<a href="{edit_meta}" target="_blank" rel="noopener" '
            'style="background:#6e7781;color:#fff;padding:8px 14px;border-radius:6px;'
            'text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.15)">⚙️ Правки meta</a>'
        )

    bar_buttons.append(
        '<a href="/preview/" '
        'style="background:#fff;color:#24292f;padding:8px 14px;border-radius:6px;'
        'text-decoration:none;border:1px solid #d0d7de">← К списку</a>'
    )

    bar = (
        '<div style="position:fixed;top:12px;right:12px;z-index:99999;display:flex;gap:8px;flex-wrap:wrap;'
        'max-width:calc(100% - 24px);'
        'font:14px system-ui,-apple-system,Segoe UI,Roboto,sans-serif">'
        + "".join(bar_buttons) +
        '</div>'
    )
    if "</body>" in raw:
        raw = raw.replace("</body>", bar + "</body>", 1)
    else:
        raw = raw + bar

    response = HTMLResponse(raw)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


# ============ PUBLIC PREVIEW (для Telegram-бота) ============
# Этот роут отдаёт черновик статьи без Basic Auth, по подписанной ссылке вида
# /p/{slug}?t=token[&v=2.1]. Токен хранится в data/bot_state.json и подставляется
# ботом в ссылках. Это удобно для заказчика с мобильного, без логина-пароля.

_BOT_STATE_PATH = ROOT / "data" / "bot_state.json"


def _bot_preview_token() -> str | None:
    """Читает токен превью из bot_state.json. Если файла нет - роут отключён."""
    if not _BOT_STATE_PATH.exists():
        return None
    try:
        data = json.loads(_BOT_STATE_PATH.read_text(encoding="utf-8"))
        token = (data or {}).get("preview_token")
        return token if isinstance(token, str) and token else None
    except (json.JSONDecodeError, OSError):
        return None


@app.get("/p/{slug}", include_in_schema=False)
def public_preview_by_token(slug: str, t: str = "", v: str = ""):
    """
    Публичный preview статьи для бота. Доступ - по совпадению токена с тем,
    что хранится в bot_state.json. Версия (v) - например "2.1"; берёт файл из
    drafts/{slug}/versions/v{v}.html. Если v не указан - подбирает
    article-v2.html → article.html.
    """
    expected = _bot_preview_token()
    if not expected:
        raise HTTPException(status_code=404, detail="Preview-роут отключён")

    # secrets.compare_digest защищает от timing-атаки.
    if not t or not secrets.compare_digest(t, expected):
        raise HTTPException(status_code=403, detail="Недействительная ссылка")

    slug = _safe_slug(slug)
    folder = DRAFTS_DIR / slug
    if not folder.exists():
        raise HTTPException(status_code=404, detail="Статья не найдена")

    # Выбор файла.
    candidate: Path | None = None
    if v:
        # ожидаем формат "2.0" / "2.1" и т.д. - простая sanity-проверка.
        if not re.fullmatch(r"\d+\.\d+", v):
            raise HTTPException(status_code=400, detail="Некорректная версия")
        cand = folder / "versions" / f"v{v}.html"
        if cand.exists():
            candidate = cand
    if candidate is None:
        v2 = folder / "article-v2.html"
        v1 = folder / "article.html"
        candidate = v2 if v2.exists() else (v1 if v1.exists() else None)

    if candidate is None:
        raise HTTPException(status_code=404, detail="Файл статьи не найден")

    raw = candidate.read_text(encoding="utf-8")
    if '<meta name="robots"' not in raw.lower():
        raw = raw.replace(
            "<head>",
            '<head>\n  <meta name="robots" content="noindex,nofollow"/>',
            1,
        )
    response = HTMLResponse(raw)
    response.headers["X-Robots-Tag"] = "noindex, nofollow"
    response.headers["Cache-Control"] = "no-store"
    return response


# ============ ADMIN: одной командой запустить слот scheduler-а ============
# POST/GET /admin/run/{category} — запускает articles_scheduler.runner с
# FORCE_CATEGORY={category} в фоне (subprocess). Возвращает PID и путь к логу.
# Защита: Basic Auth (PREVIEW_USER/PREVIEW_PASSWORD).
#
# Использование:
#   curl -u admin:admin -X POST https://pravo.shop/admin/run/news
#   или просто открыть в браузере https://pravo.shop/admin/run/news
#
# Прогресс — через тот же /preview/ или Telegram-уведомление от bot/watcher.

_VALID_RUNNER_CATEGORIES = {"fiz", "yur", "vzysk", "news"}
_RUNNER_LOG_DIR = ROOT / "data" / "local_runs"


def _start_runner_slot(category: str) -> dict:
    """Запускает один слот scheduler-а в фоне через subprocess. Возвращает
    {pid, log_path, started_at, category}. Не блокирует HTTP-ответ."""
    if category not in _VALID_RUNNER_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail=f"category must be one of: {sorted(_VALID_RUNNER_CATEGORIES)}",
        )

    _RUNNER_LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = _dt.now().strftime("%Y%m%d_%H%M%S")
    log_path = _RUNNER_LOG_DIR / f"manual_{category}_{ts}.log"

    env = os.environ.copy()
    env["FORCE_CATEGORY"] = category

    log_fh = open(log_path, "wb", buffering=0)
    log_fh.write(f"=== MANUAL RUN category={category} started_at={ts} ===\n".encode("utf-8"))
    proc = subprocess.Popen(
        [sys.executable, "-m", "articles_scheduler.runner", "--category", category],
        cwd=str(ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        # На POSIX отвязываем процесс от парента, чтобы он жил после перезапуска uvicorn.
        start_new_session=(os.name != "nt"),
    )
    log.info("Запущен ручной слот: category=%s pid=%d log=%s", category, proc.pid, log_path)
    return {
        "ok": True,
        "category": category,
        "pid": proc.pid,
        "log_path": str(log_path.relative_to(ROOT)),
        "started_at": ts,
        "hint": "Слот запущен в фоне. Прогресс смотри в /preview/ или жди уведомление в Telegram.",
    }


@app.post("/admin/run/{category}", include_in_schema=False)
def admin_run_slot(category: str, _user: str = Depends(_check_preview_auth)):
    """Запустить slot scheduler-а с указанной категорией. POST для строгости."""
    return _start_runner_slot(category)


@app.get("/admin/run/{category}", include_in_schema=False)
def admin_run_slot_get(category: str, _user: str = Depends(_check_preview_auth)):
    """Удобный GET-вариант: можно открыть в браузере и сразу запустить.
    HTML с подтверждением + автоматический POST через JS."""
    return _start_runner_slot(category)


# ============ TELEGRAM BOT (фоном вместе с FastAPI) ============
# На Timeweb Cloud Apps preset "FastAPI" запускает только uvicorn и не читает
# Procfile. Поэтому worker-процесс из Procfile там не поднимается. Чтобы не
# плодить отдельное приложение/инстанс, поднимаем aiogram прямо в lifecycle
# FastAPI: при старте сервера запускаем polling и watcher как фоновые таски.
# Если бот падает на старте (нет TG_BOT_TOKEN, конфликт сессий, что угодно) -
# сайт продолжает работать как ни в чём не бывало, в логе остаётся диагностика.

_bot_tasks: dict = {}


@app.on_event("startup")
async def _start_telegram_bot():
    try:
        from aiogram import Bot, Dispatcher
        from aiogram.client.default import DefaultBotProperties
        from aiogram.enums import ParseMode

        from bot import handlers
        from bot.main import watch_loop
        from bot.config import (
            DATA_DIR,
            TG_ALLOWED_CHAT_IDS,
            TG_BOT_TOKEN,
            BOT_WATCH_INTERVAL_SEC,
            validate_config,
        )
        from bot.fsm_storage import JsonFileStorage
    except Exception:
        log.exception("Telegram-бот: не смог импортировать модули, сайт работает без него")
        return

    errors = validate_config()
    if errors:
        for e in errors:
            log.error("Telegram-бот: %s", e)
        log.error("Telegram-бот не запущен (см. ошибки выше). Сайт продолжает работу.")
        return

    try:
        bot_obj = Bot(
            token=TG_BOT_TOKEN,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        # JsonFileStorage переживает редеплой Cloud Apps. С MemoryStorage flow
        # «Правки»/«Отклонить» терялся каждый раз когда scheduler пушил
        # bot_state.json между нажатием кнопки и ответом юзера.
        dp = Dispatcher(storage=JsonFileStorage(DATA_DIR / ".fsm_state.json"))

        # FSM-alias middleware: handlers объявляют параметр `fsm: FSMContext`,
        # но aiogram 3.x по умолчанию инжектит ключом `state`. Без алиаса каждое
        # нажатие кнопок «Правки» / «Отклонить» крашится с
        # `TypeError: missing 1 required positional argument: 'fsm'`.
        # Сразу копируем data['state'] → data['fsm'] чтобы handler'ы работали
        # с тем же FSMContext под привычным именем.
        @dp.update.outer_middleware()
        async def _alias_fsm_to_state(handler, event, data):
            state_obj = data.get("state")
            if state_obj is not None and "fsm" not in data:
                data["fsm"] = state_obj
            return await handler(event, data)

        dp.include_router(handlers.router)

        log.info(
            "Telegram-бот стартует. Whitelist chat_id: %s. Watcher: %d сек",
            TG_ALLOWED_CHAT_IDS or "ПУСТО (все)",
            BOT_WATCH_INTERVAL_SEC,
        )

        _bot_tasks["bot"] = bot_obj
        _bot_tasks["dp"] = dp
        _bot_tasks["polling"] = asyncio.create_task(
            dp.start_polling(bot_obj, allowed_updates=dp.resolve_used_update_types()),
            name="tg-polling",
        )
        _bot_tasks["watcher"] = asyncio.create_task(
            watch_loop(bot_obj),
            name="tg-watcher",
        )
        log.info("Telegram-бот запущен в фоне FastAPI")
    except Exception:
        log.exception("Telegram-бот: не смог запуститься, сайт работает без него")


@app.on_event("shutdown")
async def _stop_telegram_bot():
    for key in ("polling", "watcher"):
        task = _bot_tasks.get(key)
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
    bot_obj = _bot_tasks.get("bot")
    if bot_obj:
        try:
            await bot_obj.session.close()
        except Exception:
            log.exception("Telegram-бот: ошибка закрытия сессии")


# ============ ARTICLES SCHEDULER (фоном вместе с FastAPI) ============
# APScheduler-таймер, который раз в N минут запускает /write-article через
# Claude Code, складывает результат в drafts/ и пушит в git. Безопасный
# дефолт: если SCHEDULER_ENABLED != true в env, scheduler не стартует.
@app.on_event("startup")
async def _start_articles_scheduler():
    try:
        from articles_scheduler.lifespan import start_articles_scheduler
        start_articles_scheduler()
    except Exception:
        log.exception("Articles scheduler: не смог стартовать, сайт работает без него")


@app.on_event("shutdown")
async def _stop_articles_scheduler():
    try:
        from articles_scheduler.lifespan import stop_articles_scheduler
        await stop_articles_scheduler()
    except Exception:
        log.exception("Articles scheduler: ошибка остановки")


# ============ STATIC ============
# Монтируем ПОСЛЕ api-роутов, чтобы /api/* не перехватывался статикой.
# html=True: /path/ → /path/index.html, / → /index.html
app.mount("/", StaticFiles(directory=str(ROOT), html=True), name="static")
