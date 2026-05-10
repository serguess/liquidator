# Деплой liquidator-bot и scheduler на DigitalOcean Droplet

Эта папка содержит готовые артефакты для миграции бота и scheduler с Cloud Apps на VPS.

Полная документация в `../../DEPLOY_DIGITALOCEAN.md`. Здесь — командные снипеты для копипасты.

Предполагается:
- Ubuntu 22.04 LTS, Droplet $24/мес (4 ГБ RAM).
- Шаги 0-2 плейбука уже сделаны (закалка сервера, appuser, swap).
- Заходим под `appuser`.

## 1. Зависимости

```bash
sudo apt install -y \
    git curl wget ca-certificates gnupg \
    python3.11 python3.11-venv python3.11-dev \
    build-essential openssh-client \
    jq tree htop ncdu

curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
sudo npm install -g @anthropic-ai/claude-code
claude --version

git config --global user.email "scheduler@pravo.shop"
git config --global user.name "Liquidator Scheduler"
git config --global init.defaultBranch main
git config --global pull.rebase true
```

## 2. Git credential helper + clone

Заменить `GHP_xxxxxx` на боевой PAT (тот же что в Cloud Apps `GIT_PUSH_TOKEN`).

```bash
git config --global credential.helper store
echo "https://oauth2:GHP_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx@github.com" > ~/.git-credentials
chmod 600 ~/.git-credentials

mkdir -p ~/apps && cd ~/apps
git clone https://github.com/serguess/liquidator.git
cd liquidator

# Проверка что bootstrap-флаг в репо (иначе при первом запуске бот загасит уведомления).
ls -la data/.bootstrap_sentinel_done
```

## 3. Python venv

```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt
deactivate
```

## 4. .env

```bash
cp deploy/digitalocean/env.vps.template .env
chmod 600 .env
nano .env
```

Заполнить ВСЕ значения (TG_BOT_TOKEN сначала тестовый, GIT_PUSH_TOKEN, CLAUDE_CODE_OAUTH_TOKEN, Cloudinary, fal.ai, Groq, Yandex Cloud, IndexNow).

## 5. systemd units

```bash
sudo cp deploy/digitalocean/liquidator-bot.service /etc/systemd/system/
sudo cp deploy/digitalocean/liquidator-scheduler.service /etc/systemd/system/
sudo cp deploy/digitalocean/liquidator-scheduler.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Только бот пока. Scheduler — после smoke-теста.
sudo systemctl enable --now liquidator-bot.service
sudo systemctl status liquidator-bot --no-pager
journalctl -u liquidator-bot -f
```

## 6. Backup cron

```bash
mkdir -p ~/scripts ~/backups
cp deploy/digitalocean/backup.sh ~/scripts/backup.sh
chmod +x ~/scripts/backup.sh

crontab -e
# добавить строку:
# 30 3 * * * /home/appuser/scripts/backup.sh >> /home/appuser/backups/backup.log 2>&1
```

## 7. journald limits

```bash
sudo sed -i 's/^#\?Storage=.*/Storage=persistent/' /etc/systemd/journald.conf
sudo sed -i 's/^#\?SystemMaxUse=.*/SystemMaxUse=500M/' /etc/systemd/journald.conf
sudo sed -i 's/^#\?MaxRetentionSec=.*/MaxRetentionSec=30day/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
```

## 8. Smoke-тесты

```bash
# 8.1 ENV доходит до runner
cd ~/apps/liquidator
source .venv/bin/activate
python -c "
import os
import articles_scheduler.runner as r
print('GITHUB_REPO:', r.GITHUB_REPO)
print('ARTICLES_PER_DAY:', r.ARTICLES_PER_DAY)
print('ARTICLE_TIMEOUT_SEC:', r.ARTICLE_TIMEOUT_SEC)
print('ROTATION:', r.ROTATION)
print('GIT_PUSH_TOKEN set:', bool(os.getenv('GIT_PUSH_TOKEN')))
print('CLAUDE_CODE_OAUTH_TOKEN set:', bool(os.getenv('CLAUDE_CODE_OAUTH_TOKEN')))
print('REQUIRE_PUSHED_SENTINEL:', os.getenv('REQUIRE_PUSHED_SENTINEL'))
"
deactivate
```

Должно вывести:
- GITHUB_REPO: serguess/liquidator
- ARTICLES_PER_DAY: 10
- ARTICLE_TIMEOUT_SEC: 3600
- ROTATION: ['fiz', 'yur', 'vzysk', 'fiz', 'yur', 'vzysk', 'fiz', 'yur', 'vzysk', 'news']
- оба токена: True
- REQUIRE_PUSHED_SENTINEL: true

```bash
# 8.2 Claude CLI
which claude
claude --version
echo "напиши слово готово" | claude --print --dangerously-skip-permissions
```

Ответит «готово». Если просит логин — выполнить `claude config` и проверить токен.

```bash
# 8.3 git push credentials
git ls-remote origin HEAD
```

Должен вывести SHA HEAD'а main без запроса логина.

## 9. Полный слот scheduler руками

```bash
sudo systemctl start liquidator-scheduler.service
journalctl -u liquidator-scheduler -f
```

Ждать 15-25 минут. Должна появиться статья в drafts/{slug}/, потом TG-уведомление в тестбот.

## 10. Включить timer

После успешного полного слота:

```bash
sudo systemctl enable --now liquidator-scheduler.timer
systemctl list-timers liquidator-scheduler.timer
```

## 11. Финальное переключение (когда smoke прошёл)

1. На локальной машине: пушнуть Patch P3 (отключение бота/scheduler на Cloud Apps).
2. Дождаться пока Cloud Apps редеплоится.
3. На VPS:
   ```bash
   nano ~/apps/liquidator/.env
   # TG_BOT_TOKEN заменить на боевой
   sudo systemctl restart liquidator-bot
   ```
4. Проверка боевого бота `/start` в TG.

## Диагностика

```bash
# Бот не отвечает
journalctl -u liquidator-bot -n 100 --no-pager

# Scheduler не стартует
journalctl -u liquidator-scheduler -n 100 --no-pager
systemctl list-timers liquidator-scheduler.timer

# Память
free -h
htop  # ищем claude процессы

# Git
cd ~/apps/liquidator
git status
git log --oneline -5
```
