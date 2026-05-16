"""
Детерминированный выбор шаблона обложки (1-30) из cover-scenes.md
с гарантией ротации: исключаем последние N использованных шаблонов
и выбираем random из оставшихся.

Запускается в pipeline /write-article ПЕРЕД агентом 7-publisher.
Записывает выбор в drafts/{slug}/scene_template.txt — оттуда агент 7
читает template_id и только ОПИСЫВАЕТ выбранный шаблон под смысл статьи
(адаптирует объекты из allowed pool под тему).

История последних 30 выборов хранится в data/scene_history.json:
{
  "history": [
    {"slug": "...", "template_id": 7, "category": "fiz", "ts": "2026-05-16T17:12:00"},
    ...
  ]
}

Почему это нужно (16 мая 2026):
Раньше выбор делал sonnet-агент 7 — он имел инструкцию «избегай последних 5»,
но регулярно сваливался на категорийный default (fiz→10, yur→25, vzysk→3,
news→12). В итоге у всех fiz-статей был flat-lay (template 10), у всех yur —
executive office (25). Однотипные обложки → визуально не отличимые статьи
на сайте.

Запуск:
    python -m articles_scheduler.pick_scene_template <slug> [category]

Exit codes:
    0 — выбран template_id, записан в drafts/{slug}/scene_template.txt
    1 — ошибка (нет папки drafts/{slug}, не получилось записать)

stdout: «picked template_id=N (was excluded: [...])»
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Сцен ровно 30 (см. .claude/style/cover-scenes.md).
TOTAL_SCENES = 30
SCENE_IDS = list(range(1, TOTAL_SCENES + 1))

# Сколько последних использований исключать из пула.
# Батч = 10 статей/день. Чтобы в одном батче не было повторов, нужно >= 10.
# Берём 12 (батч + 2 запаса). При пуле 30 это оставляет 18 кандидатов
# каждый раз — достаточно для разнообразия, и через ~2.5 дня все 30 пройдут.
EXCLUDE_LAST_N = 12

HISTORY_PATH = PROJECT_ROOT / "data" / "scene_history.json"
HISTORY_MAX = 60  # храним последние 60 записей (2× max разумного окна)


def load_history() -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    try:
        raw = json.loads(HISTORY_PATH.read_text(encoding="utf-8"))
        hist = raw.get("history", [])
        if not isinstance(hist, list):
            return []
        return hist
    except (json.JSONDecodeError, OSError):
        return []


def save_history(history: list[dict]) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    trimmed = history[-HISTORY_MAX:]
    payload = {"history": trimmed}
    HISTORY_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def pick_template_id(history: list[dict], rng: random.Random) -> tuple[int, list[int]]:
    """Возвращает (выбранный_id, исключённые_ids_окно).

    Алгоритм:
      1. Берём последние EXCLUDE_LAST_N template_id из истории.
      2. Кандидаты = SCENE_IDS \ исключённые.
      3. Random из кандидатов. Если пусто (edge case — история перенасыщена) —
         random из всех 30 (fallback, не должен срабатывать).
    """
    recent_window = history[-EXCLUDE_LAST_N:] if history else []
    excluded = {entry.get("template_id") for entry in recent_window if isinstance(entry.get("template_id"), int)}
    candidates = [tid for tid in SCENE_IDS if tid not in excluded]
    if not candidates:
        candidates = SCENE_IDS
    return rng.choice(candidates), sorted(excluded)


def write_scene_template_file(slug: str, template_id: int) -> Path:
    """Записывает drafts/{slug}/scene_template.txt в формате template_id=N."""
    drafts_dir = PROJECT_ROOT / "drafts" / slug
    drafts_dir.mkdir(parents=True, exist_ok=True)
    target = drafts_dir / "scene_template.txt"
    target.write_text(f"template_id={template_id}\n", encoding="utf-8")
    return target


def detect_category(slug: str, fallback: str | None = None) -> str | None:
    """Пытается достать category из drafts/{slug}/meta.json или brief.json."""
    for name in ("meta.json", "brief.json"):
        path = PROJECT_ROOT / "drafts" / slug / name
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            category = data.get("category")
            if isinstance(category, str):
                return category
        except (json.JSONDecodeError, OSError):
            continue
    return fallback


def main() -> int:
    parser = argparse.ArgumentParser(description="Pick cover scene template (1-30) with rotation")
    parser.add_argument("slug", help="Article slug — folder name in drafts/")
    parser.add_argument("category", nargs="?", default=None, help="Optional category override (fiz/yur/vzysk/news)")
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional RNG seed for reproducible tests. Default: None (true random).",
    )
    args = parser.parse_args()

    slug = args.slug.strip()
    if not slug or "/" in slug or slug.startswith("."):
        print(f"ERROR: invalid slug '{slug}'", file=sys.stderr)
        return 1

    history = load_history()
    rng = random.Random(args.seed) if args.seed is not None else random.Random()
    template_id, excluded = pick_template_id(history, rng)

    try:
        target_path = write_scene_template_file(slug, template_id)
    except OSError as exc:
        print(f"ERROR: cannot write scene_template.txt: {exc}", file=sys.stderr)
        return 1

    category = args.category or detect_category(slug)
    entry = {
        "slug": slug,
        "template_id": template_id,
        "category": category,
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    history.append(entry)
    try:
        save_history(history)
    except OSError as exc:
        print(f"WARN: cannot save history: {exc}", file=sys.stderr)

    excluded_str = ",".join(str(t) for t in excluded) if excluded else "none"
    print(f"picked template_id={template_id} excluded=[{excluded_str}] history_len={len(history)}")
    print(f"written: {target_path.relative_to(PROJECT_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
