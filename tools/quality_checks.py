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
    "маткапитал/маткап": re.compile(r"\bматкапитал\w*|\bматкап\b", re.IGNORECASE | re.UNICODE),
}

# === Дефисные сцепки субъектов (кредитор-поставщик, организация-должник) ===
# text.ru метит их как ошибки правописания. autofix.py расцепляет их
# детерминированно ПЕРЕД этой проверкой, поэтому в норме остаток = 0.
# Детектор оставлен как WARNING (не блокирует gate, не плодит итерации) —
# чтобы ловить экзотические сцепки, которые autofix не покрыл, и видеть их
# в отчёте. Список ролей синхронизирован с tools/autofix.py.
try:
    from tools.autofix import HYPHEN_ROLE_TYPE_RX, HYPHEN_ROLE_ROLE_RX
    _HYPHEN_RX_AVAILABLE = True
except Exception:  # pragma: no cover - автономный запуск без пакета
    _HYPHEN_RX_AVAILABLE = False

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
    top1_count: int  # абсолютное число вхождений самой частой леммы (cap 12)
    top3_sum: int    # сумма топ-3 лемм (cap 30)
    top10_sum: int   # сумма топ-10 лемм (cap 80)
    ngram3_repeat_count: int
    ngram3_total: int
    ngram3_repeat_share: float
    risk_flags: list[str]
    word_warnings: list[str] = field(default_factory=list)  # леммы count>12 для прореживания


@dataclass
class TargetedTokenHit:
    """Конкретные токены, которые text.ru стабильно подсвечивает как заспам:
    «ст», «РФ», «руб», «ООО», «000 руб». Лимиты эмпирические из реальных
    скринов text.ru (13 мая 2026, статьи с spam 56-60%).

    `severity`:
        'hard' — блокирует gate (старые токены, 13 мая)
        'soft' — попадает в отчёт writer'у, но НЕ блокирует gate
                  (новые токены 16 мая: 127-фз, 213, 000, ГПК, ГК, cta_формула).
                  Цель — дать writer'у точную обратную связь без срыва слотов.
    """
    token: str
    count: int
    limit: int
    over_limit: bool
    severity: str = "hard"


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
    hyphen_compound_hits: list[str] = field(default_factory=list)  # WARNING, не блокирует
    spam: SpamHeuristics | None = None
    targeted_tokens: list[TargetedTokenHit] = field(default_factory=list)
    author_markers_count: int = 0
    author_markers_min: int = 2  # минимум 2 авторские вставки на статью

    @property
    def passed(self) -> bool:
        if self.length_status in ("too_short", "too_long"):
            return False
        if self.abbreviation_hits or self.punctuation_hits:
            return False
        if self.spam and len(self.spam.risk_flags) >= 1:
            return False
        # Только HARD-severity targeted-токены блокируют (старые: ст/РФ/руб/ООО/000руб).
        # SOFT-severity (новые 16 мая: 127-фз, 213, 000, ГПК, ГК, cta) попадают
        # в отчёт writer'у как feedback, но не срывают слот.
        if any(h.over_limit and h.severity == "hard" for h in self.targeted_tokens):
            return False
        if self.author_markers_count < self.author_markers_min:
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


