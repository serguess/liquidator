"""
Pipeline-логгер: timeline всех агентов для одной статьи.

Каждый агент в конце своей работы дописывает событие в
`drafts/{slug}/_pipeline.log.json`. Scheduler также пишет туда события про
старт/конец слота и git push.

Файл — JSON-документ со списком событий в хронологическом порядке.
Append-only (кроме summary в верхнеуровневых полях, которое перезаписывается).

Формат файла:
{
  "slug": "kak-otmenit-sudebnyj-prikaz",
  "category": "vzysk",
  "started_at": "2026-05-04T13:01:00",
  "finished_at": null,
  "total_duration_sec": null,
  "current_iteration": 1,
  "events": [
    {
      "ts": "2026-05-04T13:01:00",
      "agent": "1-semantics",
      "event": "started",
      "iteration": 1,
      ...
    },
    ...
  ]
}

Запуск как CLI (агенты используют этот режим):

    # Старт работы
    python -m tools.pipeline_log {slug} {agent} started

    # Завершение работы (с указанием output-файла и резюме)
    python -m tools.pipeline_log {slug} {agent} completed \
        --output-file brief.json \
        --summary "main_keyword=отмена приказа, intent=hot, writer_route=B" \
        --duration-sec 90

    # Возврат на писателя из агента 5/6/quality_gate
    python -m tools.pipeline_log {slug} {agent} iteration_returned \
        --reason "structure_overlap > 40%" \
        --recommendation "rewrite_with_angle:mythology"

    # Падение
    python -m tools.pipeline_log {slug} {agent} failed \
        --error "WebSearch quota exceeded"

Использование как Python-модуль (scheduler):

    from tools.pipeline_log import log_event, init_pipeline, finalize_pipeline

    init_pipeline(slug, category="vzysk")
    log_event(slug, "scheduler", "slot_started", today_count=3)
    ...
    finalize_pipeline(slug, status="ok")
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
from dataclasses import dataclass
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

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _pipeline_path(slug: str) -> Path:
    return DRAFTS_DIR / slug / "_pipeline.log.json"


def _read_pipeline(slug: str) -> dict:
    path = _pipeline_path(slug)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_pipeline(slug: str, data: dict) -> None:
    path = _pipeline_path(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp.replace(path)


def init_pipeline(slug: str, category: str | None = None) -> None:
    """
    Инициализирует pipeline-лог для статьи. Идемпотентно — если файл уже есть,
    обновляет верхнеуровневые поля, но не сбрасывает events.
    """
    with _lock:
        data = _read_pipeline(slug)
        if not data:
            data = {
                "slug": slug,
                "category": category,
                "started_at": _now_iso(),
                "finished_at": None,
                "total_duration_sec": None,
                "current_iteration": 1,
                "events": [],
            }
        else:
            if category and not data.get("category"):
                data["category"] = category
            if not data.get("started_at"):
                data["started_at"] = _now_iso()
        _write_pipeline(slug, data)


def log_event(
    slug: str,
    agent: str,
    event: str,
    **fields,
) -> None:
    """
    Дописывает событие в pipeline-лог.

    agent: '1-semantics' / '2-legal-research' / ... / 'quality_gate' / 'scheduler'
    event: 'started' / 'completed' / 'failed' / 'iteration_returned' /
           'slot_started' / 'slot_finished' / 'git_pushed' / 'git_push_failed'

    fields: произвольные ключ-значение, попадают в JSON события.
    """
    if not slug:
        return
    with _lock:
        data = _read_pipeline(slug)
        if not data:
            data = {
                "slug": slug,
                "category": fields.pop("category", None),
                "started_at": _now_iso(),
                "finished_at": None,
                "total_duration_sec": None,
                "current_iteration": 1,
                "events": [],
            }

        # Авто-инкремент current_iteration при возврате на писателя
        if event == "iteration_returned":
            data["current_iteration"] = data.get("current_iteration", 1) + 1

        entry = {
            "ts": _now_iso(),
            "agent": agent,
            "event": event,
            "iteration": data.get("current_iteration", 1),
        }
        # Добавляем переданные поля (не None)
        for k, v in fields.items():
            if v is not None and v != "":
                entry[k] = v

        data["events"].append(entry)
        _write_pipeline(slug, data)


def finalize_pipeline(slug: str, status: str, **fields) -> None:
    """Закрывает лог: ставит finished_at и считает total_duration_sec."""
    with _lock:
        data = _read_pipeline(slug)
        if not data:
            return
        data["finished_at"] = _now_iso()
        if data.get("started_at"):
            try:
                start = datetime.fromisoformat(data["started_at"])
                end = datetime.fromisoformat(data["finished_at"])
                data["total_duration_sec"] = round((end - start).total_seconds(), 1)
            except ValueError:
                pass
        data["final_status"] = status
        for k, v in fields.items():
            if v is not None and v != "":
                data[k] = v
        # Финальное событие
        data["events"].append({
            "ts": _now_iso(),
            "agent": "scheduler",
            "event": "slot_finished",
            "status": status,
            "iteration": data.get("current_iteration", 1),
        })
        _write_pipeline(slug, data)


# === CLI ===

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Pipeline logger: записывает события агентов в drafts/{slug}/_pipeline.log.json"
    )
    parser.add_argument("slug", help="slug статьи (drafts/{slug}/)")
    parser.add_argument("agent", help="имя агента (1-semantics, 2-legal-research, ..., quality_gate, scheduler)")
    parser.add_argument("event", help="тип события (started, completed, failed, iteration_returned, ...)")
    parser.add_argument("--category", help="категория (для init)")
    parser.add_argument("--output-file", help="имя файла, который положил агент (brief.json, research.json, ...)")
    parser.add_argument("--input-files", help="имена входных файлов через запятую")
    parser.add_argument("--summary", help="короткое резюме что сделал агент (≤200 символов)")
    parser.add_argument("--duration-sec", type=float, help="длительность работы в секундах")
    parser.add_argument("--reason", help="причина возврата/падения")
    parser.add_argument("--recommendation", help="рекомендация для следующей итерации")
    parser.add_argument("--error", help="текст ошибки")
    parser.add_argument("--blockers", help="блокеры через запятую (для quality_gate)")
    parser.add_argument("--metric", action="append", default=[],
                        help="метрика в формате key=value (можно несколько раз)")
    args = parser.parse_args()

    fields = {}
    if args.category:
        fields["category"] = args.category
    if args.output_file:
        fields["output_file"] = args.output_file
    if args.input_files:
        fields["input_files"] = [s.strip() for s in args.input_files.split(",") if s.strip()]
    if args.summary:
        fields["summary"] = args.summary[:200]
    if args.duration_sec is not None:
        fields["duration_sec"] = round(args.duration_sec, 1)
    if args.reason:
        fields["reason"] = args.reason
    if args.recommendation:
        fields["recommendation"] = args.recommendation
    if args.error:
        fields["error"] = args.error[:500]
    if args.blockers:
        fields["blockers"] = [s.strip() for s in args.blockers.split(",") if s.strip()]
    if args.metric:
        metrics = {}
        for m in args.metric:
            if "=" in m:
                k, v = m.split("=", 1)
                metrics[k.strip()] = v.strip()
        if metrics:
            fields["metrics"] = metrics

    # Если событие 'started' и слаг новый — init
    if args.event == "started" and not _pipeline_path(args.slug).exists():
        init_pipeline(args.slug, category=args.category)

    log_event(args.slug, args.agent, args.event, **fields)
    print(f"OK: {args.slug} / {args.agent} / {args.event}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
