#!/bin/bash
# Docker entrypoint для liquidator-приложения.
#
# Делает три вещи перед стартом uvicorn:
#   1) Если задан SSH_PRIVATE_KEY - кладёт его в ~/.ssh/id_ed25519 (RW для root)
#      и прописывает known_hosts для github.com. Так git push через SSH работает
#      без интерактивного «Are you sure you want to continue connecting».
#   2) Настраивает git remote на SSH (git@github.com:OWNER/REPO.git), если ещё
#      не настроен (например, после git clone по HTTPS).
#   3) Запускает uvicorn (или то, что передано как CMD).
#
# Безопасность: SSH_PRIVATE_KEY никуда не логируется. Если переменная пуста -
# scheduler/publisher просто не смогут пушить, но приложение стартует.

set -euo pipefail

log() { echo "[entrypoint] $*"; }

# ============ 1. SSH ключ ============
if [ -n "${SSH_PRIVATE_KEY:-}" ]; then
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh

  # SSH_PRIVATE_KEY - многострочный ключ. Cloud Apps хранит ENV как есть,
  # переводы строк сохраняются. На всякий случай нормализуем CRLF → LF.
  printf '%s\n' "$SSH_PRIVATE_KEY" | tr -d '\r' > /root/.ssh/id_ed25519
  chmod 600 /root/.ssh/id_ed25519

  # known_hosts: фиксированные ключи github.com (актуальны на конец 2024).
  # Это безопаснее, чем StrictHostKeyChecking=no, потому что защищает от
  # MITM на git@github.com при первом подключении.
  cat > /root/.ssh/known_hosts <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
EOF
  chmod 644 /root/.ssh/known_hosts

  log "SSH ключ установлен в /root/.ssh/id_ed25519, known_hosts настроен для github.com"
else
  log "SSH_PRIVATE_KEY не задан - git push не будет работать"
fi

# ============ 2. Git remote → SSH ============
# Cloud Apps клонирует по HTTPS. Чтобы push шёл через SSH-ключ выше,
# переводим origin на git@github.com:OWNER/REPO.git
GITHUB_REPO="${GITHUB_REPO:-triyul22/liquidator}"

if [ -d /app/.git ] && [ -n "${SSH_PRIVATE_KEY:-}" ]; then
  cd /app
  current_url=$(git remote get-url origin 2>/dev/null || echo "")
  ssh_url="git@github.com:${GITHUB_REPO}.git"
  if [ "$current_url" != "$ssh_url" ]; then
    git remote set-url origin "$ssh_url" || git remote add origin "$ssh_url"
    log "Git origin переключён на SSH: $ssh_url (было: ${current_url:-<пусто>})"
  fi
  # Проверка connectivity (не критично если упадёт - просто логируем)
  if git ls-remote --quiet origin > /dev/null 2>&1; then
    log "Git SSH connectivity OK"
  else
    log "ВНИМАНИЕ: git ls-remote через SSH не сработал - проверь Deploy Key в GitHub"
  fi
fi

# ============ 3. Запуск приложения ============
log "Запускаю: $*"
exec "$@"
