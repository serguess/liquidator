# Миграция bot + scheduler на DigitalOcean Droplet

Версия: 2026-05-08 (rev 3, после операционного code-audit). Целевая архитектура:

- `pravo.shop` (FastAPI + статика) остаётся на Timeweb Cloud Apps.
- `bot/main.py` и `articles_scheduler` переезжают на DigitalOcean Droplet.
- Общий git: `serguess/liquidator` (branch `main`).
- VPS пишет drafts в репо, push в `main` редеплоит ТОЛЬКО сайт. Бот и scheduler на VPS живут отдельно и self-kill больше невозможен.

> ⚠️ Прод (Timeweb) не трогаем до явной отмашки «переключаем». Параллельный запуск и rollback - в отдельных секциях (будут добавлены позже).

---

## 🛠 Operational notes (важные мелочи которые легко забыть)

### N1. Окно переключения Timeweb → VPS

Когда мы вырубаем Cloud Apps-бот (Patch P3a) и включаем боевой токен на VPS, файл `data/.fsm_state.json` на VPS стартует пустой. Это значит: если заказчик в середине двухшагового flow («Правки» нажат, ответ ещё не прислан), его сессия пропадёт.

**Сообщение заказчику ДО переключения:**

> «Юлия, в момент когда я скажу „переключаю бота“ - не нажимай кнопки в боте 5–10 минут. Если уже что-то начала и бот замолчал, не паникуй: ответь reply'ем (свайп влево по сообщению бота) на его последнее сообщение, и продолжишь с того же места.»

Это единственное окно за всю миграцию где FSM может потеряться. После — стабильно навсегда.

### N2. TTL PAT (`GIT_PUSH_TOKEN`)

Classic PAT истекает через 30/90/365 дней или «no expiration». Fine-grained PAT — максимум 1 год. **Когда истечёт — push сломается с обеих сторон** (clone helper в `~/.git-credentials` + runtime в `.env`).

**Что добавить:** календарное напоминание за 2 недели до expiry. При обновлении менять в двух местах:

```bash
nano ~/.git-credentials                  # https://oauth2:NEW_PAT@github.com
nano ~/apps/liquidator/.env              # GIT_PUSH_TOKEN=NEW_PAT
sudo systemctl restart liquidator-bot
```

### N3. Первый запуск scheduler руками после установки

После `systemctl enable --now liquidator-scheduler.timer` первый слот стартанёт автоматически через `OnBootSec=5min`. Если хочется проверить сразу:

```bash
sudo systemctl start liquidator-scheduler.service
journalctl -u liquidator-scheduler -f
```

Слот идёт 15-25 минут. Это и есть полноценный smoke-тест шага 11.3.

### N4. Healthcheck (после стабилизации)

Если systemd-timer перестанет работать (бага кода, OOM, повреждение диска), заказчик заметит только когда статьи прекратят появляться. **После стабилизации** добавить cron-проверку: «timer запускался за последние 200 минут? нет — отправь Telegram-алерт».

Не блокер для миграции, делаем потом отдельной задачей.

### N5. Pull conflict (редкий edge case)

`git pull --ff-only` в начале scheduler-слота **может изредка падать** на VPS, если bot успел записать в `bot_state.json` локально и одновременно из upstream приехал коммит на тот же файл. Runner это просто логирует и продолжает - слот отработает.

**Если такое начнёт повторяться часто** (>1 раза в неделю): добавить в `_git_pull_before_slot` обёртку через `git stash --include-untracked` + `git stash pop`. Это патч P6, но писать его сейчас не нужно - сначала наблюдаем.

### N6. Dockerfile на Cloud Apps можно упростить

После миграции в [Dockerfile](projects/bankrotstvo/Dockerfile) больше не нужны: Node.js, npm, claude CLI, openssh-client. Build будет тяжелее на ~30 секунд из-за этого. **Оптимизация в бэклог**, не блокер.

---

## 🔁 Concurrency & race-conditions: проверка ботовых кейсов на VPS

Перед составлением патчей я отдельно прогнал по коду все кейсы заказчика. Ниже разобрано **что именно произойдёт на VPS** в каждом случае, и **где есть остаточный риск**.

### Архитектура координации bot ↔ scheduler

На VPS оба процесса работают на одном диске и видят одни и те же артефакты:

| Артефакт | Назначение | Запись |
|---|---|---|
| `data/.scheduler.lock` | scheduler держит на время слота, lock-файл | scheduler (atomic create/unlink) |
| `data/bot_state.json` | reviews + очередь pending_actions + preview_token | bot (atomic tmp+rename), scheduler (только в финале коммита) |
| `data/.fsm_state.json` | FSM-состояние диалогов | bot (atomic tmp+rename) |
| `drafts/{slug}/versions/v*.html` | версии после правок | bot (через Claude Write — atomic tmp+rename) |
| `drafts/{slug}/.notified` | sentinel «уведомление отправлено» | bot watcher |
| `~/.git-credentials` | PAT для push | git (только чтение) |

`bot/state.py:91-93` пишет через `tmp.replace(STATE_FILE)` — атомарно. `runner.py:1658` снимает lock через `LOCK_FILE.unlink()` в `finally` — гарантированно. На VPS эти примитивы работают идентично Cloud Apps.

### Кейс 1: «✅ Опубликовать» во время генерации статьи

**Сценарий.** Scheduler пишет статью X (claude subprocess внутри runner). Заказчик жмёт «Опубликовать» по статье Y, которую видит в TG из прошлого слота.

