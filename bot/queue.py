"""
Очередь отложенных действий бота.

Назначение: когда заказчик жмёт «Опубликовать» во время активного слота
scheduler'а (data/.scheduler.lock существует), мы НЕ выполняем публикацию
сразу. publisher.publish() делает git push, который триггерит redeploy
Cloud Apps — это убьёт работающий контейнер вместе с ещё-не-закоммиченным
draft'ом текущего слота. Поэтому действие складывается в очередь и
исполняется после снятия lock.

В очереди сейчас только publish — reject и edit не делают git push сами
по себе (только меняют data/bot_state.json локально, изменения попадут
в git со следующим коммитом scheduler'а или publisher'а).

ХРАНИЛИЩЕ: data/bot_state.json (секция pending_actions). Этот файл
КОММИТИТСЯ scheduler'ом и publisher'ом, поэтому очередь переживает
редеплои Cloud Apps. Если в очереди A, B, C, и редеплой случится после
публикации A — новый контейнер прочтёт bot_state.json (с уже удалённым A)
и продолжит с B.

Конкуренция: бот один, scheduler не пишет в очередь. state.py использует
threading.Lock + atomic-rename при записи на диск. Этого достаточно.

Совместимость: ранее очередь жила в data/.action_queue.json. Этот файл
больше не используется — при старте бота bot/main.py его удаляет, чтобы
не вводить в заблуждение при отладке.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import state
from .config import DATA_DIR

# Файл-наследие старой версии (single-file queue). Больше не читаем и не
# пишем — оставлено только для миграционной очистки в bot/main.py.
LEGACY_QUEUE_FILE = DATA_DIR / ".action_queue.json"

SCHEDULER_LOCK_FILE = DATA_DIR / ".scheduler.lock"

# Если scheduler упал и lock завис — через час считаем его устаревшим.
# Совпадает с LOCK_STALE_SEC в articles_scheduler/runner.py.
SCHEDULER_LOCK_STALE_SEC = 3600

# Сколько примерно длится слот scheduler'а — для UX-сообщения «через ~N мин».
SCHEDULER_SLOT_TYPICAL_DURATION_SEC = 600  # 10 минут средняя длительность

log = logging.getLogger(__name__)


def is_scheduler_active() -> tuple[bool, float]:
    """
    Проверяет активен ли сейчас слот scheduler'а.

    Возвращает (active, lock_age_sec). active=False если lock-файла нет
    ИЛИ если он устарел (scheduler упал, не успев снять lock).
    """
    if not SCHEDULER_LOCK_FILE.exists():
        return False, 0.0
    try:
        age = time.time() - SCHEDULER_LOCK_FILE.stat().st_mtime
    except OSError:
        return False, 0.0
    if age > SCHEDULER_LOCK_STALE_SEC:
        return False, age
    return True, age


def estimate_eta_sec() -> int:
    """Грубая оценка сколько ещё осталось до конца слота. Для UX-сообщений."""
    active, age = is_scheduler_active()
    if not active:
        return 0
    eta = SCHEDULER_SLOT_TYPICAL_DURATION_SEC - int(age)
    return max(0, eta)


def enqueue_publish(slug: str, version: str | None, *,
                     chat_id: int | None = None,
                     reply_to_message_id: int | None = None) -> dict:
    """
    Добавляет publish-действие в очередь. Если для этого slug уже есть
    publish — обновляем version и chat_id, position в очереди не меняется
    (дедупликация по slug+action в state.add_pending_action).
    """
    entry = {
        "action": "publish",
        "slug": slug,
        "version": version,
        "chat_id": chat_id,
        "reply_to_message_id": reply_to_message_id,
    }
    saved = state.add_pending_action(entry)
    log.info("Queued publish: slug=%s position=%d",
             slug, len([a for a in peek() if a.get("action") == "publish"]))
    return saved


def peek() -> list[dict[str, Any]]:
    """Возвращает текущее состояние очереди (read-only копия)."""
    return state.list_pending_actions()


def remove_publish(slug: str) -> dict | None:
    """Удаляет publish-задачу для slug (например, заказчик отменил через reject)."""
    return state.remove_pending_action(slug, action_type="publish")


def pop_next() -> dict | None:
    """
    Атомарно достаёт первый элемент очереди и сохраняет в bot_state.json.

    Важно: вызывать только когда is_scheduler_active() == False, иначе
    выполнение pop'нутого publish немедленно сделает push → redeploy
    → текущий слот scheduler'а потеряет работу.

    Сразу после этого вызова bot_state.json содержит уменьшенную очередь.
    Публикатор включит этот файл в свой git commit перед push'ем — после
    редеплоя новый контейнер увидит правильное состояние, не повторит
    уже выполненную публикацию.
    """
    return state.pop_pending_action()


def clear() -> int:
    """Удаляет все элементы. Возвращает сколько было."""
    return state.clear_pending_actions()


def cleanup_legacy_file() -> bool:
    """
    Удаляет старый data/.action_queue.json (из предыдущей версии очереди),
    если он остался на диске. Вызывается при старте бота. Возвращает True
    если файл был удалён.

    ВАЖНО: если в legacy-файле остались действия — миграцию НЕ делаем
    автоматически (legacy-файл не переживал редеплои, его данные могут
    быть протухшими). Логируем для диагностики и удаляем.
    """
    if not LEGACY_QUEUE_FILE.exists():
        return False
    try:
        size = LEGACY_QUEUE_FILE.stat().st_size
        LEGACY_QUEUE_FILE.unlink()
        log.info("Удалён legacy %s (был %d байт)",
                 LEGACY_QUEUE_FILE.name, size)
        return True
    except OSError as exc:
        log.warning("Не смог удалить legacy %s: %s", LEGACY_QUEUE_FILE.name, exc)
        return False
