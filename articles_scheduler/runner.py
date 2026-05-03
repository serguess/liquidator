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
import time
from datetime import datetime, date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DRAFTS_DIR = ROOT / "drafts"
SCHEDULER_LOG_PATH = DATA_DIR / "scheduler_log.json"
LOCK_FILE = DATA_DIR / ".scheduler.lock"
PAUSE_FLAG = DATA_DIR / ".scheduler_paused"

ROTATION = [c.strip() for c in os.getenv("ROTATION_ORDER", "fiz,yur,vzysk,news").split(",") if c.strip()]
ARTICLES_PER_DAY = int(os.getenv("ARTICLES_PER_DAY", "1"))
ARTICLE_TIMEOUT_SEC = int(os.getenv("ARTICLE_TIMEOUT_SEC", "2400"))  # 40 минут
LOCK_STALE_SEC = int(os.getenv("LOCK_STALE_SEC", "3600"))  # 1 час
GITHUB_REPO = os.getenv("GITHUB_REPO", "triyul22/liquidator")
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


def _next_category() -> str:
    """Простая ротация: берём индекс по числу попыток сегодня (включая упавшие)."""
    today = date.today().isoformat()
    today_entries = [
        e for e in _read_log()
        if (e.get("timestamp") or "").startswith(today) and e.get("status") not in ("paused", "locked")
    ]
    idx = len(today_entries) % len(ROTATION) if ROTATION else 0
    return ROTATION[idx] if ROTATION else "fiz"


# ============ DRAFTS ============

def _detect_new_slug(started_ts: float) -> str | None:
    """
    Находит slug, чей каталог создан в течение текущего пайплайна.
    Берём подкаталоги drafts/, у которых mtime > started_ts - 30 (запас на округление).
    """
    if not DRAFTS_DIR.exists():
        return None
    candidates = [
        d for d in DRAFTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_") and d.stat().st_mtime > started_ts - 30
    ]
    if not candidates:
        return None
    # Самый свежий
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


def _git_pull_before_slot() -> dict:
    """
    Подтягивает свежие изменения с GitHub перед началом слота.

    Pull идёт через явный URL с PAT (не через `git pull origin`), чтобы
    не зависеть от настроек origin в репо (Cloud Apps клонирует по HTTPS,
    мы не переписываем origin).
    """
    cwd = str(ROOT)
    env = _git_env()
    remote_url = _git_remote_url()
    if not remote_url:
        return {"ok": False, "stdout_tail": "", "stderr_tail": "GIT_PUSH_TOKEN не задан"}

    res = subprocess.run(
        ["git", "pull", "--ff-only", remote_url, GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    return {
        "ok": res.returncode == 0,
        "stdout_tail": _mask_token((res.stdout or "")[-200:]),
        "stderr_tail": _mask_token((res.stderr or "")[-200:]),
    }


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
        "drafts/_topic-map/",
    ]
    # add может ругаться на несуществующие файлы - используем check=False
    subprocess.run(["git", "add", "--", *paths_to_add], cwd=cwd, env=env,
                   check=False, capture_output=True)

    msg_tail = f", {metrics}" if metrics else ""
    commit_msg = f"drafts: {slug} ({category}{msg_tail})"
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

    # Push через HTTPS+PAT (явный URL, чтобы не зависеть от настроек origin)
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

    category = _next_category()
    log.info("Старт слота: category=%s, today_count=%d/%d",
             category, today_count, ARTICLES_PER_DAY)

    LOCK_FILE.write_text(timestamp, encoding="utf-8")
    entry: dict = {
        "timestamp": timestamp,
        "category": category,
    }

    try:
        # Подтягиваем свежий main перед стартом - на случай ручных правок в GitHub
        pull_result = _git_pull_before_slot()
        if not pull_result["ok"]:
            log.warning("git pull --ff-only не прошёл: %s", pull_result.get("stderr_tail"))
        entry["git_pull"] = pull_result

        cmd = [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            f"/write-article {category}",
        ]
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=ARTICLE_TIMEOUT_SEC,
            encoding="utf-8",
            errors="replace",
        )

        duration = round(time.time() - started, 1)
        ok = result.returncode == 0
        slug = _detect_new_slug(started) if ok else None
        meta = _read_meta(slug) if slug else {}
        metrics = _metrics_summary(meta)

        entry.update({
            "status": "ok" if ok and slug else "failed",
            "slug": slug,
            "duration_sec": duration,
            "returncode": result.returncode,
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
        else:
            log.error(
                "Слот завершён: status=%s rc=%s stderr_tail=%r",
                entry["status"], result.returncode, entry["stderr_tail"],
            )

        _append_log(entry)
        return entry

    except subprocess.TimeoutExpired:
        entry.update({
            "status": "timeout",
            "duration_sec": round(time.time() - started, 1),
        })
        _append_log(entry)
        log.error("Слот завершён по таймауту %s сек: category=%s", ARTICLE_TIMEOUT_SEC, category)
        return entry

    except Exception as exc:
        entry.update({
            "status": "exception",
            "error": str(exc),
            "duration_sec": round(time.time() - started, 1),
        })
        _append_log(entry)
        log.exception("Слот завершён с исключением: category=%s", category)
        return entry

    finally:
        LOCK_FILE.unlink(missing_ok=True)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    result = run_one_article()
    print(json.dumps(result, ensure_ascii=False, indent=2))