**Что в коде.** [handlers.py:290-313](projects/bankrotstvo/bot/handlers.py:290) перед публикацией зовёт `action_queue.is_scheduler_active()` ([queue.py:56-71](projects/bankrotstvo/bot/queue.py:56)) — читает `data/.scheduler.lock`. Если lock есть → ставит publish в `pending_actions` (`bot_state.json`) и отвечает «📋 В очереди (#N). Опубликую через ~M мин». [bot/main.py:67-217](projects/bankrotstvo/bot/main.py:67) `queue_loop` каждые 20 сек проверяет `is_scheduler_active()` снова, как только scheduler удалил lock — `pop_next` + `publisher.publish`.

**Работает на VPS:** ✅ да.
- Lock — локальный файл, оба процесса на одном диске.
- `pop_next` ДО `publisher.publish` (`bot/main.py:101`) — гарантия что в коммите публикации уже уменьшенная очередь.
- Публикатор не делает push в момент активного scheduler — потому что lock проверен.

**Остаточный риск:** окно ~1 секунда между `runner.run_one_article()` финальным `git push` и `LOCK_FILE.unlink()`. Если bot публикует именно в этой щели, оба процесса могут одновременно делать `git push origin main` → один из них получит non-fast-forward. У runner есть retry с `pull --rebase -X theirs` ([runner.py:1062-1095](projects/bankrotstvo/articles_scheduler/runner.py:1062)). У publisher.py нет retry. Низкая вероятность, но если ловим — публикация падает с «push_failed», заказчик жмёт ещё раз. Не критично.

### Кейс 2: «✏️ Правки» (голос или текст) во время генерации статьи

**Сценарий.** Scheduler пишет статью X через Claude (~1.5 ГБ RSS). Заказчик жмёт «Правки» по статье Y, отвечает голосовым → Groq Whisper транскрибирует → editor.apply_edit запускает второй Claude subprocess (~1.5 ГБ).

**Что в коде:**
- [handlers.py:106-160](projects/bankrotstvo/bot/handlers.py:106) — FSM `EditFlow.waiting_for_edit_text`, ForceReply prompt с маркером `↩️ edit:{slug}`.
- [handlers.py:163-183](projects/bankrotstvo/bot/handlers.py:163) — голос: `transcribe.transcribe_voice_bytes` (Groq).
- [handlers.py:514-561](projects/bankrotstvo/bot/handlers.py:514) — `_run_edit` вызывает `editor.apply_edit` через `asyncio.to_thread`.
- [editor.py:206-223](projects/bankrotstvo/bot/editor.py:206) — `subprocess.run(claude, capture_output=True, timeout=360)`. **НЕ делает git push.** Создаёт `drafts/{slug}/versions/v{X+1}.html` через Claude Write (atomic tmp+rename).

**Работает на VPS:** ✅ функционально да, ⚠️ память впритык.
- FSM в `data/.fsm_state.json` (JsonFileStorage) переживает рестарты systemd.
- Маркер `↩️ edit:slug` в reply_to_message работает как fallback при потере FSM.
- Editor НЕ конфликтует с scheduler по git — пишет только в `versions/v*.html`, без коммита.
- Файл `versions/v*.html` создаётся атомарно через Claude Write — даже если scheduler в этот момент `git add drafts/`, он застейджит либо целый файл, либо его отсутствие (не partial).

**Остаточный риск — память.**
- 2 параллельных claude = ~3 ГБ RSS. Plus bot (200 МБ) + scheduler runner (200 МБ) + system + journald = ~3.6-4 ГБ.
- Droplet $12/мес: 2 ГБ RAM + 2 ГБ swap = 4 ГБ total. Будет работать в swap, медленно, но без OOM-kill **только если swappiness высокий**. У нас в плейбуке `swappiness=10` — система предпочитает убивать вместо свопа.
- **Решения (выбрать одно):**
  - **(а)** Поднять до Droplet $24/мес (4 ГБ RAM, 2 vCPU). Проблема исчезает. Рекомендую этот вариант — 12 USD/мес против стабильности UX.
  - **(б)** Установить `swappiness=60` в плейбуке → система спокойно свопит, claude переживает.
  - **(в)** Добавить в `editor.apply_edit` проверку `is_scheduler_active()` и если активен — отвечать заказчику «Сейчас scheduler пишет статью, правки применю через ~M мин», ставить в очередь и обработать после lock. Это требует правки кода (новая фича); нет в текущей версии бота.

Я бы выбрал **(а) или (б)**, без правок кода. Меньше рисков на этапе миграции.

### Кейс 3: «🗑 Отклонить» во время генерации статьи

**Сценарий.** Scheduler пишет, заказчик жмёт «Отклонить», указывает причину текстом.

**Что в коде:**
- [handlers.py:363-412](projects/bankrotstvo/bot/handlers.py:363) — FSM `EditFlow.waiting_for_rejection_reason`.
- [handlers.py:415-482](projects/bankrotstvo/bot/handlers.py:415) — `on_rejection_reason`: `action_queue.remove_publish(slug)` + `state.set_status(slug, "rejected", reason)`.
- **Никаких subprocess, никакого Claude, никакого git push.** Только запись в `data/bot_state.json`.

**Работает на VPS:** ✅ полностью.
- Параллельно с scheduler не конфликтует (нет общих ресурсов кроме bot_state.json).
- `state.py` пишет через atomic tmp+rename → нет partial write.
- Scheduler в финале слота сделает `git add data/bot_state.json` — застейджит уже актуальную версию с rejected.

**Остаточный риск.** Микро-окно ~10ms между `state.set_status` (bot) и `git add data/bot_state.json` (scheduler) — если они столкнулись миллисекунда в миллисекунду, scheduler застейджит версию ДО bot-записи. Тогда rejected не попадёт в этот коммит, останется локально на VPS, попадёт в следующий слот. **На функциональность не влияет** — статус локально сохранён, бот его помнит.

### Кейс 4: Голосовое отклонение

**Не поддерживается** ([handlers.py:621](projects/bankrotstvo/bot/handlers.py:621)): «отклонение голосом не поддерживаем — нужна причина текстом». Если заказчик пришлёт голос на reject-prompt, бот его проигнорирует. Это уже текущее поведение, миграция не меняет.

### Кейс 5: Автоматическое уведомление после генерации + sentinel `.notified`

**Сценарий.** Scheduler закончил статью. finalize_draft поставил `meta.ready_for_review=true`. Scheduler пушит. Watcher на VPS видит draft.

**Что в коде:**
- [bot/main.py:57-65](projects/bankrotstvo/bot/main.py:57) `watch_loop` каждые `BOT_WATCH_INTERVAL_SEC=60` сек.
- [watcher.py:97-200](projects/bankrotstvo/bot/watcher.py:97) `scan_for_new_drafts`: пропускает draft если `slug in known_slugs` (state) ИЛИ `notified_sentinel.is_notified(sub)`. Требует `meta.ready_for_review=true`.
- [bot/main.py:278-285](projects/bankrotstvo/bot/main.py:278) после успешной отправки в TG создаётся `drafts/{slug}/.notified` sentinel.

**Работает на VPS:** ✅ да.
- Двойная защита от повторного уведомления (state + sentinel).
- `data/.bootstrap_sentinel_done` в репо ([gitignore:29](projects/bankrotstvo/.gitignore:29)) — bootstrap-sync при первом старте бота на VPS НЕ запустится и не загасит свежие уведомления (проверено в шаге 5 плейбука).
- Sentinel остаётся в drafts/{slug}/ при рестарте systemd — bot читает его как было.

**Остаточный риск — race window до `git push`.**
- Сейчас порядок в run_one_article: `finalize_draft` → `meta.ready_for_review=true` → `git commit` → `git push`. Между установкой флажка и push'ем проходит несколько секунд.
- Watcher на VPS тикает раз в 60 сек. Если попадёт в это окно — отправит уведомление с ссылкой на pravo.shop/preview/{slug} **до того** как Cloud Apps редеплоится.
- Заказчик кликнет на ссылку → Cloud Apps ещё не подтянул новый коммит → 404 на 30-60 секунд.
- **Это не блокер**. Текущее prod-поведение Cloud Apps то же самое.
- **Опциональный фикс (рекомендую сделать сразу при миграции, мелкий):** см. Patch P4 ниже.

### Кейс 6: Один TG_BOT_TOKEN — два процесса (Cloud Apps + VPS)

**Это критично.** Telegram getUpdates отдаёт каждый update только последнему запросившему процессу. Если оба бота слушают боевой токен — события случайным образом теряются для одного из них.

**Решение в плане:** на этапе параллельного запуска (шаг 11.4) на VPS используем **отдельный тестовый токен** от @BotFather. В момент финального переключения (Patch P3a) пушим коммит, который удаляет startup hooks из main.py → Cloud Apps редеплоится без бота → VPS-бот в .env подмениваем на боевой токен → `systemctl restart liquidator-bot`. Окно перекрытия — секунды.

### Кейс 7: «Опубликовать», потом передумал → «Отклонить»

**Что в коде.** [handlers.py:477](projects/bankrotstvo/bot/handlers.py:477) `action_queue.remove_publish(slug)` вызывается ПЕРЕД `state.set_status(rejected)`. Если slug был в pending_actions — удалится. Когда scheduler снимет lock и queue_loop возьмётся за следующий item — этого slug уже не будет.

**Работает на VPS:** ✅ да.

### Кейс 8: FSM «зависает» (заказчик нажал «Правки», ушёл, через час вернулся)

**Что в коде.** FSM хранится в `data/.fsm_state.json` через JsonFileStorage. Переживает рестарт бота. Дополнительно [handlers.py:564-645](projects/bankrotstvo/bot/handlers.py:564) — fallback по reply_to_message с маркером `↩️ edit:{slug}` / `↩️ reject:{slug}` в тексте бот-prompt'а. Даже если FSM сбросился (рестарт systemd, сбой), reply на старое сообщение всё равно правильно идентифицирует slug.

**Работает на VPS:** ✅ да.

### Кейс 9: Editor правит файл `versions/v2.1.html`, scheduler в это же время `git add drafts/`

**Что в коде.** Claude Write tool пишет файлы атомарно (внутренний tmp + rename). `git add` стейджит то что лежит в файловой системе на момент вызова. Между этими операциями возможны два состояния:
- Файла v2.1.html ещё нет → git его не видит, не стейджит.
- Файл v2.1.html уже целиком записан → git стейджит как целый файл.

**Partial write невозможен** благодаря atomic-rename в Claude Write. ✅

### Кейс 10: VPS пропал (forced reboot) во время правок

`drafts/{slug}/versions/v2.1.html` — uncommitted на момент падения. Если был записан — после рестарта systemd запустит бота, файл на месте, заказчик может «Опубликовать» (publisher включит файл в коммит). Если не был записан — заказчик получит «Сессия правки потерялась», нажмёт «Правки» заново.

**Работает на VPS:** ✅ устойчиво.

### Кейс 11: Cloud Apps не успел редеплой между двумя пушами с VPS

**Сценарий.** Scheduler push (статья X) → 30 секунд → publisher push (публикация Y). Cloud Apps ещё в середине первого редеплоя.

**Что происходит:** Cloud Apps abort'ит первый деплой и стартует с новым коммитом — поведение platform-side. Сайт пропускает деплой #1 и применяет деплой #2 поверх старого. Артефакты обоих коммитов в репо, оба видны на сайте после редеплоя #2. Сайт может побыть offline ~1-2 минуты в этот момент.

**Работает:** ✅ так уже работало на Cloud Apps до миграции, не новое поведение.

### Кейс 12: Groq API лежит, заказчик прислал голосовое

[transcribe.transcribe_voice_bytes](projects/bankrotstvo/bot/transcribe.py) возвращает None при сбое → бот пишет «❌ Не удалось распознать голосовое. Попробуйте отправить текстом или повторите запись» ([handlers.py:175-180](projects/bankrotstvo/bot/handlers.py:175)). Graceful failure, FSM остаётся в waiting_for_edit_text — заказчик может ответить текстом без новой кнопки.

**Работает на VPS:** ✅ да.

---

## 📋 Дополнительные решения после code-audit (memory + race + edit-during-write)

| ID | Решение | Когда применять | Где |
|---|---|---|---|
| **R1** | Droplet 4 ГБ RAM ($24/мес) или swappiness=60 | До запуска | Шаг 1 + шаг 2 плейбука |
| **R2** | Patch P4: `.pushed` sentinel | Опционально, до запуска | См. Patches ниже |
| **R3** | Тестовый TG_BOT_TOKEN от @BotFather | На этапе параллельного запуска | Шаг 11.4 |
| **R4** | publisher.py retry на non-fast-forward | Опционально, после стабилизации | Не блокер |

---

## ⚙️ Code patches (применить ДО миграции, отдельным коммитом)

Эти правки делают код пригодным к запуску на VPS. Без них либо не стартует, либо стартует с пустыми ENV.

### Patch P1: `articles_scheduler/runner.py` — load_dotenv

Сейчас runner.py читает env на module-level (строки 34-43): `ROTATION`, `ARTICLES_PER_DAY`, `ARTICLE_TIMEOUT_SEC`, `LOCK_STALE_SEC`, `FAILURE_STREAK_LIMIT`, `HEARTBEAT_TIMEOUT_SEC`, `GITHUB_REPO`, `GITHUB_BRANCH`, `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`. Без `load_dotenv()` все они уйдут в дефолты при прямом запуске `python -m articles_scheduler.runner`.

**Что менять:** после строки 22 (`ROOT = Path(__file__).resolve().parent.parent`), до строки 34, вставить:

```python
from dotenv import load_dotenv
load_dotenv(ROOT / ".env")
```

Шаблон тот же что в [bot/config.py:39-40](projects/bankrotstvo/bot/config.py:39). Безопасен: `python-dotenv` по умолчанию не перезаписывает уже установленные ENV-переменные (override=False), так что повторный вызов из других модулей или из systemd EnvironmentFile не конфликтует.

### Patch P2: `.env.example` — обновить дефолты

Два места в [.env.example](projects/bankrotstvo/.env.example):

- Строка 88: `ARTICLE_TIMEOUT_SEC=2400` → `ARTICLE_TIMEOUT_SEC=3600` (синхрон с дефолтом в runner.py:36, увеличено в мае 2026 для сложных тем).
- Строка 96: `GITHUB_REPO=triyul22/liquidator` → `GITHUB_REPO=serguess/liquidator` (по MEMORY 4 мая 2026 единственный актуальный).

### Patch P3 (применить НЕ сейчас, а в момент переключения)

#### P3a. `main.py` — отключить bot и scheduler в FastAPI lifecycle

**Критично!** На Cloud Apps бот живёт НЕ в Procfile worker, а внутри FastAPI startup hooks ([main.py:1080-1156](projects/bankrotstvo/main.py:1080) для бота и [main.py:1180-1195](projects/bankrotstvo/main.py:1180) для scheduler). Procfile с `worker:` фактически Cloud Apps игнорирует.

После того как бот переехал на VPS, обе функции в main.py **должны быть закомментированы** или удалены, иначе один TG_BOT_TOKEN будет одновременно опрашиваться двумя процессами и Telegram отдаст update только последнему — сообщения будут пропадать. То же про scheduler — два scheduler-а в одном репо устроят гонку коммитов.

Что закомментировать:
- `_start_telegram_bot` (декоратор `@app.on_event("startup")`) - строки 1084-1156.
- `_stop_telegram_bot` (декоратор `@app.on_event("shutdown")`) - строки 1158-1173.
- `_start_articles_scheduler` (`@app.on_event("startup")`) - строки 1180-1186.
- `_stop_articles_scheduler` (`@app.on_event("shutdown")`) - строки 1189-1195.

Сам импорт `from articles_scheduler.lifespan import start_articles_scheduler` остаётся валидным (модуль не падает при импорте), просто не вызываем.

#### P3b. `Procfile` — удалить worker

Опционально, для гигиены. `worker: python -m bot.main` Cloud Apps не использует, но если когда-то поменяется конфигурация - подстраховка.

```
web: uvicorn main:app --host 0.0.0.0 --port $PORT
```

#### P3c. На Timeweb в `.env`

Проверить (не критично, но защита от случайности):
- `SCHEDULER_ENABLED=false`

### Patch P4 (опциональный, рекомендую): `.pushed` sentinel

Закрывает race window «watcher отправил уведомление до того как scheduler сделал push» (см. Кейс 5 выше).

**Что менять:**

1. В [articles_scheduler/runner.py](projects/bankrotstvo/articles_scheduler/runner.py) после успешного `git push` (внутри функции которая возвращает `{"committed": True, "pushed": True}` ~строка 1110-1111) — добавить создание sentinel:
   ```python
   # Маркер для bot/watcher.py: «push выполнен, сайт может быть готов».
   # Без этого файла watcher пропустит draft и не пошлёт уведомление.
   try:
       slug_dir = DRAFTS_DIR / slug
       if slug_dir.exists():
           (slug_dir / ".pushed").write_text(
               datetime.now().isoformat(timespec="seconds"),
               encoding="utf-8",
           )
   except OSError:
       pass  # не критично, в худшем случае уведомление чуть запоздает
   ```

2. В [bot/watcher.py](projects/bankrotstvo/bot/watcher.py) в `scan_for_new_drafts` ~около строки 165 (где проверяется `meta.get("ready_for_review")`) добавить:
   ```python
   if not (sub / ".pushed").exists():
       continue  # scheduler ещё не запушил — Cloud Apps не готов отдать /preview
   ```

3. В [.gitignore](projects/bankrotstvo/.gitignore) добавить `.pushed` в whitelist (он внутри `drafts/{slug}/`, попадает в коммит автоматически как часть папки — отдельная whitelist-запись не нужна).

---

## 0. Pre-flight checklist (на локальной машине, до создания droplet)

**Это и есть «что делать сейчас» прежде чем нажимать что-либо на DigitalOcean.** Эти пункты не требуют доступа к VPS.

Проверить перед стартом:

- [ ] Доступ к репо `serguess/liquidator` есть, ветка `main` чистая.
- [ ] Все секреты из боевого `.env` Cloud Apps скопированы в локальный безопасный менеджер паролей. Список переменных: `TG_BOT_TOKEN`, `TG_ALLOWED_CHAT_IDS`, `CLAUDE_CODE_OAUTH_TOKEN`, `GIT_PUSH_TOKEN`, `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL`, `GITHUB_REPO=serguess/liquidator`, `GITHUB_BRANCH=main`, `CLOUDINARY_CLOUD_NAME`, `CLOUDINARY_API_KEY`, `CLOUDINARY_API_SECRET`, `CLOUDINARY_FOLDER`, `CLOUDINARY_WEB_TRANSFORMATION`, `FAL_KEY`, `FAL_MODEL`, `FAL_RESOLUTION`, `FAL_ASPECT_RATIO`, `FAL_TIMEOUT_SEC`, `GROQ_API_KEY`, `BOT_PREVIEW_TOKEN`, `BOT_WATCH_INTERVAL`, `PUBLIC_BASE_URL`, `PREVIEW_USER`, `PREVIEW_PASSWORD`, `SCHEDULER_ENABLED`, `SCHEDULER_INTERVAL_MINUTES`, `SCHEDULER_TZ`, `ARTICLES_PER_DAY`, `ROTATION_ORDER`, `ARTICLE_TIMEOUT_SEC`, `INDEXNOW_KEY`, `INDEXNOW_HOST`, `YANDEX_CLOUD_API_KEY`, `YANDEX_CLOUD_FOLDER_ID`, `WORDSTAT_REGION_CODE`, `WORDSTAT_CACHE_TTL_DAYS`, `IMAGE_GEN_DEFAULT_COVER_URL`, `TEXTRU_USER_KEY`.
- [ ] Локально сгенерирован SSH-ключ для входа на VPS (если нет своего): `ssh-keygen -t ed25519 -C "asus-laptop" -f ~/.ssh/do_liquidator`. Публичный ключ `~/.ssh/do_liquidator.pub` понадобится при создании droplet.
- [ ] **GitHub PAT (`GIT_PUSH_TOKEN`) с правом write actions+repo на serguess/liquidator готов.** В коде ([runner.py:382](projects/bankrotstvo/articles_scheduler/runner.py:382), [publisher.py:360](projects/bankrotstvo/bot/publisher.py:360)) push идёт ТОЛЬКО через `https://oauth2:TOKEN@github.com/...`. SSH deploy key для push не используется — не путаемся с двумя механизмами, идём чисто на PAT.
- [ ] `claude setup-token` локально выполнен и `CLAUDE_CODE_OAUTH_TOKEN` сохранён.
- [ ] На время миграции `SCHEDULER_ENABLED=false` на новом VPS. Включаем только после smoke-теста.
- [ ] **Patch P1 и P2 (см. секцию выше) применены и закоммичены в main.** Без них runner на VPS прочтёт пустые ENV.

---

## 1. Provisioning droplet (DigitalOcean web UI)

1. Войти в `https://cloud.digitalocean.com`, Create → Droplets.
2. **Choose an image:** Ubuntu 22.04 (LTS) x64.
3. **Choose Size:** Basic → Regular Intel/AMD.
   - **Рекомендую $24/мес** (4 GB RAM, 2 vCPU, 80 GB SSD). Покрывает кейс «scheduler пишет статью + заказчик нажал Правки» без OOM (см. Кейс 2).
   - Альтернатива $12/мес (2 GB RAM, 1 vCPU) — рабочая, но в момент параллельного запуска Claude (scheduler + editor) уйдёт в swap, правки могут идти 2-3 минуты вместо 30-60 сек. Если выберешь $12, обязательно поднять `swappiness=60` в шаге 2 (вместо 10).
4. **Datacenter region:** `Frankfurt (FRA1)` или `Amsterdam (AMS3)` (ближе к Москве по латенси, и к GitHub/Cloudinary тоже норм).
5. **Authentication:** SSH Key. Загрузить `~/.ssh/do_liquidator.pub`. Пароль рутом не задавать.
6. **Hostname:** `liquidator-bot`.
7. **Tags:** `liquidator`, `bot`.
8. **Backups:** включить (+20% к цене, $2.4/мес). Это weekly snapshot, для drafts/state хватит.
9. Создать. Через ~60 секунд droplet получит публичный IP (запомнить, например `134.209.xxx.yyy`).

DNS не настраиваем: бот ходит наружу к Telegram/GitHub/Cloudinary/fal.ai, входящих HTTP-соединений к нему нет.

---

## 2. Первый вход и базовая закалка сервера (~10 минут)

С локальной машины:

```bash
ssh -i ~/.ssh/do_liquidator root@134.209.xxx.yyy
```

На сервере:

```bash
# Свежие пакеты
apt update && apt upgrade -y

# Таймзона (для логов и cron)
timedatectl set-timezone Europe/Moscow

# Создаём non-root пользователя (Claude Code не работает от root с --dangerously-skip-permissions)
adduser --disabled-password --gecos "" appuser
usermod -aG sudo appuser

# Перенос SSH-ключа на appuser
mkdir -p /home/appuser/.ssh
cp /root/.ssh/authorized_keys /home/appuser/.ssh/
chown -R appuser:appuser /home/appuser/.ssh
chmod 700 /home/appuser/.ssh
chmod 600 /home/appuser/.ssh/authorized_keys

# Sudo без пароля для systemctl (удобно для рестартов сервисов)
echo "appuser ALL=(ALL) NOPASSWD: /bin/systemctl, /usr/bin/journalctl" > /etc/sudoers.d/appuser
chmod 440 /etc/sudoers.d/appuser

# Файрвол (ufw): SSH only, остальное наглухо
ufw allow OpenSSH
ufw --force enable
ufw status

# Запрет рут-логина и пароль-логина
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
systemctl restart ssh

# Защита от брутфорса (на всякий)
apt install -y fail2ban
systemctl enable --now fail2ban

# Swap 2 GB - страховка для пиков памяти Claude Code (RAM 2 GB маловато)
fallocate -l 2G /swapfile
chmod 600 /swapfile
mkswap /swapfile
swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
## Если Droplet $24/мес (4 ГБ RAM): swappiness=10 нормально (свопить почти не придётся).
## Если Droplet $12/мес (2 ГБ RAM): swappiness=60 — claude editor + scheduler в параллель
## упираются в RAM, надо разрешить системе свопить вместо OOM-kill.
sysctl vm.swappiness=10            # ← заменить на 60 если Droplet $12
echo 'vm.swappiness=10' >> /etc/sysctl.conf

# Unattended security updates
apt install -y unattended-upgrades
dpkg-reconfigure -plow unattended-upgrades

exit
```

Проверить что вход под `appuser` работает:

```bash
ssh -i ~/.ssh/do_liquidator appuser@134.209.xxx.yyy
```

Дальше всё под `appuser`.

---

## 3. Установка зависимостей runtime (~10 минут)

Под `appuser`:

```bash
sudo apt install -y \
    git curl wget ca-certificates gnupg \
    python3.11 python3.11-venv python3.11-dev \
    build-essential \
    openssh-client \
    jq tree htop ncdu

# Node.js 20 (для Claude CLI)
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs

# Claude Code CLI глобально
sudo npm install -g @anthropic-ai/claude-code
claude --version
```

Базовая git-конфигурация:

```bash
git config --global user.email "scheduler@pravo.shop"
git config --global user.name "Liquidator Scheduler"
git config --global init.defaultBranch main
git config --global pull.rebase true
```

---

## 4. GitHub PAT credential helper (~3 минуты)

В коде runner.py и publisher.py push жёстко через `https://oauth2:TOKEN@github.com/...` (SSH не используется). Поэтому на VPS:

- `clone` через HTTPS с credential helper.
- `push` через тот же PAT в env (его читает код).

```bash
# git credential helper для clone
git config --global credential.helper store
echo "https://oauth2:GHP_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx@github.com" > ~/.git-credentials
chmod 600 ~/.git-credentials

# Проверка
git ls-remote https://github.com/serguess/liquidator.git HEAD
# Должна вывести SHA HEAD-а main без запроса логина.
```

> Замени `GHP_xxxx...` на реальный PAT (тот же что пойдёт в `.env` как `GIT_PUSH_TOKEN`).

---

## 5. Клонирование репо и Python окружение (~5 минут)

```bash
mkdir -p ~/apps && cd ~/apps
git clone https://github.com/serguess/liquidator.git
cd liquidator
git checkout main
git pull --ff-only

# Проверка: bootstrap-флаг должен быть в репо. Если есть — bootstrap-sync
# при первом запуске бота не запустится и не загасит уведомления о свежих
# статьях scheduler-а.
ls -la data/.bootstrap_sentinel_done

# venv в ~/apps/liquidator/.venv
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip wheel
pip install -r requirements.txt

deactivate
```

---

## 6. Авторизация Claude Code на VPS (~2 минуты)

Положить долгоживущий OAuth-токен в окружение (не в `claude /login`, чтобы не плодить интерактив на сервере). Токен берём из локального `claude setup-token`.

Этот токен пойдёт в `.env` ниже как `CLAUDE_CODE_OAUTH_TOKEN`. Дополнительно:

```bash
# Pre-warm: разово запустить claude чтобы создался ~/.claude/
mkdir -p ~/.claude
claude --version
```

Если scheduler/bot читают токен только из `.env`, ручной `claude /login` не нужен.

---

## 7. Конфигурация `.env` (~5 минут)

```bash
cd ~/apps/liquidator
cp .env.example .env
chmod 600 .env
nano .env
```

Заполнить ключевые поля. Минимально обязательные для VPS:

```ini
# Bot
TG_BOT_TOKEN=...
TG_ALLOWED_CHAT_IDS=...
GROQ_API_KEY=...
PUBLIC_BASE_URL=https://pravo.shop
BOT_WATCH_INTERVAL=60
BOT_PREVIEW_TOKEN=                # пусто = бот сгенерит и сохранит в bot_state.json

# Claude
CLAUDE_CODE_OAUTH_TOKEN=...

# Git push (PAT, HTTPS) — единственный вариант, поддержанный кодом
GIT_PUSH_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
GITHUB_REPO=serguess/liquidator
GITHUB_BRANCH=main
GIT_AUTHOR_NAME=Liquidator Scheduler
GIT_AUTHOR_EMAIL=scheduler@pravo.shop

# Scheduler — ВАЖНО: false на момент миграции, включаем после smoke-теста
SCHEDULER_ENABLED=false
SCHEDULER_INTERVAL_MINUTES=144
SCHEDULER_TZ=Europe/Moscow
ARTICLES_PER_DAY=1
ROTATION_ORDER=fiz,yur,vzysk,news
# 3600 а не 2400 (синхрон с дефолтом в runner.py:36 после фикса мая 2026 для сложных тем)
ARTICLE_TIMEOUT_SEC=3600

# Cloudinary
CLOUDINARY_CLOUD_NAME=...
CLOUDINARY_API_KEY=...
CLOUDINARY_API_SECRET=...
CLOUDINARY_FOLDER=articles
CLOUDINARY_WEB_TRANSFORMATION=f_auto,q_auto,w_1920

# fal.ai
FAL_KEY=...
FAL_MODEL=fal-ai/nano-banana-2
FAL_RESOLUTION=4K
FAL_ASPECT_RATIO=16:9
FAL_TIMEOUT_SEC=120

# Yandex Wordstat (если используется)
YANDEX_CLOUD_API_KEY=...
YANDEX_CLOUD_FOLDER_ID=...
WORDSTAT_REGION_CODE=225
WORDSTAT_CACHE_TTL_DAYS=30

# IndexNow (опционально)
INDEXNOW_KEY=...
INDEXNOW_HOST=pravo.shop
```

Что НЕ переносим на VPS:

- `SMTP_*`, `MAIL_*`, `RATE_LIMIT_PER_HOUR`, `ALLOWED_ORIGIN`, `PORT`, `PREVIEW_USER`, `PREVIEW_PASSWORD` - это нужно сайту, остаётся на Timeweb.

Проверка что .env читается:

```bash
source .venv/bin/activate
python -c "from dotenv import load_dotenv; load_dotenv(); import os; print('TG_BOT_TOKEN:', bool(os.getenv('TG_BOT_TOKEN')), 'CLOUD:', bool(os.getenv('CLAUDE_CODE_OAUTH_TOKEN')))"
deactivate
```

---

## 8. systemd unit-ы (~10 минут)

### 8.1. Bot service (always-on)

`/etc/systemd/system/liquidator-bot.service`:

```ini
[Unit]
Description=Liquidator Telegram Bot (review queue)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=appuser
Group=appuser
WorkingDirectory=/home/appuser/apps/liquidator
EnvironmentFile=/home/appuser/apps/liquidator/.env
# PATH явно: бот из subprocess может вызвать git/claude (publisher.py делает push,
# editor.apply_edit вызывает claude). systemd дефолтный PATH покрывает /usr/bin
# куда npm install -g кладёт claude, но фиксируем явно для надёжности.
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
ExecStart=/home/appuser/apps/liquidator/.venv/bin/python -m bot.main

Restart=always
RestartSec=10
TimeoutStopSec=30

# Логи через journald
StandardOutput=journal
StandardError=journal
SyslogIdentifier=liquidator-bot

# Защита (без излишеств, чтобы не сломать git+claude)
NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
# Что нужно писать: репо (drafts, data), git-credentials, claude session/cache
ReadWritePaths=/home/appuser/apps/liquidator /home/appuser/.git-credentials /home/appuser/.claude
PrivateTmp=true

# Лимиты ресурсов. Бот в покое 80-150 МБ, при apply_edit (вызов claude) до 1 ГБ.
MemoryMax=1200M
LimitNOFILE=8192

[Install]
WantedBy=multi-user.target
```

### 8.2. Scheduler service + timer

Scheduler стартует слот, ждёт его финиша, выходит. Через X минут systemd запускает заново. Это надёжнее чем APScheduler в always-on процессе (проще отслеживать через journalctl, нет накопления state в памяти, кран легко закрыть через `systemctl stop liquidator-scheduler.timer`).

**Точка входа подтверждена кодом:** `python -m articles_scheduler.runner` уже oneshot. В [runner.py:1661-1684](projects/bankrotstvo/articles_scheduler/runner.py:1661) `if __name__ == "__main__"` парсит argparse и вызывает `run_one_article()`, после чего процесс выходит. Никаких изменений в коде для этого не нужно — кроме патча P1 (load_dotenv).

`/etc/systemd/system/liquidator-scheduler.service`:

```ini
[Unit]
Description=Liquidator Articles Scheduler (one slot)
After=network-online.target liquidator-bot.service
Wants=network-online.target

[Service]
Type=oneshot
User=appuser
Group=appuser
WorkingDirectory=/home/appuser/apps/liquidator
EnvironmentFile=/home/appuser/apps/liquidator/.env
Environment=PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# Точка входа: runner.py имеет if __name__ == "__main__" с argparse.
# Можно опционально указать категорию: --category fiz/yur/vzysk/news.
ExecStart=/home/appuser/apps/liquidator/.venv/bin/python -m articles_scheduler.runner

Nice=10
# TimeoutStartSec > ARTICLE_TIMEOUT_SEC (3600) + запас на git push/finalize ~600
TimeoutStartSec=4500
StandardOutput=journal
StandardError=journal
SyslogIdentifier=liquidator-scheduler

NoNewPrivileges=true
ProtectSystem=full
ProtectHome=read-only
# scheduler пишет в репо (drafts, data), читает credentials, и Claude CLI
# держит state в ~/.claude/. PrivateTmp ОК — никто оттуда ничего не читает
# через коммиты.
ReadWritePaths=/home/appuser/apps/liquidator /home/appuser/.git-credentials /home/appuser/.claude
PrivateTmp=true

# Claude CLI с полным контекстом агентов поднимает RSS до 1.5-1.8 ГБ. Запас сверху.
# Если упрёшься в OOM — поднимай до 2500M или убирай лимит (положимся на swap).
MemoryMax=2000M
```

`/etc/systemd/system/liquidator-scheduler.timer`:

```ini
[Unit]
Description=Run Liquidator scheduler every 144 minutes

[Timer]
OnBootSec=5min
OnUnitInactiveSec=144min
Persistent=true
AccuracySec=30s
Unit=liquidator-scheduler.service

[Install]
WantedBy=timers.target
```

### 8.2-bis. (УБРАН) Альтернатива «scheduler как always-on APScheduler»

Этот вариант требовал бы writing entry-point (`articles_scheduler/__main__.py`), которого в коде нет. Идём timer-вариантом 8.2 — он совместим с текущим кодом без правок.

### 8.3. Установка и активация

```bash
sudo systemctl daemon-reload

# Бот - сразу always-on
sudo systemctl enable --now liquidator-bot.service

# Scheduler — пока НЕ включаем (SCHEDULER_ENABLED=false в .env). 
# Когда придёт время:
# sudo systemctl enable --now liquidator-scheduler.timer
# или для always-on варианта:
# sudo systemctl enable --now liquidator-scheduler.service

# Проверка
sudo systemctl status liquidator-bot --no-pager
journalctl -u liquidator-bot -f
```

---

## 9. Логирование и ротация (~5 минут)

journald держит логи по умолчанию. Настроим разумный лимит:

`/etc/systemd/journald.conf` - раскомментировать/выставить:

```ini
[Journal]
Storage=persistent
SystemMaxUse=500M
SystemMaxFileSize=50M
MaxRetentionSec=30day
ForwardToSyslog=no
```

```bash
sudo systemctl restart systemd-journald
```

Удобные команды:

```bash
# Live логи
journalctl -u liquidator-bot -f
journalctl -u liquidator-scheduler -f

# За сегодня
journalctl -u liquidator-bot --since "today"

# Последние 200 строк бота
journalctl -u liquidator-bot -n 200 --no-pager

# Все unit-ы liquidator
journalctl -u 'liquidator-*' --since "1 hour ago"
```

Дополнительно файлы scheduler-а (`data/scheduler_log.json`, `data/bot_state.json`) логируются в репо как часть нормальной работы pipeline - это не системные логи, а журнал слотов.

---

## 10. Бэкапы (~5 минут)

Что нужно сохранять отдельно от systemd-снапшотов droplet:

| Что | Где живёт | Зачем |
|---|---|---|
| `drafts/{slug}/` | в репо (commit от scheduler-а) | первичный source - github |
| `data/.fsm_state.json` | НЕ в репо (.gitignore) | состояние FSM бота: «правки/отклонить» переживает рестарт |
| `data/bot_state.json` | в репо (коммитится publisher-ом) | очередь публикаций + reviews; первичный source - github |
| `.env` | НЕ в репо | секреты, лежит только на VPS |
| `~/.claude/` | НЕ в репо | оauth-токен Claude (но он также дублирован в `.env`) |

Простой ежедневный бэкап в `/home/appuser/backups/`:

`/home/appuser/scripts/backup.sh`:

```bash
#!/bin/bash
set -euo pipefail
TS=$(date +%Y%m%d-%H%M)
DEST="/home/appuser/backups/$TS"
mkdir -p "$DEST"

# .env и FSM-state
cp /home/appuser/apps/liquidator/.env "$DEST/.env"
cp /home/appuser/apps/liquidator/data/.fsm_state.json "$DEST/.fsm_state.json" 2>/dev/null || true
cp /home/appuser/apps/liquidator/data/bot_state.json "$DEST/bot_state.json" 2>/dev/null || true

# Папка drafts (на случай если git-история зачищена/перезаписана)
tar czf "$DEST/drafts.tar.gz" -C /home/appuser/apps/liquidator drafts/

# Чистим бэкапы старше 14 дней
find /home/appuser/backups -mindepth 1 -maxdepth 1 -mtime +14 -exec rm -rf {} +

echo "Backup done: $DEST"
```

```bash
mkdir -p ~/scripts ~/backups
nano ~/scripts/backup.sh   # вставить содержимое
chmod +x ~/scripts/backup.sh

# Cron 03:30 ежедневно
crontab -e
# добавить строку:
30 3 * * * /home/appuser/scripts/backup.sh >> /home/appuser/backups/backup.log 2>&1
```

Дополнительно DigitalOcean weekly snapshot (включён в шаге 1) даёт полный image для восстановления.

Опционально: загрузка `~/backups/` в отдельный bucket DO Spaces или yandex disk - сделаем после стабилизации.

---

## 11. Smoke-тесты (~20 минут)

В таком порядке, ничего не трогая в проде:

### 11.1. Git push с VPS

```bash
cd ~/apps/liquidator
# Проверка что credentials работают (clone-через-helper и push-через-PAT-из-env):
git ls-remote origin HEAD

# Проверка что переменные .env реально доходят до runner:
source .venv/bin/activate
python -c "
import os
from dotenv import load_dotenv
load_dotenv()
import articles_scheduler.runner as r
print('GITHUB_REPO:', r.GITHUB_REPO)
print('ARTICLE_TIMEOUT_SEC:', r.ARTICLE_TIMEOUT_SEC)
print('GIT_PUSH_TOKEN set:', bool(os.getenv('GIT_PUSH_TOKEN')))
print('CLAUDE_CODE_OAUTH_TOKEN set:', bool(os.getenv('CLAUDE_CODE_OAUTH_TOKEN')))
"
deactivate
```

Должны быть `serguess/liquidator`, `3600`, оба токена `True`. Если что-то пусто — патч P1 не применён, или `.env` не там, где ожидается.

### 11.2. Claude CLI

```bash
source .venv/bin/activate
which claude                     # → /usr/bin/claude или /usr/local/bin/claude
claude --version
# Запуск из cwd репо, чтобы сессия легла в правильное место:
cd ~/apps/liquidator
echo "напиши слово готово" | claude --print --dangerously-skip-permissions
deactivate
```

Должно ответить «готово». Если падает с auth - проверить `CLAUDE_CODE_OAUTH_TOKEN` в `.env` и `~/.claude/`.

### 11.3. Scheduler — холодный прогон без расписания

В коде нет `--dry-run`. Самый безопасный способ проверить runner на VPS — выставить `SCHEDULER_ENABLED=true` в .env, запустить вручную, дать дойти до агента 1 (semantics) и убить ctrl+C. Это покажет: что Claude CLI стартует, что runner проходит lock+pull, что папка `drafts/{slug}/` создаётся.

```bash
source .venv/bin/activate
# ВАЖНО: SCHEDULER_ENABLED влияет на lifespan.py FastAPI — на VPS мы запускаем
# runner напрямую, флажок не читается. Но если сомневаешься, оставь false.
python -m articles_scheduler.runner --category news
# news — самая короткая категория (4500-6500 знаков), быстрее всего проверится.
deactivate
```

Если хочешь полностью безопасный smoke без записи в repo — заранее создать локальную ветку `vps-smoke` и переключиться на неё (`git checkout -b vps-smoke`), runner пушит в `GITHUB_BRANCH` (по умолчанию main), но из ветки vps-smoke push-команда не сработает на main без force, что нам и нужно — повисит на push, ты его прервёшь, проверишь логи. После теста: `git checkout main`.

### 11.4. Бот в test-режиме (отдельный chat_id)

ВАЖНО: на этом этапе боевой бот всё ещё работает на Cloud Apps (внутри FastAPI). Чтобы не получить два бота на один токен (см. Кейс 6), нужен **отдельный тестовый бот**:

1. У `@BotFather` создать `liquidator_test_bot`, получить токен.
2. В `.env` на VPS подменить `TG_BOT_TOKEN` на тестовый и `TG_ALLOWED_CHAT_IDS` на свой личный chat_id.
3. `sudo systemctl restart liquidator-bot`.
4. Написать тестовому боту `/start`, проверить ответ.
5. Положить тестовый draft в `drafts/_smoke/` (не коммитить) - проверить что watcher отправляет уведомление.

**Чек-лист поведенческих тестов (на тестовом боте, перед переключением):**

- [ ] **Кейс 1:** в момент когда scheduler работает (lock есть, проверить `ls data/.scheduler.lock`), нажать «Опубликовать» → бот отвечает «📋 В очереди (#1)». В `data/bot_state.json` появился `pending_actions[0]`. После завершения слота scheduler-а через 30-60 сек получаешь «✅ Опубликовано».
- [ ] **Кейс 2:** в момент работы scheduler-а нажать «Правки», прислать голосовое → транскрипция приходит → claude editor отрабатывает за 30-90 сек → бот возвращает «✅ Применено, v2.1». `htop` показывает два claude процесса одновременно.
- [ ] **Кейс 3:** в момент работы scheduler-а нажать «Отклонить», прислать причину → мгновенно «🗑 Отклонено». В `bot_state.json` `status=rejected`.
- [ ] **Кейс 5:** scheduler закончил слот, через ≤60 сек приходит TG-уведомление по новой статье. В `drafts/{slug}/.notified` появляется sentinel. После `systemctl restart liquidator-bot` повторное уведомление НЕ приходит (sentinel пропускает).
- [ ] **Кейс 7:** нажать «Опубликовать» → в очереди → нажать «Отклонить» → `pending_actions` очищается, после слота scheduler не публикует.
- [ ] **Кейс 8:** нажать «Правки», подождать `systemctl restart liquidator-bot`, ответить reply'ем на тот же prompt → fallback по маркеру срабатывает, правка применяется.
- [ ] **Память:** при двух одновременных claude `free -h` показывает swap usage; если active swap > 1.5 ГБ — апгрейдить Droplet до 4 ГБ.

Это проверка пункта 2 плана (parallel run) на минимуме - подробный сценарий переключения боевого бота будет в отдельной секции, когда подтвердишь готовность.

### 11.5. Память и ресурсы

```bash
htop                # бот должен есть 60-150 МБ RSS, во время Claude-слота up to 1 GB
free -h             # swap не должен использоваться больше 200 МБ в покое
df -h               # /home должен быть < 30%
```

---

## 12. Чек-лист готовности перед переключением (для будущего шага)

- [ ] `liquidator-bot.service` стабильно работает 24+ часа на тестовом токене.
- [ ] Один полный слот `articles_scheduler` отработал end-to-end на VPS, статья доехала до `_review_queue.json`, обложка сгенерирована.
- [ ] Push с VPS в `serguess/liquidator` прошёл, Cloud Apps корректно редеплоил сайт (без падений), статью видно через `/preview` на pravo.shop.
- [ ] Бэкапы крутятся (`ls ~/backups/` за два дня подряд).
- [ ] journald логи читаемы, ротация работает.
- [ ] План rollback написан и понятен (отдельная секция).
- [ ] **Patch P3a, P3b, P3c подготовлены коммитом, но НЕ запушены до момента переключения.** На пуше Cloud Apps редеплоится без бота и scheduler, в этот же момент VPS-бот берёт TG_BOT_TOKEN на себя.

После этого - переключение `TG_BOT_TOKEN` на боевой и остановка процесса бота на Timeweb. Подробно в следующих секциях документа.

---

## Время

| Этап | Время |
|---|---|
| 0. Pre-flight checklist | 15 мин (сбор секретов, ssh-keygen) |
| 1. Provisioning droplet | 5 мин |
| 2. Закалка сервера | 10 мин |
| 3. Установка зависимостей | 10 мин |
| 4. SSH deploy key для GitHub | 3 мин |
| 5. Клонирование + venv | 5 мин (зависит от скорости pip) |
| 6. Авторизация Claude | 2 мин |
| 7. .env | 5 мин |
| 8. systemd units | 10 мин |
| 9. Логирование | 5 мин |
| 10. Бэкапы | 5 мин |
| 11. Smoke-тесты | 20 мин |
| **Итого** | **~1 час 35 минут** активной работы |

Плюс: 24+ часа наблюдения за тестовым ботом до боевого переключения.

Если что-то поедет (рассинхрон версий питона, хитрый pip-конфликт, fail2ban не пускает по ssh) - закладывай ещё 30-60 минут на разруливание. Реалистичный диапазон: **2-3 часа до момента «всё стоит и крутится в тестовом режиме»**.

---

## Что дальше (не сделано в этом документе)

- Пункт 2 плана: подробная стратегия параллельного запуска (как переключать `TG_BOT_TOKEN`, что делать с `bot_state.json`, как избежать гонки за `pending_actions`).
- Пункт 3 плана: rollback на Timeweb если что-то сломается после переключения.

Скажи когда готов - распишу.
