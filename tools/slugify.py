"""
Детерминированная транслитерация русского title в slug-формат (kebab-case).

Зачем: раньше slug генерировал агент 1 (LLM), и Claude в /write-article
часто игнорировал slug из brief'а — генерил свой, не использовал переданный
из topic-map. Это вело к дубликатам тем (статьи с разными slug на одну тему)
и бесконечной петле выбора одной и той же темы из topic-map.

Теперь slug — это чистая функция от title и категории. Никакой LLM.

Использование:
    from tools.slugify import slugify
    slugify("Внесудебное банкротство через МФЦ в 2026 году: условия")
    # → "vnesudebnoe-bankrotstvo-cherez-mfc-v-2026-godu-usloviya"

CLI:
    python -m tools.slugify "title here"
    python -m tools.slugify --check {slug} {dir}
        проверяет уникальность slug относительно drafts/{dir}/ и
        published_index.json. Exit 0 если уникален, 1 если занят.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# ГОСТ-подобная транслитерация (упрощённая, без диакритики).
# Прописные → строчные, чтобы итог был всегда в lower().
_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "j", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "c", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

# Стоп-слова: убираем для краткости slug. Список консервативный:
# только бессодержательные служебные слова. Существительные/глаголы
# не трогаем.
_STOPWORDS = {
    "и", "в", "во", "на", "с", "со", "к", "ко", "от", "до", "из", "за",
    "о", "об", "обо", "по", "при", "у", "над", "под", "перед", "пред",
    "для", "то", "что", "как", "так", "же", "ли", "не", "ни", "бы",
    "или", "но", "а", "да",
}


def slugify(text: str, max_len: int = 50) -> str:
    """
    Превращает русский title в slug.

    Пример:
        "Как списать долги: банкротство в 2026 году"
        → "spisat-dolgi-bankrotstvo-2026-godu"

    Гарантии:
    - Только латиница, цифры, дефисы.
    - Без подряд идущих дефисов.
    - Не начинается и не заканчивается на дефис.
    - Длина ≤ max_len (если выходит за лимит, режем по последнему дефису
      перед лимитом — слова не обрезаем).
    - Один и тот же text всегда даёт один и тот же slug (детерминирован).

    Дефолт max_len=50 (понижен с 60 17 мая 2026): TG callback_data имеет
    жёсткий лимит 64 байта, а кнопки бота используют формат
    `publish:{slug}` (8 байт префикс). Slug > 56 → BUTTON_DATA_INVALID и
    статья не доставляется. Запас 14 байт = безопасно для любого префикса
    (`publish:`, `reject:`, `edit:`).
    """
    if not text:
        return ""
    s = text.lower().strip()

    # Шаг 1: разбить на токены ДО транслитерации (чтобы корректно фильтровать
    # стоп-слова на русском). Любой не-буквенно-цифровой символ — разделитель.
    raw_tokens = re.split(r"[^а-яёa-z0-9]+", s)

    # Шаг 2: убрать стоп-слова и пустые токены
    tokens = [t for t in raw_tokens if t and t not in _STOPWORDS]

    # Шаг 3: транслитерировать каждый токен
    translit_tokens: list[str] = []
    for tok in tokens:
        out_chars: list[str] = []
        for ch in tok:
            if ch in _TRANSLIT:
                out_chars.append(_TRANSLIT[ch])
            elif ch.isascii() and ch.isalnum():
                out_chars.append(ch)
            # иначе пропускаем (не должно случаться, мы уже сплитили выше)
        translated = "".join(out_chars)
        if translated:
            translit_tokens.append(translated)

    s = "-".join(translit_tokens)

    # Схлопнуть дефисы и убрать с краёв
    s = re.sub(r"-+", "-", s).strip("-")

    # Длина: режем по последнему дефису перед лимитом
    if len(s) > max_len:
        cut = s[:max_len].rsplit("-", 1)
        s = cut[0] if cut[0] else s[:max_len]
        s = s.rstrip("-")

    return s


def _collect_used_slugs() -> set[str]:
    """
    Собирает все «занятые» slug-и в проекте: всё что в drafts/{slug}/
    (кроме служебных _topic-map, _archive, _review) + всё в
    data/published_index.json.
    """
    used: set[str] = set()
    drafts_dir = ROOT / "drafts"
    if drafts_dir.exists():
        for d in drafts_dir.iterdir():
            if d.is_dir() and not d.name.startswith("_"):
                used.add(d.name)

    pi_path = ROOT / "data" / "published_index.json"
    if pi_path.exists():
        try:
            pi = json.loads(pi_path.read_text(encoding="utf-8"))
            for entry in pi.get("articles", []) or []:
                slug = entry.get("slug")
                if slug:
                    used.add(slug)
        except (json.JSONDecodeError, OSError):
            pass

    return used


def is_slug_unique(slug: str) -> bool:
    """True если slug свободен (не занят черновиком и не опубликован)."""
    return slug not in _collect_used_slugs()


def slugify_unique(text: str, max_len: int = 50) -> str:
    """
    Генерит slug и гарантирует уникальность через суффикс -2, -3, ...
    Если базовый slug свободен — возвращает его.
    """
    base = slugify(text, max_len=max_len)
    if not base:
        return ""
    if is_slug_unique(base):
        return base
    # Накручиваем суффикс пока не найдём свободный
    for n in range(2, 100):
        candidate_max = max_len - len(f"-{n}")
        candidate = slugify(text, max_len=candidate_max) + f"-{n}"
        if is_slug_unique(candidate):
            return candidate
    raise RuntimeError(f"Не смог найти уникальный slug для {text!r} за 100 попыток")


def _cli() -> int:
    args = sys.argv[1:]
    if not args:
        print("Usage: python -m tools.slugify <title> [--unique]", file=sys.stderr)
        print("       python -m tools.slugify --check <slug>", file=sys.stderr)
        return 2

    if args[0] == "--check":
        if len(args) < 2:
            print("Usage: python -m tools.slugify --check <slug>", file=sys.stderr)
            return 2
        slug = args[1]
        if is_slug_unique(slug):
            print(f"OK: slug '{slug}' free")
            return 0
        print(f"BUSY: slug '{slug}' already used in drafts/ or published_index")
        return 1

    title = " ".join(args[:-1] if args[-1] == "--unique" else args)
    unique = "--unique" in args
    s = slugify_unique(title) if unique else slugify(title)
    print(s)
    return 0


if __name__ == "__main__":
    sys.exit(_cli())