def check_hyphen_compounds(text: str) -> list[str]:
    """WARNING-детектор дефисных сцепок субъектов (кредитор-поставщик и т.п.).
    autofix их уже расцепляет, поэтому в норме пусто. Не блокирует gate."""
    if not _HYPHEN_RX_AVAILABLE:
        return []
    hits = []
    for rx in (HYPHEN_ROLE_TYPE_RX, HYPHEN_ROLE_ROLE_RX):
        hits.extend(m.group(0).strip() for m in rx.finditer(text))
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
    # Леммы-нарушители для прореживания (агент 6 / writer self-check).
    # >12 — порог, на котором лемма заметно тянет top10_share и заспам.
    word_warnings = [f"«{lemma}» ×{cnt}" for lemma, cnt in counter.most_common(12) if cnt > 12]
    top10_sum = sum(c for _, c in top10)
    top10_share = round(top10_sum / total_words, 3)
    top1_count = top10[0][1] if top10 else 0
    top3_sum = sum(c for _, c in top10[:3])

    # 3-граммы строим из исходных слов (не лемм), чтобы повтор был именно повтором.
    lower_words = [w.lower() for w in words]
    ngrams = [tuple(lower_words[i:i + 3]) for i in range(len(lower_words) - 2)]
    ngram_counter = Counter(ngrams)
    repeats = sum(c for c in ngram_counter.values() if c >= 2)
    total_ngrams = len(ngrams) if ngrams else 1
    ngram3_repeat_share = round(repeats / total_ngrams, 3)

    # Пороги под целевую заспамленность text.ru ≤ 50% (KPI заказчика).
    #
    # Калибровка (13 мая 2026, после анализа реальных скринов text.ru):
    # ratio-метрики (top10/ngram3/lex_div) — ОСЛАБЛЕНЫ до коридора 48-50% spam.
    # Жёсткий контроль перенесён на АБСОЛЮТНЫЕ счётчики и целевые токены —
    # они стабильнее в окрестности target и не зависят от длины статьи.
    #
    # Старые пороги (top10≤0.105 / ngram3≤0.030 / lex_div≥0.62) калибровались
    # под <40% spam и заставляли writer крутить 5-16 итераций - метрики
    # физически не сходились на 7-8к знаков с плотной legal-терминологией.
    #
    # Новый подход: смотрим в первую очередь на абсолютные cap'ы и токены.
    # Ratio-флаги работают как мягкий бэкап.
    risk_flags = []

    # Абсолютные cap'ы (не зависят от длины — урезание filler'а их не двигает)
    if top1_count > 12:
        risk_flags.append(f"top1_count>{12} (={top1_count}, лемма «{top10[0][0]}»)")
    if top3_sum > 30:
        risk_flags.append(f"top3_sum>{30} (={top3_sum})")
    if top10_sum > 80:
        risk_flags.append(f"top10_sum>{80} (={top10_sum})")

    # Ratio-флаги — мягкий бэкап под коридор 48-50% spam
    if top10_share > 0.115:
        risk_flags.append(f"top10_share>{0.115} (={top10_share})")
    if ngram3_repeat_share > 0.035:
        risk_flags.append(f"ngram3_repeat_share>{0.035} (={ngram3_repeat_share})")
    if lexical_diversity < 0.58:
        risk_flags.append(f"lexical_diversity<{0.58} (={lexical_diversity})")

    return SpamHeuristics(
        total_words=total_words,
        unique_lemmas=unique_lemmas,
        lexical_diversity=lexical_diversity,
        top10_words=top10,
        top10_share=top10_share,
        top1_count=top1_count,
        top3_sum=top3_sum,
        top10_sum=top10_sum,
        ngram3_repeat_count=repeats,
        ngram3_total=total_ngrams,
        ngram3_repeat_share=ngram3_repeat_share,
        risk_flags=risk_flags,
        word_warnings=word_warnings,
    )


