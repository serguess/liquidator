"""
Состояние бота: что в каком статусе, какая последняя версия.

Хранится в data/bot_state.json. Файл коммитится в git (data/ whitelist),
чтобы при редеплое контейнера на Timeweb ничего не терялось.

Структура:
{
  "preview_token": "случайная-строка-для-подписанных-ссылок",
  "reviews": {
    "kak-zakryt-ooo-s-dolgami": {
      "category": "yur",
      "title": "Как закрыть ООО с долгами",
      "status": "pending_review | approved | rejected | published",
      "current_version": "2.1",
      "versions": ["2.0", "2.1"],
      "tg_message_id": 123,            // id сообщения в TG для редактирования
      "tg_chat_id": 12345,
      "first_seen_at": "2026-05-02T12:34:00",
      "last_action_at": "2026-05-02T12:40:00",
      "edits_history": [
        {"version": "2.1", "edit_text": "убери блок про ИП", "applied_at": "..."}
      ],
      "rejection_reason": null,
      "published_url": null
    }
  }
}
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import STATE_FILE, get_preview_token

_lock = threading.Lock()


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _empty_state() -> dict[str, Any]:
    return {
        "preview_token": get_preview_token(),
        "reviews": {},
    }


def load() -> dict[str, Any]:
    """Читает state из JSON. При отсутствии или ошибке - создаёт пустой."""
    with _lock:
        if not STATE_FILE.exists():
            state = _empty_state()
            _save_unsafe(state)
            return state
        try:
            raw = STATE_FILE.read_text(encoding="utf-8")
            state = json.loads(raw)
        except (json.JSONDecodeError, OSError):
            # Битый файл: бэкапим и пересоздаём.
            backup = STATE_FILE.with_suffix(f".broken-{_now_iso().replace(':', '-')}.json")
            try:
                STATE_FILE.rename(backup)
            except OSError:
                pass
            state = _empty_state()
            _save_unsafe(state)
            return state

        # Гарантируем что preview_token есть.
        if not state.get("preview_token"):
            state["preview_token"] = get_preview_token()
            _save_unsafe(state)

        # Гарантируем секцию reviews.
        if "reviews" not in state:
            state["reviews"] = {}
            _save_unsafe(state)

        return state


def _save_unsafe(state: dict[str, Any]) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)


def save(state: dict[str, Any]) -> None:
    with _lock:
        _save_unsafe(state)


# === Высокоуровневые операции ===


def get_preview_token_from_state() -> str:
    return load()["preview_token"]


def get_review(slug: str) -> dict | None:
    return load()["reviews"].get(slug)


def known_slugs() -> set[str]:
    """Слаги, которые бот уже видел (любой статус)."""
    return set(load()["reviews"].keys())


def upsert_review(slug: str, data: dict[str, Any]) -> None:
    state = load()
    existing = state["reviews"].get(slug, {})
    existing.update(data)
    existing["last_action_at"] = _now_iso()
    state["reviews"][slug] = existing
    save(state)


def add_review(slug: str, *, category: str, title: str, version: str = "2.0") -> dict:
    """Регистрирует новый review при обнаружении свежего драфта."""
    state = load()
    review = {
        "category": category,
        "title": title,
        "status": "pending_review",
        "current_version": version,
        "versions": [version],
        "tg_message_id": None,
        "tg_chat_id": None,
        "first_seen_at": _now_iso(),
        "last_action_at": _now_iso(),
        "edits_history": [],
        "rejection_reason": None,
        "published_url": None,
    }
    state["reviews"][slug] = review
    save(state)
    return review


def add_edit(slug: str, *, new_version: str, edit_text: str) -> None:
    state = load()
    review = state["reviews"].get(slug)
    if not review:
        return
    review["current_version"] = new_version
    if new_version not in review["versions"]:
        review["versions"].append(new_version)
    review["edits_history"].append({
        "version": new_version,
        "edit_text": edit_text,
        "applied_at": _now_iso(),
    })
    review["last_action_at"] = _now_iso()
    state["reviews"][slug] = review
    save(state)


def set_tg_message(slug: str, *, chat_id: int, message_id: int) -> None:
    state = load()
    review = state["reviews"].get(slug)
    if not review:
        return
    review["tg_chat_id"] = chat_id
    review["tg_message_id"] = message_id
    save(state)


def set_status(slug: str, status: str, **extra) -> None:
    state = load()
    review = state["reviews"].get(slug)
    if not review:
        return
    review["status"] = status
    review["last_action_at"] = _now_iso()
    for k, v in extra.items():
        review[k] = v
    save(state)


def list_pending() -> list[tuple[str, dict]]:
    state = load()
    return [
        (slug, r) for slug, r in state["reviews"].items()
        if r.get("status") == "pending_review"
    ]
