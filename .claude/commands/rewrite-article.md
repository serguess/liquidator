---
description: Точечная доработка существующей статьи по фидбеку quality_gate. Использование - /rewrite-article {slug}. НЕ запускает агента 1 и 2 (research уже есть), стартует от агента 3 или 4 в зависимости от проблем. Экономит токены в 2-3 раза против полного перезапуска.
argument-hint: <slug>
---

Точечная доработка статьи `drafts/$ARGUMENTS/`.

## Когда применяется

- После провала quality_gate (`drafts/{slug}/quality_gate.json` exists, `passed: false`).
- Заказчик нажал в боте «Правки» с фидбеком — статья получила новый score, но не прошла, нужна доработка.
- Scheduler в начале слота нашёл застрявшую failed_qa-статью и хочет её доработать вместо новой темы.

## Принцип экономии

Полный конвейер (`/write-article`) запускает все 7 агентов с нуля (≈70-100к токенов). Доработка экономит токены так:

- **НЕ запускает агента 1** (семантика уже сделана: `brief.json` есть).
- **НЕ запускает агента 2** (research уже сделан: `research.json` есть).
- Может пропустить агента 3 если outline нормальный (только пересмотр длин/перелинковки).
- Запускает только тех агентов, чьи артефакты надо обновить.

Типичная экономия: **2-3× меньше токенов** против полного `/write-article`.

## Алгоритм

### 1. Проверка входных данных

Прочитай:
- `drafts/{slug}/quality_gate.json` — список blockers и recommendations
- `drafts/{slug}/meta.json` — последние метрики
- `drafts/{slug}/brief.json` (для контекста)
- `drafts/{slug}/research.json` (для фактчека, не пересобираем)
- `drafts/{slug}/outline.json` (если есть)

Если ни одного из этих файлов нет — это не доработка, это новая статья. Сообщи и остановись.

### 2. Определение что переделывать

По полю `quality_gate.json -> recommendations` строишь маршрут:

| recommendation | Куда возвращаться |
|---|---|
| `reduce_length` / `expand_length` | агент 4 (writer) — точечная переработка длины |
| `reduce_repetition` + топ-5 лемм | агент 4 — replace topы лемм |
| `fix_voice` («я» → «мы») | агент 4 — точечная замена |
| `fix_ai_markers` (critical) | агент 4 — убрать критичные маркеры |
| `reduce_ai_markers` + топ-3 категории | агент 4 — anti-AI rewrite pass |
| `anti_ai_rewrite` (rhythm flags) | агент 4 — anti-AI rewrite pass |
| `rewrite_unique` (anti-template hits) | агент 4 — перифразировать шаблонные фразы |
| `reduce_law_quotes` (>600 знаков цитат) | агент 4 — заменить цитаты на пересказ |
| `fix_abbreviations` (после автофикса) | агент 4 — ручная правка |
| `rewrite_with_angle:<X>` (от агента 5) | агент 3 → агент 4 — новый outline + переписать |

Если несколько recommendations и все ведут на писателя — **один проход агента 4** с консолидированным списком правок.

Если есть `rewrite_with_angle` — агент 3 переделывает outline (читая `prev_article_outline` из соседних статей категории), потом агент 4 пишет.

### 3. Запуск только нужных агентов

Запускай **минимально достаточный** набор:

- Только writer-итерация: `4-writer` → `quality_gate`. Не запускай 1, 2, 3, 5, 6 если quality_gate просит только текстовых правок.
- При смене угла: `3-architect` → `4-writer` → `5-uniqueness` → `6-seo-editor` → `quality_gate`.
- Фактчек: `6-seo-editor` (если quality_gate ругается на factcheck — только агент 6 нужен).

### 4. Pipeline-логирование (обязательно)

В начале:
```
python -m tools.pipeline_log {slug} scheduler rewrite_started \
  --reason "quality_gate failed: {краткие blockers}" \
  --recommendation "{какие меры применяем}"
```

При возврате на агента 4:
```
python -m tools.pipeline_log {slug} 4-writer iteration_returned \
  --reason "rewrite-article" \
  --recommendation "{из quality_gate.recommendations}"
```

После завершения writer и других агентов — обычные `completed` события (как в /write-article).

В конце (после нового quality_gate):
```
python -m tools.pipeline_log {slug} scheduler rewrite_finished \
  --reason "{passed: true|false}"
```

### 5. Пересборка article.html + финальный quality_gate

Если правил body.html (а не сам article.html) — пересобрать через шаблонизатор:
```
python -m tools.inject_boilerplate drafts/{slug}/ --body body.html --out article.html
```

Если агент правил body.html через writer/editor (типичный кейс), без пересборки изменения не попадут в article.html. Если правил сразу article.html (точечно, с сохранением структуры) — этот шаг можно пропустить.

После сборки — обязательный запуск quality_gate:
```
python -m tools.quality_gate drafts/{slug}/article.html --json --save-report
```

Если опять failed → можно повторить /rewrite-article ещё раз, но **максимум 5 итераций** (считается из `quality_gate.json -> retry_count`, fallback на `_pipeline.log.json -> current_iteration`). После пятой — статья уходит в `drafts/_review/` и требует ручного разбора.

**Приоритет блокеров** (`tools/quality_gate.py`):
- **Hard на любой итерации:** `spam_risk`, `anti_template_phrases`, `ai_markers_critical/density/high`, `first_person_singular`, `law_quotes_too_long`. Любой из этих блокеров — возврат писателя.
- **Soft с iteration ≥ 2:** `length_too_long` становится warning, если text_chars ≤ 9000 default / 8000 news и других блокеров нет. Не отправляй писателя на новую итерацию только из-за длины после первого ретрая.

## Лимит проходов писателя при /rewrite-article

- В одном запуске /rewrite-article — **1 проход агента 4** (точечная правка).
- Не больше — иначе слив токенов.
- Если первый проход не помог (quality_gate всё ещё failed) — **новый /rewrite-article** в следующем слоте, не сразу.

## Что в результате

- `drafts/{slug}/article.html` обновлён точечно
- `drafts/{slug}/meta.json` обновлён
- `drafts/{slug}/quality_gate.json` обновлён с новым результатом
- `drafts/{slug}/_pipeline.log.json` дописан событиями rewrite

## Если quality_gate прошёл

- Статус слота: `ok` (статья готова к публикации заказчиком в боте).
- Scheduler коммитит и пушит как обычно.
- Бот не отправляет повторное уведомление (это та же статья, не новая) — заказчик сам увидит обновление через `/queue` или новое нажатие «Превью».
