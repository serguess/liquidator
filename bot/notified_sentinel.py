"""
Sentinel-файл в каждой папке драфта: drafts/{slug}/.notified

Назначение: гарантировать, что бот НЕ отправляет повторно уведомления о тех же
драфтах после редеплоя/перезапуска. Это основной механизм дедупликации
уведомлений, дублирующий и подкрепляющий bot_state.json:reviews.

Почему два источника правды:
- bot_state.json — может быть случайно сброшен (ручная правка, конфликт rebase,
  битый JSON → пересоздание с пустым reviews).
- .notified в папке драфта — попадает в каждый git commit автоматически
  (вместе с самой папкой), переживает любой редеплой и любую sync-ошибку.

Если хотя бы один из двух источников помнит про этот slug — повторное
уведомление НЕ отправится.

Структура файла (JSON, ~80 байт):
{
  "notified_at": "2026-05-06T12:34:56",
  "chat_ids": [12345, 67890],
  "version": "2.0",
  "title": "..."
}

Файл создаётся ПОСЛЕ успешного `bot.send_message`. Если send_message упал —
не создаём, чтобы при следующем тике watcher повторил попытку.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Iterable

SENTINEL_FILENAME = ".notified"

log = logging.getLogger(__name__)


def sentinel_path(draft_dir: Path) -> Path:
    """Путь к sentinel-файлу для конкретной папки драфта."""
    return draft_dir / SENTINEL_FILENAME


def is_notified(draft_dir: Path) -> bool:
    """True если sentinel существует. Содержимое не валидируем —
    важен сам факт наличия файла."""
    return sentinel_path(draft_dir).exists()


def mark_notified(draft_dir: Path, *,
                    chat_ids: Iterable[int] = (),
                    version: str = "",
                    title: str = "",
                    bootstrap: bool = False) -> bool:
    """
    Создаёт sentinel-файл. Возвращает True если файл создан/обновлён,
    False если запись не удалась.

    bootstrap=True — пометка что это bootstrap-режим, без реальной отправки
    в Telegram (используется при миграции на новый механизм для уже
    существующих папок).

    Если файл уже существует — обновляем chat_ids (добавляем новых
    получателей если нужно) и timestamp. Это безопасно (idempotent).
    """
    p = sentinel_path(draft_dir)
    chat_ids_list = sorted(set(int(cid) for cid in chat_ids if cid))

    data = {
        "notified_at": datetime.now().isoformat(timespec="seconds"),
        "chat_ids": chat_ids_list,
        "version": version,
        "title": title,
    }
    if bootstrap:
        data["bootstrap"] = True
        data["note"] = (
            "Создано при первой инициализации механизма sentinel'ов: "
            "папка уже существовала, заказчик про неё знает, повторное "
            "уведомление не нужно."
        )

    # Если файл уже есть — мерджим chat_ids (могло прийти новое уведомление
    # от другого инстанса)
    if p.exists():
        try:
            existing = json.loads(p.read_text(encoding="utf-8"))
            existing_ids = set(existing.get("chat_ids") or [])
            data["chat_ids"] = sorted(existing_ids | set(chat_ids_list))
            # Сохраняем оригинальный notified_at если было
            if existing.get("notified_at") and not bootstrap:
                data["notified_at"] = existing["notified_at"]
            # Bootstrap-флаг сохраняется только если ВСЕ записи были bootstrap
            if data.get("bootstrap") and not existing.get("bootstrap"):
                data.pop("bootstrap", None)
                data.pop("note", None)
        except (json.JSONDecodeError, OSError):
            pass  # перезапишем

    try:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        return True
    except OSError as exc:
        log.warning("Не смог создать sentinel %s: %s", p, exc)
        return False


def remove_sentinel(draft_dir: Path) -> bool:
    """Удалить sentinel — например, для повторной отправки уведомления.
    Используется крайне редко (только для отладки)."""
    p = sentinel_path(draft_dir)
    if not p.exists():
        return False
    try:
        p.unlink()
        return True
    except OSError:
        return False
