# Карта тем (topic map)

Это предложения тем для SEO-конвейера от Агента 1 (семантика и интент). До запуска статей в работу заказчик подтверждает каждую тему.

## Файлы

- `fiz.json` - физлица, списание долгов
- `yur.json` - юрлица, ликвидация компаний
- `vzysk.json` - взыскание задолженности (для кредиторов)
- `news.json` - новости и изменения законодательства

В каждом файле 8 тем. Поля:

- `id` - короткий идентификатор (`fiz-01`, `yur-03` и т.д.)
- `slug` - URL-сегмент будущей статьи
- `title`, `description`, `h1` - SEO-заголовки и метатеги
- `main_keyword` - главный ключ, плотность ≤2.5%
- `secondary_keywords` - вторичные ключи
- `intent` - что хочет читатель: `problem-aware` (в проблеме), `solution-aware` (выбирает), `commercial` (готов покупать), `informational` (просто узнать)
- `funnel_stage` - стадия воронки: `awareness` (узнал), `consideration` (сравнивает), `decision` (готов)
- `article_type` - формат: `step-by-step`, `comparison`, `case-study`, `law-explanation`, `faq`, `myths`
- `offer` - какой CTA-блок поставить в статью
- `frequency_estimate` - грубая оценка частотности: `high` / `medium` / `low`
- `rationale` - почему эта тема важна
- `expected_length_chars` - целевой объём готовой статьи
- `status` - см. ниже
- `client_notes` - поле для комментариев заказчика

## Как менять статус

Для каждой темы поле `status` принимает значения:

- `proposed` - предложено агентом, ждёт решения (значение по умолчанию)
- `approved` - одобрено, можно запускать в работу
- `rejected` - не подходит, агент исключит из дальнейших итераций
- `rewrite` - формулировка/угол хорошие, но нужно переделать. В `client_notes` опишите, что именно поменять (тон, ключ, угол)

### Через GitHub web

1. Откройте нужный файл (`fiz.json` и т.д.) в репозитории на github.com
2. Нажмите карандаш (Edit this file)
3. Поменяйте `"status": "proposed"` на нужное значение
4. При необходимости впишите комментарий в `"client_notes": ""`
5. Внизу страницы: Commit changes

После коммита оркестратор подхватит approved-темы в следующем прогоне.

## Что писать в client_notes

Поле свободное. Полезные варианты:

- "Тон сделай мягче, аудитория - пенсионеры"
- "Добавь акцент на регион Москва и МО"
- "Замени главный ключ на 'банкротство пенсионера в 2026 году'"
- "Раздели на pillar + 2 spoke (см. fiz-01)"
- "Не наша тема - rejected"

## Какой объём статей ждать

Колонка `expected_length_chars` - целевой объём готовой статьи. По нашим стандартам:

- Минимум 6000 знаков (меньше Яндекс считает тонким контентом, особенно в YMYL)
- Базовая длина 8000-10000 знаков
- Сложные комплексные темы 11000-12000
- Максимум 15000 (выше падает дочитываемость)

Если тема явно требует больше 15000, Агент 1 разобьёт её на pillar + spokes (одна обзорная статья и 2-4 узких) с перелинковкой.

## Уже сделанные темы

Не предлагаются и не дублируются:

**Опубликовано (articles.json):**

- kak-spisat-dolgi-cherez-bankrotstvo-fizicheskogo-lica
- bankrotstvo-yuridicheskih-lic-poshagovaya-instrukciya
- kak-vernut-dolg-s-kontragenta-bez-suda
- chem-grozit-neuplata-kreditov-i-zajmov
- kakoe-imushchestvo-ne-mogut-zabrat-pri-bankrotstve
- subsidiarnaya-otvetstvennost-rukovoditelya
- ispolnitelnoe-proizvodstvo-chto-delat-dolzhniku
- izmeneniya-v-zakone-o-bankrotstve-2024

**В черновиках (drafts/):**

- spisat-dolgi-po-kreditam-bez-imushchestva
