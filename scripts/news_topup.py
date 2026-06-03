"""
Профилактическое пополнение news-тем.

Запускается еженедельно (воскресенье 04:00 МСК) через systemd timer.
Делает две вещи:
  1. /expand-topics news - генерит свежие news-темы с помощью Claude
  2. news-sanitize - чистит устаревшие темы (event_too_old, нет обязательных полей)

Цель: не давать news-слотам тратить время на expand-topics. Если topic-map
news всегда свежий, регулярные слоты идут быстро (35-45 мин вместо 85+).

Запуск:
  python -m scripts.news_topup           # реальный запуск
  python -m scripts.news_topup --dry-run # только проверка состояния topic-map

Безопасность:
  - Если scheduler.lock активен - ждёт до 30 мин
  - timeout на claude subprocess = 60 мин (с запасом)
  - news-sanitize вызывается только если /expand-topics rc=0
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT = Path(__file__).resolve().parent.parent
TOPIC_MAP_NEWS = ROOT / "drafts" / "_topic-map" / "news.json"
LOCK_FILE = ROOT / "data" / ".scheduler.lock"

EXPAND_TIMEOUT_SEC = 3600  # 60 мин на /expand-topics
LOCK_WAIT_TIMEOUT_SEC = 1800  # 30 мин ждать освобождения lock'а


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[news_topup {_ts()}] {msg}", flush=True)


def count_news_topics() -> dict:
    """Считает темы в news.json по статусу и event_date."""
    if not TOPIC_MAP_NEWS.exists():
        return {"total": 0, "active": 0, "rejected": 0, "no_event_date": 0}
    try:
        data = json.loads(TOPIC_MAP_NEWS.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"total": 0, "active": 0, "rejected": 0, "no_event_date": 0}

    topics = data.get("topics", [])
    active = 0
    rejected = 0
    no_event_date = 0
    for t in topics:
        if t.get("status") == "rejected":
            rejected += 1
        else:
            active += 1
            if not t.get("event_date"):
                no_event_date += 1

    return {
        "total": len(topics),
        "active": active,
        "rejected": rejected,
        "no_event_date": no_event_date,
    }


def wait_for_lock_free(timeout_sec: int) -> bool:
    """Ждёт пока scheduler-lock не освободится."""
    if not LOCK_FILE.exists():
        return True
    _log(f"Жду освобождения scheduler.lock (max {timeout_sec}s)...")
    start = time.time()
    while LOCK_FILE.exists():
        if time.time() - start > timeout_sec:
            return False
        time.sleep(15)
    _log(f"Lock освободился через {int(time.time() - start)}s")
    return True


def run_expand_topics() -> int:
    """Запускает claude /expand-topics news. Возвращает returncode."""
    cmd = [
        "claude",
        "--print",
        "--dangerously-skip-permissions",
        "/expand-topics news",
    ]
    _log(f"Запускаю: {' '.join(cmd)}")
    start = time.time()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(ROOT),
            timeout=EXPAND_TIMEOUT_SEC,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        _log(f"TIMEOUT после {EXPAND_TIMEOUT_SEC}s")
        return -1

    duration = int(time.time() - start)
    _log(f"claude finished rc={result.returncode} duration={duration}s "
         f"stdout_chars={len(result.stdout or '')} stderr_chars={len(result.stderr or '')}")

    if result.returncode != 0:
        _log(f"STDERR tail: {(result.stderr or '')[-300:]}")

    return result.returncode


def run_news_sanitize() -> int:
    """Вызывает _sanitize_news_topics через runner.py (импорт)."""
    try:
        # Импорт делается лениво, чтобы скрипт могли запускать без полной env
        sys.path.insert(0, str(ROOT))
        from articles_scheduler.runner import _sanitize_news_topics  # type: ignore
        cleaned = _sanitize_news_topics()
        _log(f"news-sanitize: {cleaned} тем помечены rejected")
        return cleaned
    except Exception as e:
        _log(f"news-sanitize упал: {e}")
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Weekly news topic top-up")
    p.add_argument("--dry-run", action="store_true",
                   help="Не запускать /expand-topics, только показать текущее состояние")
    args = p.parse_args()

    before = count_news_topics()
    _log(f"Состояние news.json ДО: total={before['total']} "
         f"active={before['active']} rejected={before['rejected']} "
         f"no_event_date={before['no_event_date']}")

    if args.dry_run:
        _log("DRY-RUN: реально не запускаю /expand-topics")
        return 0

    # Сначала чистим устаревшие
    cleaned_before = run_news_sanitize()

    # Ждём scheduler.lock
    if not wait_for_lock_free(LOCK_WAIT_TIMEOUT_SEC):
        _log(f"STOP — lock не освободился за {LOCK_WAIT_TIMEOUT_SEC}s")
        return 1

    # Запускаем /expand-topics news
    rc = run_expand_topics()
    if rc != 0:
        _log(f"FAIL — /expand-topics вернул rc={rc}, выхожу")
        return rc

    # После expand - снова sanitize (модель может сгенерить кривые темы)
    cleaned_after = run_news_sanitize()

    after = count_news_topics()
    _log(f"Состояние news.json ПОСЛЕ: total={after['total']} "
         f"active={after['active']} rejected={after['rejected']} "
         f"no_event_date={after['no_event_date']}")
    _log(f"Sanitize cleaned: до expand={cleaned_before}, после expand={cleaned_after}")
    _log(f"OK — news-topup завершён")
    return 0


if __name__ == "__main__":
    sys.exit(main())
