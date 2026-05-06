---
description: Запустить полный конвейер написания одной статьи. Использование - /write-article {category} slug={slug} {topic}. Категории - fiz / yur / vzysk / news.
argument-hint: <category> slug=<slug> <topic>
---

Запусти конвейер написания одной статьи для сайта Ликвидатор.

Аргументы: $ARGUMENTS

Формат: первое слово - категория (`fiz`, `yur`, `vzysk` или `news`), второе — `slug=<значение>` (slug темы из topic-map, который уже выбрал scheduler), дальше — тема.

## ⚠ Slug — обязательный аргумент

Если в аргументах есть `slug=foo-bar-2026` — этот slug ДОЛЖЕН использоваться:
- Папка статьи строго `drafts/foo-bar-2026/` (не другая, даже похожая)
- В brief.json `"slug": "foo-bar-2026"` (буквально, без изменений)
- Все артефакты pipeline'а пишутся в эту папку

НЕ генерируй свой slug, даже если кажется красивее. Slug связан с записью в `drafts/_topic-map/{category}.json`. Если ты используешь другой slug — тема в topic-map останется «свободной», и следующий слот scheduler'а её снова выберет → бесконечный цикл по одной теме (этот баг уже стрелял).

Если `slug=...` нет в аргументах (старый формат вызова) — запусти `python -m tools.slugify "{title}"` для детерминированной генерации.

## Конвейер (строго последовательно)

### Heartbeat (обязательно перед каждым агентом)

Перед запуском каждого агента (1, 2, 3, 4, 5, 6, 7) и перед длинными скриптами (quality_gate, inject_boilerplate) обнови heartbeat-файл одной командой:

```bash
date -u +"%Y-%m-%dT%H:%M:%S | агент-N" > data/.scheduler_heartbeat
```

Где `агент-N` — имя текущего шага (например `1-semantics`, `quality_gate`, `7-publisher`). Scheduler следит за mtime этого файла: если он не обновляется 5 минут — subprocess убивается раньше общего 40-минутного таймаута. Без heartbeat зависший слот съест всю квоту.

### Pipeline-логирование (только при проблемах)

В норме НЕ вызывай `python -m tools.pipeline_log` после успешного завершения каждого агента. Scheduler сам пишет timeline-события (slot_started, slot_finished, статусы, метрики) после твоего завершения. Каждый твой вызов `python -m tools.pipeline_log` — это subprocess-запуск, который тратит сообщение Pro-лимита Claude. На статью мы экономим 7-8 messages.

Логируй ТОЛЬКО эти случаи:

**1. Возврат на агента 4** (из 5/6/quality_gate с `passed: false`):
```
python -m tools.pipeline_log {slug} 4-writer iteration_returned \
  --reason "{почему вернули}" \
  --recommendation "{что переделать}"
```

**2. Падение агента** (исключение / отсутствие нужного файла / несовместимый JSON):
```
python -m tools.pipeline_log {slug} {agent-name} failed --error "{короткий текст}"
```

**3. Каннибализация на агенте 1** — если cannibalization_check вернул conflict, логируй и останавливайся:
```
python -m tools.pipeline_log {slug} 1-semantics failed --error "cannibalization:{conflict_slug}"
```

slug ещё неизвестен на агенте 1 до создания brief.json — если он упал ДО brief.json, просто остановись с понятным stdout-сообщением, scheduler разберётся по rc.

### Шаги

1. Запусти агента `1-semantics` с category и topic. Агент сам прогоняет preflight через `tools/cannibalization_check.py` (preflight + full режимы) — отдельно его звать не надо. Дождись `drafts/{slug}/brief.json`. Если вернул `error: cannibalization` (любая стадия) — сообщи slug-и конфликтов и остановись (см. блок «Pipeline-логирование» как залогировать), не запускай агентов 2-7.
2. Запусти агента `2-legal-research` с slug. Дождись `drafts/{slug}/research.json`.
3. Запусти агента `3-architect`. **Перед запуском найди `prev_article_outline`**: возьми последний по mtime файл `drafts/*/outline.json` или `articles/{category}/*` той же категории, что текущая (если есть) — извлеки структуру блоков (имена H2, порядок, `cta_final.text`) и передай архитектору в prompt как `prev_article_outline`. Архитектор обязан отстроиться по структуре от этой статьи. Дождись `drafts/{slug}/outline.json`.
4. Запусти агента `4-writer`. Дождись `drafts/{slug}/draft.md`.
5. Запусти агента `5-uniqueness`. Если `passed: false` - возврат на агента 4 с указанием `recommendation` (одна или несколько меток). Максимум 3 итерации, после - в `drafts/_review/`. **При возврате обязательно** залогируй `iteration_returned` (см. блок «Pipeline-логирование»), чтобы scheduler видел причину.
6. Запусти агента `6-seo-editor`. Дождись `drafts/{slug}/body.html` + `meta.json`. Агент 6 САМ пишет body.html (только содержание с placeholder-комментариями BP:CTA-*, BP:DISCLAIMER) и заполняет meta.json (title, description, h1, lead, topic_action, faq и т.д.). HTML-каркас не пишет. Если `factcheck_passed: false` - возврат на агента 4 (логируй `iteration_returned`).
6a. **Сборка финального article.html (детерминированно):**
    ```
    python -m tools.inject_boilerplate drafts/{slug}/ --body body.html --out article.html
    ```
    Скрипт подставляет CTA, дисклеймер, JSON-LD, header/footer/aside из шаблонов. Exit ≠ 0:
    - 1 — отсутствуют обязательные поля meta.json (slug, category, title, description, h1, topic_action) — возврат на агента 6 с пометкой какие поля дозаполнить.
    - 2 — нет body.html — возврат на агента 6, что-то пошло не так.
    Идеально агент 6 сам зовёт этот скрипт в финале своей работы — тогда мы экономим один re-invocation.
