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
import time
import json
import html
import secrets
import smtplib
import logging
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
# Если API будет на поддомене api.liquidator.ru - поставь "https://liquidator.ru".
ALLOWED_ORIGIN = os.getenv("ALLOWED_ORIGIN", "same-origin")

RATE_LIMIT_PER_HOUR = int(os.getenv("RATE_LIMIT_PER_HOUR", "3"))

# Preview-роут для черновиков SEO-конвейера. Если переменные не заданы - роут отключён (404).
PREVIEW_USER = os.getenv("PREVIEW_USER", "")
PREVIEW_PASSWORD = os.getenv("PREVIEW_PASSWORD", "")
DRAFTS_DIR = ROOT / "drafts"
# Репо на GitHub: используется для кнопки "Править на GitHub" в /preview/.
# Формат: "owner/repo" (например, "triyul22/liquidator"). Ветка - GITHUB_BRANCH (по умолчанию main).
GITHUB_REPO = os.getenv("GITHUB_REPO", "triyul22/liquidator")
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
_NO_REWRITE_PREFIXES = ("/api", "/preview", "/assets", "/favicon")


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
        article_html = sub / "article.html"
        meta_path = sub / "meta.json"
        if not article_html.exists():
            continue
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = {}
        items.append({
            "slug": sub.name,
            "category": meta.get("category", "?"),
            "title": meta.get("title") or meta.get("h1") or sub.name,
            "h1": meta.get("h1", ""),
            "description": meta.get("description", ""),
            "factcheck_passed": meta.get("factcheck_passed"),
            "text_chars": meta.get("text_chars"),
            "text_words": meta.get("text_words"),
            "updated_at": time.strftime("%Y-%m-%d %H:%M", time.localtime(article_html.stat().st_mtime)),
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
  <a href="{repo_url}" target="_blank" rel="noopener" style="background:#fff;border:1px solid #d0d7de;color:#24292f;padding:6px 12px;border-radius:6px;text-decoration:none">📁 Репо на GitHub</a>
  <a href="{prompts_url}" target="_blank" rel="noopener" style="background:#fff;border:1px solid #d0d7de;color:#24292f;padding:6px 12px;border-radius:6px;text-decoration:none">🤖 Промпты агентов (.claude/agents)</a>
  <a href="{readme_url}" target="_blank" rel="noopener" style="background:#fff;border:1px solid #d0d7de;color:#24292f;padding:6px 12px;border-radius:6px;text-decoration:none">📖 Инструкция (README)</a>
  <a href="{issues_url}" target="_blank" rel="noopener" style="background:#fff;border:1px solid #d0d7de;color:#24292f;padding:6px 12px;border-radius:6px;text-decoration:none">💬 Комментарии и задачи</a>
</div>
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
            rows.append(
                f'<tr>'
                f'<td><span class="cat">{e(str(it["category"]))}</span></td>'
                f'<td><a class="title" href="/preview/{e(it["slug"])}">{e(it["title"])}</a>'
                f'<div class="meta slug">{e(it["slug"])}</div></td>'
                f'<td>{size_html}</td>'
                f'<td>{fc_html}</td>'
                f'<td>{e(it["updated_at"])}</td>'
                f'</tr>'
            )
        table = (
            '<table>'
            '<thead><tr><th>Категория</th><th>Заголовок / slug</th><th>Объём</th><th>Фактчек</th><th>Обновлено</th></tr></thead>'
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


@app.get("/preview/{slug}", include_in_schema=False)
def preview_slug(slug: str, _user: str = Depends(_check_preview_auth)):
    slug = _safe_slug(slug)
    article = DRAFTS_DIR / slug / "article.html"
    if not article.exists():
        raise HTTPException(status_code=404, detail="Черновик не найден")
    raw = article.read_text(encoding="utf-8")
    # Принудительный noindex в самом HTML (на случай если кто-то скопирует исходник)
    if '<meta name="robots"' not in raw.lower():
        raw = raw.replace("<head>", '<head>\n  <meta name="robots" content="noindex,nofollow"/>', 1)
    # Плавающая панель: ссылки на GitHub для правок (статья + meta.json)
    if GITHUB_REPO:
        edit_article = f"https://github.com/{GITHUB_REPO}/edit/{GITHUB_BRANCH}/drafts/{slug}/article.html"
        edit_meta = f"https://github.com/{GITHUB_REPO}/edit/{GITHUB_BRANCH}/drafts/{slug}/meta.json"
        bar = (
            '<div style="position:fixed;top:12px;right:12px;z-index:99999;display:flex;gap:8px;'
            'font:14px system-ui,-apple-system,Segoe UI,Roboto,sans-serif">'
            f'<a href="{edit_article}" target="_blank" rel="noopener" '
            'style="background:#0969da;color:#fff;padding:8px 14px;border-radius:6px;'
            'text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.15)">✏️ Править текст на GitHub</a>'
            f'<a href="{edit_meta}" target="_blank" rel="noopener" '
            'style="background:#6e7781;color:#fff;padding:8px 14px;border-radius:6px;'
            'text-decoration:none;box-shadow:0 2px 8px rgba(0,0,0,.15)">⚙️ Правки meta</a>'
            '<a href="/preview/" '
            'style="background:#fff;color:#24292f;padding:8px 14px;border-radius:6px;'
            'text-decoration:none;border:1px solid #d0d7de">← К списку</a>'
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


# ============ STATIC ============
# Монтируем ПОСЛЕ api-роутов, чтобы /api/* не перехватывался статикой.
# html=True: /path/ → /path/index.html, / → /index.html
app.mount("/", StaticFiles(directory=str(ROOT), html=True), name="static")
