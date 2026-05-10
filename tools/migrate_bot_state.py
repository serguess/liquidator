"""
Разовая миграция при переезде бота на VPS.

Что чинит: на Cloud Apps watcher регистрировал draft в bot_state.json:reviews
при первом уведомлении, но bot сам не коммитит — ждёт scheduler/publisher.
Если между регистрацией и коммитом был redeploy Cloud Apps (working tree
обнулялся), запись в reviews терялась. На VPS клонировался git без этих
записей. Старые уведомления в TG ссылаются на slug-и которых на VPS
нет в reviews → кнопки «Правки»/«Опубликовать»/«Отклонить» отвечают
«Статья не найдена в базе бота».

Что делает скрипт:
- Идёт по всем drafts/{slug}/ с meta.ready_for_review=true.
- Если slug отсутствует в bot_state.json:reviews — добавляет запись
  через state.add_review (category, title, version из meta.json).
- Создаёт sentinel `.notified` (если ещё нет) — чтобы watcher не
  отправил повторное уведомление.
- Создаёт sentinel `.pushed` (если ещё нет) — чтобы REQUIRE_PUSHED_SENTINEL
  не блокировал watcher для этих папок.

Идемпотентен: повторный запуск ничего не ломает, просто пропускает то
что уже сделано.

Запуск:
    cd ~/apps/liquidator
    source .venv/bin/activate
    python -m tools.migrate_bot_state
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

# Импортируем bot.state ПОСЛЕ load_dotenv
sys.path.insert(0, str(ROOT))
from bot import state, notified_sentinel  # noqa: E402
from bot.config import DRAFTS_DIR  # noqa: E402


def _read_meta(folder: Path) -> dict:
    meta_path = folder / "meta.json"
    if not meta_path.exists():
        return {}
    try:
        return json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def main() -> int:
    if not DRAFTS_DIR.exists():
        print(f"DRAFTS_DIR не найдена: {DRAFTS_DIR}")
        return 1

    known = state.known_slugs()
    print(f"В bot_state.json уже зарегистрировано reviews: {len(known)}")

    restored = 0
    notified_created = 0
    pushed_created = 0
    skipped = 0

    for sub in sorted(DRAFTS_DIR.iterdir()):
        if not sub.is_dir() or sub.name.startswith("_") or sub.name.startswith("."):
            continue

        slug = sub.name
        meta = _read_meta(sub)

        if not meta.get("ready_for_review"):
            skipped += 1
            continue

        # 1. Восстановление записи в reviews
        if slug not in known:
            category = meta.get("category", "fiz")
            title = meta.get("title") or meta.get("h1") or slug
            try:
                state.add_review(slug, category=category, title=title, version="2.0")
                restored += 1
                print(f"  [+ review] {slug} ({category}) — {title[:60]}")
            except Exception as exc:
                print(f"  [! review] {slug}: {exc}")
                continue

        # 2. Sentinel .notified — чтобы watcher не отправил повторное уведомление
        if not notified_sentinel.is_notified(sub):
            try:
                if notified_sentinel.mark_notified(
                    sub, chat_ids=(), title=meta.get("title", ""), bootstrap=True
                ):
                    notified_created += 1
            except Exception as exc:
                print(f"  [! .notified] {slug}: {exc}")

        # 3. Sentinel .pushed — чтобы REQUIRE_PUSHED_SENTINEL не блокировал
        #    (статья давно в репо, push был уже на Cloud Apps).
        pushed_file = sub / ".pushed"
        if not pushed_file.exists():
            try:
                pushed_file.write_text(
                    f"{datetime.now().isoformat(timespec='seconds')} (migrated from Cloud Apps)\n",
                    encoding="utf-8",
                )
                pushed_created += 1
            except OSError as exc:
                print(f"  [! .pushed] {slug}: {exc}")

    print()
    print("=== Итого ===")
    print(f"  Восстановлено записей в reviews: {restored}")
    print(f"  Создано .notified sentinel: {notified_created}")
    print(f"  Создано .pushed sentinel: {pushed_created}")
    print(f"  Пропущено (нет ready_for_review): {skipped}")
    print()
    if restored or notified_created or pushed_created:
        print("Теперь старые уведомления в TG будут корректно отвечать на кнопки.")
        print("Повторные уведомления НЕ придут (sentinel .notified предотвращает).")
    else:
        print("Нечего мигрировать, всё уже на месте.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
