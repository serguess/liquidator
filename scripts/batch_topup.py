"""
Pre-batch safety net.

Запускается за час-полтора до 10:00 МСК batch'а через systemd timer.
Проверяет сколько статей в pending_batch. Если меньше 10 - запускает
недостающие слоты по нужным категориям до cutoff (за 30 мин до batch'а).

Ожидаемое распределение: 3 fiz, 3 yur, 3 vzysk, 1 news = 10.

Запуск:
  python -m scripts.batch_topup            # реальный запуск backup-слотов
  python -m scripts.batch_topup --dry-run  # только показать что не хватает
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
BOT_STATE = ROOT / "data" / "bot_state.json"
LOCK_FILE = ROOT / "data" / ".scheduler.lock"

# Целевое распределение: 3-3-3-1 = 10 статей
EXPECTED = {"fiz": 3, "yur": 3, "vzysk": 3, "news": 1}
# Приоритет при выборе категории для backup-слота (если несколько не хватает)
PRIORITY = ["fiz", "yur", "vzysk", "news"]

# Час начала batch'а
BATCH_HOUR = 10
# Не запускать новый слот, если до batch'а осталось меньше N минут
# (типичный слот 30-50 мин, news с /expand-topics до 85 мин)
CUTOFF_MIN_BEFORE_BATCH = 25

# Сколько ждать пока освободится scheduler-lock от текущего слота
LOCK_WAIT_TIMEOUT_SEC = 1200  # 20 мин


def _now() -> datetime:
    return datetime.now()


def _ts() -> str:
    return _now().strftime("%H:%M:%S")


def _log(msg: str) -> None:
    print(f"[batch_topup {_ts()}] {msg}", flush=True)


def count_pending() -> dict[str, int]:
    """Считает статьи в pending_batch (без tg_message_id) по категориям."""
    try:
        state = json.loads(BOT_STATE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        _log(f"ERROR: не смог прочитать bot_state.json: {e}")
        return {}
    counts: dict[str, int] = {}
    for slug, r in (state.get("reviews") or {}).items():
        if r.get("pending_batch") and not r.get("tg_message_id"):
            cat = r.get("category", "?")
            counts[cat] = counts.get(cat, 0) + 1
    return counts


def get_missing() -> list[str]:
    """Список категорий которых не хватает (с дубликатами, в порядке приоритета)."""
    pending = count_pending()
    missing: list[str] = []
    for cat in PRIORITY:
        have = pending.get(cat, 0)
        needed = EXPECTED.get(cat, 0)
        gap = needed - have
        if gap > 0:
            missing.extend([cat] * gap)
    return missing


def remaining_minutes_to_batch() -> int:
    """Сколько минут до 10:00 МСК (или -1 если уже прошло)."""
    now = _now()
    target = now.replace(hour=BATCH_HOUR, minute=0, second=0, microsecond=0)
    if target < now:
        return -1
    return int((target - now).total_seconds() / 60)


def wait_for_lock_free(timeout_sec: int) -> bool:
    """Ждёт пока scheduler-lock не освободится. True если освободился."""
    start = time.time()
    while LOCK_FILE.exists():
        elapsed = time.time() - start
        if elapsed > timeout_sec:
            return False
        time.sleep(15)
    return True


def run_backup_slot(category: str, env: dict[str, str]) -> int:
    """Запускает backup-слот для категории. Возвращает returncode."""
    cmd = [
        str(ROOT / ".venv/bin/python"),
        "-m", "articles_scheduler.runner",
        "--category", category,
    ]
    result = subprocess.run(cmd, env=env, cwd=str(ROOT))
    return result.returncode


def main() -> int:
    p = argparse.ArgumentParser(description="Pre-batch safety net")
    p.add_argument("--dry-run", action="store_true",
                   help="Не запускать слоты, только показать что не хватает")
    args = p.parse_args()

    pending = count_pending()
    missing = get_missing()
    total_pending = sum(pending.values())

    _log(f"Pending now: total={total_pending} (fiz={pending.get('fiz', 0)} "
         f"yur={pending.get('yur', 0)} vzysk={pending.get('vzysk', 0)} "
         f"news={pending.get('news', 0)})")

    if not missing:
        _log("OK — 10 статей уже в очереди, ничего не запускаю")
        return 0

    _log(f"Не хватает {len(missing)}: {missing}")

    if args.dry_run:
        _log("DRY-RUN: реально не запускаю")
        return 0

    # Bump ARTICLES_PER_DAY в env подпроцессов чтобы backup не упёрся в лимит.
    # load_dotenv в runner.py использует override=False, поэтому установленный
    # здесь env прибьёт значение из .env.
    env = os.environ.copy()
    try:
        current_limit = int(env.get("ARTICLES_PER_DAY", "10"))
    except ValueError:
        current_limit = 10
    new_limit = current_limit + len(missing) + 2  # +2 буфер
    env["ARTICLES_PER_DAY"] = str(new_limit)
    _log(f"ARTICLES_PER_DAY bumped to {new_limit} для backup-подпроцессов")

    # Добиваем до 10 по РЕАЛЬНОЙ очереди, а не по rc слота. Раньше цикл шёл по
    # фиксированному `missing` и считал слот успешным при rc=0 — но слот может
    # вернуть rc=0 и при этом НЕ поставить статью в очередь (finalize_draft
    # отбраковал по factcheck/qa). Тогда очередь оставалась короткой (инцидент
    # 19 июня: вышло 9, фактчек завернул одну vzysk). Теперь после каждого слота
    # пересчитываем get_missing() и продолжаем, пока очередь не укомплектована
    # ИЛИ не упёрлись в cutoff/лимит попыток.
    MAX_BACKUP_ATTEMPTS = len(missing) + 5  # запас на отбраковки, защита от вечного цикла
    attempts = 0
    while True:
        missing_now = get_missing()
        if not missing_now:
            _log("OK — очередь укомплектована (10/10)")
            break
        if attempts >= MAX_BACKUP_ATTEMPTS:
            _log(f"STOP — исчерпан лимит попыток ({MAX_BACKUP_ATTEMPTS}). "
                 f"Не хватает: {missing_now}")
            break

        rem = remaining_minutes_to_batch()
        if rem < CUTOFF_MIN_BEFORE_BATCH:
            _log(f"STOP — осталось {rem} мин до batch (cutoff={CUTOFF_MIN_BEFORE_BATCH}). "
                 f"Не хватает: {missing_now}")
            break

        _log(f"Жду освобождения lock'а (max {LOCK_WAIT_TIMEOUT_SEC}s)...")
        if not wait_for_lock_free(LOCK_WAIT_TIMEOUT_SEC):
            _log(f"STOP — lock не освободился за {LOCK_WAIT_TIMEOUT_SEC}s")
            break

        cat = missing_now[0]  # самая приоритетная недостающая категория
        attempts += 1
        before = sum(count_pending().values())
        _log(f"Backup-слот #{attempts}: {cat} (не хватает {len(missing_now)}, "
             f"очередь {before}/10, осталось {rem} мин)")
        rc = run_backup_slot(cat, env)
        after = sum(count_pending().values())
        # Успех = статья РЕАЛЬНО встала в очередь (after>before), а не просто rc=0.
        if after > before:
            _log(f"  OK: {cat} → очередь {after}/10")
        else:
            _log(f"  слот не дал статью (rc={rc}, очередь {after}/10) — "
                 f"вероятно отбраковка factcheck/qa, пробую снова другой темой")

    final_total = sum(count_pending().values())
    _log(f"Итог: очередь {final_total}/10 за {attempts} попыток")

    return 0 if final_total >= 10 else 1


if __name__ == "__main__":
    sys.exit(main())
