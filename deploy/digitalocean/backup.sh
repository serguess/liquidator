#!/bin/bash
# Ежедневный бэкап критичных файлов VPS.
# Запускается через cron в 03:30 (см. README).
#
# Что бэкапим:
# - .env (секреты, не в git)
# - data/.fsm_state.json (FSM-state бота, не в git, эфемерный но удобно иметь)
# - data/bot_state.json (в git, но локальная копия быстрее восстанавливается)
# - drafts/ целиком (в git, но архив на случай repo corruption)
#
# Retention: 14 дней.

set -euo pipefail

REPO=/home/appuser/apps/liquidator
BACKUPS=/home/appuser/backups
TS=$(date +%Y%m%d-%H%M)
DEST="$BACKUPS/$TS"

mkdir -p "$DEST"

cp "$REPO/.env" "$DEST/.env" 2>/dev/null || true
cp "$REPO/data/.fsm_state.json" "$DEST/.fsm_state.json" 2>/dev/null || true
cp "$REPO/data/bot_state.json" "$DEST/bot_state.json" 2>/dev/null || true

tar czf "$DEST/drafts.tar.gz" -C "$REPO" drafts/ 2>/dev/null || true

# Чистим бэкапы старше 14 дней.
find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d -mtime +14 -exec rm -rf {} + 2>/dev/null || true

echo "[$(date -Iseconds)] Backup done: $DEST"
