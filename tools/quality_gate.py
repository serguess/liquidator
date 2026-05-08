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

from tools import ai_markers_check, anti_template_check, autofix, quality_checks, rhythm_check


PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Пороги, при превышении которых блокируем коммит.
AI_MARKERS_DENSITY_MAX = 2.0  # маркеров на 1000 знаков
AI_MARKERS_CRITICAL_MAX = 0   # critical-маркеры запрещены полностью
AI_MARKERS_HIGH_MAX = 8       # high-маркеры — допустимо до 8 на статью

# Лимит на длину прямых цитат закона (в blockquote).
# Заказчик: одна из причин уникальности 48% — большие цитаты норм,
# которые есть везде одинаково. Сокращаем до 600 знаков суммарно.
LAW_QUOTE_CHARS_MAX = 600

# Soft-потолок длины: после первой итерации правок length_too_long перестаёт
# быть hard-блоком (становится warning), пока text_chars не превышает SOFT_LENGTH_MAX.
# Логика: главный приоритет — заспамленность/уникальность/AI. Если на 2+ итерации
# писатель пофиксил спам, но не уложился в 8000 — пропускаем, иначе бесконечный цикл.
# Цель: < 9000 default, < 8000 news (длиннее всё равно блок).
SOFT_LENGTH_MAX = {"default": 9000, "news": 8000}

# Hard-cap итераций writer-цикла. После 5-го failed-gate статья уходит в _review/
# для ручного разбора (см. runner.py:_find_failed_qa_for_retry).
MAX_RETRY_COUNT = 5


@dataclass
class GateResult:
    file: str
    passed: bool = True
    blockers: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    autofix: dict | None = None
    quality_checks: dict | None = None
    ai_markers: dict | None = None
    anti_template: dict | None = None
    law_quotes: dict | None = None
    rhythm: dict | None = None
    recommendations: list[str] = field(default_factory=list)
    retry_count: int = 1  # сколько раз gate был запущен по этой статье (1 = первый раз)


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

    # 6. Ритмический анализ (warning, не блок — без ML-модели мы не воспроизведём
    # text.ru AI-detector точно, но грубые случаи «гладкого ChatGPT-ритма» ловим).
    # При warnings — рекомендация anti-AI rewrite pass.
    rh_rep = rhythm_check.analyze(path)
    result.rhythm = rhythm_check.to_dict(rh_rep)
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
    # Логика: если writer уже один раз правил и попал в спам/уник, тратить циклы
    # на дополнительное сокращение длины не нужно — это раздувает цикл.
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
                # Чистим reduce_length из recommendations — не отправляем писателя
                # править длину если других блокеров нет.
                result.recommendations = [r for r in result.recommendations if not r.startswith("reduce_length")]

    result.passed = not result.blockers

    # 6. Записываем локальные метрики и textru_status в meta.json
    # Заказчик зафиксировал (май 2026): text.ru API не подключаем, режим local_only.
    _update_meta(path, qc_rep, am_rep, result.passed, result.blockers, result.retry_count)

    return result


def _update_meta(article_path: Path, qc_rep, am_rep, passed: bool, blockers: list[str], retry_count: int = 1) -> None:
    """
    Дописывает в meta.json локальные метрики качества и textru_status.

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

    # text.ru статус: режим local_only (без API, по решению заказчика)
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

    # Результат gate
    meta["quality_gate_passed"] = passed
    meta["quality_gate_blockers"] = blockers
    meta["quality_gate_retry_count"] = retry_count

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

    return 0 if result.passed else 1


if __name__ == "__main__":
    sys.exit(main())
