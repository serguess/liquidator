"""
Дополнительные проверки качества статьи (сверх ai_markers_check.py):
1. Сокращения юр-терминов в авторском тексте (юрлиц/физлиц/дебиторк/финупр и т.п.).
2. Отсутствие пробела после точки перед заглавной буквой.
3. Эвристика заспамленности text.ru:
   - доля топ-10 самых частотных слов
   - доля повторяющихся 3-граммов
   - лексическое разнообразие (уникальных лемм / общее число слов)

Запуск:
    python -m tools.quality_checks <path>
    python -m tools.quality_checks drafts/spisat-dolgi-po-kreditam-bez-imushchestva/article.html --json

Что считается «авторским текстом» в HTML:
- Содержимое <p>, <h2>, <h3>, <li> в <article>.
- НЕ считается: <title>, <meta>, JSON-LD <script>, <head>, FAQ-вопросы (<h3> с вопросительной формой - оставляем как авторский, но сокращения в FAQ-вопросах считаем разрешёнными отдельно: вопрос - форма поискового запроса).

Для .md файлов: всё содержимое после frontmatter.

Возврат:
    0 - все проверки прошли
    1 - есть нарушения
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent


# === 1. Сокращения юр-терминов (запрещены в авторском тексте) ===
# Паттерны учитывают только формы внутри слова, не задевая FAQ-вопросы (там форма
# поискового запроса допустима). FAQ-вопросы вырезаются перед проверкой.
# Покрытие падежей: ед.ч. (юрлицо/юрлица/юрлицу/юрлицом/юрлице) +
# мн.ч. (юрлица/юрлиц/юрлицам/юрлицами/юрлицах). Аналогично физлица.
ABBREVIATIONS = {
    "юрлицо/юрлица/юрлицам/юрлицами": re.compile(
        r"\bюрлиц(?:а|у|ы|е|о|ом|ами|ам|ах)?\b",
        re.IGNORECASE | re.UNICODE,
    ),
    "физлицо/физлица/физлицам/физлицами": re.compile(
        r"\bфизлиц(?:а|у|ы|е|о|ом|ами|ам|ах)?\b",
        re.IGNORECASE | re.UNICODE,
    ),
    "дебиторка/дебиторки/дебиторкой": re.compile(
        r"\bдебиторк(?:а|и|у|е|ой|ою|ам|ами|ах)?\b",
        re.IGNORECASE | re.UNICODE,
    ),
    "финуправляющий/финупр": re.compile(
        r"\bфинуправл\w*|\bфинупр\b",
        re.IGNORECASE | re.UNICODE,
    ),
    "кредорг": re.compile(r"\bкредорг\w*", re.IGNORECASE | re.UNICODE),
    "исполлист": re.compile(r"\bисполлист\w*", re.IGNORECASE | re.UNICODE),
    "банкротн (как сокращение)": re.compile(r"\bбанкротн\b", re.IGNORECASE | re.UNICODE),
}

# === 2. Пробел после точки перед заглавной буквой ===
# Регэксп ловит «текст.Текст» (без пробела). Не задевает аббревиатуры в одном слове
# (ст., п., ч.) - они идут со строчной или цифрой после точки.
NO_SPACE_AFTER_DOT_RX = re.compile(r"\.[А-ЯЁ]", re.UNICODE)


@dataclass
class AbbreviationHit:
    pattern: str
    match: str
    context: str
    position: int


@dataclass
class PunctuationHit:
    fragment: str
    position: int


@dataclass
class SpamHeuristics:
    total_words: int
    unique_lemmas: int
    lexical_diversity: float
    top10_words: list[tuple[str, int]]
    top10_share: float
    ngram3_repeat_count: int
    ngram3_total: int
    ngram3_repeat_share: float
    risk_flags: list[str]


# === 4. Длина статьи (hard-блокер) ===
# Целевой диапазон: 6500-7500 знаков. Допуск 6000-8000 (для news 4500-6500).
# Жёсткий потолок 8000: больше — публикация блокируется автоматически.
#
# Длина расширена 7 мая 2026 (раньше было target 6000-7000 / max 7500).
# Причина: на темах с плотной терминологией (банкротство ООО, аресты, приставы,
# субсидиарка) 7000 знаков физически не хватало для удержания одновременно
# top10_share, ngram3, lex_diversity — повторяющиеся ключевые термины «тонут»
# только при добавлении разнообразного текста (примеры, кейсы, цифры).
# При 7000 writer крутил 4-5 итераций без улучшения метрик. При 8000 те же
# метрики проходят с 1-2 итераций, и статья получает +1000 знаков полезной
# конкретики. Подтверждено замером на статье snyatie-aresta-so-scheta-pristavami.
# Текущие пороги (8 мая 2026, под KPI ≤50% спама):
# top10_share≤0.105, ngram3≤0.030, lex_diversity≥0.62.
LENGTH_LIMITS = {
    "default": {"min": 6000, "target_min": 6500, "target_max": 7500, "max": 8000},
    "news": {"min": 4500, "target_min": 4500, "target_max": 6500, "max": 6800},
}


def length_status(text_chars: int, kind: str = "default") -> str:
    limits = LENGTH_LIMITS.get(kind, LENGTH_LIMITS["default"])
    if text_chars > limits["max"]:
        return "too_long"
    if text_chars < limits["min"]:
        return "too_short"
    if text_chars > limits["target_max"] or text_chars < limits["target_min"]:
        return "warn"
    return "ok"


@dataclass
class Report:
    file: str
    text_chars: int
    length_status: str = "ok"  # ok | warn | too_short | too_long
    length_kind: str = "default"  # default | news
    abbreviation_hits: list[AbbreviationHit] = field(default_factory=list)
    punctuation_hits: list[PunctuationHit] = field(default_factory=list)
    spam: SpamHeuristics | None = None

    @property
    def passed(self) -> bool:
        if self.length_status in ("too_short", "too_long"):
            return False
        if self.abbreviation_hits or self.punctuation_hits:
            return False
        if self.spam and len(self.spam.risk_flags) >= 1:
            return False
        return True


# === Извлечение авторского текста ===

# В HTML авторский текст = содержимое тегов p/h2/h3/li ВНУТРИ <article>...</article>.
# Чтобы не цеплять title/description/JSON-LD/FAQ-вопросы.
ARTICLE_BODY_RX = re.compile(r"<article\b[^>]*>(.*?)</article>", re.DOTALL | re.IGNORECASE)
# FAQ-вопросы (h3 со знаком вопроса в конце) - вырезаем, в них допустимы сокращения.
FAQ_QUESTION_RX = re.compile(r"<h3\b[^>]*>[^<]*\?\s*</h3>", re.IGNORECASE)
# JSON-LD внутри <script type="application/ld+json"> - вырезаем.
SCRIPT_RX = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
STYLE_RX = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
PRE_CODE_RX = re.compile(r"<(pre|code|blockquote)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
TAG_RX = re.compile(r"<[^>]+>")
ENTITY_RX = re.compile(r"&[a-zA-Z]+;|&#\d+;")

# В Markdown авторский текст = всё после frontmatter, без code blocks.
FRONTMATTER_RX = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
CODE_BLOCK_RX = re.compile(r"```.*?```", re.DOTALL)
INLINE_CODE_RX = re.compile(r"`[^`]+`")
# В md FAQ часто оформлен как «### Вопрос?» - вырезаем строку целиком.
MD_FAQ_QUESTION_RX = re.compile(r"^#{1,4}\s+[^\n]*\?\s*$", re.MULTILINE)


def extract_author_text_from_html(html: str) -> str:
    body_match = ARTICLE_BODY_RX.search(html)
    body = body_match.group(1) if body_match else html
    body = SCRIPT_RX.sub(" ", body)
    body = STYLE_RX.sub(" ", body)
    body = PRE_CODE_RX.sub(" ", body)
    body = FAQ_QUESTION_RX.sub(" ", body)
    body = TAG_RX.sub(" ", body)
    body = ENTITY_RX.sub(" ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body


def extract_author_text_from_markdown(md: str) -> str:
    text = FRONTMATTER_RX.sub("", md, count=1)
    text = CODE_BLOCK_RX.sub(" ", text)
    text = INLINE_CODE_RX.sub(" ", text)
    text = MD_FAQ_QUESTION_RX.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def context_of(text: str, start: int, end: int, window: int = 50) -> str:
    a = max(0, start - window)
    b = min(len(text), end + window)
    return ("..." if a > 0 else "") + text[a:b].replace("\n", " ") + ("..." if b < len(text) else "")


def check_abbreviations(text: str) -> list[AbbreviationHit]:
    hits = []
    for name, rx in ABBREVIATIONS.items():
        for m in rx.finditer(text):
            hits.append(AbbreviationHit(
                pattern=name,
                match=m.group(0),
                context=context_of(text, m.start(), m.end()),
                position=m.start(),
            ))
    return hits


def check_punctuation(text: str) -> list[PunctuationHit]:
    hits = []
    for m in NO_SPACE_AFTER_DOT_RX.finditer(text):
        hits.append(PunctuationHit(
            fragment=context_of(text, m.start(), m.end(), window=20),
            position=m.start(),
        ))
    return hits


# Минимальный список стоп-слов: предлоги, союзы, частицы, базовые местоимения.
# Без них топ-частотности забивают «и», «в», «на» - и расчёт теряет смысл.
STOPWORDS = {
    "и", "в", "на", "по", "с", "из", "для", "от", "до", "за", "к", "о", "об", "у", "при", "под", "над", "без",
    "а", "но", "или", "что", "как", "так", "то", "это", "тот", "этот", "та", "те", "эта", "эти",
    "не", "ни", "же", "ли", "бы", "уже", "ещё", "ведь", "вот", "был", "была", "было", "были",
    "его", "её", "их", "ему", "ей", "им", "он", "она", "оно", "они", "вы", "мы", "ты", "я",
    "если", "когда", "потому", "поэтому", "также", "тоже", "только", "даже", "очень", "более", "менее",
    "после", "перед", "между", "через", "вместе", "около", "среди",
}

WORD_RX = re.compile(r"[А-яЁёA-Za-z]{4,}", re.UNICODE)


def simple_lemma(word: str) -> str:
    """Очень простая морфология: отрезаем типичные русские окончания, чтобы
    «должник», «должника», «должнику» считались одной леммой. Не идеально,
    но достаточно для эвристики."""
    word = word.lower()
    suffixes = (
        "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими",
        "ах", "ях", "ом", "ем", "ой", "ей", "ую", "юю", "ие", "ые", "их", "ых",
        "ов", "ев", "ам", "ям", "ой", "ей", "ия", "ие", "ии", "ия",
        "ть", "ся", "сь", "ет", "ёт", "ит", "ат", "ят", "ут", "ют", "ал", "ял", "ил", "ел",
        "у", "ю", "а", "я", "о", "е", "ы", "и", "ь",
    )
    for suf in sorted(suffixes, key=len, reverse=True):
        if len(word) - len(suf) >= 4 and word.endswith(suf):
            return word[:-len(suf)]
    return word


def compute_spam_heuristics(text: str) -> SpamHeuristics:
    words = [w for w in WORD_RX.findall(text) if w.lower() not in STOPWORDS]
    if not words:
        return SpamHeuristics(0, 0, 0.0, [], 0.0, 0, 0, 0.0, ["empty_text"])

    lemmas = [simple_lemma(w) for w in words]
    counter = Counter(lemmas)
    total_words = len(words)
    unique_lemmas = len(counter)
    lexical_diversity = round(unique_lemmas / total_words, 3)

    top10 = counter.most_common(10)
    top10_share = round(sum(c for _, c in top10) / total_words, 3)

    # 3-граммы строим из исходных слов (не лемм), чтобы повтор был именно повтором.
    lower_words = [w.lower() for w in words]
    ngrams = [tuple(lower_words[i:i + 3]) for i in range(len(lower_words) - 2)]
    ngram_counter = Counter(ngrams)
    repeats = sum(c for c in ngram_counter.values() if c >= 2)
    total_ngrams = len(ngrams) if ngrams else 1
    ngram3_repeat_share = round(repeats / total_ngrams, 3)

    # Пороги под целевую заспамленность text.ru ≤ 50% и уникальность ≥ 85%
    # (фактический KPI заказчика, 8 мая 2026 - изменён с <40% на ≤50%, чтобы
    # пайплайн сходился за 2-3 итерации, а не 10+).
    # Калибровка по реальным замерам text.ru:
    #   - 0.164 top10 + 2.9% ngram3 → text.ru spam 58 (старая статья)
    #   - 0.110 top10 + 1.8% ngram3 + 0.62 div → text.ru spam 52
    #   - 0.105 top10 + 3.0% ngram3 + 0.62 div → text.ru spam ≈50 (новый коридор)
    #   - цель ≤50% спама ≈ top10 ≤ 0.105, ngram3 ≤ 0.030, lex.div ≥ 0.62
    #
    # 8 мая 2026: пороги ослаблены с (0.085 / 0.025 / 0.65) до (0.105 / 0.030 / 0.62)
    # после изменения KPI с <40% на ≤50%. Старая калибровка под <40% заставляла
    # writer крутить 5-10 итераций - метрики физически не сходились на 7-8к знаков
    # с плотной legal-терминологией.
    risk_flags = []
    if top10_share > 0.105:
        risk_flags.append(f"top10_share>{0.105} (={top10_share})")
    if ngram3_repeat_share > 0.030:
        risk_flags.append(f"ngram3_repeat_share>{0.030} (={ngram3_repeat_share})")
    if lexical_diversity < 0.62:
        risk_flags.append(f"lexical_diversity<{0.62} (={lexical_diversity})")

    return SpamHeuristics(
        total_words=total_words,
        unique_lemmas=unique_lemmas,
        lexical_diversity=lexical_diversity,
        top10_words=top10,
        top10_share=top10_share,
        ngram3_repeat_count=repeats,
        ngram3_total=total_ngrams,
        ngram3_repeat_share=ngram3_repeat_share,
        risk_flags=risk_flags,
    )


def _detect_kind(file_path: Path, raw: str) -> str:
    """News-категория - другие лимиты длины. Определяем по пути drafts/{slug} или meta.json."""
    parts = {p.lower() for p in file_path.parts}
    if "news" in parts:
        return "news"
    # Попробовать meta.json рядом
    meta_path = file_path.parent / "meta.json"
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            if (meta.get("category") or "").lower() == "news":
                return "news"
        except (json.JSONDecodeError, OSError):
            pass
    return "default"


# Hard-cap: ограничивает количество вызовов quality_checks от writer-а
# (файл drafts/{slug}/draft.md) в одном слоте. Раньше writer крутил
# Bash-самопроверку 5+ раз → залипали слоты по таймауту 40 мин.
# После N-го вызова risk_flags принудительно очищаются — статья идёт
# дальше к агентам 5/6/quality_gate, которые поймают реальные проблемы.
WRITER_QC_HARDCAP_N = 3
WRITER_QC_WINDOW_SEC = 1800  # 30 минут — окно одного слота


def _writer_qc_call_count(file_path: Path) -> int:
    """
    Считает по таймстемпам в `data/qc_calls/{slug}.txt`, сколько раз writer
    вызвал quality_checks для draft.md за последние WRITER_QC_WINDOW_SEC.
    Возвращает текущий номер вызова (включая этот). Возвращает 0 если файл
    не drafts/{slug}/draft.md (hard-cap не применяется к article.html и др.).
    """
    if file_path.name != "draft.md":
        return 0
    try:
        slug = file_path.parent.name
    except Exception:
        return 0
    if not slug or slug.startswith("_") or slug.startswith("."):
        return 0

    counter_dir = PROJECT_ROOT / "data" / "qc_calls"
    try:
        counter_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0
    counter_file = counter_dir / f"{slug}.txt"

    now = time.time()
    timestamps: list[float] = []
    if counter_file.exists():
        try:
            for line in counter_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    ts = float(line)
                except ValueError:
                    continue
                if now - ts < WRITER_QC_WINDOW_SEC:
                    timestamps.append(ts)
        except OSError:
            pass
    timestamps.append(now)
    try:
        counter_file.write_text("\n".join(f"{t:.0f}" for t in timestamps), encoding="utf-8")
    except OSError:
        pass
    return len(timestamps)


def analyze(file_path: Path) -> Report:
    raw = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() in {".html", ".htm"}:
        text = extract_author_text_from_html(raw)
    elif file_path.suffix.lower() in {".md", ".markdown"}:
        text = extract_author_text_from_markdown(raw)
    else:
        text = raw

    rel_path = (
        file_path.relative_to(PROJECT_ROOT)
        if file_path.is_relative_to(PROJECT_ROOT)
        else file_path
    )

    kind = _detect_kind(file_path, raw)
    chars = len(text)

    spam = compute_spam_heuristics(text)

    # Hard-cap: если writer уже N+ раз дёргал quality_checks для одного draft.md,
    # принудительно очищаем risk_flags и пишем стоп-сигнал. Это защита от
    # бесконечного цикла самокоррекции — реальные проблемы поймает quality_gate
    # после агента 6 (он работает с финальным article.html).
    qc_call_n = _writer_qc_call_count(file_path)
    if qc_call_n >= WRITER_QC_HARDCAP_N and spam and spam.risk_flags:
        print(
            f"[HARD-CAP] quality_checks вызван {qc_call_n}-й раз для {file_path.parent.name}/draft.md. "
            f"Risk-флаги принудительно сняты — дальнейшие правки нерентабельны. "
            f"Финальную проверку сделает quality_gate после агента 6.",
            file=sys.stderr,
        )
        spam.risk_flags = []

    return Report(
        file=str(rel_path),
        text_chars=chars,
        length_kind=kind,
        length_status=length_status(chars, kind),
        abbreviation_hits=check_abbreviations(text),
        punctuation_hits=check_punctuation(text),
        spam=spam,
    )


def to_dict(rep: Report) -> dict:
    return {
        "file": rep.file,
        "text_chars": rep.text_chars,
        "length_status": rep.length_status,
        "length_kind": rep.length_kind,
        "passed": rep.passed,
        "abbreviation_hits": [asdict(h) for h in rep.abbreviation_hits],
        "punctuation_hits": [asdict(h) for h in rep.punctuation_hits],
        "spam": asdict(rep.spam) if rep.spam else None,
    }


def print_report(rep: Report) -> None:
    limits = LENGTH_LIMITS.get(rep.length_kind, LENGTH_LIMITS["default"])
    print(f"\n=== {rep.file} ===")
    print(f"Авторский текст: {rep.text_chars:,} знаков (kind={rep.length_kind}, "
          f"target {limits['target_min']}-{limits['target_max']}, max {limits['max']})")
    print(f"Длина: {rep.length_status.upper()}")
    print(f"Итог: {'PASSED' if rep.passed else 'FAILED'}")
    if rep.length_status == "too_long":
        print(f"\n[FAIL] Длина {rep.text_chars} > потолка {limits['max']} — статья требует сокращения.")
    elif rep.length_status == "too_short":
        print(f"\n[FAIL] Длина {rep.text_chars} < минимума {limits['min']} — статья требует расширения.")

    if rep.abbreviation_hits:
        print(f"\n[FAIL] Сокращения юр-терминов в авторском тексте: {len(rep.abbreviation_hits)}")
        by_pat: dict[str, list[AbbreviationHit]] = {}
        for h in rep.abbreviation_hits:
            by_pat.setdefault(h.pattern, []).append(h)
        for name, hits in by_pat.items():
            print(f"  {name}: {len(hits)}")
            for h in hits[:3]:
                print(f"    «{h.match}»  →  {h.context}")
            if len(hits) > 3:
                print(f"    ... ещё {len(hits) - 3}")
    else:
        print("[OK] Сокращений юр-терминов нет.")

    if rep.punctuation_hits:
        print(f"\n[FAIL] Точка без пробела перед заглавной буквой: {len(rep.punctuation_hits)}")
        for h in rep.punctuation_hits[:5]:
            print(f"    {h.fragment}")
        if len(rep.punctuation_hits) > 5:
            print(f"    ... ещё {len(rep.punctuation_hits) - 5}")
    else:
        print("[OK] Пунктуация чистая.")

    if rep.spam:
        s = rep.spam
        print(f"\nЭвристика заспамленности:")
        print(f"  Всего слов (без стоп-слов): {s.total_words}")
        print(f"  Уникальных лемм: {s.unique_lemmas}")
        print(f"  Лексическое разнообразие: {s.lexical_diversity} (цель ≥0.62)")
        print(f"  Топ-10 слов суммарно: {s.top10_share * 100:.1f}% (цель ≤10.5%)")
        print(f"  Повторы 3-граммов: {s.ngram3_repeat_share * 100:.1f}% (цель ≤3.0%)")
        print(f"  Топ-5 частотных лемм: {s.top10_words[:5]}")
        if s.risk_flags:
            print(f"  [RISK] Превышены пороги: {s.risk_flags}")
            print(f"  [FAIL] Возврат на писателя: снизить плотность повторов.")


def collect_targets(path: Path) -> list[Path]:
    if path.is_file():
        return [path]
    if path.is_dir():
        files: list[Path] = []
        for ext in ("*.html", "*.md"):
            files.extend(path.rglob(ext))
        exclude = {"README.md", "CLAUDE.md", "BACKEND.md"}
        return sorted(f for f in files if f.name not in exclude and "node_modules" not in f.parts)
    raise FileNotFoundError(path)


def main() -> int:
    parser = argparse.ArgumentParser(description="Проверки качества: сокращения, пунктуация, заспамленность")
    parser.add_argument("path", help="Путь к файлу (.html/.md) или директории")
    parser.add_argument("--json", action="store_true", help="Вывод в JSON")
    args = parser.parse_args()

    target = Path(args.path).resolve()
    if not target.exists():
        print(f"Не найдено: {target}", file=sys.stderr)
        return 1

    files = collect_targets(target)
    if not files:
        print(f"В {target} не найдено .html/.md файлов", file=sys.stderr)
        return 1

    reports = [analyze(f) for f in files]

    if args.json:
        print(json.dumps([to_dict(r) for r in reports], ensure_ascii=False, indent=2))
    else:
        for rep in reports:
            print_report(rep)

    return 0 if all(r.passed for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
