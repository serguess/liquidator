"""
Одноразовый фикс CTA-кнопок в уже опубликованных статьях.

Контекст (баг 9 мая 2026): кнопки CTA в опубликованных статьях были собраны
шаблоном `<a href="/index.html#contacts" class="article__cta--hero" data-source="...">`.
Якоря `#contacts` в index.html не существует (есть только `#contactsModal`,
который требует JS data-open-modal-обработчика). В итоге клик по кнопке открывал
просто главную, не открывал модалку.

При этом в каждой статье уже:
  - Лежит сама модалка (id=contactsModal, форма leadForm) — вставляется
    inject_boilerplate'ом через render_lead_modal().
  - Подключён script.js — обрабатывает data-open-modal.

Значит для починки кнопок достаточно:
  1. Поменять <a href="/index.html#contacts" ...> на <button type="button" ...>
  2. Добавить data-open-modal="contactsModal"
  3. Поменять позиции в data-source: top→1, mid→2, bottom→3 (нумерация сверху вниз
     = 1 primary над лидом, 2 inline в середине, 3 final перед FAQ).

Backend (main.py:_render_mail) парсит source формата `article-{slug}-{N}` и
формирует строку «Статья «{полное название}», кнопка №N из 3» из page_title.

Запуск:
    python -m tools.fix_cta_links                        # все articles/**/*.html
    python -m tools.fix_cta_links --path articles/fiz/   # конкретная подпапка
    python -m tools.fix_cta_links --dry-run              # показать что было бы

Возврат:
    0 — успех (даже если нечего было править)
    1 — структурная ошибка (нет articles/)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Соответствие словесных позиций номерам (нумерация сверху вниз).
POSITION_MAP = {"top": 1, "mid": 2, "bottom": 3}

# Старый формат CTA (broken): <a href="/index.html#contacts" class="article__cta--hero" data-source="article-{slug}-{top|mid|bottom}">
#   <span>{text} →</span>
# </a>
# Регулярка ловит весь блок целиком включая внутренний <span>...</span>.
# Слаг и позиция — capture-группы.
OLD_CTA_RX = re.compile(
    r'<a\s+href="/index\.html#contacts"\s+class="article__cta--hero"\s+'
    r'data-source="article-([^"\-]+(?:-[^"\-]+)*)-(top|mid|bottom)"\s*>\s*'
    r'(<span\b[^>]*>.*?</span>)\s*'
    r'</a>',
    re.DOTALL,
)


# Для статей которые уже имеют <button data-open-modal>, но в data-source
# использованы словесные позиции top/mid/bottom. Меняем их на 1/2/3 чтобы
# в письме выводилось «кнопка №N из 3».
POSITION_ONLY_RX = re.compile(
    r'(data-source="article-[^"\-]+(?:-[^"\-]+)*?)-(top|mid|bottom)(")',
)


def _replace_old_anchor(match: re.Match) -> str:
    slug = match.group(1)
    position_word = match.group(2)
    inner_span = match.group(3)
    position_num = POSITION_MAP[position_word]
    return (
        f'<button type="button" class="article__cta--hero" '
        f'data-open-modal="contactsModal" data-source="article-{slug}-{position_num}">\n'
        f'  {inner_span}\n'
        f'</button>'
    )


def _replace_position_only(match: re.Match) -> str:
    prefix = match.group(1)
    position_word = match.group(2)
    suffix = match.group(3)
    position_num = POSITION_MAP[position_word]
    return f"{prefix}-{position_num}{suffix}"


def fix_file(path: Path, dry_run: bool = False) -> dict:
    """
    Применяет два прохода:
      1. <a href="/index.html#contacts" ...> → <button data-open-modal>
      2. data-source="article-{slug}-(top|mid|bottom)" → -1|-2|-3
    Второй проход догоняет статьи где уже есть <button data-open-modal>, но
    позиции остались словесные.

    Возвращает dict: {file, replacements, replacements_anchor, replacements_position, changed}.
    """
    raw = path.read_text(encoding="utf-8")
    new_raw, n_anchor = OLD_CTA_RX.subn(_replace_old_anchor, raw)
    new_raw, n_position = POSITION_ONLY_RX.subn(_replace_position_only, new_raw)
    total = n_anchor + n_position
    if total > 0 and not dry_run:
        path.write_text(new_raw, encoding="utf-8")
    return {
        "file": str(path),
        "replacements": total,
        "replacements_anchor": n_anchor,
        "replacements_position": n_position,
        "changed": total > 0,
    }


def collect_files(root: Path) -> list[Path]:
    """Все .html в articles/ кроме служебных."""
    if not root.exists():
        return []
    return sorted(root.rglob("*.html"))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Одноразовый фикс CTA-кнопок в опубликованных статьях"
    )
    parser.add_argument("--path", default="articles",
                        help="Папка с HTML-файлами (default: articles)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Не писать изменения, только показать счётчики")
    args = parser.parse_args()

    target = (PROJECT_ROOT / args.path).resolve()
    if not target.exists():
        print(f"Не найдено: {target}", file=sys.stderr)
        return 1

    if target.is_file():
        files = [target]
    else:
        files = collect_files(target)

    if not files:
        print(f"В {target} не найдено .html файлов")
        return 0

    total_files = 0
    total_changes = 0
    changed_files = 0

    for f in files:
        result = fix_file(f, dry_run=args.dry_run)
        total_files += 1
        if result["changed"]:
            changed_files += 1
            total_changes += result["replacements"]
            tag = "[DRY]" if args.dry_run else "[OK]"
            detail = (
                f"anchor->button={result['replacements_anchor']}, "
                f"position(top/mid/bottom->1/2/3)={result['replacements_position']}"
            )
            print(f"{tag} {result['file']}: {result['replacements']} замен ({detail})")

    print(f"\nИтого: файлов проверено {total_files}, изменено {changed_files}, "
          f"всего замен {total_changes}{' (dry-run, ничего не записано)' if args.dry_run else ''}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
