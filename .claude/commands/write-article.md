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

НЕ генерируй свой slug, даже если кажется красивее. Slug связан с записью в `drafts/_topic-map/{category}.json`. Если ты используешь другой slug — тема в topic-map останется «свободной», и следующий слот scheduler'а её снова выберет → бесконечный цикл по одной теме.

Если `slug=...` нет в аргументах (старый формат вызова) — запусти `.venv/bin/python -m tools.slugify "{title}"` для детерминированной генерации.

## Конвейер (строго последовательно)

### Heartbeat (обязательно перед каждым агентом)

Перед запуском каждого агента (1, 2, 3, 4, 5, 6, 7) и перед длинными скриптами (quality_gate, inject_boilerplate, finalize_draft) обнови heartbeat-файл одной командой:

```bash
date -u +"%Y-%m-%dT%H:%M:%S | агент-N" > data/.scheduler_heartbeat
```

Где `агент-N` — имя текущего шага (например `1-semantics`, `quality_gate`, `7-publisher`). Scheduler следит за mtime этого файла: если он не обновляется 5 минут — subprocess убивается раньше общего 40-минутного таймаута. Без heartbeat зависший слот съест всю квоту.

### Pipeline-логирование (только при проблемах)

В норме НЕ вызывай `.venv/bin/python -m tools.pipeline_log` после успешного завершения каждого агента. Scheduler сам пишет timeline-события (slot_started, slot_finished, статусы, метрики) после твоего завершения. Каждый твой вызов `.venv/bin/python -m tools.pipeline_log` — это subprocess-запуск, который тратит сообщение Pro-лимита Claude. На статью мы экономим 7-8 messages.

Логируй ТОЛЬКО эти случаи:

**1. Возврат на агента 4** (из 5/6/quality_gate с `passed: false`):
```
.venv/bin/python -m tools.pipeline_log {slug} 4-writer iteration_returned \
  --reason "{почему вернули}" \
  --recommendation "{что переделать}"
```

**2. Падение агента** (исключение / отсутствие нужного файла / несовместимый JSON):
```
.venv/bin/python -m tools.pipeline_log {slug} {agent-name} failed --error "{короткий текст}"
```

**3. Каннибализация на агенте 1** — если cannibalization_check вернул conflict, логируй и останавливайся:
```
.venv/bin/python -m tools.pipeline_log {slug} 1-semantics failed --error "cannibalization:{conflict_slug}"
```

slug ещё неизвестен на агенте 1 до создания brief.json — если он упал ДО brief.json, просто остановись с понятным stdout-сообщением, scheduler разберётся по rc.

### Шаги

1. Запусти агента `1-semantics` с category и topic. Агент сам прогоняет preflight через `tools/cannibalization_check.py` (preflight + full режимы) — отдельно его звать не надо. Дождись `drafts/{slug}/brief.json`. Если вернул `error: cannibalization` (любая стадия) — сообщи slug-и конфликтов и остановись (см. блок «Pipeline-логирование» как залогировать), не запускай агентов 2-7.
2. Запусти агента `2-legal-research` с slug. Дождись `drafts/{slug}/research.json`.
3. Запусти агента `3-architect`. Дождись `drafts/{slug}/outline.json`. Затем прогони детерминированную валидацию:
   ```bash
   .venv/bin/python -m tools.outline_validate drafts/{slug}/outline.json --fix
   ```
   Она авто-подрезает длины блоков и проверяет повтор корней в H2 + похожесть на предыдущую статью категории (соседа находит сама — вручную `prev_article_outline` собирать НЕ нужно). Если `exit != 0` и в выводе есть `[FIXES NEEDED]` — перезапусти агента `3-architect` РОВНО ОДИН раз, передав ему этот список правок, затем сразу к шагу 4 (валидацию повторно НЕ гоняй). Если `exit = 0` — сразу к шагу 4.
4. Запусти агента `4-writer`. Дождись `drafts/{slug}/draft.md`.
5. Запусти агента `5-uniqueness`. Если `passed: false` - возврат на агента 4 с указанием `recommendation`. **Максимум 1 итерация возврата здесь** (раньше было 5). После — продолжай к агенту 6, gate финально решит. **При возврате обязательно** залогируй `iteration_returned`.
6. Запусти агента `6-seo-editor`. Дождись `drafts/{slug}/body.html` + `meta.json`. Агент 6 САМ пишет body.html и заполняет meta.json. Если `factcheck_passed: false` - возврат на агента 4 (**максимум 1 итерация здесь**, логируй `iteration_returned`).
6a. **Сборка финального article.html (детерминированно):**
    ```
    .venv/bin/python -m tools.inject_boilerplate drafts/{slug}/ --body body.html --out article.html
    ```
    Скрипт подставляет CTA, дисклеймер, JSON-LD, header/footer/aside из шаблонов. Exit ≠ 0:
    - 1 — отсутствуют обязательные поля meta.json (slug, category, title, description, h1, topic_action) — возврат на агента 6 с пометкой какие поля дозаполнить.
    - 2 — нет body.html — возврат на агента 6, что-то пошло не так.
    Идеально агент 6 сам зовёт этот скрипт в финале своей работы — тогда мы экономим один re-invocation.
