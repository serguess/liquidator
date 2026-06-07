---
description: Дописать статью когда есть outline.json, но нет draft.md (зависло между агентом 3 и 4). Запускает агентов 4→5→6→сборку→quality_gate→7→finalize. Закрывает разрыв, который не покрывают /continue-article (с агента 2) и /finish-article (с агента 6).
argument-hint: slug=<значение>
---

Доведи статью до публикации, начиная с агента 4 (writer). У слота уже есть `drafts/{slug}/brief.json`, `research.json`, `outline.json` — не пересоздавай их.

Аргументы: $ARGUMENTS

Формат: `slug=foo-bar-2026`.

## Контекст и зачем эта команда

При зависании между агентом 3 (architect) и агентом 4 (writer) слот падает: `outline.json` уже есть, но `draft.md` ещё нет. `/continue-article` стартует с агента 2 (переделает outline зря), `/finish-article` стартует с агента 6 (нет draft.md — не сработает). Эта команда закрывает разрыв: **продолжить с шага 4**, не повторяя 1-3.

## Обязательные условия

1. **`drafts/{slug}/outline.json` ДОЛЖЕН существовать.** Если нет — используй `/continue-article` (стартует с агента 2).
2. **`drafts/{slug}/research.json` и `brief.json`** должны быть — без них агент 4 не работает.
3. **`drafts/{slug}/draft.md` НЕ должен быть готов** (отсутствует или < 5000 байт). Если draft.md уже полный — используй `/finish-article` (стартует с агента 6).

Если условия не сходятся — остановись и сообщи, какую команду использовать вместо этой.

## Heartbeat

Перед каждым шагом:
```bash
date -u +"%Y-%m-%dT%H:%M:%S | агент-N" > data/.scheduler_heartbeat
```

## Шаги (начинаем с 4)

1. **Проверка артефактов:**
   ```bash
   ls -la drafts/{slug}/
   ```
   Должны быть outline.json, research.json, brief.json. draft.md — отсутствует или неполный. Если не так — останься и подскажи команду.

1a. **Подстраховочная валидация outline** (вдруг зависло во время неё):
   ```bash
   .venv/bin/python -m tools.outline_validate drafts/{slug}/outline.json --fix
   ```
   Если `[FIXES NEEDED]` — это мелкие правки H2/структуры; их сделает агент 4 по ходу, отдельный перезапуск архитектора здесь НЕ нужен (цель — добить слот). Просто иди дальше.

2. **Запуск агента `4-writer`** с slug. Дождись `drafts/{slug}/draft.md`.

3. **Запуск агента `5-uniqueness`.** Если `passed: false` — 1 возврат на агента 4, потом дальше (gate решит финально). Больше одного возврата не делать.

4. **Запуск агента `6-seo-editor`** с slug. Дождись `drafts/{slug}/body.html` + `meta.json`.

5. **Сборка article.html:**
   ```bash
   .venv/bin/python -m tools.inject_boilerplate drafts/{slug}/ --body body.html --out article.html
   ```

6. **quality_gate:**
   ```bash
   .venv/bin/python -m tools.quality_gate drafts/{slug}/article.html --json --save-report
   ```
   Читай `drafts/{slug}/quality_gate.json -> blockers`. Если непусто — 1 возврат на агента 4 (не больше; gate сам сделает forced_pass на следующей итерации).

7. **Запуск агента `7-publisher`** — пишет `drafts/{slug}/scene.txt`.

8. **Финализация:**
   ```bash
   .venv/bin/python -m articles_scheduler.finalize_draft {slug}
   ```
   Сгенерирует обложку, проставит `ready_for_review=true`, добавит запись в `drafts/_review_queue.json`.

9. **Вывод:** короткий отчёт. Бот-watcher подхватит новый драфт и отправит/поставит в batch.

## Что НЕ делать

- НЕ запускать агентов 1-3 (outline.json уже есть и валиден).
- НЕ пересоздавать brief.json / research.json / outline.json.
- НЕ ходить в WebSearch.

## Когда использовать

- Зависание/падение между агентом 3 и 4 (есть outline.json, нет draft.md).
- НЕ для слотов с готовым draft.md (там `/finish-article`).
- НЕ для слотов без outline.json (там `/continue-article`).
