"""
Articles scheduler: APScheduler-таймер, который раз в N минут запускает
полный конвейер агентов через `claude --print "/write-article {category}"`,
складывает результат в drafts/ и пушит в git.

Интегрируется в lifecycle FastAPI через `lifespan.start_articles_scheduler()`
и `lifespan.stop_articles_scheduler()`.

Все настройки - через переменные окружения:
- SCHEDULER_ENABLED        : true|false (по умолчанию false - в проде включить вручную)
- SCHEDULER_INTERVAL_MINUTES : шаг таймера (по умолчанию 144 - ровно 10 пусков в сутки)
- SCHEDULER_TZ             : таймзона (по умолчанию Europe/Moscow)
- ARTICLES_PER_DAY         : дневной лимит статей (по умолчанию 1)
- ROTATION_ORDER           : порядок категорий через запятую (по умолчанию fiz,yur,vzysk,news)
- ARTICLE_TIMEOUT_SEC      : таймаут одного пайплайна (по умолчанию 2400 = 40 мин)
- CLAUDE_CODE_OAUTH_TOKEN  : токен подписки Claude Code (обязательно)
- GIT_PUSH_TOKEN           : GitHub PAT с правом push (обязательно для push)
- GITHUB_REPO              : owner/repo (по умолчанию serguess/liquidator)
- GITHUB_BRANCH            : ветка (по умолчанию main)
"""
