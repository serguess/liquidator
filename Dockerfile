# Liquidator: единый контейнер для FastAPI (сайт + лид-форма) и articles_scheduler
# (APScheduler-таймер, который через Claude Code генерирует статьи в drafts/).
#
# Один процесс - uvicorn. APScheduler крутится внутри его asyncio loop'а через
# FastAPI lifespan (см. articles_scheduler/lifespan.py).
#
# Системные зависимости:
# - Python 3.11 (база)
# - Node.js 20 + npm (для Claude Code CLI)
# - git (для commit + push сгенерированных статей)
# - claude (CLI Claude Code, ставится через npm)

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NODE_VERSION=20 \
    DEBIAN_FRONTEND=noninteractive

# Системные пакеты + Node.js + git + openssh-client (для git push через SSH Deploy Key)
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        gnupg \
        openssh-client \
    && curl -fsSL "https://deb.nodesource.com/setup_${NODE_VERSION}.x" | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Claude Code CLI глобально
RUN npm install -g @anthropic-ai/claude-code \
    && npm cache clean --force \
    && claude --version || echo "claude CLI installed (version check may need auth)"

WORKDIR /app

# Сначала зависимости Python (лучше кэшируется в Docker layer)
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Затем код проекта
COPY . .

# Делаем entrypoint исполняемым (на Windows-разработке chmod может слететь)
RUN chmod +x /app/docker-entrypoint.sh

# Базовая git-конфигурация (фактические значения подставит scheduler/runner.py
# через GIT_AUTHOR_NAME/GIT_AUTHOR_EMAIL переменные окружения)
RUN git config --global user.email "scheduler@pravo.shop" \
    && git config --global user.name "Liquidator Scheduler" \
    && git config --global --add safe.directory /app \
    && git config --global init.defaultBranch main

EXPOSE 8000

# entrypoint настраивает SSH-ключ из ENV и перенаправляет origin на SSH-URL,
# затем exec'ает CMD ниже.
ENTRYPOINT ["/app/docker-entrypoint.sh"]

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
