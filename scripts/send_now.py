"""
Ручная отправка указанных drafts в TG-бот СЕЙЧАС, не дожидаясь batch-доставки.

Использует те же модули что и `bot.main._batch_delivery_iteration`:
- `_draft_from_state_and_meta` для восстановления draft-dict
- `_send_review_notification` для отправки в TG
- `state.mark_batch_sent` для снятия флага pending_batch после успеха

Также автоматически:
- Подтягивает .env через python-dotenv (если установлен) или ручным parse
- Гарантирует pending_batch=True перед отправкой (на случай разморозки руками)

Запуск:
    cd ~/apps/liquidator
    .venv/bin/python -m scripts.send_now <slug1> [<slug2> ...]

Пример:
    .venv/bin/python -m scripts.send_now \
        kollektory-prava-i-zashchita \
        snizhenie-voznagrazdeniya-au-pozitsiya-vs

Exit codes:
    0 — все указанные slug'и успешно отправлены
    1 — хотя бы один не отправлен (см. лог)
    2 — ошибка валидации входа (slug не найден, нет meta.json и т.п.)
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# --- Загружаем .env ---
# Простой ручной parse, чтобы не зависеть от python-dotenv.
env_file = PROJECT_ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

# Меняем CWD на корень проекта, чтобы относительные пути bot/* работали.
os.chdir(PROJECT_ROOT)

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot import config, state
from bot.main import _draft_from_state_and_meta, _send_review_notification, _mark_sentinel_after_send


async def _send_one(bot: Bot, slug: str, token: str) -> bool:
    """Отправляет одну статью. Возвращает True если хотя бы одному чату ушло."""
    # Восстанавливаем draft
    draft = _draft_from_state_and_meta(slug)
    if draft is None:
        print(f"  [SKIP] {slug}: state.review или meta.json не найден")
        return False

    # Гарантируем pending_batch=True (на случай если флаг слетел после разморозки)
    review = state.get_review(slug)
    if review and not review.get("pending_batch"):
        print(f"  [INFO] {slug}: pending_batch был False — выставляю True перед отправкой")
        state.mark_pending_batch(slug)

    print(f"  [SEND] {slug}: title={draft.get('title', '')[:60]}")
    sent_to = await _send_review_notification(bot, draft, token)
    if not sent_to:
        print(f"  [FAIL] {slug}: ни одному чату не ушло — см. логи бота")
        return False

    _mark_sentinel_after_send(draft, sent_to)
    state.mark_batch_sent(slug)
    print(f"  [OK]   {slug}: отправлено в {sent_to}")
    return True


async def main(slugs: list[str]) -> int:
    if not slugs:
        print("Usage: python -m scripts.send_now <slug1> [<slug2> ...]", file=sys.stderr)
        return 2

    # Проверяем что все slug'и есть в state
    missing = [s for s in slugs if not state.get_review(s)]
    if missing:
        print(f"ERROR: следующие slug'и отсутствуют в bot_state.json: {missing}", file=sys.stderr)
        return 2

    # Проверяем env
    if not config.TG_BOT_TOKEN:
        print("ERROR: TG_BOT_TOKEN не задан. Проверьте .env", file=sys.stderr)
        return 2
    if not config.TG_ALLOWED_CHAT_IDS:
        print("ERROR: TG_ALLOWED_CHAT_IDS не задан. Проверьте .env", file=sys.stderr)
        return 2

    print(f"=== Отправка {len(slugs)} статей в TG ===")
    print(f"Целевые чаты: {config.TG_ALLOWED_CHAT_IDS}")
    print()

    bot = Bot(
        token=config.TG_BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    token = state.get_preview_token_from_state()

    ok_count = 0
    fail_count = 0

    try:
        for i, slug in enumerate(slugs, 1):
            print(f"[{i}/{len(slugs)}]")
            success = await _send_one(bot, slug, token)
            if success:
                ok_count += 1
            else:
                fail_count += 1
            # Пауза между отправками — защита от TG flood-limit
            if i < len(slugs):
                await asyncio.sleep(2)
    finally:
        await bot.session.close()

    print()
    print(f"=== Итог: {ok_count} отправлено, {fail_count} ошибок ===")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    rc = asyncio.run(main(sys.argv[1:]))
    sys.exit(rc)