# === Целевые токены — главные виновники text.ru spam (из анализа 13 мая 2026) ===
# Лимиты эмпирические из реальных скринов text.ru:
# - «ст» × 26 (плохая статья 56%) vs 0 (эталон 49%)
# - «РФ» × 27 vs 0
# - «руб» × 23 vs 0 (эталон использует полное «рублей»)
# - «ООО» × 24 vs 0
# Каждое снижение этих токенов на 5-7 даёт ~2% text.ru-spam.
TARGETED_TOKENS = {
    # === HARD-cap'ы (блокируют gate, существуют с 13 мая 2026) ===
    "ст_сокращение": {  # «ст. X», «ст N» - короткая форма «статья»
        "rx": re.compile(r"\bст\.?\s*\d", re.IGNORECASE | re.UNICODE),
        "limit": 5,
        "severity": "hard",
        "rationale": "сокращение «ст.» накапливается в юр-цитатах. Писать «статья» полным словом, либо описательно через смысл нормы. Cap ≤5 на статью.",
    },
    "РФ_рудимент": {  # «РФ» в «ГК РФ», «ГПК РФ», «закон РФ»
        "rx": re.compile(r"\bРФ\b", re.UNICODE),
        "limit": 2,
        "severity": "hard",
        "rationale": "«РФ» в «ГК РФ»/«ГПК РФ» — рудимент, дроп без потери смысла. Cap ≤2 на статью.",
    },
    "руб_сокращение": {  # «руб.», «руб» как отдельный токен
        "rx": re.compile(r"\bруб\.?\b", re.IGNORECASE | re.UNICODE),
        "limit": 0,
        "severity": "hard",
        "rationale": "«руб» — другой токен чем «рублей». Писать только полным словом «рублей»/«рубля». Cap = 0.",
    },
    "ООО_бренд": {  # «ООО» в авторском тексте (не в footer/aside)
        "rx": re.compile(r"\bООО\b", re.UNICODE),
        "limit": 3,
        "severity": "hard",
        "rationale": "«ООО» накапливается из бренд-боилерплейта и упоминаний категории. В авторском тексте писать «юридическое лицо» или «компания». Cap ≤3.",
    },
    "000руб_паттерн": {  # «X 000 руб» — главный n-gram повторов сумм
        "rx": re.compile(r"\b\d+\s*000\s*руб", re.IGNORECASE | re.UNICODE),
        "limit": 2,
        "severity": "hard",
        "rationale": "Шаблон «X 000 руб» (100 000 руб / 300 000 руб) — повторяющийся n-gram. Использовать полное «рублей» + некруглые суммы (82 400, 147 500). Cap ≤2 (для статутных порогов).",
    },
    # === SOFT-cap'ы (16 мая 2026, выводятся в отчёт writer'у, НЕ блокируют gate) ===
    # Эмпирика из анализа 16 мая (статьи 56-60% spam): эти токены забивают топ-10
    # text.ru, но HARD-cap здесь нельзя — слоты сорвутся. Soft-cap даёт writer'у
    # точную обратную связь «снизить X с 14 до 2», работает через playbook §9.
    "127фз_закон": {  # «127-ФЗ», «ФЗ-127», «N 127-ФЗ»
        "rx": re.compile(r"\b127[-\s]?ФЗ\b|\bФЗ[-\s]?127\b|\bN[°№]?\s*127[-\s]?ФЗ\b", re.IGNORECASE | re.UNICODE),
        "limit": 2,
        "severity": "soft",
        "rationale": "«127-ФЗ» как номер закона стабильно × 12-14 раз в плохих fiz/yur-статьях. Заменять на «закон о банкротстве» / «федеральный закон» / «закон». Soft-cap ≤2.",
    },
    "213_артикул": {  # «213.28», «213.30», «ст. 213»
        "rx": re.compile(r"\b213\.\d+\b|\bст\.?\s*213\b", re.IGNORECASE | re.UNICODE),
        "limit": 3,
        "severity": "soft",
        "rationale": "Номера статей 213.28 / 213.30 закона о банкротстве × 11-15 в плохих кейсах. Заменять описательно: «правило о добросовестности», «норма о завершении процедуры». Soft-cap ≤3.",
    },
    "ГПК_кодекс": {  # «ГПК» (любая форма, кроме «ГПК РФ» — это уже в РФ_рудимент)
        "rx": re.compile(r"\bГПК\b", re.UNICODE),
        "limit": 3,
        "severity": "soft",
        "rationale": "«ГПК» × 12 в плохих взыск-статьях. Заменять на «процессуальный кодекс», «кодекс», описательно. Soft-cap ≤3.",
    },
    "ГК_кодекс": {  # «ГК» (любая форма)
        "rx": re.compile(r"\bГК\b", re.UNICODE),
        "limit": 3,
        "severity": "soft",
        "rationale": "«ГК» × 10 в плохих взыск-статьях. Заменять на «гражданский кодекс», «кодекс». Soft-cap ≤3.",
    },
    "000_число": {  # «000» как часть числа («1 000 000», «300 000»)
        "rx": re.compile(r"(?<!\d)0{3}(?!\d)", re.UNICODE),
        "limit": 8,
        "severity": "soft",
        "rationale": "Тройной ноль в круглых суммах × 11-24 раз. Использовать некруглые числа (82 400, 326 900) или словесную форму («триста тысяч», «полмиллиона»). Soft-cap ≤8.",
    },
    "cta_формула_повтор": {  # «оставить заявку» — повторяющаяся CTA-триграмма
        "rx": re.compile(r"оставить\s+заявку", re.IGNORECASE | re.UNICODE),
        "limit": 1,
        "severity": "soft",
        "rationale": "Если фраза «оставить заявку» встречается × 3 — три CTA-блока используют одинаковый текст. inject_boilerplate.py теперь по дефолту даёт три РАЗНЫЕ формулировки (TOP/MID/BOTTOM). Если cap превышен — заданы кастомные cta_*_text в meta.json, надо их разнести. Soft-cap ≤1 (одна на финальный CTA-BOTTOM).",
    },
}


