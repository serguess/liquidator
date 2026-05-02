# Telegram-бот для ревью статей

Бот для заказчика-юриста: показывает черновики новых статей, принимает правки
текстом и голосом, публикует на сайт. Применение правок - через Claude Code,
запущенный на сервере как subprocess.

## Что внутри

```
bot/
├── config.py        Конфиг из env (TG_BOT_TOKEN, TG_ALLOWED_CHAT_IDS, ...)
├── state.py         Состояние ревью в data/bot_state.json
├── messages.py      Шаблоны сообщений в Telegram (HTML)
├── watcher.py       Сканирует drafts/ → находит новые статьи
├── editor.py        Применяет правку через `claude -p ...` (subprocess)
├── transcribe.py    Голосовые → текст через Groq Whisper API (бесплатно)
├── handlers.py      Команды и inline-кнопки aiogram
└── main.py          Точка входа: polling + фоновый watcher
```

## Установка зависимостей

```bash
pip install -r requirements.txt
```

Дополнительно нужно:
- **Node.js + Claude Code** на машине, где запускается бот:
  ```bash
  # Node.js 20+ нужен. На Ubuntu:
  curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
  sudo apt-get install -y nodejs

  # Claude Code (CLI):
  npm install -g @anthropic-ai/claude-code
  claude --version  # проверка
  ```

## Авторизация Claude Code

Два варианта:

### A) Через ANTHROPIC_API_KEY (платный API)
```bash
export ANTHROPIC_API_KEY=sk-ant-...
```
Просто и без возни. Стоимость ~$0.04 на правку.

### B) Через подписку Pro / Max (если у заказчика есть)
1. На локальной машине авторизоваться: `claude /login` → пройти OAuth.
2. Скопировать `~/.claude.json` (или `~/.config/claude/...`) с локальной машины
   на сервер в домашнюю папку пользователя, который запускает бота.
3. Проверить: `claude --version` → должен показать "Authenticated as ...".

В рамках Pro fair use лимиты выше, чем у бесплатного, но не безграничные.
Если правок больше ~200/день - можно упереться.

## Конфиг (env переменные)

### Обязательные
```bash
TG_BOT_TOKEN="123456:abc..."           # от @BotFather
TG_ALLOWED_CHAT_IDS="12345678"         # chat_id заказчика, через запятую
```

Где взять:
1. **TG_BOT_TOKEN**: пишете `@BotFather` → `/newbot` → название → юзернейм →
   получаете токен.
2. **TG_ALLOWED_CHAT_IDS**: пишете `@userinfobot` → получаете число.

### Опциональные
```bash
GROQ_API_KEY="gsk_..."                 # для голосовых, бесплатно
TEXTRU_USER_KEY="..."                  # для проверки уникальности
PUBLIC_BASE_URL="https://pravo.shop"   # по умолчанию
BOT_PREVIEW_TOKEN=""                   # фиксированный токен (если хотите)
BOT_WATCH_INTERVAL="60"                # секунды между сканированиями drafts/
```

## Локальный запуск

```bash
# Из корня проекта (bankrotstvo/):
export TG_BOT_TOKEN="..."
export TG_ALLOWED_CHAT_IDS="..."
export ANTHROPIC_API_KEY="..."

python -m bot.main
```

Бот стартует, делает polling Telegram, и каждую минуту сканирует drafts/.

## Запуск на Timeweb (рядом с FastAPI)

В корне есть `Procfile`:
```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
worker: python -m bot.main
```

На Timeweb Cloud Apps включите два процесса (web + worker) в настройках
приложения. Все env переменные положите в раздел Environment Variables.

Если ваш тариф не поддерживает несколько процессов - запустите бот на отдельной
машине (любой VPS с Python 3.10+ и доступом в интернет).

## Как пользуется заказчик

1. Команда пишет статью → пушит в `drafts/{slug}/article-v2.html`.
2. Бот замечает новый драфт за минуту и шлёт уведомление в Telegram заказчику:
   - ссылка на превью (без логина, по подписанному токену),
   - три кнопки: ✅ Опубликовать, ✏️ Правки, 🗑 Отклонить.
3. Заказчик читает с телефона, жмёт кнопку.
4. Если ✏️ Правки → пишет текстом или голосом «убери блок про X»,
   «перепиши абзац мягче» и т.п. Бот применяет правку через Claude Code,
   создаёт новую версию `versions/v2.1.html`, шлёт обновлённое сообщение
   с новой ссылкой.
5. Заказчик читает новую версию, ещё правит или одобряет.
6. ✅ Опубликовать → статья переносится из drafts/ в articles/, обновляется
   articles.json и sitemap.xml, отправляется ping в IndexNow и Яндекс.Вебмастер.

## История версий

В каждой папке драфта появится `versions/`:
```
drafts/kak-zakryt-ooo-s-dolgami/
├── article.html         (старая v1, как было раньше)
├── article-v2.html      (исходная v2 после ИИ-конвейера)
└── versions/
    ├── v2.0.html        (копия article-v2.html на момент первого уведомления)
    ├── v2.1.html        (после первой правки заказчика)
    └── v2.2.html        (после второй правки)
```

Можно посмотреть любую версию через `/p/{slug}?t={token}&v={version}`.

## Состояние

Файл `data/bot_state.json` хранит что в каком статусе. Коммитится в git
(в `.gitignore` есть исключение для этого файла), чтобы при редеплое контейнера
ничего не терялось.

## Тестирование без Telegram

Запустить watcher вручную:
```bash
python -m bot.watcher
```
Покажет какие новые драфты найдены (но в TG ничего не отправит).

Применить правку напрямую к существующему драфту:
```python
from bot import editor
result = editor.apply_edit(
    slug="kak-zakryt-ooo-s-dolgami",
    current_version="2.0",
    versions=["2.0"],
    edit_text="убери первый абзац после короткого ответа",
)
print(result)
```

## Лимиты

- **Groq Whisper**: 2000 запросов/день бесплатно (с большим запасом для одного заказчика).
- **Claude Code (через Pro)**: fair use, ~150-200 запросов/час.
- **Claude Code (через API)**: ~$0.04 на правку, лимиты Anthropic API.
- **text.ru**: 1000 проверок/мес бесплатно.

## Известные ограничения MVP

1. Публикация (drafts → articles) - заглушка во второй итерации.
2. text.ru API - подключим после первого боевого теста.
3. Дайджест за день - сейчас каждая статья = отдельное уведомление.
4. Один whitelist на всех - нет ролевой модели "смотреть/одобрять".