7. **Обязательный шаг: quality_gate.** Запусти `.venv/bin/python -m tools.quality_gate drafts/{slug}/article.html --json --save-report`. Если exit ≠ 0 — читай `drafts/{slug}/quality_gate.json`, поле `recommendations`, и возвращай на агента 4 с конкретной пометкой. **Максимум 1 итерация возврата**. На 2-й итерации writer'а (учитывая возвраты от 5/6) gate сам делает **forced_pass с `metrics_warning=true`** — статья всё равно идёт в TG-очередь.

   **Глобальный cap итераций writer'а = 2** (изменено 26 мая 2026 с 3). Это сумма всех возвратов: от агента 5 + от агента 6 + от quality_gate. Анализ показал что 3-я итерация не улучшает метрики, статья всё равно идёт в forced_pass.

   **Приоритет блокеров (после 13 мая 2026):**
   - **Hard на любой итерации:** `targeted_tokens_over_limit` (ст/РФ/руб/ООО/X000руб), `author_markers_missing`, `too_few_short_sentences`, `too_many_long_sentences`, `anti_template_phrases`, `ai_markers_critical`, `ai_markers_density`, `ai_markers_high`, `first_person_singular`, `law_quotes_too_long`, `abbreviations_after_autofix`, `punctuation_after_autofix`.
   - **Soft с iteration ≥ 2:**
     - `length_too_long` → warning, если text_chars ≤ 8500 (default) / 7500 (news).
     - `spam_risk` → warning, если все 3 ratio-метрики в коридоре (top1≤14, top10≤0.120, ngram3≤0.060, lex_div≥0.55) И targeted_tokens чистые.
   - **Force-pass на iter=2:** все оставшиеся блокеры пропускаются, в meta.json пишется `metrics_warning=true` + `metrics_warning_blockers`. Статья идёт в очередь.

   **Счётчик итераций** хранится в `quality_gate.json:retry_count` — gate инкрементирует его при каждом запуске.
7b. **Выбор шаблона обложки (детерминированно, ПЕРЕД агентом 7):**
   ```bash
   .venv/bin/python -m articles_scheduler.pick_scene_template {slug} {category}
   ```
   Скрипт ротации (с 16 мая 2026) выбирает один template_id из 30 (см. `.claude/style/cover-scenes.md`), исключая последние 12 использованных из `data/scene_history.json`. Результат пишется в `drafts/{slug}/scene_template.txt` в формате `template_id=N`. История апдейтится автоматически.

   Зачем: раньше выбор делал sonnet-агент 7, и он стабильно сваливался на категорийный default (fiz→10, yur→25, vzysk→3, news→12) → у всех статей одной категории была одинаковая обложка. Python-скрипт гарантирует, что в одном батче 10 статей/день не будет ни одного повтора, и за ~3 дня все 30 сцен пройдут.

   Exit ≠ 0 — крайне редкий случай (нет папки `drafts/{slug}/`). Если упало — пропусти и зови агента 7 как раньше; агент 7 увидит отсутствие `scene_template.txt` и выведет `scene_skipped`, finalize_draft возьмёт fallback по категории.

8. Запусти агента `7-publisher`. С 16 мая 2026 он **не выбирает template_id сам** — читает уже выбранный из `drafts/{slug}/scene_template.txt`, открывает соответствующий шаблон в `.claude/style/cover-scenes.md` и адаптирует его под смысл статьи (подбирает 3-7 предметов из allowed pool под тему). Финальная английская scene-строка пишется в `drafts/{slug}/scene.txt`. Ни meta.json, ни картинок, ни очереди ревью он не трогает. **Файл `scene_template.txt` агент 7 НЕ перезаписывает** — иначе сломается ротация.

9. **Финализация драфта (детерминированно):**
   ```bash
   .venv/bin/python -m articles_scheduler.finalize_draft {slug}
   ```
   Скрипт сам:
   - валидирует article.html (≥5000 байт), meta.json (обязательные поля), quality_gate.json;
   - читает scene.txt от агента 7 (если файла нет/пуст — fallback на CATEGORY_SCENE_DEFAULT по category);
   - вызывает `tools.image_gen.generate_and_upload_cover` напрямую через import (с одним retry при провале) → fal.ai → лого → Cloudinary → запись `cover_url`/`cover_url_master`/`image_prompt`/`cover_uploaded_at` в meta.json;
   - дописывает в meta.json: `ready_for_review=true`, `ready_at`, `publication_target=telegram_review`;
   - добавляет запись в `drafts/_review_queue.json` (slug, category, title, added_at, char_count, cover_url, status, quality_gate). Идемпотентно: при повторном запуске запись с тем же slug обновляется на месте.

   Exit codes:
   - 0 — драфт финализирован (даже если обложка не сгенерилась — тогда `cover_generation_failed=true` в meta, статья всё равно идёт в очередь без обложки);
   - 1 — структурная проблема (нет article.html / нет обязательных полей meta / quality_gate.hard_failed). Slot уйдёт в failed_qa.

   На сайт НЕ публикуется — публикация только через нажатие заказчиком "Опубликовать" в Telegram-боте, который вызывает bot/publisher.py. Никаких git push, articles/, изменения articles.json/sitemap.xml здесь не происходит.

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