def check_targeted_tokens(text: str) -> list[TargetedTokenHit]:
    """Считает целевые токены, которые text.ru стабильно ловит как заспам."""
    hits = []
    for name, cfg in TARGETED_TOKENS.items():
        count = len(cfg["rx"].findall(text))
        hits.append(TargetedTokenHit(
            token=name,
            count=count,
            limit=cfg["limit"],
            over_limit=count > cfg["limit"],
            severity=cfg.get("severity", "hard"),
        ))
    return hits


# === Авторские вставки бренда (обязательны для AI-detector ≤ 5%) ===
# Эмпирика: статьи с 0% AI имеют 2× «по нашему опыту»/«в нашей практике».
# Статьи с 7-19% AI — 0 раз. Это сильнейший человеческий маркер.
AUTHOR_MARKER_RX = re.compile(
    r"\b(?:"
    r"по\s+нашему\s+опыту|"
    r"в\s+нашей\s+практике|"
    r"мы\s+(?:считаем|видим|понимаем|думаем|знаем|сталкивались|наблюдаем)|"
    r"на\s+нашей\s+практике|"
    r"наши\s+клиенты"
    r")\b",
    re.IGNORECASE | re.UNICODE,
)


def count_author_markers(text: str) -> int:
    """Считает упоминания «мы»/«наш опыт» — обязательно ≥2 для бренд-голоса."""
    return len(AUTHOR_MARKER_RX.findall(text))