7. **Обязательный шаг: quality_gate.** Запусти `python -m tools.quality_gate drafts/{slug}/article.html --json --save-report`. Если exit ≠ 0 - читай `drafts/{slug}/quality_gate.json`, поле `recommendations`, и возвращай на агента 4 с конкретной пометкой. Максимум 3 итерации возврата. После третьей - в `drafts/_review/`. **quality_gate сам пишет своё событие в pipeline_log через scheduler — отдельно логировать не нужно**.
8. Запусти агента `7-publisher`. Он только финализирует drafts/{slug}/ (записывает поля ready_for_review=true в meta.json и добавляет запись в drafts/_review_queue.json). На сайт НЕ публикует — публикация только через нажатие заказчиком "Опубликовать" в Telegram-боте, который вызывает bot/publisher.py. Никаких git push, articles/, картинок здесь не происходит.

**Важно:** даже если ты пропустишь шаг 7 (quality_gate) - scheduler всё равно его запустит после твоего завершения. Если gate упадёт, scheduler пометит слот как `failed_qa` и заблокирует публикацию. Лучше прогнать самому, чтобы успеть зациклить итерации с агентом 4.

## Параметры из батча (если /write-article запускается из /batch-run)

Когда конвейер запускается оркестратором батча, в prompt каждого агента 4 (включая итерации возврата) ОБЯЗАТЕЛЬНО передаются дополнительные поля:

- `batch_position` - порядковый номер статьи в текущем батче (1, 2, 3...). Нужен для нарастающей строгости антишаблонности.
- `batch_total` - общее количество статей в батче.
- `prev_article_summary` - краткие тезисы (3-5 строк) предыдущей статьи в этом батче: вступление одной фразой, перечень H2, формула финального абзаца. Это нужно, чтобы писатель явно отстраивался от предыдущего соседа, а не подсознательно повторял.
- `prev_article_slug` - slug предыдущей статьи (для трассировки).

При первой статье в батче (`batch_position = 1`) поля `prev_article_summary` и `prev_article_slug` отсутствуют.

При одиночном запуске `/write-article` (не из батча) этих полей нет вовсе - писатель работает только по своему правилу «одна последняя статья в категории + эталон».

## Передача рекомендаций при возврате на агента 4

Когда агент 5 или 6 возвращает с `passed: false`, в prompt итерации агента 4 кроме исходных JSON-входов передаётся:

- `previous_iteration_issues` - список конкретных проблем из `recommendation` агента 5/6 / `tools/quality_gate.py`:
  - `fix_ai_markers` + список найденных маркеров с цитатами (critical)
  - `reduce_ai_markers` + топ-3 категории high-маркеров (раздувание/реклама/параллелизмы)
  - `reduce_density` + текущая density и какое слово/фраза переоптимизированы
  - `reduce_repetition` + **топ-5 самых частотных лемм с количеством вхождений** (обязательно перечислить)
  - `fix_abbreviations` + список конкретных вхождений с контекстом (если автофикс не справился)
  - `fix_punctuation` + примеры мест без пробела после точки
  - `reduce_ai_score` + значение AI-detector text.ru и список самых сильных маркеров
  - `reduce_length` / `expand_length` + конкретное число знаков и целевой коридор
  - `fix_voice` + цитаты с «я», «мой опыт», «в моей практике» (заменить на «мы»)
  - `rewrite_with_angle:<angle>` + конкретный угол подачи

Писатель в итерации правит точечно по списку, не переписывает всё заново.

При `reduce_repetition` обязательно использовать recipe из `writer-cheatsheet.md` («Контроль заспамленности»): по каждой лемме из топ-5 уменьшить вхождения минимум на 30%, замены первой очереди (должник→он/заёмщик/гражданин, процедура→дело/порядок и т.д.).

## Отчёт по завершении

Кратко: slug, категория, длина статьи, прошёл ли uniqueness и factcheck, режим публикации, путь к готовому файлу.

Также вывести: `textru_ai_detector`, `textru_uniqueness`, `textru_spam` из `meta.json` - чтобы заказчик видел метрики каждой статьи без захода в text.ru.

## Если конвейер упал

Указать на каком агенте, что в логах, какой файл получился последним. Не пытаться "почистить" частичные drafts/ - оставить как есть для разбора.
