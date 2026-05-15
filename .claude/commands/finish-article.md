---
description: Дописать статью когда уже есть draft.md (агенты 1-4/5 прошли). Запускает агента 6 (seo-editor) → сборку → quality_gate → агента 7 → finalize. Использовать при failed_qa слотах где writer успел отдать draft.md, но дальше зависло.
argument-hint: slug=<значение>
---

Доведи статью до публикации, начиная с агента 6. У слота уже есть `drafts/{slug}/draft.md`, `research.json`, `outline.json` (и, возможно, `uniqueness.json`) — не пересоздавай их.

Аргументы: $ARGUMENTS

Формат: `slug=foo-bar-2026`.

## Контекст и зачем эта команда

При hang_heartbeat_timeout на агенте 6 или после слот помечается failed_qa. Но writer уже сделал работу — `draft.md` сохранён. Эта команда позволяет **продолжить с шага 6**, не повторяя 1-5.

## Обязательные условия

1. **`drafts/{slug}/draft.md` ДОЛЖЕН существовать** и быть ≥ 5000 байт. Если меньше — writer не закончил, лучше /continue-article.
2. **`drafts/{slug}/research.json`, `outline.json`** должны быть — без них агент 6 не работает.

Если что-то отсутствует — остановись и сообщи: «<file> отсутствует, используй /write-article вместо /finish-article».

## Heartbeat

Перед каждым шагом:
```bash
date -u +"%Y-%m-%dT%H:%M:%S | агент-N" > data/.scheduler_heartbeat
```

## Шаги (начинаем с 6)

1. **Проверка артефактов:**
   ```bash
   ls -la drafts/{slug}/
   ```
   Должны быть draft.md (≥5000 байт), research.json, outline.json. Если нет — остановись.

2. **Запуск агента `6-seo-editor`** с slug. Дождись `drafts/{slug}/body.html` + `meta.json`.

3. **Сборка article.html:**
   ```bash
   .venv/bin/python -m tools.inject_boilerplate drafts/{slug}/ --body body.html --out article.html
   ```

4. **quality_gate:**
   ```bash
   .venv/bin/python -m tools.quality_gate drafts/{slug}/article.html --json --save-report
   ```
   Если `exit != 0` — 1 возврат на агента 4 с конкретной пометкой (но **не больше одного** — статья уже один раз пыталась, цикл не нужен). На 2-й попытке — force-pass с `metrics_warning=true`.

5. **Запуск агента `7-publisher`** — пишет `drafts/{slug}/scene.txt`.

6. **Финализация:**
   ```bash
   .venv/bin/python -m articles_scheduler.finalize_draft {slug}
   ```
   Это сгенерирует обложку через fal.ai + Cloudinary, проставит `ready_for_review=true`, добавит запись в `drafts/_review_queue.json`.

7. **Вывод:** короткий отчёт. После этого бот-watcher в течение 60 сек подхватит новый драфт и пометит `pending_batch=true` (если batch-режим включён) или отправит сразу (если до batch_start_at).

## Что НЕ делать

- НЕ запускать агентов 1-4 (draft.md уже есть).
- НЕ пересоздавать research.json / outline.json.
- НЕ удалять uniqueness.json (если есть — он валиден, проверка уже была).
- НЕ ходить в WebSearch (агент 6 не имеет права, его tools=Read+Write+Bash).

## Когда использовать

- При failed_qa слотах с готовым draft.md.
- Для ручного завершения слота после ночного hang'a.
- НЕ для слотов где hang был на агенте 2 (там брат-команда /continue-article).
