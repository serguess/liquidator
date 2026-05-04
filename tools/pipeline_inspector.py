"""
Pipeline-инспектор: смотрит логи pipeline и scheduler в человекочитаемом виде.

Источники:
- `drafts/*/_pipeline.log.json` — детальная timeline по статьям
- `data/scheduler_log.json` — лог слотов scheduler'а
- `data/git_errors.log` — полные stderr ошибок git

Команды:

    # Последние N статей с timeline (по умолчанию 5)
    python -m tools.pipeline_inspector --last 5

    # Конкретная статья (полная timeline всех событий)
    python -m tools.pipeline_inspector --slug kak-otmenit-sudebnyj-prikaz

    # Последние ошибки git
    python -m tools.pipeline_inspector --git-errors

    # Сводная статистика по агентам (среднее время, частота возвратов, фейлов)
    python -m tools.pipeline_inspector --stats

    # Только слоты scheduler'а
    python -m tools.pipeline_inspector --slots --last 20

Можно комбинировать (например `--slug X --json` для машинного чтения).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = PROJECT_ROOT / "drafts"
DATA_DIR = PROJECT_ROOT / "data"
SCHEDULER_LOG_PATH = DATA_DIR / "scheduler_log.json"
GIT_ERRORS_LOG_PATH = DATA_DIR / "git_errors.log"


def _load_pipeline_logs() -> list[dict]:
    """Загружает все _pipeline.log.json из drafts/, отсортированные по started_at."""
    logs: list[dict] = []
    if not DRAFTS_DIR.exists():
        return logs
    for path in DRAFTS_DIR.glob("*/_pipeline.log.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data["_path"] = str(path.relative_to(PROJECT_ROOT))
            logs.append(data)
        except (json.JSONDecodeError, OSError):
            continue
    logs.sort(key=lambda x: x.get("started_at") or "", reverse=True)
    return logs


def _load_scheduler_log() -> list[dict]:
    if not SCHEDULER_LOG_PATH.exists():
        return []
    try:
        return json.loads(SCHEDULER_LOG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []


def _status_marker(status: str) -> str:
    """Текстовый маркер статуса (без эмодзи для совместимости с консолью)."""
    return {
        "ok": "[OK]      ",
        "completed": "[OK]      ",
        "passed": "[OK]      ",
        "started": "[..]      ",
        "failed": "[FAIL]    ",
        "failed_qa": "[FAIL_QA] ",
        "iteration_returned": "[RETURN]  ",
        "git_pushed": "[OK]      ",
        "git_push_failed": "[GIT FAIL]",
        "slot_started": "[SLOT]    ",
        "slot_finished": "[SLOT]    ",
        "exception": "[EXCEPT]  ",
        "timeout": "[TIMEOUT] ",
        "limit_reached": "[LIMIT]   ",
        "paused": "[PAUSE]   ",
        "locked": "[LOCKED]  ",
    }.get(status, f"[{status[:8]:8s}] ")


def _short_ts(ts: str) -> str:
    if not ts or len(ts) < 16:
        return ts or "-"
    return ts[5:16].replace("T", " ")  # "MM-DD HH:MM"


def _print_pipeline_summary(log: dict, verbose: bool = False) -> None:
    """Краткая сводка одной статьи."""
    slug = log.get("slug", "?")
    cat = log.get("category", "-")
    started = log.get("started_at") or "-"
    finished = log.get("finished_at") or "(в процессе)"
    duration = log.get("total_duration_sec")
    status = log.get("final_status", "?")
    iters = log.get("current_iteration", 1)
    events = log.get("events", [])

    # Считаем сколько каждый агент работал и сколько было возвратов на писателя
    agent_durations: dict[str, list[float]] = defaultdict(list)
    returns = 0
    for ev in events:
        ag = ev.get("agent")
        d = ev.get("duration_sec")
        if ag and d is not None:
            agent_durations[ag].append(d)
        if ev.get("event") == "iteration_returned":
            returns += 1

    print(f"\n{'='*68}")
    print(f"  SLUG: {slug}")
    print(f"  Категория: {cat}  |  Статус: {status}  |  Итераций: {iters}  |  Возвратов: {returns}")
    print(f"  Старт: {_short_ts(started)}  →  Финиш: {_short_ts(finished)}", end="")
    if duration is not None:
        m, s = divmod(int(duration), 60)
        print(f"  ({m}м {s}с)")
    else:
        print()

    # Время по агентам
    if agent_durations:
        print(f"\n  Время по агентам:")
        for ag in sorted(agent_durations):
            ds = agent_durations[ag]
            total = sum(ds)
            print(f"    {ag:20s}  {len(ds)}× = {total:5.0f}с")

    if verbose:
        print(f"\n  Timeline ({len(events)} событий):")
        for ev in events:
            ts = _short_ts(ev.get("ts", ""))
            ag = ev.get("agent", "?")
            evt = ev.get("event", "?")
            mark = _status_marker(evt)
            extra = ""
            if ev.get("summary"):
                extra = f"  {ev['summary'][:60]}"
            elif ev.get("reason"):
                extra = f"  reason={ev['reason'][:60]}"
            elif ev.get("error"):
                extra = f"  error={ev['error'][:60]}"
            elif ev.get("blockers"):
                extra = f"  blockers={ev['blockers']}"
            elif ev.get("recommendation"):
                extra = f"  rec={ev['recommendation'][:60]}"
            d = ev.get("duration_sec")
            d_str = f" ({d}с)" if d else ""
            print(f"    {ts}  {mark} {ag:20s}  {evt:20s}{d_str}{extra}")


def cmd_last(n: int, verbose: bool = False) -> None:
    logs = _load_pipeline_logs()
    if not logs:
        print(f"Pipeline-логов не найдено (ожидаются в {DRAFTS_DIR}/*/_pipeline.log.json)")
        return
    print(f"\nПоследние {min(n, len(logs))} статей по started_at:")
    for log in logs[:n]:
        _print_pipeline_summary(log, verbose=verbose)


def cmd_slug(slug: str) -> None:
    path = DRAFTS_DIR / slug / "_pipeline.log.json"
    if not path.exists():
        print(f"Лог не найден: {path}", file=sys.stderr)
        sys.exit(1)
    log = json.loads(path.read_text(encoding="utf-8"))
    log["_path"] = str(path.relative_to(PROJECT_ROOT))
    _print_pipeline_summary(log, verbose=True)


def cmd_git_errors(tail_n: int | None = None) -> None:
    if not GIT_ERRORS_LOG_PATH.exists():
        print(f"Файл {GIT_ERRORS_LOG_PATH} пуст или не существует. Push-ошибок нет.")
        return
    text = GIT_ERRORS_LOG_PATH.read_text(encoding="utf-8")
    blocks = [b for b in text.split("====\n") if b.strip()]
    if tail_n:
        blocks = blocks[-tail_n:]
    print(f"\nGit errors ({len(blocks)} блоков):\n")
    for b in blocks:
        print(b.strip())
        print("-" * 60)


def cmd_slots(n: int = 20) -> None:
    log = _load_scheduler_log()
    if not log:
        print(f"scheduler_log.json пуст или не существует.")
        return
    print(f"\nПоследние {min(n, len(log))} слотов scheduler'а:\n")
    for entry in log[-n:]:
        ts = _short_ts(entry.get("timestamp", ""))
        st = entry.get("status", "?")
        cat = entry.get("category", "-")
        slug = entry.get("slug", "-") or "-"
        dur = entry.get("duration_sec", "-")
        mark = _status_marker(st)
        git = entry.get("git", {})
        git_str = ""
        if git:
            if git.get("pushed"):
                git_str = "  git=pushed"
            elif git.get("reason"):
                git_str = f"  git={git['reason']}"
        print(f"  {ts}  {mark} {cat:6s} {slug:50s}  {dur:>6}s{git_str}")


def cmd_stats() -> None:
    """Сводная статистика по всем pipeline-логам."""
    logs = _load_pipeline_logs()
    if not logs:
        print("Логов нет.")
        return

    total_articles = len(logs)
    total_duration = sum(l.get("total_duration_sec") or 0 for l in logs)
    statuses: dict[str, int] = defaultdict(int)
    agent_times: dict[str, list[float]] = defaultdict(list)
    agent_failures: dict[str, int] = defaultdict(int)
    return_reasons: dict[str, int] = defaultdict(int)
    total_returns = 0

    for log in logs:
        statuses[log.get("final_status", "unknown")] += 1
        for ev in log.get("events", []):
            ag = ev.get("agent")
            d = ev.get("duration_sec")
            if ag and d is not None and ev.get("event") in ("completed", "passed"):
                agent_times[ag].append(d)
            if ev.get("event") == "failed":
                agent_failures[ag] = agent_failures.get(ag, 0) + 1
            if ev.get("event") == "iteration_returned":
                total_returns += 1
                reason = (ev.get("reason") or "?")[:30]
                return_reasons[reason] += 1

    print(f"\n=== Сводная статистика ===")
    print(f"Всего pipeline-логов: {total_articles}")
    if total_duration:
        m, s = divmod(int(total_duration), 60)
        print(f"Суммарное время: {m}м {s}с  ({total_duration / total_articles:.1f}с в среднем на статью)")

    print(f"\nСтатусы статей:")
    for st, n in sorted(statuses.items(), key=lambda x: -x[1]):
        print(f"  {st:20s}  {n}")

    print(f"\nВремя по агентам (среднее / max / запусков):")
    for ag in sorted(agent_times):
        ds = agent_times[ag]
        avg = sum(ds) / len(ds)
        mx = max(ds)
        print(f"  {ag:20s}  avg={avg:5.1f}с  max={mx:5.1f}с  n={len(ds)}")

    if agent_failures:
        print(f"\nПадения по агентам:")
        for ag, n in sorted(agent_failures.items(), key=lambda x: -x[1]):
            print(f"  {ag:20s}  {n}")

    print(f"\nВозвратов на писателя: {total_returns}")
    if return_reasons:
        print(f"Топ причин возврата:")
        for r, n in sorted(return_reasons.items(), key=lambda x: -x[1])[:10]:
            print(f"  {n:3d}× {r}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Pipeline-инспектор")
    parser.add_argument("--last", type=int, default=None,
                        help="Показать последние N статей (по started_at)")
    parser.add_argument("--slug", help="Подробная timeline конкретной статьи")
    parser.add_argument("--git-errors", action="store_true",
                        help="Последние ошибки git push/pull")
    parser.add_argument("--slots", action="store_true",
                        help="Лог слотов scheduler'а")
    parser.add_argument("--stats", action="store_true",
                        help="Сводная статистика по всем агентам")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Подробная timeline (применимо к --last)")
    parser.add_argument("--tail", type=int, default=10,
                        help="Сколько последних git-ошибок показать (default 10)")
    args = parser.parse_args()

    if args.slug:
        cmd_slug(args.slug)
    elif args.git_errors:
        cmd_git_errors(args.tail)
    elif args.slots:
        cmd_slots(args.last or 20)
    elif args.stats:
        cmd_stats()
    elif args.last is not None:
        cmd_last(args.last, verbose=args.verbose)
    else:
        # По умолчанию — последние 5 + статистика
        cmd_last(5, verbose=False)
        print()
        cmd_stats()

    return 0


if __name__ == "__main__":
    sys.exit(main())
