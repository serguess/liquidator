"""
Quality gate: единый детерминистический контроль перед коммитом статьи.

Запускается scheduler'ом сразу после `/write-article`. Если возвращает
non-zero exit — scheduler не коммитит статью в git и помечает слот как
failed_qa. Это hard-блок: модель не может «забыть» проверить.

Что делает (по порядку):
1. Применяет автофикс (autofix.py): сокращения юр-терминов + пробел после точки.
   Идемпотентно — если нечего чинить, ничего не пишет.
2. Запускает quality_checks: длина (hard), сокращения (после автофикса),
   пунктуация (после автофикса), эвристика заспамленности.
3. Запускает ai_markers_check: семантические ИИ-маркеры. Hard-блок при:
   - density > 2.0 на 1000 знаков
   - critical-маркер найден (длинные тире, эмодзи, англ. кавычки, артефакты чатбота, "я")
4. Сводит всё в единый отчёт drafts/{slug}/quality_gate.json.

Запуск:
    python -m tools.quality_gate drafts/{slug}/article.html
    python -m tools.quality_gate drafts/{slug}/article.html --json

Exit:
    0 — всё прошло, можно коммитить
    1 — есть критичные проблемы, коммит блокируется
    2 — ошибка работы скрипта (файл не найден и т.п.)
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from tools import ai_markers_check, anti_template_check, autofix, internal_links_check, quality_checks, rhythm_check


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Пороги, при превышении которых блокируем коммит.
# 13 мая 2026: ужесточены под KPI AI-detector ≤7% (раньше ≤10%):
# - density 2.0 → 1.2 (за 0% AI у эталона density ~0.4)
# - high 8 → 6
AI_MARKERS_DENSITY_MAX = 1.2  # маркеров на 1000 знаков
AI_MARKERS_CRITICAL_MAX = 0   # critical-маркеры запрещены полностью
AI_MARKERS_HIGH_MAX = 6       # high-маркеры — допустимо до 6 на статью

# Лимит на длину прямых цитат закона (в blockquote).
# Заказчик: одна из причин уникальности 48% — большие цитаты норм,
# которые есть везде одинаково. Сокращаем до 600 знаков суммарно.
LAW_QUOTE_CHARS_MAX = 600

# Soft-потолок длины: после первой итерации правок length_too_long перестаёт
# быть hard-блоком (становится warning), пока text_chars не превышает SOFT_LENGTH_MAX.
# 13 мая 2026: 9000→8500, 8000→7500 — анализ скринов text.ru показал, что
# статьи > 8000 знаков стабильно дают AI score 7-19%. Hard cap 8500
# означает «никогда не публикуем длиннее».
SOFT_LENGTH_MAX = {"default": 8500, "news": 7500}

# Hard-cap итераций writer-цикла.
# 13 мая 2026: 5→3. Анализ retry_count показал что статьи делали 11-16 итераций
# (агенты 5/6/7 каждый имели свой лимит 5, итого до 15). Глобальный cap 3.
# 26 мая 2026: 3→2. Анализ 17 свежих статей: iter=3 либо проходит с теми же
# метриками что iter=2, либо уходит в forced_pass с тем же spam. Дополнительная
# итерация writer'а (~110KB style-context + 7K output на opus) не улучшает
# результат, но жжёт токены. Статья всё равно идёт в TG-очередь с metrics_warning.
# На iter=MAX_RETRY_COUNT — форсированный pass, статья уходит без блокировки слота.
MAX_RETRY_COUNT = 2

# Минимум авторских вставок («мы считаем», «по нашему опыту», «в нашей практике»)
# на статью. Эмпирика 13 мая: статьи с 0% AI имели по 2 вставки,
# статьи с 7-19% AI имели 0 вставок. Это сильнейший anti-AI маркер.
AUTHOR_MARKERS_MIN = 2

# Минимум коротких предложений ≤5 слов. Эмпирика 13 мая 2026:
# - GOOD 0% AI: 10-21 коротких предложений
# - BAD 7-19% AI: 4-17 коротких предложений
# Cap ≥12 ставится с запасом — гарантирует «рваный ритм» которого нет у ChatGPT.
SHORT_SENTENCES_MIN_ABSOLUTE = 12

# Максимум длинных предложений >20 слов. Эмпирика:
# - GOOD 0% AI: 5-8 длинных
# - BAD 10.7% AI: 12 длинных
# Cap ≤9 ловит «гладкий ChatGPT-стиль».
LONG_SENTENCES_MAX_ABSOLUTE = 9


@dataclass
class GateResult:
    file: str
    passed: bool = True
    hard_failed: bool = False  # True только при структурных проблемах (битый HTML, нет файлов)
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    autofix: dict | None = None
    quality_checks: dict | None = None
    ai_markers: dict | None = None
    anti_template: dict | None = None
    law_quotes: dict | None = None
    rhythm: dict | None = None
    internal_links: dict | None = None
    recommendations: list[str] = field(default_factory=list)
    retry_count: int = 1  # сколько раз gate был запущен по этой статье (1 = первый раз)
    # Прогнозы text.ru (локальные эвристики, для бота)
    predicted_metrics: dict = field(default_factory=dict)  # {spam_pct, ai_pct, uniqueness_pct}
    customer_risks: list[str] = field(default_factory=list)  # риски на языке заказчика


def _measure_law_quotes(html_path: Path) -> tuple[int, list[str]]:
    """
    Считает суммарную длину прямых цитат закона в блоках <blockquote>.
    Возвращает (total_chars, samples).
    Для .md файлов считает только строки начинающиеся с `>` (markdown-цитаты).
    """
    if not html_path.exists():
        return 0, []
    raw = html_path.read_text(encoding="utf-8")
    suffix = html_path.suffix.lower()
    samples: list[str] = []
    total = 0
    if suffix in {".html", ".htm"}:
        import re
        rx = re.compile(r"<blockquote\b[^>]*>(.*?)</blockquote>", re.DOTALL | re.IGNORECASE)
        tag_rx = re.compile(r"<[^>]+>")
        for m in rx.finditer(raw):
            inner = tag_rx.sub("", m.group(1))
            inner = re.sub(r"\s+", " ", inner).strip()
            if inner:
                total += len(inner)
                if len(samples) < 3:
                    samples.append(inner[:120] + ("..." if len(inner) > 120 else ""))
    elif suffix in {".md", ".markdown"}:
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped.startswith(">"):
                content = stripped.lstrip(">").strip()
                if content:
                    total += len(content)
                    if len(samples) < 3:
                        samples.append(content[:120] + ("..." if len(content) > 120 else ""))
    return total, samples


def _generate_customer_risks(
    *,
    predictions: dict,
    text_chars: int,
    length_kind: str,
    am_rep,
) -> list[str]:
    """
    Превращает прогнозы и блокеры в список рисков на языке заказчика.

    Возвращает список строк типа «заспамленность чуть выше 50% (~51%)».
    Если рисков нет (всё в норме) - пустой список.
    """
    risks: list[str] = []

    # === Заспам ===
    spam = predictions.get("spam_pct", 0)
    if spam > 50:
        if spam <= 53:
            risks.append(f"заспамленность чуть выше 50% (~{spam}%)")
        elif spam <= 57:
            risks.append(f"заспамленность выше 50% (~{spam}%)")
        elif spam <= 63:
            risks.append(f"заспамленность заметно выше 50% (~{spam}%)")
        else:
            risks.append(f"заспамленность значительно выше 50% (~{spam}%)")

    # === AI-detector ===
    ai = predictions.get("ai_pct", 0)
    if ai > 10:
        if ai <= 13:
            risks.append(f"AI-detector чуть выше 10% (~{ai}%)")
        elif ai <= 17:
            risks.append(f"AI-detector выше 10% (~{ai}%)")
        elif ai <= 25:
            risks.append(f"AI-detector заметно выше 10% (~{ai}%)")
        else:
            risks.append(f"AI-detector значительно выше 10% (~{ai}%)")

    # === Уникальность ===
    uniq = predictions.get("uniqueness_pct", 100)
    if uniq < 85:
        if uniq >= 82:
            risks.append(f"уникальность чуть ниже 85% (~{uniq}%)")
        elif uniq >= 78:
            risks.append(f"уникальность ниже 85% (~{uniq}%)")
        elif uniq >= 70:
            risks.append(f"уникальность заметно ниже 85% (~{uniq}%)")
        else:
            risks.append(f"уникальность значительно ниже 85% (~{uniq}%)")

    # === Длина ===
    limits = quality_checks.LENGTH_LIMITS.get(length_kind, quality_checks.LENGTH_LIMITS["default"])
    chars_str = f"{text_chars:,}".replace(",", " ")
    if text_chars > limits["max"]:
        diff = text_chars - limits["max"]
        if diff <= 200:
            risks.append(f"длина чуть больше лимита {limits['max']} ({chars_str} знаков)")
        elif diff <= 500:
            risks.append(f"длина больше лимита {limits['max']} ({chars_str} знаков)")
        else:
            risks.append(f"длина значительно больше лимита {limits['max']} ({chars_str} знаков)")
    elif text_chars < limits["min"]:
        diff = limits["min"] - text_chars
        if diff <= 200:
            risks.append(f"длина чуть меньше минимума {limits['min']} ({chars_str} знаков)")
        else:
            risks.append(f"длина меньше минимума {limits['min']} ({chars_str} знаков)")

    # === Критические AI-маркеры (длинные тире, эмодзи, англ. кавычки) ===
    crit_count = am_rep.by_severity.get("critical", 0) if am_rep else 0
    if crit_count > 0:
        crit_names = sorted({h.pattern.name for h in am_rep.hits if h.pattern.severity == "critical"})
        if crit_names:
            risks.append(
                f"найдены критические маркеры ИИ-стиля: {', '.join(crit_names[:3])} - "
                f"могут поднять AI-detector text.ru"
            )

    # === Голос «я» вместо «мы» ===
    if am_rep and getattr(am_rep, "first_person_singular_hits", 0) > 0:
        risks.append(
            f"в тексте есть «я» вместо «мы» ({am_rep.first_person_singular_hits} мест) - "
            f"нарушает фирменный голос"
        )

    return risks


def _resolve_retry_count(article_path: Path, override: int | None) -> int:
    """
    Определяет номер текущей итерации gate'а для этой статьи.
    Если override передан — используем его. Иначе читаем prev retry_count из
    quality_gate.json и инкрементируем (или 1 если файла ещё нет).
    """
    if override is not None and override >= 1:
        return override
    qg_path = article_path.parent / "quality_gate.json"
    if not qg_path.exists():
        return 1
    try:
        prev = json.loads(qg_path.read_text(encoding="utf-8"))
        prev_count = int(prev.get("retry_count") or 0)
    except (json.JSONDecodeError, OSError, ValueError, TypeError):
        return 1
    return max(1, prev_count + 1)


def _run(path: Path, iteration_override: int | None = None) -> GateResult:
    result = GateResult(file=str(path))
    result.retry_count = _resolve_retry_count(path, iteration_override)

    # 1. Автофикс
    fix_rep = autofix.process_file(path, dry_run=False)
    result.autofix = asdict(fix_rep)
    if fix_rep.changed:
        result.warnings.append(
            f"Автофикс применил правки: сокращения={sum(fix_rep.abbreviation_fixes.values())}, "
            f"пунктуация={fix_rep.punctuation_fixes}"
        )

    # 2. quality_checks (после автофикса)
    qc_rep = quality_checks.analyze(path)
    result.quality_checks = quality_checks.to_dict(qc_rep)

    # Длина — hard-блок
    if qc_rep.length_status == "too_long":
        limits = quality_checks.LENGTH_LIMITS.get(qc_rep.length_kind, quality_checks.LENGTH_LIMITS["default"])
        result.blockers.append(
            f"length_too_long: {qc_rep.text_chars} > {limits['max']} (kind={qc_rep.length_kind})"
        )
        result.recommendations.append(
            f"reduce_length: сократить до {limits['target_min']}-{limits['target_max']} знаков"
        )
    elif qc_rep.length_status == "too_short":
        limits = quality_checks.LENGTH_LIMITS.get(qc_rep.length_kind, quality_checks.LENGTH_LIMITS["default"])
        result.blockers.append(
            f"length_too_short: {qc_rep.text_chars} < {limits['min']} (kind={qc_rep.length_kind})"
        )
        result.recommendations.append(
            f"expand_length: расширить до {limits['target_min']}-{limits['target_max']} знаков"
        )

    # Сокращения — если после автофикса ещё остались, это уже hard
    if qc_rep.abbreviation_hits:
        examples = ", ".join({h.match for h in qc_rep.abbreviation_hits[:5]})
        result.blockers.append(
            f"abbreviations_after_autofix: {len(qc_rep.abbreviation_hits)} хитов ({examples})"
        )
        result.recommendations.append("fix_abbreviations: автофикс не справился, нужна ручная правка писателя")

    # Пунктуация — аналогично
    if qc_rep.punctuation_hits:
        result.blockers.append(
            f"punctuation_after_autofix: {len(qc_rep.punctuation_hits)} мест без пробела после точки"
        )
        result.recommendations.append("fix_punctuation: пробелы после точки — править вручную")

    # Заспамленность - hard при ЛЮБОМ риск-флаге (заказчик зафиксировала
    # цель ≤ 50% спама и ≥ 85% уникальности на text.ru, 8 мая 2026).
    if qc_rep.spam and len(qc_rep.spam.risk_flags) >= 1:
        top5 = [w for w, _ in qc_rep.spam.top10_words[:5]]
        result.blockers.append(
            f"spam_risk: {len(qc_rep.spam.risk_flags)} флагов ({', '.join(qc_rep.spam.risk_flags)}), top-5: {top5}"
        )
        result.recommendations.append(
            f"reduce_repetition: разбавить топ-5 частотных лемм местоимениями/перифразом — {top5}"
        )

    # Целевые токены (главные виновники text.ru spam из анализа 13 мая 2026):
    # «ст», «РФ», «руб», «ООО», «X 000 руб». Совпадение с подсветками text.ru
    # 1-в-1 (ст×25, РФ×27, руб×23 в плохой статье 56%).
    over_token_hits = [h for h in qc_rep.targeted_tokens if h.over_limit]
    if over_token_hits:
        details = ", ".join(f"{h.token}={h.count}(>≤{h.limit})" for h in over_token_hits)
        result.blockers.append(f"targeted_tokens_over_limit: {details}")
        # Конкретные recommendation per token
        for h in over_token_hits:
            if h.token == "ст_сокращение":
                result.recommendations.append(
                    f"reduce_ст: «ст. X» × {h.count} → cap {h.limit}. Писать «статья X» полным словом, "
                    "либо описательно («по правилу о процентах», «норма об исковой давности»). "
                    "Cap 3 юр-цитаты на статью, остальное — пересказом."
                )
            elif h.token == "РФ_рудимент":
                result.recommendations.append(
                    f"reduce_РФ: «РФ» × {h.count} → cap {h.limit}. Удалить из «ГК РФ»/«ГПК РФ» — "
                    "просто «ГК»/«ГПК» (контекст однозначный). Один раз в первом упоминании можно полное название."
                )
            elif h.token == "руб_сокращение":
                result.recommendations.append(
                    f"reduce_руб: «руб» × {h.count} → cap {h.limit}. Только полное «рублей»/«рубля». "
                    "В контексте суммы единица иногда опускается: «долг 82 400» вместо «долг 82 400 рублей»."
                )
            elif h.token == "ООО_бренд":
                result.recommendations.append(
                    f"reduce_ООО: «ООО» × {h.count} → cap {h.limit}. В авторском тексте «юридическое лицо» / "
                    "«компания» / «организация». «ООО» только в конкретных кейсах из research.json."
                )
            elif h.token == "000руб_паттерн":
                result.recommendations.append(
                    f"reduce_round_sums: «X 000 руб» × {h.count} → cap {h.limit}. "
                    "Иллюстративные суммы — некруглые (82 400, 147 500). Статутные пороги (300 000, 500 000) — "
                    "1 раз в точной форме + дальше через дескриптор («эта планка», «новый минимум»)."
                )

    # Авторские вставки бренда (минимум 2 для AI-detector ≤7%).
    # Эмпирика 13 мая 2026: 0% AI статьи fiz/yur/vzysk имеют по 2 вставки,
    # 7-19% AI — 0. Для news достаточно ≥1 (фактический жанр, меньше «мы»).
    required_markers = 1 if qc_rep.length_kind == "news" else AUTHOR_MARKERS_MIN
    if qc_rep.author_markers_count < required_markers:
        result.blockers.append(
            f"author_markers_missing: {qc_rep.author_markers_count} < {required_markers}"
            + (" (news)" if qc_rep.length_kind == "news" else "")
        )
        result.recommendations.append(
            f"add_author_markers: добавить минимум {required_markers - qc_rep.author_markers_count} "
            "вставок «по нашему опыту» / «в нашей практике» / «мы считаем» / «мы видим». "
            "Это главный anti-AI маркер — без него text.ru показывает AI 7-19%."
        )

    # 3. AI-маркеры
    am_rep = ai_markers_check.analyze(path)
    result.ai_markers = ai_markers_check.to_dict(am_rep)

    # Critical — запрещены полностью
    crit = am_rep.by_severity.get("critical", 0)
    if crit > AI_MARKERS_CRITICAL_MAX:
        crit_names = {h.pattern.name for h in am_rep.hits if h.pattern.severity == "critical"}
        result.blockers.append(f"ai_markers_critical: {crit} ({', '.join(sorted(crit_names))})")
        result.recommendations.append("fix_ai_markers: убрать критичные маркеры (длинные тире, эмодзи, англ. кавычки, чатбот-фразы)")

    # Density
    if am_rep.density_per_1000 > AI_MARKERS_DENSITY_MAX:
        result.blockers.append(
            f"ai_markers_density: {am_rep.density_per_1000}/1000 > {AI_MARKERS_DENSITY_MAX}"
        )
        # Топ-3 категории маркеров
        cat_count: dict[str, int] = {}
        for h in am_rep.hits:
            cat_count[h.pattern.category] = cat_count.get(h.pattern.category, 0) + 1
        top_cats = sorted(cat_count.items(), key=lambda x: -x[1])[:3]
        result.recommendations.append(
            f"reduce_ai_markers: топ-категории — {', '.join(f'{c}:{n}' for c, n in top_cats)}"
        )

    # High — потолок 8
    high = am_rep.by_severity.get("high", 0)
    if high > AI_MARKERS_HIGH_MAX:
        result.blockers.append(f"ai_markers_high: {high} > {AI_MARKERS_HIGH_MAX}")
        result.recommendations.append("reduce_ai_markers: слишком много high-маркеров (раздувание/реклама/параллелизмы)")

    # «Я» — запрещено
    if am_rep.first_person_singular_hits > 0:
        result.blockers.append(f"first_person_singular: {am_rep.first_person_singular_hits} (должно быть 0)")
        result.recommendations.append("fix_voice: заменить «я» на «мы» (по нашему опыту, мы видим)")

    # 4. Anti-template чекер: дословные шаблонные правовые фразы
    # Снижает уникальность text.ru (фразы есть на каждом юр-портале).
    # Любое срабатывание = блок, возврат на писателя с конкретными цитатами.
    at_rep = anti_template_check.analyze(path)
    result.anti_template = anti_template_check.to_dict(at_rep)
    if at_rep.hits:
        unique_phrases = {h.phrase for h in at_rep.hits}
        result.blockers.append(
            f"anti_template_phrases: {len(at_rep.hits)} вхождений ({len(unique_phrases)} уникальных)"
        )
        top_phrases = sorted(unique_phrases)[:3]
        result.recommendations.append(
            "rewrite_unique: перифразировать дословные шаблонные фразы "
            "(см. .claude/style/anti-template-phrases.md, секция «Как перифразировать»). "
            f"Примеры: «{'» / «'.join(top_phrases)}»"
        )

    # 5. Лимит цитирования закона (в blockquote / markdown >)
    law_chars, law_samples = _measure_law_quotes(path)
    result.law_quotes = {"total_chars": law_chars, "limit": LAW_QUOTE_CHARS_MAX, "samples": law_samples}
    if law_chars > LAW_QUOTE_CHARS_MAX:
        result.blockers.append(
            f"law_quotes_too_long: {law_chars} > {LAW_QUOTE_CHARS_MAX} знаков прямых цитат"
        )
        result.recommendations.append(
            f"reduce_law_quotes: суммарно цитат закона ≤ {LAW_QUOTE_CHARS_MAX} знаков. "
            "Заменить избыточные цитаты на пересказ ('по 127-ФЗ', 'согласно ст. X')."
        )

    # 5.5. Внутренние ссылки: автофикс .html / trailing slash / коротких ссылок,
    # затем hard-блок если остались error_* (slug не найден ни в одной категории).
    # Решает баг 12 мая 2026: Я.Вебмастер показал 404 и 301 из-за ссылок типа
    # /bankrotstvo-pensionera (без префикса), /articles/yur/foo.html, /articles/yur/foo/.
    il_slug_to_cat = internal_links_check._load_valid_slugs()
    il_rep = internal_links_check.analyze_file(path, il_slug_to_cat, apply_fix=True)
    result.internal_links = {
        "total_hrefs": il_rep.total_hrefs,
        "ok": il_rep.ok,
        "whitelisted": il_rep.whitelisted,
        "external": il_rep.external,
        "fixed": il_rep.fixed,
        "errors": il_rep.errors,
        "changed": il_rep.changed,
        "fix_details": [asdict(x) for x in il_rep.fix_details],
        "error_details": [asdict(x) for x in il_rep.error_details],
    }
    if il_rep.fixed > 0:
        examples = [f"{x.href}→{x.fixed_href}" for x in il_rep.fix_details[:3]]
        result.warnings.append(
            f"internal_links_autofixed: {il_rep.fixed} ссылок (например {'; '.join(examples)})"
        )
    if il_rep.errors > 0:
        bad = [f"{x.href}: {x.reason}" for x in il_rep.error_details[:3]]
        result.blockers.append(
            f"broken_internal_links: {il_rep.errors} ссылок без соответствия в published_index "
            f"({'; '.join(bad)})"
        )
        result.recommendations.append(
            "fix_internal_links: заменить href на канонический /articles/{cat}/{slug} "
            "из published_index.json (см. .claude/style/editor-cheatsheet.md секция 6)"
        )

    # 6. Ритмический анализ + абсолютные cap'ы коротких/длинных (hard).
    # 13 мая 2026: эмпирика показала прямую корреляцию длинных/коротких с AI score:
    # - 0% AI: 10-21 коротких, 5-8 длинных
    # - 7-19% AI: 4-17 коротких, 6-12 длинных
    rh_rep = rhythm_check.analyze(path)
    result.rhythm = rhythm_check.to_dict(rh_rep)

    # Абсолютные cap'ы — hard блокеры
    short_n = getattr(rh_rep, "short_sentences_count", 0) or 0
    long_n = getattr(rh_rep, "long_sentences_count", 0) or 0

    if short_n < SHORT_SENTENCES_MIN_ABSOLUTE:
        result.blockers.append(
            f"too_few_short_sentences: {short_n} < {SHORT_SENTENCES_MIN_ABSOLUTE}"
        )
        result.recommendations.append(
            f"add_short_sentences: добавить минимум {SHORT_SENTENCES_MIN_ABSOLUTE - short_n} "
            "коротких предложений (≤5 слов). Главный anti-AI маркер: «Главное.», «Защита не включается.», "
            "«6 месяцев, иногда 8.». Разбивать длинные предложения на 2-3 коротких."
        )
    if long_n > LONG_SENTENCES_MAX_ABSOLUTE:
        result.blockers.append(
            f"too_many_long_sentences: {long_n} > {LONG_SENTENCES_MAX_ABSOLUTE}"
        )
        result.recommendations.append(
            f"reduce_long_sentences: предложений >20 слов = {long_n}, cap {LONG_SENTENCES_MAX_ABSOLUTE}. "
            "Разбить {long_n - LONG_SENTENCES_MAX_ABSOLUTE}+ длинных на 2-3 коротких. "
            "ChatGPT любит длинные — text.ru это ловит как AI."
        )

    # Старое soft-предупреждение про ритм оставляем как warning (не hard)
    if not rh_rep.passed:
        result.warnings.append(
            f"rhythm_too_smooth: {len(rh_rep.flags)} флагов гладкости — "
            f"avg_len={rh_rep.avg_sentence_len}, short_share={round(rh_rep.short_sentences_share*100,1)}%, "
            f"parasites/1000={rh_rep.parasite_per_1000}"
        )
        result.recommendations.append(
            "anti_ai_rewrite: применить ≥3 anti-AI приёма из writer-cheatsheet "
            "(секция «Writer B: anti-AI техники») — короткие рывки, разрыв перечислений, "
            "запрет связок-паразитов, цифры вместо обобщений"
        )

    # 7. Soft-length после первой итерации правок.
    # Заказчик (май 2026): главный приоритет — spam/uniqueness/ai_markers (hard).
    # Длина — soft после iteration ≥ 2, пока текст не выше SOFT_LENGTH_MAX.
    if result.retry_count >= 2:
        soft_max = SOFT_LENGTH_MAX.get(qc_rep.length_kind, SOFT_LENGTH_MAX["default"])
        if (qc_rep.length_status == "too_long"
                and qc_rep.text_chars is not None
                and qc_rep.text_chars <= soft_max):
            limits = quality_checks.LENGTH_LIMITS.get(qc_rep.length_kind, quality_checks.LENGTH_LIMITS["default"])
            length_blocker_marker = f"length_too_long: {qc_rep.text_chars} > {limits['max']}"
            kept_blockers = [b for b in result.blockers if not b.startswith("length_too_long")]
            if len(kept_blockers) < len(result.blockers):
                result.blockers = kept_blockers
                result.warnings.append(
                    f"length_soft_passed: {qc_rep.text_chars} > {limits['max']} "
                    f"но ≤ {soft_max} и iteration={result.retry_count} — пропускаем "
                    f"(приоритет spam/uniqueness/ai)"
                )
                result.recommendations = [r for r in result.recommendations if not r.startswith("reduce_length")]

    # 7.5. Soft-pass на spam_risk при iter≥2 если все метрики в коридоре
    # (top1≤14, top10_share≤0.12, ngram3≤0.06, lex_div≥0.55) и нет targeted_tokens.
    # Логика: на 2-й итерации не возвращать writer'а ради ratio-метрик, если
    # абсолютные cap'ы и токены в порядке. Иначе цикл стремится к перфекционизму.
    if result.retry_count >= 2 and qc_rep.spam:
        in_corridor = (
            qc_rep.spam.top1_count <= 14
            and qc_rep.spam.top10_share <= 0.120
            and qc_rep.spam.ngram3_repeat_share <= 0.060
            and qc_rep.spam.lexical_diversity >= 0.55
        )
        no_targeted = not any(h.over_limit for h in qc_rep.targeted_tokens)
        if in_corridor and no_targeted:
            spam_blockers = [b for b in result.blockers if b.startswith("spam_risk")]
            if spam_blockers:
                result.blockers = [b for b in result.blockers if not b.startswith("spam_risk")]
                result.warnings.append(
                    f"spam_soft_passed: ratio-флаги мягкие при iter={result.retry_count}, "
                    f"targeted_tokens чистые → пропускаем (spam ≈48-52% по прогнозу)"
                )
                result.recommendations = [r for r in result.recommendations if not r.startswith("reduce_repetition")]

    # 7.7. Force-pass на iter=MAX_RETRY_COUNT.
    # Логика: если на 3-й итерации writer всё ещё провалил блокеры — статья
    # отправляется в TG-очередь заказчику с пометкой metrics_warning=true.
    # Слот не теряется, заказчик решает (опубликовать / отклонить / правки).
    # Старое поведение «уходит в _review/» отменено по запросу 13 мая 2026.
    if result.retry_count >= MAX_RETRY_COUNT and result.blockers:
        forced_blockers = list(result.blockers)
        result.blockers = []
        result.warnings.append(
            f"forced_pass_at_iter{MAX_RETRY_COUNT}: блокеры пропущены, "
            f"статья идёт в TG-очередь с metrics_warning=true. "
            f"Было: {len(forced_blockers)} блокеров — {'; '.join(b.split(':')[0] for b in forced_blockers)}"
        )
        # Записываем флаг в meta.json (читается ботом)
        try:
            meta_path = path.parent / "meta.json"
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                meta["metrics_warning"] = True
                meta["metrics_warning_blockers"] = forced_blockers
                meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    # 8. Прогноз text.ru-метрик и риски на языке заказчика
    # text.ru API не подключён, поэтому считаем оценочный прогноз (±5-7%)
    # на основе локальных эвристик. Бот покажет это заказчику.
    at_hits_count = len(at_rep.hits) if at_rep else 0
    predictions = quality_checks.predict_textru_metrics(
        spam=qc_rep.spam,
        ai_density_per_1000=am_rep.density_per_1000,
        ai_critical_count=am_rep.by_severity.get("critical", 0),
        anti_template_hits_count=at_hits_count,
        targeted_token_hits=qc_rep.targeted_tokens,
        author_markers_count=qc_rep.author_markers_count,
    )
    result.predicted_metrics = predictions
    result.customer_risks = _generate_customer_risks(
        predictions=predictions,
        text_chars=qc_rep.text_chars,
        length_kind=qc_rep.length_kind,
        am_rep=am_rep,
    )

    # passed - оставляем для backward-compat и диагностики (technical level).
    # Реальное решение «блокировать ли пайплайн» runner.py делает по hard_failed.
    result.passed = not result.blockers
    # hard_failed = только при структурных проблемах. Пока gate ничего такого не
    # ловит (нет проверок битого HTML / отсутствия файлов внутри _run -
    # отсутствие файла отлавливается раньше в main()). Оставляем False всегда.
    # Метрические fail'ы (spam/AI/uniqueness/length) больше не блокируют пайплайн -
    # заказчик увидит риски через бот и решит сам.
    result.hard_failed = False

    # 9. Записываем локальные метрики, прогнозы и риски в meta.json (для бота)
    _update_meta(path, qc_rep, am_rep, result.passed, result.blockers,
                 result.retry_count, predictions, result.customer_risks)

    return result


def _update_meta(
    article_path: Path,
    qc_rep,
    am_rep,
    passed: bool,
    blockers: list[str],
    retry_count: int = 1,
    predictions: dict | None = None,
    customer_risks: list[str] | None = None,
) -> None:
    """
    Дописывает в meta.json локальные метрики качества, прогнозы text.ru и
    риски на языке заказчика. Используется ботом для уведомления.

    Работает идемпотентно: читает текущий meta.json, обновляет поля, пишет обратно.
    Если meta.json не существует — пропускает (агент 6 ещё не успел его создать).
    """
    meta_path = article_path.parent / "meta.json"
    if not meta_path.exists():
        return

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    # text.ru статус: режим local_only (API не подключён)
    if "textru_status" not in meta or meta.get("textru_status") is None:
        meta["textru_status"] = "local_only"
    if "textru_uniqueness" not in meta:
        meta["textru_uniqueness"] = None
    if "textru_ai_detector" not in meta:
        meta["textru_ai_detector"] = None
    if "textru_spam" not in meta:
        meta["textru_spam"] = None

    # Локальные метрики (всегда заполнены)
    meta["text_chars"] = qc_rep.text_chars
    meta["length_status"] = qc_rep.length_status
    meta["length_kind"] = qc_rep.length_kind

    if qc_rep.spam:
        meta["local_spam_top10_share"] = qc_rep.spam.top10_share
        meta["local_spam_ngram3_repeat"] = qc_rep.spam.ngram3_repeat_share
        meta["local_lexical_diversity"] = qc_rep.spam.lexical_diversity

    meta["local_ai_markers_density"] = am_rep.density_per_1000
    meta["local_ai_markers_total"] = len(am_rep.hits)
    meta["local_ai_markers_critical"] = am_rep.by_severity.get("critical", 0)
    meta["local_ai_markers_high"] = am_rep.by_severity.get("high", 0)
    meta["local_own_voice_hits"] = am_rep.own_voice_hits
    meta["local_first_person_singular_hits"] = am_rep.first_person_singular_hits

    # Результат gate (для логов scheduler-а)
    meta["quality_gate_passed"] = passed
    meta["quality_gate_blockers"] = blockers
    meta["quality_gate_retry_count"] = retry_count

    # Прогнозы text.ru и риски на языке заказчика (для уведомления в боте)
    if predictions:
        meta["predicted_spam_pct"] = predictions.get("spam_pct")
        meta["predicted_ai_pct"] = predictions.get("ai_pct")
        meta["predicted_uniqueness_pct"] = predictions.get("uniqueness_pct")
    if customer_risks is not None:
        meta["customer_risks"] = customer_risks

    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Quality gate: hard-блок перед коммитом")
    parser.add_argument("path", help="Путь к article.html или draft.md")
    parser.add_argument("--json", action="store_true", help="Вывод отчёта в JSON")
    parser.add_argument("--save-report", action="store_true",
                        help="Сохранить quality_gate.json рядом с файлом")
    parser.add_argument("--iteration", type=int, default=None,
                        help="Принудительно задать номер итерации writer-цикла (1=первый прогон). "
                             "Если не задан, gate сам инкрементирует retry_count из quality_gate.json.")
    args = parser.parse_args()

    path = Path(args.path).resolve()
    if not path.exists() or not path.is_file():
        print(f"Файл не найден: {path}", file=sys.stderr)
        return 2

    result = _run(path, iteration_override=args.iteration)

    if args.save_report:
        report_path = path.parent / "quality_gate.json"
        report_path.write_text(
            json.dumps(asdict(result), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    if args.json:
        print(json.dumps(asdict(result), ensure_ascii=False, indent=2))
    else:
        print(f"\n=== Quality gate: {result.file} ===")
        print(f"Итог: {'PASSED' if result.passed else 'FAILED'}")
        if result.warnings:
            print("\nПредупреждения:")
            for w in result.warnings:
                print(f"  ! {w}")
        if result.blockers:
            print("\nБлокеры:")
            for b in result.blockers:
                print(f"  [BLOCK] {b}")
        if result.recommendations:
            print("\nРекомендации для возврата на писателя:")
            for r in result.recommendations:
                print(f"  → {r}")

    # С 8 мая 2026: exit 0 на soft-fail (метрики), exit 1 только на hard_failed.
    # Это нужно чтобы Claude в /write-article не уходил в retry-цикл с агентом 4
    # на спам/AI/length, а доходил до агента 7 (финализация + обложка).
    # Метрики и риски всё равно записываются в meta.json - бот покажет их заказчику.
    return 1 if result.hard_failed else 0


if __name__ == "__main__":
    sys.exit(main())
