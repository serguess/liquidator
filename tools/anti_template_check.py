"""
Чекер дословных шаблонных правовых фраз.

Проблема: Sonnet знает наизусть типовые формулировки с юр-порталов
(КонсультантПлюс, Гарант, Юрист.ру) и часто воспроизводит их дословно.
Это даёт уникальность 48-60% в text.ru — потому что эти фразы есть на
сотнях сайтов одинаково.

Решение: грипаем по списку фраз из `.claude/style/anti-template-phrases.md`.
Любое дословное совпадение → блок коммита, возврат на писателя с конкретной
цитатой и предложением перифраза.

Запуск:
    python -m tools.anti_template_check drafts/{slug}/article.html
    python -m tools.anti_template_check drafts/{slug}/article.html --json

Exit:
    0 — нет дословных совпадений
    1 — найдены совпадения
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
PHRASES_FILE = PROJECT_ROOT / ".claude" / "style" / "anti-template-phrases.md"


# Устойчивые юр-формулировки — их можно оставить дословно (не блокировать),
# потому что перифраз снижает правовую точность. Добавлены в исключения
# на основании пометки в anti-template-phrases.md.
EXEMPT_PHRASES = {
    "единственное пригодное для постоянного проживания жилое помещение",
    "исполнительский иммунитет",
    "сведения внесены в ефрсб",
    "единое федеральное реестр сведений о банкротстве",
    "неразрывно связанное с личностью кредитора",
}


@dataclass
class TemplateHit:
    phrase: str
    category: str
    position: int
    context: str


@dataclass
class Report:
    file: str
    text_chars: int
    hits: list[TemplateHit] = field(default_factory=list)
    phrases_loaded: int = 0

    @property
    def passed(self) -> bool:
        return len(self.hits) == 0


def load_phrases() -> list[tuple[str, str]]:
    """
    Парсит anti-template-phrases.md и возвращает список (phrase, category).

    Формат файла: секции `## Категория N: <название>` с буллетами `- «фраза»`.
    Берём только фразы из секций «Категория N» (не служебных).
    """
    if not PHRASES_FILE.exists():
        return []

    text = PHRASES_FILE.read_text(encoding="utf-8")
    phrases: list[tuple[str, str]] = []
    current_category = None
    in_phrase_section = False

    for line in text.splitlines():
        line = line.rstrip()

        # Заголовок категории
        m_cat = re.match(r"^##\s+Категория\s+\d+:\s*(.+)$", line)
        if m_cat:
            current_category = m_cat.group(1).strip()
            in_phrase_section = True
            continue

        # Секция "Как перифразировать" — заканчиваем сбор фраз для категории
        if line.startswith("**Как перифразировать") or line.startswith("**Эти ") \
                or line.startswith("**Эти три фразы"):
            in_phrase_section = False
            continue

        # Любой другой H2 кроме "Категория N" — выходим из сбора
        if line.startswith("## ") and not line.startswith("## Категория"):
            in_phrase_section = False
            current_category = None
            continue

        # Буллет с фразой в кавычках
        if in_phrase_section and current_category:
            m_bullet = re.match(r"^-\s+«(.+?)»\s*$", line)
            if m_bullet:
                phrase = m_bullet.group(1).strip().lower()
                if phrase not in EXEMPT_PHRASES:
                    phrases.append((phrase, current_category))

    return phrases


# === Извлечение авторского текста (HTML и Markdown) ===

ARTICLE_BODY_RX = re.compile(r"<article\b[^>]*>(.*?)</article>", re.DOTALL | re.IGNORECASE)
SCRIPT_RX = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)
STYLE_RX = re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE)
BLOCKQUOTE_RX = re.compile(r"<blockquote\b[^>]*>.*?</blockquote>", re.DOTALL | re.IGNORECASE)
TAG_RX = re.compile(r"<[^>]+>")
ENTITY_RX = re.compile(r"&[a-zA-Z]+;|&#\d+;")
FRONTMATTER_RX = re.compile(r"^---\n.*?\n---\n", re.DOTALL)
CODE_BLOCK_RX = re.compile(r"```.*?```", re.DOTALL)


def extract_author_text(file_path: Path) -> str:
    raw = file_path.read_text(encoding="utf-8")
    if file_path.suffix.lower() in {".html", ".htm"}:
        m = ARTICLE_BODY_RX.search(raw)
        body = m.group(1) if m else raw
        # Цитаты закона в blockquote — НЕ считаем (это допустимое цитирование)
        body = BLOCKQUOTE_RX.sub(" ", body)
        body = SCRIPT_RX.sub(" ", body)
        body = STYLE_RX.sub(" ", body)
        body = TAG_RX.sub(" ", body)
        body = ENTITY_RX.sub(" ", body)
    elif file_path.suffix.lower() in {".md", ".markdown"}:
        body = FRONTMATTER_RX.sub("", raw, count=1)
        body = CODE_BLOCK_RX.sub(" ", body)
    else:
        body = raw

    body = re.sub(r"\s+", " ", body).strip()
    return body


def context_of(text: str, start: int, end: int, window: int = 50) -> str:
    a = max(0, start - window)
    b = min(len(text), end + window)
    return ("..." if a > 0 else "") + text[a:b] + ("..." if b < len(text) else "")


def check_phrases(text: str, phrases: list[tuple[str, str]]) -> list[TemplateHit]:
    text_lower = text.lower()
    hits: list[TemplateHit] = []
    for phrase, category in phrases:
        # Ищем дословные совпадения (с учётом пробелов).
        # Фразы хранятся в нижнем регистре, ищем в нижнем регистре.
        start = 0
        while True:
            idx = text_lower.find(phrase, start)
            if idx == -1:
                break
            hits.append(TemplateHit(
                phrase=phrase,
                category=category,
                position=idx,
                context=context_of(text, idx, idx + len(phrase)),
            ))
            start = idx + len(phrase)
    return hits


def analyze(file_path: Path) -> Report:
    phrases = load_phrases()
    text = extract_author_text(file_path)
    rel_path = (
        file_path.relative_to(PROJECT_ROOT)
        if file_path.is_relative_to(PROJECT_ROOT)
        else file_path
    )
    return Report(
        file=str(rel_path),
        text_chars=len(text),
        hits=check_phrases(text, phrases),
        phrases_loaded=len(phrases),
    )


def to_dict(rep: Report) -> dict:
    return {
        "file": rep.file,
        "text_chars": rep.text_chars,
        "phrases_loaded": rep.phrases_loaded,
        "passed": rep.passed,
        "hits": [asdict(h) for h in rep.hits],
    }


def print_report(rep: Report) -> None:
    print(f"\n=== {rep.file} ===")
    print(f"Загружено шаблонных фраз: {rep.phrases_loaded}")
    print(f"Текст: {rep.text_chars:,} знаков")
    print(f"Итог: {'PASSED' if rep.passed else 'FAILED'}")
    if rep.hits:
        print(f"\n[FAIL] Дословные совпадения с шаблонными фразами: {len(rep.hits)}")
        by_cat: dict[str, list[TemplateHit]] = {}
        for h in rep.hits:
            by_cat.setdefault(h.category, []).append(h)
        for cat, hits in by_cat.items():
            print(f"\n  Категория: {cat}")
            for h in hits[:3]:
                print(f"    «{h.phrase}»")
                print(f"      → {h.context}")
            if len(hits) > 3:
                print(f"    ... ещё {len(hits) - 3}")
        print("\nДействие: писатель должен перифразировать каждое срабатывание.")
        print(f"См. подсказки в {PHRASES_FILE.relative_to(PROJECT_ROOT)} (раздел «Как перифразировать»).")


def main() -> int:
    parser = argparse.ArgumentParser(description="Чекер дословных шаблонных юр-фраз")
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
