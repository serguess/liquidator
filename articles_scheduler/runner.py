"""
Один тик scheduler'а: запустить /write-article {category} через Claude Code,
залогировать результат, запушить в git.

Запуск вручную для отладки:
    python -m articles_scheduler.runner

Возвращает dict с результатом, который APScheduler/тесты могут проверить.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Загружаем .env из корня проекта ДО чтения os.getenv ниже.
# Без этого при прямом запуске `python -m articles_scheduler.runner` (на VPS
# через systemd) module-level константы ARTICLES_PER_DAY, ARTICLE_TIMEOUT_SEC,
# ROTATION, GITHUB_REPO и т.д. ушли бы в дефолты. На Cloud Apps это работало
# благодаря FastAPI-lifespan, который инициализировал env заранее.
# python-dotenv по умолчанию override=False — повторный вызов из других
# модулей (bot/config.py делает load_dotenv тоже) безопасен.
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

DATA_DIR = ROOT / "data"
DRAFTS_DIR = ROOT / "drafts"
TOPIC_MAP_DIR = DRAFTS_DIR / "_topic-map"
SCHEDULER_LOG_PATH = DATA_DIR / "scheduler_log.json"
GIT_ERRORS_LOG_PATH = DATA_DIR / "git_errors.log"
PUBLISHED_INDEX_PATH = DATA_DIR / "published_index.json"
LOCK_FILE = DATA_DIR / ".scheduler.lock"
PAUSE_FLAG = DATA_DIR / ".scheduler_paused"
FAILURE_STREAK_PATH = DATA_DIR / ".scheduler_failure_streak"
HEARTBEAT_PATH = DATA_DIR / ".scheduler_heartbeat"

ROTATION = [c.strip() for c in os.getenv("ROTATION_ORDER", "fiz,yur,vzysk,news").split(",") if c.strip()]
ARTICLES_PER_DAY = int(os.getenv("ARTICLES_PER_DAY", "1"))
ARTICLE_TIMEOUT_SEC = int(os.getenv("ARTICLE_TIMEOUT_SEC", "3600"))  # 60 минут. Раньше было 2400 (40 мин), но на сложных темах с плотной терминологией (банкротство ООО, ипотека) writer крутил 5+ внутренних итераций самокоррекции и не дотягивал до агентов 6-7. Увеличено в мае 2026.
LOCK_STALE_SEC = int(os.getenv("LOCK_STALE_SEC", "3600"))  # 1 час
FAILURE_STREAK_LIMIT = int(os.getenv("FAILURE_STREAK_LIMIT", "3"))
HEARTBEAT_TIMEOUT_SEC = int(os.getenv("HEARTBEAT_TIMEOUT_SEC", "1800"))  # 30 минут тишины = kill. Если subprocess реально завис — heartbeat-таймаут добьёт раньше общего ARTICLE_TIMEOUT_SEC.
GITHUB_REPO = os.getenv("GITHUB_REPO", "serguess/liquidator")
GITHUB_BRANCH = os.getenv("GITHUB_BRANCH", "main")
GIT_AUTHOR_NAME = os.getenv("GIT_AUTHOR_NAME", "Liquidator Scheduler")
GIT_AUTHOR_EMAIL = os.getenv("GIT_AUTHOR_EMAIL", "scheduler@pravo.shop")

log = logging.getLogger("scheduler.runner")


# ============ ЛОГ ============

def _read_log() -> list[dict]:
    if not SCHEDULER_LOG_PATH.exists():
        return []
    try:
        return json.loads(SCHEDULER_LOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _append_log(entry: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    log_data = _read_log()
    log_data.append(entry)
    SCHEDULER_LOG_PATH.write_text(
        json.dumps(log_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _today_ok_count() -> int:
    today = date.today().isoformat()
    return sum(
        1 for e in _read_log()
        if e.get("status") == "ok" and (e.get("timestamp") or "").startswith(today)
    )


VALID_CATEGORIES = {"fiz", "yur", "vzysk", "news"}


def _next_category() -> str:
    """Простая ротация: берём индекс по числу попыток сегодня (включая упавшие).

    Override через ENV FORCE_CATEGORY (например, FORCE_CATEGORY=news для разовой
    публикации новости). Применяется и в SDK-вызове, и в CLI-режиме.
    """
    forced = (os.getenv("FORCE_CATEGORY") or "").strip().lower()
    if forced in VALID_CATEGORIES:
        return forced
    today = date.today().isoformat()
    today_entries = [
        e for e in _read_log()
        if (e.get("timestamp") or "").startswith(today) and e.get("status") not in ("paused", "locked")
    ]
    idx = len(today_entries) % len(ROTATION) if ROTATION else 0
    return ROTATION[idx] if ROTATION else "fiz"


# ============ TOPIC SELECTION ============

def _collect_used_slugs() -> set[str]:
    """
    Slug-и тем, которые уже взяты в работу.
    Источники: drafts/{slug}/ (черновик) и data/published_index.json (опубликованные).
    """
    used: set[str] = set()
    if DRAFTS_DIR.exists():
        for d in DRAFTS_DIR.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                used.add(d.name)
    if PUBLISHED_INDEX_PATH.exists():
        try:
            pi = json.loads(PUBLISHED_INDEX_PATH.read_text(encoding="utf-8"))
            for entry in pi.get("articles", []) or []:
                slug = entry.get("slug")
                if slug:
                    used.add(slug)
        except (json.JSONDecodeError, OSError):
            pass
    return used


# ============ AUTO-SKIP по каннибализации (с 10 мая 2026) ============
#
# Если агент 1 отказался писать статью потому что тема дублирует уже
# опубликованную (или news-тема устарела/evergreen) — автоматически
# помечаем её status="rejected" в topic-map и берём следующую в том же
# слоте. Без этого scheduler стабильно тратит 7 минут на reject темы и
# отдаёт failed_qa, заказчик не получает статью когда запускает пайплайн.

TOPIC_REJECT_PATTERNS = (
    "cannibalization:", "evergreen", "not-news-fresh",
    "outside-30-day-window", "not_news", "topic_outdated",
)
MAX_TOPIC_RETRIES_PER_SLOT = int(os.getenv("MAX_TOPIC_RETRIES_PER_SLOT", "3"))


def _detect_topic_rejection(slug: str) -> str | None:
    """Читает _pipeline.log.json. Если агент 1 упал с ошибкой из
    TOPIC_REJECT_PATTERNS - возвращает текст. Иначе None."""
    if not slug:
        return None
    log_path = DRAFTS_DIR / slug / "_pipeline.log.json"
    if not log_path.exists():
        return None
    try:
        d = json.loads(log_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None
    for ev in d.get("events") or []:
        if ev.get("agent") == "1-semantics" and ev.get("event") == "failed":
            err = ev.get("error") or ""
            if any(p in err for p in TOPIC_REJECT_PATTERNS):
                return err
    return None


def _mark_topic_rejected(category: str, slug: str, reason: str) -> bool:
    """Помечает тему status='rejected' в topic-map. _pick_topic skip-ит rejected."""
    if not category or not slug:
        return False
    map_path = TOPIC_MAP_DIR / f"{category}.json"
    if not map_path.exists():
        return False
    try:
        d = json.loads(map_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    for t in d.get("topics") or []:
        if t.get("slug") == slug:
            t["status"] = "rejected"
            t["rejected_at"] = datetime.now().isoformat(timespec="seconds")
            t["rejected_reason"] = reason
            t["rejected_by"] = "auto_skip_in_slot"
            try:
                map_path.write_text(
                    json.dumps(d, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                log.warning("Auto-skip: %s/%s rejected (reason=%s)",
                            category, slug, reason)
                return True
            except OSError:
                log.exception("_mark_topic_rejected: запись не удалась")
                return False
    return False


def _pick_topic(category: str) -> dict | None:
    """
    Возвращает первую неиспользованную тему из drafts/_topic-map/{category}.json.

    Тема считается «использованной» если её slug есть в drafts/{slug}/ или
    в data/published_index.json. Темы с явным status='rejected' пропускаются:
    остальные статусы (proposed/approved/rewrite/без статуса) не блокируют.

    Возвращает None если в файле topic-map свободных тем не осталось — тогда
    scheduler должен запустить /expand-topics для пополнения.
    """
    map_path = TOPIC_MAP_DIR / f"{category}.json"
    if not map_path.exists():
        return None
    try:
        data = json.loads(map_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    topics = data.get("topics") or []
    if not topics:
        return None

    used_slugs = _collect_used_slugs()
    for t in topics:
        if t.get("status") == "rejected":
            continue
        slug = t.get("slug")
        if not slug or slug in used_slugs:
            continue
        return t
    return None


# ============ CIRCUIT BREAKER ============

def _get_failure_streak() -> int:
    """Текущее число подряд идущих сбоев."""
    if not FAILURE_STREAK_PATH.exists():
        return 0
    try:
        return int((FAILURE_STREAK_PATH.read_text(encoding="utf-8") or "0").strip() or "0")
    except (ValueError, OSError):
        return 0


def _set_failure_streak(n: int) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    try:
        FAILURE_STREAK_PATH.write_text(str(max(0, n)), encoding="utf-8")
    except OSError:
        pass


def _is_quota_failure(stdout_tail: str | None, stderr_tail: str | None) -> bool:
    """
    Сбой из-за лимита Claude Pro / API rate limit. Такие падения НЕ считаются
    streak-сбоями: лимит сам сбросится через несколько часов, нет смысла
    автоматически ставить scheduler на паузу.
    """
    blob = ((stdout_tail or "") + " " + (stderr_tail or "")).lower()
    return any(m in blob for m in (
        "hit your limit",
        "rate limit",
        "quota exceeded",
        "you have exceeded",
    ))


def _update_failure_streak(entry: dict, timestamp: str) -> None:
    """
    После каждого слота решает: сбрасывать счётчик, инкрементить, или сработал
    circuit breaker и пора ставить scheduler на паузу.

    Логика:
    - status=ok / topics_expanded → сброс в 0.
    - status=paused / locked / limit_reached → не считаем (нет реальной попытки).
    - сбой из-за квоты Claude → не считаем (ждём сброса лимита).
    - иначе (failed / failed_qa / timeout / exception) → +1.
      При >= FAILURE_STREAK_LIMIT создаётся PAUSE_FLAG и scheduler замолкает
      до ручного снятия (rm data/.scheduler_paused).
    """
    status = entry.get("status")
    if status in ("ok", "topics_expanded"):
        _set_failure_streak(0)
        return
    if status in ("paused", "locked", "limit_reached"):
        return
    if _is_quota_failure(entry.get("stdout_tail"), entry.get("stderr_tail")):
        return

    streak = _get_failure_streak() + 1
    _set_failure_streak(streak)
    entry["failure_streak"] = streak
    if streak >= FAILURE_STREAK_LIMIT:
        try:
            PAUSE_FLAG.write_text(
                f"auto-paused at {timestamp}\n"
                f"reason: {streak} consecutive failed slots\n"
                f"last_status: {status}\n"
                f"снимите паузу: rm {PAUSE_FLAG.relative_to(ROOT)}\n",
                encoding="utf-8",
            )
            log.error(
                "Circuit breaker: %d слотов подряд упали, scheduler приостановлен. "
                "Удалите %s после фикса.",
                streak, PAUSE_FLAG.relative_to(ROOT),
            )
            entry["circuit_breaker_triggered"] = True
        except OSError as exc:
            log.warning("Не удалось создать PAUSE_FLAG: %s", exc)


# ============ DRAFTS ============

def _detect_new_slug(started_ts: float, expected_slug: str | None = None) -> str | None:
    """
    Находит slug, чей каталог создан в течение текущего пайплайна.

    Если передан `expected_slug` (slug из topic-map, который мы ожидаем) —
    проверяем, что папка `drafts/{expected_slug}/` существует и имеет
    свежий mtime. Это защита от случая, когда Claude игнорирует slug из
    brief'а и пишет в существующую похожую папку (бесконечная петля по
    одной теме).

    Если `expected_slug` не задан — поведение как раньше: самый свежий
    подкаталог по mtime (для rewrite-режима, где slug известен иначе).
    """
    if not DRAFTS_DIR.exists():
        return None

    # Жёсткий путь: ожидаем конкретный slug
    if expected_slug:
        expected_dir = DRAFTS_DIR / expected_slug
        if expected_dir.is_dir() and expected_dir.stat().st_mtime > started_ts - 30:
            return expected_slug
        # Папка не создалась или не обновилась — это ошибка пайплайна.
        # Пробуем найти что Claude создал вместо этого, для лога.
        candidates = sorted(
            [d for d in DRAFTS_DIR.iterdir()
             if d.is_dir() and not d.name.startswith("_")
             and d.stat().st_mtime > started_ts - 30],
            key=lambda d: d.stat().st_mtime, reverse=True,
        )
        if candidates:
            log.error(
                "Slug mismatch: ожидали drafts/%s/, но Claude создал/обновил %s. "
                "Это нарушение инструкции — slug должен браться из brief'а.",
                expected_slug, [c.name for c in candidates[:3]],
            )
        else:
            log.error("Slug mismatch: drafts/%s/ не создан и других свежих папок нет",
                      expected_slug)
        return None

    # Старое поведение для rewrite-режима
    candidates = [
        d for d in DRAFTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_") and d.stat().st_mtime > started_ts - 30
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda d: d.stat().st_mtime, reverse=True)
    return candidates[0].name


def _read_meta(slug: str) -> dict:
    meta_path = DRAFTS_DIR / slug / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _run_quality_gate(slug: str) -> dict:
    """
    Hard-блок: запускаем tools.quality_gate на article.html. Если он возвращает
    non-zero — статью нельзя коммитить. Раньше эти проверки висели на модели
    (агент 6 «должен запустить»), что приводило к пропускам шагов на длинных
    статьях. Теперь шаг детерминистический и неотменяемый.
    """
    article_path = DRAFTS_DIR / slug / "article.html"
    if not article_path.exists():
        return {"ran": False, "reason": "no_article_html", "passed": False}

    cmd = [
        sys.executable, "-m", "tools.quality_gate",
        str(article_path), "--json", "--save-report",
    ]
    try:
        res = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=120,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return {"ran": True, "reason": "timeout", "passed": False}
    except Exception as exc:
        return {"ran": True, "reason": f"exception:{exc}", "passed": False}

    parsed: dict = {}
    if res.stdout:
        try:
            parsed = json.loads(res.stdout)
        except json.JSONDecodeError:
            parsed = {"raw_stdout_tail": (res.stdout or "")[-500:]}

    return {
        "ran": True,
        "exit_code": res.returncode,
        "passed": res.returncode == 0,
        "blockers": parsed.get("blockers") or [],
        "warnings": parsed.get("warnings") or [],
        "recommendations": parsed.get("recommendations") or [],
        "stderr_tail": (res.stderr or "")[-300:],
    }


def _metrics_summary(meta: dict) -> str:
    """Короткая строка для commit message: 'AI 4%, заспам 47%, уник 91%'."""
    parts = []
    ai = meta.get("textru_ai_detector")
    if ai is not None:
        parts.append(f"AI {round(ai * 100)}%")
    spam = meta.get("textru_spam")
    if spam is not None:
        parts.append(f"заспам {round(spam * 100)}%")
    uniq = meta.get("textru_uniqueness")
    if uniq is not None:
        parts.append(f"уник {round(uniq * 100)}%")
    return ", ".join(parts)


# ============ GIT ============

def _git_env() -> dict:
    return {
        **os.environ,
        "GIT_AUTHOR_NAME": GIT_AUTHOR_NAME,
        "GIT_AUTHOR_EMAIL": GIT_AUTHOR_EMAIL,
        "GIT_COMMITTER_NAME": GIT_AUTHOR_NAME,
        "GIT_COMMITTER_EMAIL": GIT_AUTHOR_EMAIL,
    }


def _git_remote_url() -> Optional[str]:
    """
    Собирает URL для push/pull через HTTPS+PAT.
    Формат: https://oauth2:TOKEN@github.com/OWNER/REPO.git

    `oauth2` как username работает и с Classic PAT, и с Fine-grained PAT.
    Раньше был `x-access-token` - он только для GitHub App installation tokens,
    Classic PAT с ним падает с Permission denied.
    """
    token = os.getenv("GIT_PUSH_TOKEN", "").strip()
    if not token:
        return None
    return f"https://oauth2:{token}@github.com/{GITHUB_REPO}.git"


def _mask_token(text: str) -> str:
    """Прячем токен из stderr перед логированием."""
    token = os.getenv("GIT_PUSH_TOKEN", "").strip()
    if token and token in text:
        return text.replace(token, "***")
    return text


def _append_git_error(slot_ts: str, slug: str | None, category: str, action: str,
                       returncode: int, stderr: str, stdout: str = "") -> None:
    """
    Дописывает полный stderr ошибки git в data/git_errors.log.
    Маскирует токен. Используется для отладки проблем с git push/pull,
    когда краткого summary в scheduler_log.json не хватает.
    """
    DATA_DIR.mkdir(exist_ok=True)
    masked_stderr = _mask_token(stderr or "")
    masked_stdout = _mask_token(stdout or "")
    block = (
        f"[{slot_ts}] slot={slug or '-'} category={category} action={action} "
        f"returncode={returncode}\n"
        f"--- stderr ---\n{masked_stderr.rstrip()}\n"
    )
    if masked_stdout.strip():
        block += f"--- stdout ---\n{masked_stdout.rstrip()}\n"
    block += "====\n\n"
    try:
        with GIT_ERRORS_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(block)
    except OSError:
        # Лог-файл недоступен — не прерываем основную работу
        pass


def _safe_pipeline_log(slug: str | None, agent: str, event: str, **fields) -> None:
    """
    Обёртка над pipeline_log: не падает если что-то пошло не так
    (например, при инициализации до того как drafts/{slug}/ создалась).
    """
    if not slug:
        return
    try:
        from tools.pipeline_log import log_event
        log_event(slug, agent, event, **fields)
    except Exception as exc:
        log.debug("pipeline_log failed: %s", exc)


def _find_failed_qa_for_retry(max_iterations: int = 5) -> str | None:
    """
    Ищет статью failed_qa, которую можно дорабатывать (а не брать новую тему).

    Условия:
    1. В drafts/{slug}/quality_gate.json есть `passed: false`.
    2. Текущее число итераций ещё не достигло max_iterations (по умолчанию 5).
       Источник счётчика — `quality_gate.json:retry_count` (надёжно: gate сам
       инкрементирует при каждом прогоне). Fallback — `_pipeline.log.json:current_iteration`
       для старых драфтов, где новый счётчик ещё не записан.
    3. В drafts/_review/ её нет (значит ручной разбор не запрошен).

    После max_iterations провалов статья принудительно перемещается в drafts/_review/
    для ручного разбора (это делает _archive_dead_drafts ниже, не здесь).

    Возвращает slug самой старой такой статьи (приоритет очистке хвоста).
    Если кандидатов нет — None.
    """
    candidates: list[tuple[str, float]] = []  # (slug, mtime quality_gate.json)
    for slug_dir in DRAFTS_DIR.iterdir():
        if not slug_dir.is_dir() or slug_dir.name.startswith("_"):
            continue
        qg_path = slug_dir / "quality_gate.json"
        if not qg_path.exists():
            continue
        try:
            qg = json.loads(qg_path.read_text(encoding="utf-8"))
            if qg.get("passed", True):
                continue  # уже прошёл, не нужен retry
        except (json.JSONDecodeError, OSError):
            continue

        # Проверяем лимит итераций — сначала из quality_gate.json (надёжно),
        # затем fallback на _pipeline.log.json для совместимости со старыми драфтами.
        iterations = int(qg.get("retry_count") or 0)
        if iterations <= 0:
            pipe_path = slug_dir / "_pipeline.log.json"
            if pipe_path.exists():
                try:
                    pipe = json.loads(pipe_path.read_text(encoding="utf-8"))
                    iterations = pipe.get("current_iteration", 1)
                except (json.JSONDecodeError, OSError):
                    iterations = 1
            else:
                iterations = 1
        if iterations >= max_iterations:
            continue

        candidates.append((slug_dir.name, qg_path.stat().st_mtime))

    if not candidates:
        return None
    # Самая старая по mtime quality_gate (хвостовой принцип: не накапливать долги)
    candidates.sort(key=lambda x: x[1])
    return candidates[0][0]


def _refresh_published_index() -> None:
    """
    Обновляет data/published_index.json перед запуском конвейера.
    Архитектор (агент 3) обязан читать этот индекс для перелинковки —
    иначе он будет галлюцинировать slug-и или вставлять 404 ссылки.

    Идемпотентно. При ошибке — логируем, но не падаем (это не критично
    для самого слота).
    """
    try:
        from tools.build_published_index import build_index
        index = build_index(include_drafts=True)
        path = DATA_DIR / "published_index.json"
        path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        log.info("published_index обновлён: %d статей", index["total"])
    except Exception as exc:
        log.warning("Не удалось обновить published_index: %s", exc)


def _refresh_prev_summary(category: str, exclude_slug: str | None = None) -> None:
    """
    Готовит data/_prev_summary_{category}.json — лёгкий summary последней
    статьи в категории (intro, h2_list, формула финала, main_keyword).

    Раньше: писатель (агент 4) и архитектор (агент 3) читали ПОЛНЫЙ текст
    последней статьи в категории, чтобы отстроиться по структуре. Это
    5-15k токенов на каждый прогон. Теперь читают этот лёгкий JSON
    (~30 строк) — экономия ~3-6k токенов на писателя.

    При ошибке — логируем, но не падаем. Агент 4 без prev_summary продолжит
    работать как раньше (просто не сможет сослаться на формулу соседа).
    """
    try:
        from tools.build_prev_summary import write_summary
        result = write_summary(category, exclude_slug=exclude_slug)
        log.info(
            "prev_summary готов: cat=%s prev_slug=%s",
            category, result.get("prev_slug") or "—",
        )
    except Exception as exc:
        log.warning("Не удалось собрать prev_summary для %s: %s", category, exc)


def _ensure_on_branch() -> dict:
    """
    Гарантирует что HEAD указывает на ветку GITHUB_BRANCH, а не висит
    в detached state. Cloud Apps при деплое может делать `git checkout {sha}`
    вместо `git checkout main` — тогда HEAD detached, последующие коммиты
    создаются "в воздухе" и push origin main отвечает Everything up-to-date,
    хотя локальный коммит никуда не уходит.

    Если HEAD detached:
    1. Запоминаем текущий HEAD SHA (там могут быть локальные коммиты).
    2. `git checkout GITHUB_BRANCH` (создаст ветку если её нет, или переключится).
    3. Если SHA отличается от main — `git merge {sha}` подтягиваем висящие коммиты.

    Возвращает {"ok": bool, "was_detached": bool, "details": ...}
    """
    cwd = str(ROOT)
    env = _git_env()
    res = subprocess.run(
        ["git", "symbolic-ref", "--short", "-q", "HEAD"],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    if res.returncode == 0:
        current_branch = (res.stdout or "").strip()
        if current_branch == GITHUB_BRANCH:
            return {"ok": True, "was_detached": False, "branch": current_branch}
        log.warning(
            "HEAD на ветке %s, ожидалась %s — переключаюсь",
            current_branch, GITHUB_BRANCH,
        )
        sw = subprocess.run(
            ["git", "checkout", GITHUB_BRANCH],
            cwd=cwd, env=env, capture_output=True, text=True,
        )
        return {
            "ok": sw.returncode == 0,
            "was_detached": False,
            "switched_from": current_branch,
            "stderr_tail": (sw.stderr or "")[-200:],
        }

    # symbolic-ref упал — HEAD detached
    sha_res = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    detached_sha = (sha_res.stdout or "").strip()
    log.error(
        "HEAD detached (sha=%s) — переключаюсь на %s и сливаю висящие коммиты",
        detached_sha[:8], GITHUB_BRANCH,
    )

    sw = subprocess.run(
        ["git", "checkout", GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    if sw.returncode != 0:
        _append_git_error(
            slot_ts=datetime.now().isoformat(timespec="seconds"),
            slug=None, category="-", action="git_checkout_branch_after_detached",
            returncode=sw.returncode,
            stderr=sw.stderr or "", stdout=sw.stdout or "",
        )
        return {
            "ok": False, "was_detached": True, "detached_sha": detached_sha,
            "stderr_tail": (sw.stderr or "")[-200:],
        }

    # Если detached SHA не совпадает с тем, на что теперь указывает branch —
    # это значит на detached HEAD были локальные коммиты, надо их merge'ить.
    branch_sha_res = subprocess.run(
        ["git", "rev-parse", GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    branch_sha = (branch_sha_res.stdout or "").strip()
    merged = False
    if detached_sha and branch_sha and detached_sha != branch_sha:
        # Проверяем — detached_sha это потомок branch_sha (наши коммиты впереди)
        # или предок (мы были на старом коммите, значит ничего сливать не надо).
        ancestor_check = subprocess.run(
            ["git", "merge-base", "--is-ancestor", branch_sha, detached_sha],
            cwd=cwd, env=env, capture_output=True, text=True,
        )
        if ancestor_check.returncode == 0:
            log.warning(
                "На detached HEAD были коммиты впереди %s — merge'у их",
                GITHUB_BRANCH,
            )
            merge_res = subprocess.run(
                ["git", "merge", "--ff-only", detached_sha],
                cwd=cwd, env=env, capture_output=True, text=True,
            )
            merged = merge_res.returncode == 0
            if not merged:
                _append_git_error(
                    slot_ts=datetime.now().isoformat(timespec="seconds"),
                    slug=None, category="-", action="git_merge_detached_commits",
                    returncode=merge_res.returncode,
                    stderr=merge_res.stderr or "", stdout=merge_res.stdout or "",
                )

    return {
        "ok": True, "was_detached": True, "detached_sha": detached_sha,
        "merged_orphan_commits": merged,
    }


def _git_pull_before_slot() -> dict:
    """
    Подтягивает свежие изменения с GitHub перед началом слота.

    Pull идёт через явный URL с PAT (не через `git pull origin`), чтобы
    не зависеть от настроек origin в репо.

    Стратегия:
    1. Пробуем `git pull --rebase --autostash` — это auto-stash локальных
       runtime-файлов (scheduler_log.json, git_errors.log, _pipeline.log.json
       и т.п.), rebase, потом auto-restore. Закрывает баг «cannot pull with
       rebase: You have unstaged changes», который ловит scheduler на VPS
       (на Cloud Apps его не было — там working tree обнулялся каждым
        redeploy, всегда был чист).
    2. Если autostash-pull не помог (например конфликт между нашим
       коммитом и origin) — пробуем `--rebase -X theirs` для разрешения
       в пользу удалённой версии журналов.
    3. Если и это упало — возвращаем ok=False, runner продолжит работу
       (статья всё равно напишется, push попробуем в финале).
    """
    cwd = str(ROOT)
    env = _git_env()
    remote_url = _git_remote_url()
    if not remote_url:
        return {"ok": False, "stdout_tail": "", "stderr_tail": "GIT_PUSH_TOKEN не задан"}

    # Попытка 1: --rebase --autostash
    res = subprocess.run(
        ["git", "pull", "--rebase", "--autostash", remote_url, GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=90,
    )
    if res.returncode == 0:
        return {
            "ok": True,
            "stdout_tail": _mask_token((res.stdout or "")[-200:]),
            "stderr_tail": _mask_token((res.stderr or "")[-200:]),
            "strategy": "rebase+autostash",
        }

    # Попытка 2: --rebase -X theirs (на случай конфликта в журналах)
    log.warning("Pull rebase+autostash упал, пробую -X theirs: %s",
                _mask_token((res.stderr or "")[-200:]))
    # Если предыдущий rebase оставил незавершённое состояние — abort.
    subprocess.run(["git", "rebase", "--abort"], cwd=cwd, env=env, capture_output=True)
    res2 = subprocess.run(
        ["git", "pull", "--rebase", "-X", "theirs", "--autostash",
         remote_url, GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=90,
    )
    if res2.returncode == 0:
        return {
            "ok": True,
            "stdout_tail": _mask_token((res2.stdout or "")[-200:]),
            "stderr_tail": _mask_token((res2.stderr or "")[-200:]),
            "strategy": "rebase+theirs+autostash",
        }

    # Обе попытки упали — abort и продолжаем без pull.
    subprocess.run(["git", "rebase", "--abort"], cwd=cwd, env=env, capture_output=True)
    _append_git_error(
        slot_ts=datetime.now().isoformat(timespec="seconds"),
        slug=None, category="-", action="git_pull",
        returncode=res2.returncode,
        stderr=res2.stderr or "", stdout=res2.stdout or "",
    )
    return {
        "ok": False,
        "stdout_tail": _mask_token((res2.stdout or "")[-200:]),
        "stderr_tail": _mask_token((res2.stderr or "")[-200:]),
        "strategy": "all_failed",
    }


def _find_orphan_drafts() -> list[str]:
    """
    Ищет ИСТИННЫЕ orphans: drafts/{slug}/article.html которые сами не
    закоммичены в git (untracked / added / modified). Это сценарий
    «прошлый слот написал статью, но коммит не прошёл» (commit_failed /
    OOM kill / контейнер прибили посреди commit).

    КРИТИЧНО: orphan НЕ значит «в папке что-то изменилось». Orphan значит
    «сам article.html не в актуальной версии в git». Если article.html
    уже tracked и не modified, а в папке появился новый файл (например,
    бот создал .notified sentinel, или watcher что-то записал) — это
    НЕ orphan. Иначе rescue провоцирует push → redeploy Cloud Apps →
    кладёт текущий слот scheduler'а.

    Возвращает только реально нуждающиеся в спасении папки.
    """
    if not DRAFTS_DIR.exists():
        return []
    cwd = str(ROOT)
    env = _git_env()
    res = subprocess.run(
        ["git", "status", "--porcelain", "--", "drafts/"],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    if res.returncode != 0:
        return []

    # git status --porcelain формат: XY <space> path
    #   X = staged status (' ', 'M', 'A', 'D', 'R', 'C', '?')
    #   Y = unstaged status (то же)
    #   '??' = untracked
    # Нам интересно только когда сам article.html (или вся папка как untracked)
    # имеет статус не «всё в порядке».
    orphan_slugs: set[str] = set()
    for line in (res.stdout or "").splitlines():
        if not line or len(line) < 4:
            continue
        status_x = line[0]
        status_y = line[1]
        path = line[3:].strip().strip('"')
        if not path.startswith("drafts/"):
            continue
        rest = path[len("drafts/"):]
        # rest может быть либо "{slug}/" (untracked папка целиком), либо
        # "{slug}/some/file.ext"
        parts = rest.split("/", 1)
        slug = parts[0]
        if not slug or slug.startswith("_"):
            continue

        # Случай 1: ВСЯ папка drafts/{slug}/ untracked. Git выводит как
        # "?? drafts/{slug}/" (rest = "{slug}/", без дальнейшего пути).
        # ВАЖНО: rest должен быть строго "{slug}/", а не "{slug}/versions/"
        # (это уже подпапка существующего drafts/{slug}/).
        if status_x == "?" and status_y == "?" and rest == f"{slug}/":
            article_path = DRAFTS_DIR / slug / "article.html"
            if article_path.exists():
                orphan_slugs.add(slug)
            continue

        # Случай 2: конкретный файл в drafts/{slug}/. Orphan ТОЛЬКО если
        # это сам article.html и он untracked / added / modified. Любые
        # другие файлы (.notified от бота, _pipeline.log.json от логгера,
        # versions/v2.X.html от editor'а, любые подпапки) НЕ должны
        # триггерить rescue: это нормальная работа бота/логгера с уже
        # опубликованным draft'ом, и rescue→push→редеплой убил бы текущий
        # слот scheduler'а.
        sub_path = parts[1] if len(parts) > 1 else ""
        if sub_path != "article.html":
            continue
        # article.html в неоптимальном статусе → orphan
        if status_x in ("?", "A", "M") or status_y in ("?", "M"):
            orphan_slugs.add(slug)

    return sorted(orphan_slugs)


def _rescue_orphan_drafts(orphans: list[str]) -> dict:
    """
    Коммитит и пушит orphan-драфты которые остались с прошлых слотов.
    Один общий коммит — чтобы не плодить N редеплоев Cloud Apps.
    """
    if not orphans:
        return {"rescued": 0}

    cwd = str(ROOT)
    env = _git_env()
    remote_url = _git_remote_url()
    if not remote_url:
        return {"rescued": 0, "reason": "no_token", "found": orphans}

    log.warning(
        "Orphan rescue: найдено %d драфтов без коммита (%s) — спасаю",
        len(orphans), ", ".join(orphans),
    )
    paths = [f"drafts/{s}/" for s in orphans] + [
        "data/keywords.json", "data/clusters.json",
        "data/scheduler_log.json", "data/git_errors.log",
        "data/published_index.json", "drafts/_topic-map/",
    ]
    add_res = _git_run_with_retry(
        ["git", "add", "--", *paths],
        env=env, cwd=cwd,
    )
    if add_res.returncode != 0:
        _append_git_error(
            slot_ts=datetime.now().isoformat(timespec="seconds"),
            slug=None, category="-", action="orphan_rescue_add",
            returncode=add_res.returncode,
            stderr=add_res.stderr or "", stdout=add_res.stdout or "",
        )

    commit_msg = (
        f"rescue: {len(orphans)} orphan draft(s) from prior slot — "
        f"{', '.join(orphans[:3])}"
        + (f" +{len(orphans)-3}" if len(orphans) > 3 else "")
    )
    commit_res = _git_run_with_retry(
        ["git", "commit", "-m", commit_msg],
        env=env, cwd=cwd,
    )
    if commit_res.returncode != 0:
        combined = (commit_res.stdout or "") + (commit_res.stderr or "")
        if "nothing to commit" in combined:
            return {"rescued": 0, "found": orphans, "reason": "nothing_to_commit"}
        _append_git_error(
            slot_ts=datetime.now().isoformat(timespec="seconds"),
            slug=None, category="-", action="orphan_rescue_commit",
            returncode=commit_res.returncode,
            stderr=commit_res.stderr or "", stdout=commit_res.stdout or "",
        )
        return {"rescued": 0, "found": orphans, "reason": "commit_failed",
                "stderr": combined[-200:]}

    push_res = _git_run_with_retry(
        ["git", "push", remote_url, GITHUB_BRANCH],
        env=env, cwd=cwd, timeout=60,
    )
    # При non-fast-forward — pull --rebase + retry (как в основной commit_and_push)
    if push_res.returncode != 0:
        stderr_lower = (push_res.stderr or "").lower()
        non_ff_markers = ("non-fast-forward", "fetch first", "updates were rejected",
                           "tip of your current branch is behind")
        if any(m in stderr_lower for m in non_ff_markers):
            log.warning("Orphan rescue: non-fast-forward, делаю pull --rebase -X theirs")
            # -X theirs автоматически разрешает конфликты в журнальных JSON
            # (data/*.json) в пользу origin — это безопасно, журналы append-only.
            rebase_res = subprocess.run(
                ["git", "pull", "--rebase", "-X", "theirs", "--autostash",
                 remote_url, GITHUB_BRANCH],
                cwd=cwd, env=env, capture_output=True, text=True, timeout=90,
            )
            if rebase_res.returncode == 0:
                push_res = _git_run_with_retry(
                    ["git", "push", remote_url, GITHUB_BRANCH],
                    env=env, cwd=cwd, timeout=60,
                )
            else:
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=cwd, env=env, capture_output=True,
                )
                log.error(
                    "Orphan rescue: rebase -X theirs всё равно упал, abort"
                )

    if push_res.returncode != 0:
        _append_git_error(
            slot_ts=datetime.now().isoformat(timespec="seconds"),
            slug=None, category="-", action="orphan_rescue_push",
            returncode=push_res.returncode,
            stderr=push_res.stderr or "", stdout=push_res.stdout or "",
        )
        # Коммит создан, но push не прошёл. Следующий вызов
        # _push_pending_local_commits подхватит его как pending commit.
        return {"rescued": len(orphans), "found": orphans,
                "pushed": False,
                "stderr": _mask_token((push_res.stderr or "")[-200:])}

    log.info("Orphan rescue: запушено %d драфтов", len(orphans))
    return {"rescued": len(orphans), "found": orphans, "pushed": True}


def _push_pending_local_commits() -> dict:
    """
    Pre-flight в начале слота: если в локальном репо есть коммиты которые
    ещё не на origin/main (например, прошлый слот написал статью, но push
    упал из-за отвала сети) — пушим их сейчас.

    Это страховка от потери коммитов при следующем редеплое контейнера:
    если коммит не доехал до GitHub, при пересоздании контейнера он
    исчезнет вместе со всем drafts/{slug}/ который ещё не в репо.

    Также вызывает orphan rescue: если есть drafts/{slug}/article.html
    в working tree без коммита (commit_failed / OOM kill прошлого слота),
    спасаем их одним общим коммитом перед основной работой слота.
    """
    cwd = str(ROOT)
    env = _git_env()
    remote_url = _git_remote_url()
    if not remote_url:
        return {"checked": False, "reason": "no_token"}

    # Шаг 1: orphan rescue. Делаем ДО проверки pending — orphan rescue сам
    # создаёт коммиты, которые потом попадают в pending.
    orphans = _find_orphan_drafts()
    rescue_result = _rescue_orphan_drafts(orphans) if orphans else {"rescued": 0}

    # Шаг 2: fetch + проверяем что осталось не на origin
    subprocess.run(
        ["git", "fetch", remote_url, GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    res = subprocess.run(
        ["git", "rev-list", f"origin/{GITHUB_BRANCH}..HEAD"],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    pending = [c for c in (res.stdout or "").strip().splitlines() if c]
    if not pending:
        return {"checked": True, "pending": 0, "orphan_rescue": rescue_result}

    log.warning(
        "Найдено %d недопушенных коммитов с прошлых слотов, пушу сейчас",
        len(pending),
    )
    push_res = _git_run_with_retry(
        ["git", "push", remote_url, GITHUB_BRANCH],
        env=env, cwd=cwd, timeout=60,
    )
    if push_res.returncode == 0:
        log.info("Догнали %d коммитов на origin/%s", len(pending), GITHUB_BRANCH)
        return {"checked": True, "pending": len(pending), "pushed": True,
                "orphan_rescue": rescue_result}
    return {"checked": True, "pending": len(pending), "pushed": False,
            "stderr": _mask_token((push_res.stderr or "")[-200:]),
            "orphan_rescue": rescue_result}


def _git_run_with_retry(args: list[str], env: dict, cwd: str, timeout: int | None = None,
                          retries: int = 3) -> subprocess.CompletedProcess:
    """
    Запускает git-команду с retry при известных временных проблемах:
    - index.lock держится другим процессом (race condition)
    - проблемы сети при push (timed out, connection reset, early EOF)
    Не-ретраимые ошибки (auth, конфликты, плохой commit message) возвращаем
    с первой попытки.
    """
    retriable_markers = (
        "index.lock", "another git process",
        "could not resolve", "timed out", "connection reset",
        "remote end hung up", "early eof", "operation too slow",
    )
    last_result = None
    for attempt in range(retries):
        last_result = subprocess.run(
            args, cwd=cwd, env=env, capture_output=True, text=True, timeout=timeout,
        )
        if last_result.returncode == 0:
            return last_result
        stderr_lower = (last_result.stderr or "").lower()
        if not any(m in stderr_lower for m in retriable_markers):
            return last_result
        if attempt < retries - 1:
            sleep_sec = 2 * (attempt + 1)
            log.warning(
                "git %s упал (rc=%s, временная ошибка), ретрай через %ds",
                args[1] if len(args) > 1 else "?", last_result.returncode, sleep_sec,
            )
            time.sleep(sleep_sec)
    return last_result


def _git_commit_and_push(slug: str, category: str, metrics: str = "") -> dict:
    """
    Коммитит drafts/{slug} и data/scheduler_log.json, пушит в GitHub.

    Push идёт через HTTPS+PAT (GIT_PUSH_TOKEN). Без токена - только
    локальный commit, push возвращает reason="no_token".
    """
    cwd = str(ROOT)
    env = _git_env()

    paths_to_add = [
        f"drafts/{slug}/",
        "data/keywords.json",
        "data/clusters.json",
        "data/scheduler_log.json",
        "data/git_errors.log",
        "data/published_index.json",
        "drafts/_topic-map/",
    ]
    # Добавляем пути ПО ОДНОМУ с флагом -A (handles add/modify/delete).
    # Раньше делали единым batch'ем `git add -- path1 path2 ...`, но если хоть
    # один путь в списке не существует (например, опциональные data/keywords.json
    # или data/git_errors.log на свежем контейнере) - git выдаёт fatal и НИЧЕГО
    # не стейджит атомарно. drafts/{slug}/ оставался untracked, commit пустой,
    # commit_failed → слот не пушит draft в git → потеря статьи при редеплое.
    # Зафиксировано в scheduler_log.json от 8 мая 2026 13:56:18.
    add_errors_count = 0
    for path in paths_to_add:
        single_res = _git_run_with_retry(
            ["git", "add", "-A", "--", path],
            env=env, cwd=cwd,
        )
        if single_res.returncode != 0:
            add_errors_count += 1
            # Логируем только первую ошибку (чтобы не спамить git_errors.log),
            # детали остальных - в общий счётчик.
            if add_errors_count == 1:
                _append_git_error(
                    slot_ts=datetime.now().isoformat(timespec="seconds"),
                    slug=slug, category=category, action=f"git_add ({path})",
                    returncode=single_res.returncode,
                    stderr=single_res.stderr or "", stdout=single_res.stdout or "",
                )
    if add_errors_count:
        log.info(
            "git add: %d путей не добавились (вероятно опциональные файлы отсутствуют) - не фатально",
            add_errors_count,
        )

    msg_tail = f", {metrics}" if metrics else ""
    commit_msg = f"drafts: {slug} ({category}{msg_tail})"
    commit_res = _git_run_with_retry(
        ["git", "commit", "-m", commit_msg],
        env=env, cwd=cwd,
    )
    combined = (commit_res.stdout or "") + (commit_res.stderr or "")
    if "nothing to commit" in combined:
        # Может быть две причины: (a) git add действительно не нашёл новых
        # файлов (нормально если drafts/{slug}/ уже был в репо), (b) git add
        # упал из-за index.lock и индекс пустой. Различаем по наличию папки.
        article_path = DRAFTS_DIR / slug / "article.html"
        if article_path.exists():
            log.warning(
                "nothing to commit, но drafts/%s/article.html существует — "
                "возможно git add упал. Делаем повторную попытку add+commit.",
                slug,
            )
            # Финальная попытка: add ещё раз и commit. Если снова nothing —
            # значит файлы уже в репо (например, остались с прошлого слота
            # после rescue), не блокируем слот.
            for path in paths_to_add:
                _git_run_with_retry(
                    ["git", "add", "-A", "--", path],
                    env=env, cwd=cwd,
                )
            commit_res = _git_run_with_retry(
                ["git", "commit", "-m", commit_msg],
                env=env, cwd=cwd,
            )
            combined = (commit_res.stdout or "") + (commit_res.stderr or "")
            if "nothing to commit" in combined:
                return {"committed": False, "pushed": False,
                        "reason": "nothing_to_commit"}
        else:
            return {"committed": False, "pushed": False, "reason": "nothing_to_commit"}
    if commit_res.returncode != 0:
        _append_git_error(
            slot_ts=datetime.now().isoformat(timespec="seconds"),
            slug=slug, category=category, action="git_commit",
            returncode=commit_res.returncode,
            stderr=commit_res.stderr or "", stdout=commit_res.stdout or "",
        )
        return {"committed": False, "pushed": False,
                "reason": "commit_failed", "stderr": combined[-300:]}

    # Push через HTTPS+PAT (явный URL, чтобы не зависеть от настроек origin)
    remote_url = _git_remote_url()
    if not remote_url:
        return {"committed": True, "pushed": False, "reason": "no_token"}

    push_res = _git_run_with_retry(
        ["git", "push", remote_url, GITHUB_BRANCH],
        env=env, cwd=cwd, timeout=60,
    )

    # Если push отклонён из-за non-fast-forward (кто-то запушил параллельно за
    # время слота — например, бот публикатор), делаем pull --rebase и пробуем
    # ещё раз. Это закрывает гонку «scheduler vs bot vs ручные правки».
    if push_res.returncode != 0:
        stderr_lower = (push_res.stderr or "").lower()
        non_ff_markers = ("non-fast-forward", "fetch first", "updates were rejected",
                          "tip of your current branch is behind")
        if any(m in stderr_lower for m in non_ff_markers):
            log.warning(
                "Push отклонён (non-fast-forward), делаю pull --rebase -X theirs и повторяю"
            )
            # -X theirs автоматически разрешает конфликты в журнальных JSON
            # (data/scheduler_log.json, data/bot_state.json, data/git_errors.log,
            # data/published_index.json) в пользу версии с origin. Эти файлы
            # — append-only журналы, и в случае конфликта версия из origin
            # обычно более актуальная (другой инстанс уже её обновил).
            # Без этого rebase падал, --abort оставлял локальный коммит,
            # следующий редеплой его терял вместе с working tree.
            rebase_res = subprocess.run(
                ["git", "pull", "--rebase", "-X", "theirs", "--autostash",
                 remote_url, GITHUB_BRANCH],
                cwd=cwd, env=env, capture_output=True, text=True, timeout=90,
            )
            if rebase_res.returncode == 0:
                push_res = _git_run_with_retry(
                    ["git", "push", remote_url, GITHUB_BRANCH],
                    env=env, cwd=cwd, timeout=60,
                )
            else:
                # Rebase с конфликтом который не разрулился даже с -X theirs
                # (это значит конфликт в КОДЕ, не в журналах). Безопасный
                # вариант — abort. Локальный коммит остаётся, следующий слот
                # подхватит через _push_pending_local_commits в pre-flight.
                subprocess.run(
                    ["git", "rebase", "--abort"],
                    cwd=cwd, env=env, capture_output=True,
                )
                log.error(
                    "pull --rebase -X theirs упал с конфликтом в коде, abort: %s",
                    _mask_token((rebase_res.stderr or "")[-200:]),
                )

    if push_res.returncode != 0:
        _append_git_error(
            slot_ts=datetime.now().isoformat(timespec="seconds"),
            slug=slug, category=category, action="git_push",
            returncode=push_res.returncode,
            stderr=push_res.stderr or "", stdout=push_res.stdout or "",
        )
        _safe_pipeline_log(slug, "scheduler", "git_push_failed",
                           reason="push_failed",
                           stderr_tail=_mask_token((push_res.stderr or "")[-200:]))
        return {"committed": True, "pushed": False,
                "reason": "push_failed",
                "stderr": _mask_token((push_res.stderr or "")[-300:])}
    _safe_pipeline_log(slug, "scheduler", "git_pushed", branch=GITHUB_BRANCH)

    # Health-check Cloud Apps перед созданием .pushed sentinel.
    # После git push GitHub Actions/Cloud Apps webhook триггерит redeploy
    # сайта, который занимает 30-120 сек. Если watcher отправит уведомление
    # СРАЗУ после push, заказчик кликнет на /preview ссылку и получит 401/404
    # потому что Cloud Apps ещё доделывает redeploy.
    #
    # Делаем GET на /preview/{slug}?t=TOKEN и ждём 200. Только после этого
    # пишем .pushed, который и триггерит отправку уведомления через watcher.
    # Таймаут 3 минуты — если Cloud Apps дольше, всё равно отдаём уведомление
    # (лучше уведомить чем потерять).
    _wait_cloud_apps_ready(slug)

    try:
        slug_dir = DRAFTS_DIR / slug
        if slug_dir.exists():
            (slug_dir / ".pushed").write_text(
                datetime.now().isoformat(timespec="seconds") + "\n",
                encoding="utf-8",
            )
    except OSError:
        # Не критично: в худшем случае уведомление чуть запоздает или вообще
        # не уйдёт до следующего слота — это лучше чем 404 у заказчика.
        log.warning("Не смог записать .pushed sentinel для slug=%s", slug)

    return {"committed": True, "pushed": True}


def _wait_cloud_apps_ready(slug: str, timeout_sec: int = 180) -> bool:
    """
    Ждёт пока Cloud Apps подтянет статью после git push.
    Делает GET на /preview/{slug}?t=TOKEN каждые 5 сек, до получения 200.
    Возвращает True если дождались, False если timeout (но всё равно
    продолжаем pipeline — лучше уведомить с задержкой чем потерять статью).
    """
    public_base = os.getenv("PUBLIC_BASE_URL", "https://pravo.shop").rstrip("/")

    # Берём preview_token из bot_state.json — тот же что в TG-ссылке у заказчика
    token = ""
    try:
        bs_path = DATA_DIR / "bot_state.json"
        if bs_path.exists():
            bs = json.loads(bs_path.read_text(encoding="utf-8"))
            token = (bs.get("preview_token") or "").strip()
    except Exception:
        log.warning("Не смог прочитать preview_token из bot_state.json для health-check")

    if not token:
        # Без токена /preview всё равно вернёт 401, health-check бессмысленен.
        # Просто подождём 60 сек чтобы Cloud Apps успел редеплоить.
        log.info("preview_token не найден, ждём 60 сек безусловно перед .pushed sentinel")
        time.sleep(60)
        return False

    url = f"{public_base}/preview/{slug}?t={token}"
    deadline = time.time() + timeout_sec
    log.info("Cloud Apps health-check: %s", url)

    try:
        import urllib.request
        import urllib.error
    except ImportError:
        log.warning("urllib недоступен, пропускаю health-check")
        return False

    attempt = 0
    while time.time() < deadline:
        attempt += 1
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=10) as resp:
                if resp.status == 200:
                    log.info("Cloud Apps готов (attempt=%d): %d", attempt, resp.status)
                    return True
                else:
                    log.debug("Cloud Apps attempt=%d status=%d, retry через 5с", attempt, resp.status)
        except urllib.error.HTTPError as e:
            log.debug("Cloud Apps attempt=%d HTTPError=%d, retry через 5с", attempt, e.code)
        except urllib.error.URLError as e:
            log.debug("Cloud Apps attempt=%d URLError=%s, retry через 5с", attempt, e)
        except Exception as e:
            log.debug("Cloud Apps attempt=%d exception=%s, retry через 5с", attempt, e)
        time.sleep(5)

    log.warning(
        "Cloud Apps health-check timeout (%ds, %d попыток) — продолжаем без подтверждения. "
        "Заказчик может увидеть 404 при клике в первые минуту-две.",
        timeout_sec, attempt,
    )
    return False


def _git_commit_qa_only(slug: str) -> dict:
    """
    Коммитит drafts/{slug}/ + quality_gate.json + scheduler_log.json при провале gate.
    Статья НЕ публикуется в articles/, но артефакты сохраняются для разбора заказчиком.
    """
    cwd = str(ROOT)
    env = _git_env()
    paths_to_add = [
        f"drafts/{slug}/",
        "data/scheduler_log.json",
        "data/git_errors.log",
        "data/bot_state.json",
    ]
    _git_run_with_retry(
        ["git", "add", "--", *paths_to_add],
        env=env, cwd=cwd,
    )
    commit_res = _git_run_with_retry(
        ["git", "commit", "-m", f"failed_qa: {slug} (quality_gate blocked)"],
        env=env, cwd=cwd,
    )
    if "nothing to commit" in (commit_res.stdout or "") + (commit_res.stderr or ""):
        return {"committed": False}
    if commit_res.returncode != 0:
        return {"committed": False, "stderr": ((commit_res.stderr or "")[-200:])}

    remote_url = _git_remote_url()
    if not remote_url:
        return {"committed": True, "pushed": False, "reason": "no_token"}

    push_res = _git_run_with_retry(
        ["git", "push", remote_url, GITHUB_BRANCH],
        env=env, cwd=cwd, timeout=60,
    )
    if push_res.returncode != 0:
        return {"committed": True, "pushed": False,
                "stderr": _mask_token((push_res.stderr or "")[-200:])}
    return {"committed": True, "pushed": True}


def _git_commit_log_only() -> dict:
    """
    Коммитит и пушит логи (scheduler_log.json, bot_state.json, git_errors.log)
    плюс drafts/_topic-map/*.json - чтобы при expand_topics режиме
    сгенерированные новые темы попадали в репо и не терялись при редеплое
    Cloud Apps. Если ничего из этого не менялось - git сам выдаст
    "nothing to commit" и функция вернёт committed=False.
    """
    cwd = str(ROOT)
    env = _git_env()
    _git_run_with_retry(
        ["git", "add", "--", "data/scheduler_log.json",
         "data/bot_state.json", "data/git_errors.log",
         "drafts/_topic-map/"],
        env=env, cwd=cwd,
    )
    commit_res = _git_run_with_retry(
        ["git", "commit", "-m", "log: scheduler state update"],
        env=env, cwd=cwd,
    )
    if "nothing to commit" in (commit_res.stdout or "") + (commit_res.stderr or ""):
        return {"committed": False}
    if commit_res.returncode != 0:
        return {"committed": False, "stderr": ((commit_res.stderr or "")[-200:])}

    remote_url = _git_remote_url()
    if not remote_url:
        return {"committed": True, "pushed": False, "reason": "no_token"}

    push_res = _git_run_with_retry(
        ["git", "push", remote_url, GITHUB_BRANCH],
        env=env, cwd=cwd, timeout=60,
    )
    if push_res.returncode != 0:
        return {"committed": True, "pushed": False,
                "stderr": _mask_token((push_res.stderr or "")[-200:])}
    return {"committed": True, "pushed": True}


# ============ HEARTBEAT-RUNNER ============

class _HeartbeatTimeout(Exception):
    """Subprocess убит из-за тишины heartbeat дольше HEARTBEAT_TIMEOUT_SEC."""


def _run_claude_with_heartbeat(cmd: list[str]):
    """
    Запускает claude-subprocess в режиме Popen и параллельно следит за
    HEARTBEAT_PATH. Если файл не обновлялся HEARTBEAT_TIMEOUT_SEC секунд —
    убивает процесс раньше общего таймаута (ARTICLE_TIMEOUT_SEC).

    Возвращает объект с returncode/stdout/stderr (как у subprocess.run).
    Если случился heartbeat-timeout — returncode = -1, в stderr пометка.
    """
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    hard_deadline = time.time() + ARTICLE_TIMEOUT_SEC
    heartbeat_killed = False
    hard_killed = False

    # poll каждые 5 сек: проверяем закончился ли процесс, общий таймаут, heartbeat
    while proc.poll() is None:
        time.sleep(5)
        now = time.time()
        if now >= hard_deadline:
            proc.kill()
            hard_killed = True
            break
        if HEARTBEAT_PATH.exists():
            age = now - HEARTBEAT_PATH.stat().st_mtime
            if age > HEARTBEAT_TIMEOUT_SEC:
                log.error(
                    "Heartbeat молчит %.0f сек (>%ds), убиваю claude-subprocess",
                    age, HEARTBEAT_TIMEOUT_SEC,
                )
                proc.kill()
                heartbeat_killed = True
                break

    # process либо завершился сам, либо был убит — communicate безопасен
    stdout, stderr = proc.communicate()

    if hard_killed:
        raise subprocess.TimeoutExpired(cmd, ARTICLE_TIMEOUT_SEC, stdout, stderr)

    class _Result:
        pass
    r = _Result()
    r.stdout = stdout or ""
    r.stderr = stderr or ""
    if heartbeat_killed:
        r.returncode = -1
        r.stderr += f"\n[scheduler] killed by heartbeat timeout (>{HEARTBEAT_TIMEOUT_SEC}s of silence)"
    else:
        r.returncode = proc.returncode
    return r


# ============ ОСНОВНОЙ ЦИКЛ ============

def run_one_article() -> dict:
    """
    Один тик scheduler'а. Идемпотентен: если идёт другой пайплайн (lock),
    лимит достигнут или scheduler на паузе - просто возвращает статус и выходит.
    """
    started = time.time()
    timestamp = datetime.now().isoformat(timespec="seconds")

    # Пауза
    if PAUSE_FLAG.exists():
        log.info("scheduler приостановлен флагом %s", PAUSE_FLAG)
        return {"status": "paused", "timestamp": timestamp}

    # Lock от параллельного запуска
    DATA_DIR.mkdir(exist_ok=True)
    if LOCK_FILE.exists():
        age = time.time() - LOCK_FILE.stat().st_mtime
        if age > LOCK_STALE_SEC:
            log.warning("Снимаю устаревший lock (возраст %.0f сек)", age)
            LOCK_FILE.unlink(missing_ok=True)
        else:
            log.info("lock активен (%.0f сек), пропуск слота", age)
            return {"status": "locked", "timestamp": timestamp, "lock_age_sec": int(age)}

    # Дневной лимит
    today_count = _today_ok_count()
    if today_count >= ARTICLES_PER_DAY:
        log.info("Дневной лимит %d достигнут (сегодня ok=%d), пропуск",
                 ARTICLES_PER_DAY, today_count)
        return {
            "status": "limit_reached",
            "timestamp": timestamp,
            "today_count": today_count,
            "limit": ARTICLES_PER_DAY,
        }

    # Перед тем как брать новую тему — проверяем, есть ли failed_qa-статья
    # которую можно дорабатывать через /rewrite-article. Это экономит токены
    # (не запускаем агентов 1-2-3 заново) и чистит хвост из «зависших» статей.
    # ВАЖНО: при явном FORCE_CATEGORY (CLI --category или ENV) retry применяется
    # только если он той же категории. Иначе пользователь явно попросил новую
    # статью X, а получил бы ремонт чужой Y — это противоречит запросу.
    forced_cat = (os.getenv("FORCE_CATEGORY") or "").strip().lower()
    forced_cat = forced_cat if forced_cat in VALID_CATEGORIES else ""

    retry_slug = _find_failed_qa_for_retry(max_iterations=5)
    if retry_slug:
        retry_meta = _read_meta(retry_slug)
        retry_cat = retry_meta.get("category")
        if forced_cat and retry_cat != forced_cat:
            log.info(
                "failed_qa-статья %s (cat=%s) пропущена: FORCE_CATEGORY=%s — берём новую тему",
                retry_slug, retry_cat, forced_cat,
            )
            retry_slug = None
        else:
            log.info("Найдена failed_qa статья для доработки: %s", retry_slug)

    if retry_slug:
        # Категория берётся из meta.json статьи, а не из ротации
        category = retry_meta.get("category") or _next_category()
        slot_mode = "rewrite"
    else:
        category = _next_category()  # _next_category уважает FORCE_CATEGORY
        slot_mode = "new"

    log.info("Старт слота: mode=%s category=%s today_count=%d/%d",
             slot_mode, category, today_count, ARTICLES_PER_DAY)

    LOCK_FILE.write_text(timestamp, encoding="utf-8")
    entry: dict = {
        "timestamp": timestamp,
        "category": category,
    }

    # Pipeline-лог: событие старта слота. slug ещё неизвестен (его генерит
    # агент 1) — пишем в специальный slug "_slot_{timestamp}", потом
    # переименовываем когда узнаем настоящий slug. Но это сложно, проще:
    # лог пишется когда мы определим slug после write-article. Сейчас
    # просто фиксируем в общем логе.
    log.info("scheduler: slot_started ts=%s category=%s today=%d/%d",
             timestamp, category, today_count, ARTICLES_PER_DAY)

    try:
        # Сначала гарантируем что HEAD на ветке (а не detached). Cloud Apps при
        # деплое мог сделать `git checkout {sha}` — тогда последующие коммиты
        # не попадут на main, push скажет Everything up-to-date.
        branch_result = _ensure_on_branch()
        entry["git_branch_check"] = branch_result
        if not branch_result.get("ok"):
            log.error(
                "Не удалось переключиться на %s: %s",
                GITHUB_BRANCH, branch_result.get("stderr_tail"),
            )

        # Подтягиваем свежий main перед стартом - на случай ручных правок в GitHub
        pull_result = _git_pull_before_slot()
        if not pull_result["ok"]:
            log.warning("git pull --ff-only не прошёл: %s", pull_result.get("stderr_tail"))
        entry["git_pull"] = pull_result

        # Pre-flight: догоняем коммиты которые не доехали до origin в прошлых слотах
        # (например, push упал из-за отвала сети). Без этого редеплой их потеряет.
        catchup = _push_pending_local_commits()
        if catchup.get("pending", 0) > 0:
            entry["catchup_pending"] = catchup

        # Обновляем индекс опубликованных статей: архитектор и критик его читают.
        # Без актуального индекса — галлюцинации slug-ов и 404-ссылки.
        _refresh_published_index()

        # Готовим prev_summary последней статьи категории (для антишаблонности
        # агента 4). Лёгкий JSON вместо чтения полного текста соседа —
        # экономия 3-6k токенов на писателя.
        # exclude_slug: при rewrite не сравниваем статью саму с собой.
        _exclude = retry_slug if slot_mode == "rewrite" else None
        _refresh_prev_summary(category, exclude_slug=_exclude)

        # Выбираем команду: новая статья, доработка failed_qa, или
        # пополнение topic-map если темы для категории кончились.
        # Для mode="new": если агент 1 откажется писать тему (каннибализация
        # с уже опубликованной, evergreen в news, событие старше 30 дней) —
        # автоматически помечаем тему rejected и берём следующую, до
        # MAX_TOPIC_RETRIES_PER_SLOT попыток. Так заказчик при ручном
        # запуске пайплайна гарантированно получает статью, а не failed_qa.
        if slot_mode == "rewrite" and retry_slug:
            claude_command = f"/rewrite-article {retry_slug}"
            entry["mode"] = "rewrite"
            entry["retry_slug"] = retry_slug
            cmd = ["claude", "--print", "--dangerously-skip-permissions", claude_command]
            HEARTBEAT_PATH.write_text(
                f"{datetime.now().isoformat(timespec='seconds')} | started",
                encoding="utf-8",
            )
            result = _run_claude_with_heartbeat(cmd)
        else:
            auto_skipped: list[dict] = []
            result = None
            for attempt_num in range(MAX_TOPIC_RETRIES_PER_SLOT):
                topic = _pick_topic(category)
                if topic is None:
                    # Темы кончились → /expand-topics. Сам слот не пишет статью,
                    # статус будет "topics_expanded", следующий слот возьмёт первую из новых.
                    claude_command = f"/expand-topics {category}"
                    entry["mode"] = "expand_topics"
                    log.info("Темы для %s исчерпаны, запускаю /expand-topics", category)
                    cmd = ["claude", "--print", "--dangerously-skip-permissions", claude_command]
                    HEARTBEAT_PATH.write_text(
                        f"{datetime.now().isoformat(timespec='seconds')} | started",
                        encoding="utf-8",
                    )
                    result = _run_claude_with_heartbeat(cmd)
                    break

                topic_title = (topic.get("title") or topic.get("topic_action") or "").strip()
                topic_slug = topic.get("slug") or ""
                # Slug передаём явно — иначе Claude игнорировал brief и генерил свой
                # → бесконечный цикл по теме.
                claude_command = f"/write-article {category} slug={topic_slug} {topic_title}"
                entry["mode"] = "new"
                entry["topic_id"] = topic.get("id")
                entry["topic_slug"] = topic_slug
                log.info(
                    "Тема выбрана (попытка %d/%d): cat=%s id=%s slug=%s title=%s",
                    attempt_num + 1, MAX_TOPIC_RETRIES_PER_SLOT,
                    category, topic.get("id"), topic_slug, topic_title[:80],
                )

                cmd = ["claude", "--print", "--dangerously-skip-permissions", claude_command]
                HEARTBEAT_PATH.write_text(
                    f"{datetime.now().isoformat(timespec='seconds')} | started",
                    encoding="utf-8",
                )
                attempt_started = time.time()
                result = _run_claude_with_heartbeat(cmd)
                attempt_duration = round(time.time() - attempt_started, 1)

                # Детектим: агент 1 отверг тему (каннибализация / не-news / evergreen)?
                rejection = _detect_topic_rejection(topic_slug)
                if rejection:
                    _mark_topic_rejected(category, topic_slug, rejection)
                    auto_skipped.append({
                        "slug": topic_slug,
                        "reason": rejection,
                        "duration_sec": attempt_duration,
                    })
                    if attempt_num + 1 < MAX_TOPIC_RETRIES_PER_SLOT:
                        log.warning(
                            "Auto-skip %d/%d: %s/%s — %s. Беру следующую тему.",
                            attempt_num + 1, MAX_TOPIC_RETRIES_PER_SLOT,
                            category, topic_slug, rejection,
                        )
                        continue
                    log.error(
                        "Auto-skip исчерпан (%d попыток подряд rejected). Слот завершится failed.",
                        MAX_TOPIC_RETRIES_PER_SLOT,
                    )
                # Не rejection (либо успех, либо иная причина — slug_mismatch,
                # timeout, rate_limit, quality_gate). Auto-skip не помогает — break.
                break

            if auto_skipped:
                entry["auto_skipped"] = auto_skipped

        duration = round(time.time() - started, 1)
        ok = result.returncode == 0

        # Режим expand_topics: статью не пишем, только пополняем topic-map.
        # Slug отсутствует, quality_gate пропускаем, отдельный статус.
        if entry.get("mode") == "expand_topics":
            entry.update({
                "status": "topics_expanded" if ok else "failed",
                "duration_sec": duration,
                "returncode": result.returncode,
                "stdout_tail": (result.stdout or "")[-500:],
                "stderr_tail": (result.stderr or "")[-500:],
            })
            if ok:
                log.info(
                    "Слот завершён: status=topics_expanded category=%s duration=%.1fs",
                    category, duration,
                )
            else:
                log.error(
                    "Слот завершён (expand): status=failed rc=%s stdout=%r",
                    result.returncode, entry["stdout_tail"],
                )
            _append_log(entry)
            _git_commit_log_only()
            _update_failure_streak(entry, timestamp)
            return entry

        # При rewrite slug известен заранее (берём из retry_slug).
        # При new — обнаруживаем по mtime новых драфтов.
        # Важно: ищем slug ДАЖЕ при rc != 0. Claude мог написать статью,
        # но вылететь на финале (например, hit limit на агенте 7 publisher).
        # Если папка готова и article.html существует — спасаем её, иначе
        # вся проделанная работа теряется при следующем редеплое.
        if slot_mode == "rewrite" and retry_slug:
            slug = retry_slug
        else:
            # Передаём expected_slug из topic-map чтобы поймать случай когда
            # Claude не использовал slug из brief'а (а пишет в чужую папку).
            # Если slot_mode == "expand_topics" — мы не дойдём сюда (там early
            # return выше). Так что entry["topic_slug"] всегда задан в new-режиме.
            expected_slug = entry.get("topic_slug")
            slug = _detect_new_slug(started, expected_slug=expected_slug)
            if expected_slug and slug != expected_slug:
                # Slug mismatch уже залогирован в _detect_new_slug.
                # Помечаем флагом в entry для scheduler_log
                entry["slug_mismatch"] = True
                entry["expected_slug"] = expected_slug

        # Если Claude упал, но статья на диске готова — переопределяем ok=True.
        rescued_after_failure = False
        if not ok and slug:
            article_path = DRAFTS_DIR / slug / "article.html"
            if article_path.exists():
                rescued_after_failure = True
                ok = True
                log.warning(
                    "Claude rc=%s но drafts/%s/article.html готов — спасаем статью "
                    "(stdout_tail=%r)",
                    result.returncode, slug, (result.stdout or "")[-200:],
                )

        meta = _read_meta(slug) if slug else {}
        metrics = _metrics_summary(meta)

        # Pipeline-лог: ретроактивно фиксируем что слот стартовал для этой статьи
        # (мы не знали slug до того как агент 1 его сгенерил)
        if slug:
            _safe_pipeline_log(slug, "scheduler", "slot_started",
                               category=category,
                               today_count=today_count,
                               limit=ARTICLES_PER_DAY,
                               write_article_duration_sec=duration)

        # Quality gate — hard-блок перед коммитом. Запускается только если
        # write-article отработал и есть slug. Если gate упал — статус failed_qa,
        # коммит блокируется, статья остаётся в drafts/{slug}/ для разбора.
        gate = None
        if ok and slug:
            gate = _run_quality_gate(slug)
            entry["quality_gate"] = gate
            _safe_pipeline_log(slug, "quality_gate",
                               "passed" if gate.get("passed") else "failed",
                               blockers=gate.get("blockers") or [],
                               warnings=gate.get("warnings") or [],
                               recommendations=gate.get("recommendations") or [])

        # С 8 мая 2026: метрические fail'ы (spam/AI/uniqueness/length) больше
        # не блокируют публикацию. Заказчик увидит риски через бот и решит сам.
        # Блокируем только структурные проблемы (gate не запустился = нет article.html
        # = нечего показывать) или явный hard_failed (пока всегда False, зарезервировано).
        gate_ran = gate is not None and gate.get("ran", False)
        gate_hard_failed = gate is not None and gate.get("hard_failed", False)
        # gate_passed теперь = «пайплайн доехал до конца, статью можно показать».
        # Не означает что все метрики ок - значит просто что заказчик может ревьюить.
        gate_passed = gate is None or (gate_ran and not gate_hard_failed)

        # Safety net: финализация драфта (обложка + ready_for_review + review_queue).
        # Орхестратор /write-article должен сам это сделать на шаге 9 через
        # `python -m articles_scheduler.finalize_draft {slug}`. Но если LLM
        # отвлёкся, вылетел по таймауту/токенам или скипнул шаг — финализируем
        # сами. finalize_draft идемпотентен: при повторном запуске cover_url
        # уже в meta, image_gen пропускается; запись в _review_queue.json
        # обновляется на месте. Запускаем только при gate_passed - иначе
        # драфт всё равно failed_qa и не пойдёт на публикацию.
        if ok and slug and gate_passed:
            try:
                fresh_meta = _read_meta(slug) or {}
            except Exception:
                fresh_meta = {}
            if not fresh_meta.get("ready_for_review"):
                log.warning(
                    "finalize_draft не отработал в /write-article для slug=%s — "
                    "запускаем как safety net из scheduler",
                    slug,
                )
                try:
                    from articles_scheduler.finalize_draft import finalize as _finalize_draft
                    rc = _finalize_draft(slug)
                    entry["finalize_safety_net"] = {"ran": True, "rc": rc}
                    if rc != 0:
                        log.error(
                            "finalize_draft (safety net) вернул rc=%d для slug=%s — "
                            "драфт без ready_for_review, не попадёт в очередь бота",
                            rc, slug,
                        )
                except Exception:
                    log.exception("finalize_draft (safety net) упал для slug=%s", slug)
                    entry["finalize_safety_net"] = {"ran": True, "error": True}

        if ok and slug and not gate_passed:
            entry.update({
                "status": "failed_qa",
                "slug": slug,
                "duration_sec": duration,
                "returncode": result.returncode,
                "rescued_after_failure": rescued_after_failure,
                "stdout_tail": (result.stdout or "")[-500:],
                "stderr_tail": (result.stderr or "")[-500:],
                "metrics": {
                    "ai_detector": meta.get("textru_ai_detector"),
                    "uniqueness": meta.get("textru_uniqueness"),
                    "spam": meta.get("textru_spam"),
                    "text_chars": meta.get("text_chars"),
                } if meta else None,
            })
        else:
            entry.update({
                "status": "ok" if ok and slug else "failed",
                "slug": slug,
                "duration_sec": duration,
                "returncode": result.returncode,
                "rescued_after_failure": rescued_after_failure,
                "stdout_tail": (result.stdout or "")[-500:],
                "stderr_tail": (result.stderr or "")[-500:],
                "metrics": {
                    "ai_detector": meta.get("textru_ai_detector"),
                    "uniqueness": meta.get("textru_uniqueness"),
                    "spam": meta.get("textru_spam"),
                    "text_chars": meta.get("text_chars"),
                } if meta else None,
            })

        if entry["status"] == "ok":
            push_result = _git_commit_and_push(slug, category, metrics)
            entry["git"] = push_result
            log.info(
                "Слот завершён: status=ok slug=%s duration=%.1fs git=%s",
                slug, duration, push_result.get("reason") or ("pushed" if push_result.get("pushed") else "committed"),
            )
        elif entry["status"] == "failed_qa":
            log.error(
                "Слот завершён: status=failed_qa slug=%s blockers=%s",
                slug, (gate or {}).get("blockers"),
            )
        else:
            log.error(
                "Слот завершён: status=%s rc=%s stderr_tail=%r stdout_tail=%r",
                entry["status"], result.returncode,
                entry["stderr_tail"], entry["stdout_tail"],
            )

        _append_log(entry)

        # Pipeline-лог: финальная запись слота со статусом и метриками
        if slug:
            try:
                from tools.pipeline_log import finalize_pipeline
                finalize_pipeline(
                    slug,
                    status=entry["status"],
                    metrics=entry.get("metrics"),
                    git=entry.get("git"),
                )
            except Exception as exc:
                log.debug("finalize_pipeline failed: %s", exc)

        # Сохраняем scheduler_log.json в git даже при failed/failed_qa слотах,
        # чтобы история не пропадала при следующем редеплое Cloud Apps.
        # При success git_commit_and_push выше уже включил его в коммит.
        # При failed_qa коммитим quality_gate.json в drafts/{slug}/ для разбора.
        if entry["status"] == "failed_qa" and slug:
            _git_commit_qa_only(slug)
        elif entry["status"] != "ok":
            _git_commit_log_only()

        _update_failure_streak(entry, timestamp)
        return entry

    except subprocess.TimeoutExpired:
        entry.update({
            "status": "timeout",
            "duration_sec": round(time.time() - started, 1),
        })
        _append_log(entry)
        log.error("Слот завершён по таймауту %s сек: category=%s", ARTICLE_TIMEOUT_SEC, category)
        _update_failure_streak(entry, timestamp)
        return entry

    except Exception as exc:
        entry.update({
            "status": "exception",
            "error": str(exc),
            "duration_sec": round(time.time() - started, 1),
        })
        _append_log(entry)
        log.exception("Слот завершён с исключением: category=%s", category)
        _update_failure_streak(entry, timestamp)
        return entry

    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Один слот scheduler-а: запустить /write-article или /rewrite-article"
    )
    parser.add_argument(
        "--category",
        choices=sorted(VALID_CATEGORIES),
        help="Принудительно задать категорию (fiz, yur, vzysk, news). "
             "Переопределяет ротацию ROTATION_ORDER. Эквивалент ENV FORCE_CATEGORY.",
    )
    args = parser.parse_args()

    # Если задан --category — выставляем FORCE_CATEGORY на время процесса.
    if args.category:
        os.environ["FORCE_CATEGORY"] = args.category

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = run_one_article()
    print(json.dumps(result, ensure_ascii=False, indent=2))
