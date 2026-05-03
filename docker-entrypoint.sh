#!/bin/bash
# Docker entrypoint для liquidator-приложения.
#
# Делает две вещи перед стартом uvicorn:
#   1) Если задан SSH_PRIVATE_KEY (опционально, для тех кто использует SSH-ключ
#      вместо PAT) - кладёт его в ~/.ssh/id_ed25519 и прописывает known_hosts
#      для github.com. По умолчанию мы используем GIT_PUSH_TOKEN (PAT) - тогда
#      эта секция просто пропускается.
#   2) Запускает uvicorn (или то, что передано как CMD).
#
# Push идёт через HTTPS+PAT в самом коде (runner.py / publisher.py): они
# используют явный URL https://x-access-token:TOKEN@github.com/OWNER/REPO.git,
# поэтому переключать origin не нужно.

set -euo pipefail

log() { echo "[entrypoint] $*"; }

# ============ SSH ключ (опционально) ============
if [ -n "${SSH_PRIVATE_KEY:-}" ]; then
  mkdir -p /root/.ssh
  chmod 700 /root/.ssh
  printf '%s\n' "$SSH_PRIVATE_KEY" | tr -d '\r' > /root/.ssh/id_ed25519
  chmod 600 /root/.ssh/id_ed25519
  cat > /root/.ssh/known_hosts <<'EOF'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
EOF
  chmod 644 /root/.ssh/known_hosts
  log "SSH ключ установлен в /root/.ssh/id_ed25519"
fi

# ============ Git push готовность ============
if [ -n "${GIT_PUSH_TOKEN:-}" ]; then
  log "GIT_PUSH_TOKEN задан - push через HTTPS+PAT включён"
elif [ -n "${SSH_PRIVATE_KEY:-}" ]; then
  log "GIT_PUSH_TOKEN не задан, но SSH_PRIVATE_KEY есть - используется SSH"
else
  log "ВНИМАНИЕ: ни GIT_PUSH_TOKEN, ни SSH_PRIVATE_KEY не заданы - git push не будет работать"
fi

# ============ Запуск приложения ============
log "Запускаю: $*"
exec "$@"
