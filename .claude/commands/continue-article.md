---
description: Восстановить упавшую статью с шага 2 (legal-research). Использовать когда у слота уже есть brief.json, но research.json не создан (hang_heartbeat_timeout на агенте 2). Пропускает агента 1 — экономит 2-3 мин и не пересоздаёт brief.
argument-hint: slug=<значение>
---

Восстанови написание статьи начиная с агента 2 — у слота уже есть `drafts/{slug}/brief.json` от предыдущей попытки, не пересоздавай его.

Аргументы: $ARGUMENTS

Формат: `slug=foo-bar-2026` (обязательный) — папка драфта в `drafts/{slug}/`.

## Контекст и зачем эта команда

С 14 мая 2026: на агенте 2 (legal-research) случаются hang_heartbeat_timeout на WebSearch. После kill слот помечается `failed` или `topics_expanded`, тема rejected. Но `brief.json` остаётся в драфте. Эта команда позволяет **продолжить с шага 2** на той же теме, не выбирая новую через _next_category и не дублируя работу агента 1.

## Обязательные условия

1. **`drafts/{slug}/brief.json` ДОЛЖЕН существовать.** Если нет — останавливайся и сообщи. Запускать /continue-article на чистой папке бессмысленно.
2. **Тема должна быть валидной** — не утратившая силу норма, не cannibalization. Если в `data/topic_rejects.json` или коммитах есть запись `topic: reject slug=<этот slug>` — это нормально для restore, флаг reject ставится для scheduler автоматического выбора, но ручной restore разрешён.

## Heartbeat (обязательно перед каждым агентом)

```bash
date -u +"%Y-%m-%dT%H:%M:%S | агент-N" > data/.scheduler_heartbeat
```

## Шаги

1. **Проверка brief.json.** Если файла `drafts/{slug}/brief.json` нет — остановись с сообщением «brief отсутствует, используй /write-article вместо /continue-article».

2. **Запуск агента `2-legal-research`** с slug. Дождись `drafts/{slug}/research.json`. **ВАЖНО:** агент 2 должен соблюдать **минимизированный бюджет WebSearch** (≤1 запрос, ≤3 WebFetch). Если ENV `WEBSEARCH_BUDGET=0` — НЕ делать WebSearch вообще, использовать только legal-facts.md + cache.

3. **Запуск агента `3-architect`** с slug + `prev_article_outline` (последний по mtime outline.json из `drafts/*/` той же категории). Дождись `outline.json`.

4. **Запуск агента `4-writer`**. Дождись `draft.md`.

5. **Запуск агента `5-uniqueness`**. При `passed: false` — 1 возврат на агента 4.

6. **Запуск агента `6-seo-editor`**. Дождись `body.html` + `meta.json`.

6a. **Сборка article.html:**
   ```bash
   .venv/bin/python -m tools.inject_boilerplate drafts/{slug}/ --body body.html --out article.html
   ```

7. **quality_gate:**
   ```bash
   .venv/bin/python -m tools.quality_gate drafts/{slug}/article.html --json --save-report
   ```
   Если `exit != 0` — 1 возврат на агента 4 с конкретной пометкой.

8. **Запуск агента `7-publisher`** (sonnet) — пишет `drafts/{slug}/scene.txt`.

9. **Финализация:**
   ```bash
   .venv/bin/python -m articles_scheduler.finalize_draft {slug}
   ```

10. **Вывод:** короткий отчёт о слоте (длительность, итерации, passed metrics). Заказчица увидит его в TG через pending_batch (статья помечается `pending_batch=true` в state, отправится в ближайший 10:00 МСК batch).

## Что НЕ делать

- НЕ запускать агента 1 (он уже отработал, brief есть).
- НЕ создавать новый slug — использовать ровно тот, что в аргументе.
- НЕ пересоздавать brief.json (это потерь информации).
- НЕ обходить агента 2, даже если фактов мало — research.json обязателен для writer и seo-editor.

## Когда использовать

- После hang на агенте 2 у конкретной темы, когда заказчица хочет статью именно по этой теме.
- При ручном тестировании WebSearch budget правок.
- НЕ использовать в auto-retry watcher — там лучше брать новую тему через _next_category.
