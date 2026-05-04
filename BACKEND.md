# Бэкенд ЛИКВИДАТОР: развёртывание

Одно приложение на Python (FastAPI), которое:
- отдаёт статический сайт (HTML/CSS/JS/images) с корня;
- принимает заявки из формы на `POST /api/lead` и шлёт их на `lead@pravo.shop`.

## Локальный запуск

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux/macOS
pip install -r requirements.txt
copy .env.example .env        # и заполни SMTP_PASS
uvicorn main:app --reload --port 8000
```

Открыть http://localhost:8000 - должен загрузиться сайт.
Проверить API: http://localhost:8000/api/health → `{"ok":true,"smtp_configured":...}`.

Тест заявки:
```bash
curl -X POST http://localhost:8000/api/lead ^
  -H "Content-Type: application/json" ^
  -d "{\"name\":\"Тест\",\"phone\":\"+79001234567\",\"consent\":true}"
```

## Деплой на Timeweb Cloud Apps

1. **Cloud Apps → Создать → Backend → Python 3.11**
2. Источник: GitHub, репозиторий `serguess/liquidator`, ветка `main`, корень репозитория `/`.
3. **Build command:** `pip install -r requirements.txt`
4. **Run command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
5. **Переменные окружения** (Environment → Variables) - скопировать из `.env.example`:
   - `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASS`
   - `MAIL_TO=lead@pravo.shop`
   - `MAIL_FROM=lead@pravo.shop`
   - `MAIL_FROM_NAME=Сайт ЛИКВИДАТОР`
   - `ALLOWED_ORIGIN=same-origin`
   - `RATE_LIMIT_PER_HOUR=3`
6. **Домен:** привязать `pravo.shop` + `www.pravo.shop`, включить Let's Encrypt SSL.
7. Deploy.

## Почта: настройка отправителя

### Вариант 1. Яндекс 360 для бизнеса (рекомендую)
- Подключить Яндекс 360 на домен `pravo.shop` (249 ₽/мес за ящик)
- Создать ящик `lead@pravo.shop`
- В `id.yandex.ru` → **Безопасность → Пароли приложений → Почта** создать отдельный пароль для SMTP
- Положить его в `SMTP_PASS`

### Вариант 2. Mail.ru для бизнеса
- Аналогично: подключить домен, создать ящик, получить "пароль для внешних приложений", `SMTP_HOST=smtp.mail.ru`.

### Обязательно: DNS-записи на домене pravo.shop
Чтобы письма не уходили в спам, в панели домена добавить:
- **SPF** (TXT на `@`): `v=spf1 include:_spf.yandex.net ~all` (для Яндекса)
- **DKIM**: Яндекс выдаст значение в панели - добавить TXT-запись
- **DMARC** (TXT на `_dmarc`): `v=DMARC1; p=none; rua=mailto:lead@pravo.shop`

Без этих записей почта будет улетать в спам у 80%+ получателей.

## Подключение формы (позже)

Сейчас форма на сайте только валидирует и показывает "Спасибо" - не шлёт ничего.
Когда бэкенд задеплоен, обновить `script.js`: заменить обработчик `form.addEventListener('submit', ...)`
на вызов `fetch('/api/lead', ...)`. Сниппет будет приложен отдельным коммитом.

И добавить скрытое поле honeypot в форму (`index.html`):
```html
<input type="text" name="_hp" tabindex="-1" autocomplete="off"
       style="position:absolute;left:-9999px;width:1px;height:1px;opacity:0;"
       aria-hidden="true"/>
```

## Эксплуатация

- **Логи:** Timeweb Cloud Apps → приложение → Logs. Успешные заявки: `lead sent name=...`. Спам: `honeypot triggered`. Rate limit: `rate limit ip=...`.
- **Здоровье:** `GET /api/health` должен вернуть `{"ok":true,"smtp_configured":true}`. Если `false` - проверь ENV.
- **Rate limit хранится в памяти процесса.** При рестарте обнуляется. Для одной реплики ок.
- **Масштаб.** Если приложение начнёт падать под нагрузкой - увеличиваем тариф Cloud App, а rate limit переносим в Redis.
