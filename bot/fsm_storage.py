"""
JSON-file-based FSM storage для aiogram 3.

Зачем нужен: дефолтный MemoryStorage хранит FSM-состояние в RAM процесса.
Cloud Apps делает редеплой при каждом git push (включая push'и от scheduler'а
и публикатора), контейнер бота пересоздаётся, FSM забывается. В результате
ломаются двухшаговые flow:
  - "Правки": жмём кнопку → бот спрашивает что менять → редеплой →
              юзер пишет текст → бот забыл что это правка
  - "Отклонить": жмём кнопку → бот спрашивает причину → редеплой →
                  юзер пишет причину → бот забыл что это отклонение

JsonFileStorage пишет state на диск в data/.fsm_state.json. После редеплоя
новый бот загружает state и подхватывает незавершённые диалоги.

Конкуренция: aiogram polling однопоточный, но aiogram использует asyncio.
asyncio.Lock защищает от параллельных модификаций (например если несколько
update'ов от Telegram прилетели одновременно). Запись на диск через
atomic-rename (tmp + replace).
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Union

from aiogram.fsm.state import State
from aiogram.fsm.storage.base import BaseStorage, StorageKey

log = logging.getLogger(__name__)


def _key_to_str(key: StorageKey) -> str:
    """
    StorageKey уникально идентифицирует диалог: bot_id + chat_id + user_id +
    thread_id + business_connection_id + destiny. Все поля кроме destiny —
    числа или None. Сериализуем в строку с разделителем чтобы можно было
    использовать как ключ JSON-объекта.
    """
    parts = [
        str(key.bot_id),
        str(key.chat_id),
        str(key.user_id),
        str(key.thread_id) if key.thread_id is not None else "-",
        str(key.business_connection_id) if key.business_connection_id is not None else "-",
        str(key.destiny) if key.destiny else "default",
    ]
    return ":".join(parts)


class JsonFileStorage(BaseStorage):
    """Persisted FSM storage с JSON-файлом на диске."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = asyncio.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read_unsafe(self) -> Dict[str, Dict[str, Any]]:
        if not self.path.exists():
            return {}
        try:
            raw = self.path.read_text(encoding="utf-8")
            data = json.loads(raw)
            if isinstance(data, dict):
                return data
            return {}
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("FSM storage: повреждённый файл %s: %s — сбрасываю", self.path, exc)
            return {}

    def _write_unsafe(self, data: Dict[str, Dict[str, Any]]) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            tmp.replace(self.path)
        except OSError as exc:
            log.error("FSM storage: не смог записать %s: %s", self.path, exc)

    def _purge_empty_record(self, data: Dict[str, Dict[str, Any]], k: str) -> None:
        """Удаляем записи без state и без data — чтобы файл не рос вечно."""
        rec = data.get(k)
        if not rec:
            return
        if not rec.get("state") and not rec.get("data"):
            data.pop(k, None)

    async def get_state(self, key: StorageKey) -> Optional[str]:
        async with self._lock:
            data = self._read_unsafe()
            rec = data.get(_key_to_str(key)) or {}
            return rec.get("state")

    async def set_state(self, key: StorageKey,
                          state: Union[str, State, None] = None) -> None:
        if isinstance(state, State):
            state_value = state.state
        else:
            state_value = state  # str | None

        async with self._lock:
            data = self._read_unsafe()
            k = _key_to_str(key)
            rec = data.get(k) or {}
            if state_value is None:
                rec.pop("state", None)
            else:
                rec["state"] = state_value
            data[k] = rec
            self._purge_empty_record(data, k)
            self._write_unsafe(data)

    async def get_data(self, key: StorageKey) -> Dict[str, Any]:
        async with self._lock:
            data = self._read_unsafe()
            rec = data.get(_key_to_str(key)) or {}
            return dict(rec.get("data") or {})

    async def set_data(self, key: StorageKey, data: Dict[str, Any]) -> None:
        async with self._lock:
            full = self._read_unsafe()
            k = _key_to_str(key)
            rec = full.get(k) or {}
            if data:
                rec["data"] = dict(data)
            else:
                rec.pop("data", None)
            full[k] = rec
            self._purge_empty_record(full, k)
            self._write_unsafe(full)

    async def update_data(self, key: StorageKey,
                            data: Dict[str, Any]) -> Dict[str, Any]:
        async with self._lock:
            full = self._read_unsafe()
            k = _key_to_str(key)
            rec = full.get(k) or {}
            current = dict(rec.get("data") or {})
            current.update(data)
            rec["data"] = current
            full[k] = rec
            self._write_unsafe(full)
            return current

    async def close(self) -> None:
        # Файл-based storage ничего не держит открытым.
        return None
