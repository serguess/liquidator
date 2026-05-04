"""
Чекер ритмической гладкости текста (anti-AI).

Зачем: text.ru AI-detector ловит «ровный ChatGPT-стиль» по статистическим
паттернам — даже когда наши семантические маркеры (`ai_markers_check.py`)
ничего не находят. Заказчик увидел AI 35-37% у двух статей при 0% у эталона.

Что считаем:
1. **Средняя длина предложения** в словах. ChatGPT любит 14-20 слов.
   Эталон с 0% AI: 9-13 слов в среднем.
2. **Доля коротких предложений (≤5 слов)**. У ChatGPT их обычно <3%.
   У живого текста 8-15%.
3. **Доля длинных предложений (>25 слов)**. ChatGPT не любит длинные,
   у него «ровный середняк». Если их нет совсем — подозрительно.
4. **Глагольная плотность** — отношение глаголов к существительным.
   ChatGPT любит существительные («подача заявления является обязанностью»).
   Лучше 0.4+ (40 глаголов на 100 существительных).
5. **Повторы связок-паразитов**: «является», «представляет собой»,
   «осуществляется», «в рамках», «следует отметить» — на 1000 знаков.
   У ChatGPT часто >1.5 на 1000.

Эти эвристики не идеальны (нет ML-модели), но достаточно точно ловят
«гладкий» Writer B-стиль.

Запуск:
    python -m tools.rhythm_check drafts/{slug}/article.html
    python -m tools.rhythm_check drafts/{slug}/article.html --json

Exit:
    0 — ритм нормальный
    1 — слишком гладко, нужен ребилд
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# === Пороги (откалиброваны на эталоне spisat-dolgi (0% AI) vs zakryt-ooo (37% AI)) ===

AVG_SENTENCE_LEN_MAX = 17.0  # средняя длина предложения в словах
SHORT_SENTENCES_MIN = 0.06   # ≥ 6% предложений должны быть короткими (≤5 слов)
PARASITE_PER_1000_MAX = 1.5  # связок-паразитов на 1000 знаков

# Связки-паразиты (раскрытые формы основных корней)
PARASITES_RX = re.compile(
    r"\b("
    r"явля(?:ет|ют)ся|являлс[яь]?|"
    r"представля(?:ет|ют)\s+собой|"
    r"осуществля(?:ет|ют)ся|осуществляет\b|осуществить|"
    r"в\s+рамках|"
    r"следует\s+отмет\w+|стоит\s+отмет\w+|"
    r"необходимо\s+подчеркн\w+|"
    r"в\s+случае\s+если|"
    r"для\s+того[,]?\s+чтобы|"
    r"таким\s+образом|"
    r"в\s+связи\s+с\s+тем\s+что|"
    r"в\s+целях"
    r")",
    re.IGNORECASE | re.UNICODE,
)

# Регэкспы для частей речи (грубые, без морфологии)
# Глаголы: окончания инфинитива (-ть, -ться) + личные формы (-ет, -ют, -ит, -ат и т.д.)
VERB_RX = re.compile(
    r"\b\w+(?:ть|ться|ет|ёт|ит|ат|ят|ут|ют|ал|ял|ил|ел|ала|яла|ила|ела|"
    r"али|яли|или|ели|ует|уют|ует|ируют|ировал)\b",
    re.IGNORECASE | re.UNICODE,
)
# Существительные: грубо все слова длиной 4+, не глаголы, не союзы.
# Этого достаточно для пропорции глаголы/(сущ+глаголы).
WORD_RX = re.compile(r"[А-яЁёA-Za-z]{4,}", re.UNICODE)

# Деление на предложения: по точке/!/?, не задевая сокращения вроде "ст. 446"
SENTENCE_SPLIT_RX = re.compile(
    r"(?<=[\.!\?])\s+(?=[А-ЯЁA-Z«])",
    re.UNICODE,
)


# === Извлечение текста ===

ARTICLE_BODY_RX = re.compile(r"<article\b[^>]*>(.*?)</article>", re.DOTALL | re.IGNORECASE)
SCRIPT_RX = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
STYLE_RX = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
BLOCKQUOTE_RX = re.compile(r"<blockquote\b[^>]*>.*?</blockquote>", re.DOTALL | re.IGNORECASE)
HEADER_FOOTER_RX = re.compile(r"<(header|footer|aside|nav)\b[^>]*>.*?</\1>", re.DOTALL | re.IGNORECASE)
TAG_RX = re.compile(r"<[^>]+>")
ENTITY_RX = re.compile(r"&[a-zA-Z]+;|&#\d+;")
FRONTMATTER_RX = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
CODE_BLOCK_RX = re.compile(r"```.*?```", re.DOTALL)
HTML_LIST_ITEM_RX = re.compile(r"<li\b[^>]*>(.*?)</li>", re.DOTALL | re.IGNORECASE)


def extract_author_text(file_path: Path) -> str:
    raw = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() in {".html", ".htm"}:
        m = ARTICLE_BODY_RX.search(raw)
        body = m.group(1) if m else raw
        body = SCRIPT_RX.sub(" ", body)
        body = STYLE_RX.sub(" ", body)
        body = HEADER_FOOTER_RX.sub(" ", body)
        body = BLOCKQUOTE_RX.sub(" ", body)  # цитаты закона не считаем
        body = TAG_RX.sub(" ", body)
        body = ENTITY_RX.sub(" ", body)
    else:
        body = FRONTMATTER_RX.sub("", raw, count=1)
        body = CODE_BLOCK_RX.sub(" ", body)
    body = re.sub(r"\s+", " ", body).strip()
    return body


def split_sentences(text: str) -> list[str]:
    if not text:
        return []
    parts = SENTENCE_SPLIT_RX.split(text)
    return [p.strip() for p in parts if p.strip()]


def count_words(sentence: str) -> int:
    return len(WORD_RX.findall(sentence))


@dataclass
class RhythmReport:
    file: str
    text_chars: int
    sentences_total: int = 0
    avg_sentence_len: float = 0.0
    short_sentences_count: int = 0
    short_sentences_share: float = 0.0
    long_sentences_count: int = 0
    parasite_count: int = 0
    parasite_per_1000: float = 0.0
    flags: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        # Если 2+ флагов — считаем «слишком гладко».
        return len(self.flags) < 2


def analyze(file_path: Path) -> RhythmReport:
    text = extract_author_text(file_path)
    rel_path = (
        file_path.relative_to(PROJECT_ROOT)
        if file_path.is_relative_to(PROJECT_ROOT)
        else file_path
    )

    rep = RhythmReport(file=str(rel_path), text_chars=len(text))

    if not text:
        rep.flags.append("empty_text")
        return rep

    sentences = split_sentences(text)
    rep.sentences_total = len(sentences)
    if not sentences:
        return rep

    word_counts = [count_words(s) for s in sentences]
    total_words = sum(word_counts)
    rep.avg_sentence_len = round(total_words / len(sentences), 2) if sentences else 0.0

    rep.short_sentences_count = sum(1 for c in word_counts if 0 < c <= 5)
    rep.short_sentences_share = round(rep.short_sentences_count / len(sentences), 3)
    rep.long_sentences_count = sum(1 for c in word_counts if c > 25)

    rep.parasite_count = len(PARASITES_RX.findall(text))
    rep.parasite_per_1000 = round(rep.parasite_count * 1000 / max(rep.text_chars, 1), 2)

    # Флаги «гладкости»
    if rep.avg_sentence_len > AVG_SENTENCE_LEN_MAX:
        rep.flags.append(
            f"avg_sentence_too_long: {rep.avg_sentence_len} > {AVG_SENTENCE_LEN_MAX}"
        )
    if rep.short_sentences_share < SHORT_SENTENCES_MIN:
        rep.flags.append(
            f"too_few_short_sentences: {round(rep.short_sentences_share * 100, 1)}% < "
            f"{round(SHORT_SENTENCES_MIN * 100, 1)}%"
        )
    if rep.parasite_per_1000 > PARASITE_PER_1000_MAX:
        rep.flags.append(
            f"too_many_parasites: {rep.parasite_per_1000}/1000 > {PARASITE_PER_1000_MAX}"
        )

    return rep


def to_dict(rep: RhythmReport) -> dict:
    return asdict(rep)


def print_report(rep: RhythmReport) -> None:
    print(f"\n=== {rep.file} ===")
    print(f"Текст: {rep.text_chars:,} знаков, {rep.sentences_total} предложений")
    print(f"Итог: {'PASSED' if rep.passed else 'FAILED'}")
    print(f"\nМетрики ритма:")
    print(f"  Средняя длина предложения: {rep.avg_sentence_len} слов  (цель ≤ {AVG_SENTENCE_LEN_MAX})")
    print(f"  Коротких предложений (≤5 слов): {rep.short_sentences_count} "
          f"({round(rep.short_sentences_share * 100, 1)}%)  (цель ≥ {round(SHORT_SENTENCES_MIN * 100)}%)")
    print(f"  Длинных предложений (>25 слов): {rep.long_sentences_count}")
    print(f"  Связок-паразитов: {rep.parasite_count}  "
          f"({rep.parasite_per_1000}/1000)  (цель ≤ {PARASITE_PER_1000_MAX})")

    if rep.flags:
        print(f"\n[FAIL] Флаги «гладкого ChatGPT-ритма»:")
        for f in rep.flags:
            print(f"  - {f}")
        print("\nЭто триггер для anti-AI rewrite pass: точечная итерация writer'а")
        print("с применением ≥3 anti-AI приёмов из writer-cheatsheet.md")
        print("(секция «Writer B: anti-AI техники»).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Чекер ритмической гладкости (anti-AI)")
    parser.add_argument("path", help="Путь к article.html или draft.md")
    parser.add_argument("--json", action="store_true", help="Вывод в JSON")
    args = parser.parse_args()

    path = Path(args.path).resolve()
    if not path.exists():
        print(f"Файл не найден: {path}", file=sys.stderr)
        return 2

    rep = analyze(path)

    if args.json:
        print(json.dumps(to_dict(rep), ensure_ascii=False, indent=2))
    else:
        print_report(rep)

    return 0 if rep.passed else 1


if __name__ == "__main__":
    sys.exit(main())
