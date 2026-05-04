"""
Детерминистический автофикс HTML/MD статьи.

Задача: убрать из авторского текста запрещённые сокращения юр-терминов
и пропуски пробела после точки. Раньше это было soft-обязательством LLM
в seo-editor.md (агент 6) — модель иногда забывала. Теперь это hard-шаг
конвейера, вызываемый scheduler'ом перед quality_gate.

Что чинит:
1. юрлицо/юрлица/юрлицам/юрлицами → юридическое лицо/юридические лица/...
2. физлицо/физлица/физлицам/физлицами → физическое лицо/физические лица/...
3. дебиторка/дебиторки/дебиторкой → дебиторская задолженность/...
4. финуправляющий/финупр → арбитражный управляющий
5. кредорг → кредитная организация
6. исполлист → исполнительный лист
7. Точка без пробела перед заглавной буквой: «слово.Слово» → «слово. Слово»

Не трогает:
- <title>, <meta>, JSON-LD <script>, <head>
- FAQ-вопросы (<h3>...?</h3>) — там сокращения допустимы (форма поискового запроса)
- <pre>, <code>, <blockquote>, <style>

Запуск:
    python -m tools.autofix drafts/{slug}/article.html
    python -m tools.autofix drafts/{slug}/article.html --dry-run
    python -m tools.autofix drafts/{slug}/draft.md

Возврат: 0 — успех (даже если ничего не изменилось), 1 — ошибка чтения/записи.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict, field
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


# === Карта замен по падежам ===
# Каждое сокращение покрыто всеми падежами ед.ч. и мн.ч.
# Ключ: regex, значение: lambda(match) → замена с учётом падежа.
# Падеж определяется по окончанию: -о (им. ед.), -а (род./им.мн.), -у (дат./вин.),
# -ом (тв. ед.), -ами (тв. мн.), -ам (дат. мн.), -ах (пр. мн.), -е (пр. ед.).

REPLACEMENTS = [
    # юрлицо
    (
        re.compile(r"\bюрлиц(а|у|ом|ами|ам|ах|ы|е|о)?\b", re.IGNORECASE | re.UNICODE),
        {
            "":     "юридическое лицо",
            "о":    "юридическое лицо",
            "а":    "юридического лица",   # род. ед., может быть им./вин.мн. — не различаем без морфологии
            "у":    "юридическому лицу",
            "ом":   "юридическим лицом",
            "е":    "юридическом лице",
            "ы":    "юридические лица",    # формально нестандарт, но встречается
            "ами":  "юридическими лицами",
            "ам":   "юридическим лицам",
            "ах":   "юридических лицах",
        },
    ),
    # физлицо
    (
        re.compile(r"\bфизлиц(а|у|ом|ами|ам|ах|ы|е|о)?\b", re.IGNORECASE | re.UNICODE),
        {
            "":     "физическое лицо",
            "о":    "физическое лицо",
            "а":    "физического лица",
            "у":    "физическому лицу",
            "ом":   "физическим лицом",
            "е":    "физическом лице",
            "ы":    "физические лица",
            "ами":  "физическими лицами",
            "ам":   "физическим лицам",
            "ах":   "физических лицах",
        },
    ),
    # дебиторка
    (
        re.compile(r"\bдебиторк(и|у|е|ой|ою|ам|ами|ах|а)?\b", re.IGNORECASE | re.UNICODE),
        {
            "":     "дебиторская задолженность",
            "а":    "дебиторская задолженность",
            "и":    "дебиторской задолженности",
            "у":    "дебиторскую задолженность",
            "е":    "дебиторской задолженности",
            "ой":   "дебиторской задолженностью",
            "ою":   "дебиторской задолженностью",
            "ам":   "дебиторским задолженностям",
            "ами":  "дебиторскими задолженностями",
            "ах":   "дебиторских задолженностях",
        },
    ),
    # финуправляющий — упрощённо все формы заменяем на «арбитражный управляющий»,
    # padеж берём по окончанию основной формы
    (
        re.compile(r"\bфинуправляющ(ий|его|ему|им|ем|ие|их|ими|ая|ую|ей)?\b", re.IGNORECASE | re.UNICODE),
        {
            "":     "арбитражный управляющий",
            "ий":   "арбитражный управляющий",
            "его":  "арбитражного управляющего",
            "ему":  "арбитражному управляющему",
            "им":   "арбитражным управляющим",
            "ем":   "арбитражном управляющем",
            "ие":   "арбитражные управляющие",
            "их":   "арбитражных управляющих",
            "ими":  "арбитражными управляющими",
            "ая":   "арбитражная управляющая",
            "ую":   "арбитражную управляющую",
            "ей":   "арбитражной управляющей",
        },
    ),
    # финупр (короткое) → арбитражный управляющий
    (
        re.compile(r"\bфинупр\b", re.IGNORECASE | re.UNICODE),
        {"": "арбитражный управляющий"},
    ),
    # кредорг
    (
        re.compile(r"\bкредорг(а|у|и|е|ой|ой|ах|ам|ами)?\b", re.IGNORECASE | re.UNICODE),
        {
            "":     "кредитная организация",
            "а":    "кредитная организация",
            "и":    "кредитной организации",
            "у":    "кредитную организацию",
            "е":    "кредитной организации",
            "ой":   "кредитной организацией",
            "ам":   "кредитным организациям",
            "ами":  "кредитными организациями",
            "ах":   "кредитных организациях",
        },
    ),
    # исполлист
    (
        re.compile(r"\bисполлист(а|у|ом|е|ы|ов|ам|ами|ах)?\b", re.IGNORECASE | re.UNICODE),
        {
            "":     "исполнительный лист",
            "а":    "исполнительного листа",
            "у":    "исполнительному листу",
            "ом":   "исполнительным листом",
            "е":    "исполнительном листе",
            "ы":    "исполнительные листы",
            "ов":   "исполнительных листов",
            "ам":   "исполнительным листам",
            "ами":  "исполнительными листами",
            "ах":   "исполнительных листах",
        },
    ),
]

PUNCT_RX = re.compile(r"\.([А-ЯЁ])", re.UNICODE)

# Длинные тире (—, –) → короткое тире с пробелами или двоеточие.
# По правилам пользователя длинные тире запрещены полностью.
# Заменяем на короткое тире с пробелами вокруг (наиболее безопасный вариант).
EM_DASH_RX = re.compile(r"\s*[—–]\s*")

# Английские кавычки "..." вокруг русского текста → «ёлочки».
# Простое регэкс-решение: пара прямых кавычек, между которыми буквы/цифры.
# Не задеваем атрибуты HTML (=", ="), они пока защищены _protect через head/script.
ENG_QUOTES_RX = re.compile(r'(?<![=:\(\[\{])"([^"\n<>]{1,200}?)"', re.UNICODE)


# === Защищаемые блоки HTML (не правим) ===
# Вырезаем перед автозаменой, потом возвращаем на место.
PROTECT_PATTERNS = [
    re.compile(r"<head\b[^>]*>.*?</head>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<style\b[^>]*>.*?</style>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<pre\b[^>]*>.*?</pre>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<code\b[^>]*>.*?</code>", re.DOTALL | re.IGNORECASE),
    re.compile(r"<blockquote\b[^>]*>.*?</blockquote>", re.DOTALL | re.IGNORECASE),
    # FAQ-вопросы — h3 со знаком вопроса
    re.compile(r"<h3\b[^>]*>[^<]*\?\s*</h3>", re.IGNORECASE),
    # <title> на всякий
    re.compile(r"<title\b[^>]*>.*?</title>", re.DOTALL | re.IGNORECASE),
]


@dataclass
class FixReport:
    file: str
    abbreviation_fixes: dict[str, int] = field(default_factory=dict)
    punctuation_fixes: int = 0
    em_dash_fixes: int = 0
    eng_quotes_fixes: int = 0
    changed: bool = False
    bytes_before: int = 0
    bytes_after: int = 0


def _protect(text: str) -> tuple[str, list[str]]:
    """Заменяет защищённые блоки на плейсхолдеры. Возвращает (изменённый текст, список оригиналов)."""
    saved: list[str] = []

    def repl(m: re.Match) -> str:
        saved.append(m.group(0))
        return f"\0PROTECT_{len(saved) - 1}\0"

    for pat in PROTECT_PATTERNS:
        text = pat.sub(repl, text)
    return text, saved


def _restore(text: str, saved: list[str]) -> str:
    for i, original in enumerate(saved):
        text = text.replace(f"\0PROTECT_{i}\0", original)
    return text


def _apply_replacement(rx: re.Pattern, mapping: dict[str, str], text: str) -> tuple[str, int]:
    count = 0

    def replace(m: re.Match) -> str:
        nonlocal count
        original = m.group(0)
        suffix = (m.group(1) or "").lower() if m.lastindex else ""
        replacement = mapping.get(suffix, mapping.get("", original))
        count += 1
        # Сохраняем регистр первой буквы (если оригинал начинался с заглавной)
        if original and original[0].isupper():
            replacement = replacement[0].upper() + replacement[1:]
        return replacement

    new_text = rx.sub(replace, text)
    return new_text, count


def fix_text(text: str, is_html: bool) -> tuple[str, FixReport]:
    rep = FixReport(file="", bytes_before=len(text.encode("utf-8")))

    if is_html:
        text, saved = _protect(text)
    else:
        saved = []

    # Сокращения
    for rx, mapping in REPLACEMENTS:
        new_text, n = _apply_replacement(rx, mapping, text)
        if n:
            rep.abbreviation_fixes[rx.pattern] = n
            text = new_text

    # Пунктуация
    text, n_punct = PUNCT_RX.subn(r". \1", text)
    rep.punctuation_fixes = n_punct

    # Длинные тире → короткое тире с пробелами
    text, n_em = EM_DASH_RX.subn(" - ", text)
    rep.em_dash_fixes = n_em

    # Английские кавычки → «ёлочки»
    text, n_quotes = ENG_QUOTES_RX.subn(r"«\1»", text)
    rep.eng_quotes_fixes = n_quotes

    if is_html:
        text = _restore(text, saved)

    rep.bytes_after = len(text.encode("utf-8"))
    rep.changed = (
        bool(rep.abbreviation_fixes)
        or rep.punctuation_fixes > 0
        or rep.em_dash_fixes > 0
        or rep.eng_quotes_fixes > 0
    )
    return text, rep


def process_file(path: Path, dry_run: bool = False) -> FixReport:
    raw = path.read_text(encoding="utf-8")
    is_html = path.suffix.lower() in {".html", ".htm"}
    new_text, rep = fix_text(raw, is_html)
    rep.file = str(path)

    if rep.changed and not dry_run:
        path.write_text(new_text, encoding="utf-8")

    return rep


def main() -> int:
    parser = argparse.ArgumentParser(description="Автофикс сокращений и пунктуации в статье")
    parser.add_argument("path", help="Путь к .html или .md")
    parser.add_argument("--dry-run", action="store_true", help="Не записывать, только показать диффы")
    parser.add_argument("--json", action="store_true", help="Вывод отчёта в JSON")
    args = parser.parse_args()

    path = Path(args.path).resolve()
    if not path.exists() or not path.is_file():
        print(f"Файл не найден: {path}", file=sys.stderr)
        return 1

    rep = process_file(path, dry_run=args.dry_run)

    if args.json:
        print(json.dumps(asdict(rep), ensure_ascii=False, indent=2))
    else:
        print(f"Файл: {rep.file}")
        print(f"Изменено: {rep.changed} (dry_run={args.dry_run})")
        if rep.abbreviation_fixes:
            print("Сокращения исправлены:")
            for pat, n in rep.abbreviation_fixes.items():
                print(f"  {pat}: {n}")
        if rep.punctuation_fixes:
            print(f"Пунктуация (точка без пробела): {rep.punctuation_fixes}")
        if rep.em_dash_fixes:
            print(f"Длинные тире (— → -): {rep.em_dash_fixes}")
        if rep.eng_quotes_fixes:
            print(f"Английские кавычки (\"...\" → «…»): {rep.eng_quotes_fixes}")
        if not rep.changed:
            print("Изменений нет.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
