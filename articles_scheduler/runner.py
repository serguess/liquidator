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
DATA_DIR = ROOT / "data"
DRAFTS_DIR = ROOT / "drafts"
SCHEDULER_LOG_PATH = DATA_DIR / "scheduler_log.json"
GIT_ERRORS_LOG_PATH = DATA_DIR / "git_errors.log"
LOCK_FILE = DATA_DIR / ".scheduler.lock"
PAUSE_FLAG = DATA_DIR / ".scheduler_paused"

ROTATION = [c.strip() for c in os.getenv("ROTATION_ORDER", "fiz,yur,vzysk,news").split(",") if c.strip()]
ARTICLES_PER_DAY = int(os.getenv("ARTICLES_PER_DAY", "1"))
ARTICLE_TIMEOUT_SEC = int(os.getenv("ARTICLE_TIMEOUT_SEC", "2400"))  # 40 минут
LOCK_STALE_SEC = int(os.getenv("LOCK_STALE_SEC", "3600"))  # 1 час
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


def _find_failed_qa_for_retry(max_iterations: int = 3) -> str | None:
    """
    Ищет статью failed_qa, которую можно дорабатывать (а не брать новую тему).

    Условия:
    1. В drafts/{slug}/quality_gate.json есть `passed: false`.
    2. Текущее число итераций (current_iteration в _pipeline.log.json)
       ещё не достигло max_iterations (по умолчанию 3).
    3. В drafts/_review/ её нет (значит ручной разбор не запрошен).

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

        # Проверяем лимит итераций
        pipe_path = slug_dir / "_pipeline.log.json"
        iterations = 1
        if pipe_path.exists():
            try:
                pipe = json.loads(pipe_path.read_text(encoding="utf-8"))
                iterations = pipe.get("current_iteration", 1)
            except (json.JSONDecodeError, OSError):
                pass
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
    ok = res.returncode == 0
    if not ok:
        _append_git_error(
            slot_ts=datetime.now().isoformat(timespec="seconds"),
            slug=None, category="-", action="git_pull",
            returncode=res.returncode,
            stderr=res.stderr or "", stdout=res.stdout or "",
        )
    return {
        "ok": ok,
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
        "data/git_errors.log",
        "data/published_index.json",
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
    return {"committed": True, "pushed": True}


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
    subprocess.run(
        ["git", "add", "--", *paths_to_add],
        cwd=cwd, env=env, check=False, capture_output=True,
    )
    commit_res = subprocess.run(
        ["git", "commit", "-m", f"failed_qa: {slug} (quality_gate blocked)"],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    if "nothing to commit" in (commit_res.stdout or "") + (commit_res.stderr or ""):
        return {"committed": False}
    if commit_res.returncode != 0:
        return {"committed": False, "stderr": ((commit_res.stderr or "")[-200:])}

    remote_url = _git_remote_url()
    if not remote_url:
        return {"committed": True, "pushed": False, "reason": "no_token"}

    push_res = subprocess.run(
        ["git", "push", remote_url, GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    if push_res.returncode != 0:
        return {"committed": True, "pushed": False,
                "stderr": _mask_token((push_res.stderr or "")[-200:])}
    return {"committed": True, "pushed": True}


def _git_commit_log_only() -> dict:
    """
    Коммитит и пушит только scheduler_log.json и bot_state.json - чтобы
    история работы сохранялась даже при failed слотах (без них при следующем
    редеплое Cloud Apps лог пропадёт).
    """
    cwd = str(ROOT)
    env = _git_env()
    subprocess.run(
        ["git", "add", "--", "data/scheduler_log.json",
         "data/bot_state.json", "data/git_errors.log"],
        cwd=cwd, env=env, check=False, capture_output=True,
    )
    commit_res = subprocess.run(
        ["git", "commit", "-m", "log: scheduler state update"],
        cwd=cwd, env=env, capture_output=True, text=True,
    )
    if "nothing to commit" in (commit_res.stdout or "") + (commit_res.stderr or ""):
        return {"committed": False}
    if commit_res.returncode != 0:
        return {"committed": False, "stderr": ((commit_res.stderr or "")[-200:])}

    remote_url = _git_remote_url()
    if not remote_url:
        return {"committed": True, "pushed": False, "reason": "no_token"}

    push_res = subprocess.run(
        ["git", "push", remote_url, GITHUB_BRANCH],
        cwd=cwd, env=env, capture_output=True, text=True, timeout=60,
    )
    if push_res.returncode != 0:
        return {"committed": True, "pushed": False,
                "stderr": _mask_token((push_res.stderr or "")[-200:])}
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

    # Перед тем как брать новую тему — проверяем, есть ли failed_qa-статья
    # которую можно дорабатывать через /rewrite-article. Это экономит токены
    # (не запускаем агентов 1-2-3 заново) и чистит хвост из «зависших» статей.
    retry_slug = _find_failed_qa_for_retry(max_iterations=3)
    if retry_slug:
        log.info("Найдена failed_qa статья для доработки: %s", retry_slug)
        # Категория берётся из meta.json статьи, а не из ротации
        retry_meta = _read_meta(retry_slug)
        category = retry_meta.get("category") or _next_category()
        slot_mode = "rewrite"
    else:
        category = _next_category()
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
        # Подтягиваем свежий main перед стартом - на случай ручных правок в GitHub
        pull_result = _git_pull_before_slot()
        if not pull_result["ok"]:
            log.warning("git pull --ff-only не прошёл: %s", pull_result.get("stderr_tail"))
        entry["git_pull"] = pull_result

        # Обновляем индекс опубликованных статей: архитектор и критик его читают.
        # Без актуального индекса — галлюцинации slug-ов и 404-ссылки.
        _refresh_published_index()

        # Готовим prev_summary последней статьи категории (для антишаблонности
        # агента 4). Лёгкий JSON вместо чтения полного текста соседа —
        # экономия 3-6k токенов на писателя.
        # exclude_slug: при rewrite не сравниваем статью саму с собой.
        _exclude = retry_slug if slot_mode == "rewrite" else None
        _refresh_prev_summary(category, exclude_slug=_exclude)

        # Выбираем команду: новая статья или доработка зависшей failed_qa
        if slot_mode == "rewrite" and retry_slug:
            claude_command = f"/rewrite-article {retry_slug}"
            entry["mode"] = "rewrite"
            entry["retry_slug"] = retry_slug
        else:
            claude_command = f"/write-article {category}"
            entry["mode"] = "new"

        cmd = [
            "claude",
            "--print",
            "--dangerously-skip-permissions",
            claude_command,
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
        # При rewrite slug известен заранее (берём из retry_slug),
        # при new — обнаруживаем по mtime новых драфтов.
        if slot_mode == "rewrite" and retry_slug:
            slug = retry_slug if ok else None
        else:
            slug = _detect_new_slug(started) if ok else None
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

        gate_passed = gate is None or gate.get("passed", False) or not gate.get("ran", False)
        # Если gate не запустился из-за отсутствия article.html — это уже сигнал
        # что pipeline не дошёл до публикатора, помечаем как failed_qa.
        if gate is not None and not gate.get("passed", False):
            gate_passed = False

        if ok and slug and not gate_passed:
            entry.update({
                "status": "failed_qa",
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
        else:
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