def predict_textru_metrics(
    spam: SpamHeuristics | None,
    ai_density_per_1000: float,
    ai_critical_count: int,
    anti_template_hits_count: int,
    targeted_token_hits: list[TargetedTokenHit] | None = None,
    author_markers_count: int = 0,
) -> dict:
    """
    Прогноз метрик text.ru на основе локальных эвристик.

    text.ru API не подключён, поэтому реальные метрики недоступны.
    Эта функция даёт оценочный прогноз (±5-7%), откалиброванный по реальным
    замерам:
        - lex_div 0.62 → spam ~50%, lex_div 0.70 → ~42%
        - ai_density 1.0 → AI ~10%, density 2.0 → ~18%, critical → ~35%
        - 0 anti-template hits → uniqueness ~88-90%, 3+ hits → ~78%

    Возвращает: {"spam_pct": int, "ai_pct": int, "uniqueness_pct": int}
    Все значения округлены до целых процентов.
    """
    if spam is None:
        return {"spam_pct": 50, "ai_pct": 10, "uniqueness_pct": 85}

    # === Заспам (главный предиктор: lex_div + targeted tokens) ===
    # Калибровка по реальным данным:
    #   - 0.633 lex_div + 25 «ст» + 27 «РФ» + 23 «руб» → spam 56% (isk, скрин 13 мая)
    #   - 0.62 lex_div, чистые токены → spam ~49% (эталон)
    #   - 0.70 lex_div, чистые токены → spam ~42%
    # Логика: ratio-метрики дают «базу», targeted-токены добавляют сверху.
    lex_div = spam.lexical_diversity
    if lex_div >= 0.73:
        spam_pct = 38
    elif lex_div >= 0.70:
        spam_pct = 42
    elif lex_div >= 0.65:
        spam_pct = 47
    elif lex_div >= 0.62:
        spam_pct = 49
    elif lex_div >= 0.59:
        spam_pct = 53
    else:
        spam_pct = 58
    # Добавки от ratio-метрик за пределами коридора 48-50%
    if spam.top10_share > 0.115:
        spam_pct += 2
    if spam.ngram3_repeat_share > 0.035:
        spam_pct += 2

    # Targeted-tokens — эмпирическая калибровка по скрину 13 мая:
    # плохая статья 56% spam имела ст=25 (over 5 на 20), РФ=27 (over 2 на 25),
    # руб=23 (over 0 на 23), X000руб=20 (over 2 на 18). Суммарно ~86 «лишних».
    # Эталон 49% spam: все 0. Разница 7 п.п. = 86 лишних / 12 = ~7.
    # Формула: +1% за каждые 12 «лишних» HARD-токенов.
    # Калибровка 16 мая: soft-токены (127-фз, 213, 000, ГПК, ГК, cta) тоже
    # дают вклад в spam, но меньший — +1% за каждые 20 лишних. Без этого
    # МФО-статья с soft-cap превышенным × 19 даёт прогноз «49%» при реальных 57%.
    if targeted_token_hits:
        over_hard = sum(
            max(0, h.count - h.limit) for h in targeted_token_hits
            if getattr(h, "severity", "hard") == "hard"
        )
        over_soft = sum(
            max(0, h.count - h.limit) for h in targeted_token_hits
            if getattr(h, "severity", "hard") == "soft"
        )
        spam_pct += min(15, over_hard // 12)  # hard: +1% за каждые 12 лишних, cap +15%
        spam_pct += min(8, over_soft // 20)   # soft: +1% за каждые 20 лишних, cap +8%

    # === AI-detector (density маркеров + author_markers) ===
    # Эмпирика 13 мая 2026: главный anti-AI маркер — author_markers («мы», «по нашему опыту»).
    # 0 author_markers → AI 10-19%; 2+ author_markers → AI 0-3%.
    if ai_critical_count > 0:
        ai_pct = 35  # критические маркеры (длинные тире, эмодзи, «я») = ChatGPT-стиль
    elif ai_density_per_1000 >= 2.5:
        ai_pct = 22
    elif ai_density_per_1000 >= 2.0:
        ai_pct = 16
    elif ai_density_per_1000 >= 1.5:
        ai_pct = 12
    elif ai_density_per_1000 >= 1.0:
        ai_pct = 8
    elif ai_density_per_1000 >= 0.5:
        ai_pct = 5
    else:
        ai_pct = 3
    # Без author_markers AI растёт на 5-10%
    if author_markers_count == 0:
        ai_pct += 8
    elif author_markers_count == 1:
        ai_pct += 3

    # === Уникальность (anti-template hits + lex_div) ===
    if anti_template_hits_count == 0 and lex_div >= 0.65:
        uniq_pct = 90
    elif anti_template_hits_count == 0:
        uniq_pct = 87
    elif anti_template_hits_count <= 2:
        uniq_pct = 83
    elif anti_template_hits_count <= 5:
        uniq_pct = 78
    else:
        uniq_pct = 70

    return {
        "spam_pct": min(max(spam_pct, 25), 90),
        "ai_pct": min(max(ai_pct, 3), 50),
        "uniqueness_pct": min(max(uniq_pct, 50), 95),
    }


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
        hyphen_compound_hits=check_hyphen_compounds(text),
        spam=spam,
        targeted_tokens=check_targeted_tokens(text),
        author_markers_count=count_author_markers(text),
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
        "hyphen_compound_hits": rep.hyphen_compound_hits,
        "spam": asdict(rep.spam) if rep.spam else None,
        "targeted_tokens": [asdict(h) for h in rep.targeted_tokens],
        "author_markers_count": rep.author_markers_count,
        "author_markers_min": rep.author_markers_min,
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

    if rep.hyphen_compound_hits:
        uniq = sorted(set(rep.hyphen_compound_hits))
        print(f"\n[WARN] Дефисные сцепки субъектов (autofix должен был расцепить): "
              f"{len(rep.hyphen_compound_hits)}")
        for h in uniq[:8]:
            print(f"    «{h}»")
        if len(uniq) > 8:
            print(f"    ... ещё {len(uniq) - 8} уникальных")

    if rep.spam:
        s = rep.spam
        print(f"\nЭвристика заспамленности (под KPI text.ru ≤50%):")
        print(f"  Всего слов (без стоп-слов): {s.total_words}")
        print(f"  Уникальных лемм: {s.unique_lemmas}")
        print(f"  Лексическое разнообразие: {s.lexical_diversity} (цель ≥0.58)")
        print(f"  Топ-1 лемма: {s.top1_count} вхождений (cap ≤12)")
        print(f"  Топ-3 в сумме: {s.top3_sum} (cap ≤30)")
        print(f"  Топ-10 в сумме: {s.top10_sum} (cap ≤80)")
        print(f"  Топ-10 доля: {s.top10_share * 100:.1f}% (цель ≤11.5%)")
        print(f"  Повторы 3-граммов: {s.ngram3_repeat_share * 100:.1f}% (цель ≤3.5%)")
        print(f"  Топ-5 частотных лемм: {s.top10_words[:5]}")
        if s.word_warnings:
            print(f"  [WORDS] Леммы для прореживания (count>12, главный драйвер заспама): {', '.join(s.word_warnings)}")
        if s.risk_flags:
            print(f"  [RISK] Превышены пороги: {s.risk_flags}")
            print(f"  [FAIL] Возврат на писателя: снизить плотность повторов.")

    if rep.targeted_tokens:
        print(f"\nЦелевые токены (главные виновники text.ru spam):")
        for h in rep.targeted_tokens:
            if h.over_limit:
                status = "[FAIL]" if h.severity == "hard" else "[WARN]"
            else:
                status = "[OK]"
            tag = "" if h.severity == "hard" else " (soft)"
            print(f"  {status} {h.token}{tag}: {h.count} (cap ≤{h.limit})")
        problem_hard = [h for h in rep.targeted_tokens if h.over_limit and h.severity == "hard"]
        problem_soft = [h for h in rep.targeted_tokens if h.over_limit and h.severity == "soft"]
        if problem_hard:
            print(f"  → [HARD] Снизить (блокирует gate): {', '.join(h.token for h in problem_hard)}")
        if problem_soft:
            print(f"  → [SOFT] Снизить для коридора 47-50% spam (не блокирует, но даёт +п.п.): "
                  f"{', '.join(h.token for h in problem_soft)}")

    print(f"\nАвторские вставки (мы считаем / по нашему опыту):")
    if rep.author_markers_count >= rep.author_markers_min:
        print(f"  [OK] {rep.author_markers_count} ≥ {rep.author_markers_min}")
    else:
        print(f"  [FAIL] {rep.author_markers_count} < {rep.author_markers_min} — добавить хотя бы 2 «мы считаем/по нашему опыту/в нашей практике»")


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
