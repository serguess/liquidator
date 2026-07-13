"""
Одноразовый патчер: заменяет footer-заглушки на реальные контакты
в уже сгенерированных статьях (articles/**, drafts/**).

Что меняет (приводит к эталону index.html / нового render_footer):
  - телефон tel:+78000000000 / «8 800 ХХХ-ХХ-ХХ» / «Бесплатно по России»
    -> +7(922)615-48-88 / «Звоните нам»
  - соцсети href="#": удаляет WhatsApp-заглушку, оживляет Telegram и Max.

Идемпотентен: повторный прогон уже исправленных файлов ничего не трогает.

Запуск:
    python tools/fix_footer_contacts.py --dry-run          # показать что изменится
    python tools/fix_footer_contacts.py                    # применить
    python tools/fix_footer_contacts.py path1 path2 ...    # свои корни (по умолчанию articles + drafts)
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent

TG_URL = "https://t.me/pravoshop"
MAX_URL = "https://max.ru/u/f9LHodD0cOJYbXu43FRYPB9YNd_5Cl3Y5mw7ECvU8ezOS-qFltfy7GCLDkw"

# Простые строковые замены (старое -> новое)
STR_REPLACEMENTS = [
    ('href="tel:+78000000000"', 'href="tel:+79226154888"'),
    ("<strong>8 800 ХХХ-ХХ-ХХ</strong>", "<strong>+7(922)615-48-88</strong>"),
    ("<span>Бесплатно по России</span>", "<span>Звоните нам</span>"),
    (
        '<a href="#" aria-label="Telegram" class="footer__social footer__social--tg">',
        f'<a href="{TG_URL}" target="_blank" rel="noopener" aria-label="Telegram" class="footer__social footer__social--tg">',
    ),
    (
        '<a href="#" aria-label="Max" class="footer__social footer__social--max">',
        f'<a href="{MAX_URL}" target="_blank" rel="noopener" aria-label="Max" class="footer__social footer__social--max">',
    ),
]

# Удаление WhatsApp-заглушки целиком (её нет в эталоне index.html)
WHATSAPP_RX = re.compile(
    r'\n\s*<a href="#" aria-label="WhatsApp"[^>]*>.*?</a>',
    re.DOTALL,
)


def patch_text(text: str) -> tuple[str, bool]:
    original = text
    for old, new in STR_REPLACEMENTS:
        text = text.replace(old, new)
    text = WHATSAPP_RX.sub("", text)
    return text, (text != original)


def main() -> int:
    parser = argparse.ArgumentParser(description="Фикс footer-контактов в статьях")
    parser.add_argument("roots", nargs="*", help="Корневые папки (по умолчанию articles + drafts)")
    parser.add_argument("--dry-run", action="store_true", help="Не писать, только показать")
    args = parser.parse_args()

    if args.roots:
        roots = [Path(r).resolve() for r in args.roots]
    else:
        roots = [PROJECT_ROOT / "articles", PROJECT_ROOT / "drafts"]

    files: list[Path] = []
    for root in roots:
        if root.is_file() and root.suffix == ".html":
            files.append(root)
        elif root.is_dir():
            files.extend(sorted(root.rglob("*.html")))

    changed = 0
    scanned = 0
    for f in files:
        scanned += 1
        try:
            text = f.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            print(f"SKIP (read error): {f} — {exc}", file=sys.stderr)
            continue
        new_text, did = patch_text(text)
        if did:
            changed += 1
            try:
                rel = f.relative_to(PROJECT_ROOT)
            except ValueError:
                rel = f
            if args.dry_run:
                print(f"WOULD FIX: {rel}")
            else:
                f.write_text(new_text, encoding="utf-8")
                print(f"FIXED: {rel}")

    print(f"\nScanned: {scanned} | {'would fix' if args.dry_run else 'fixed'}: {changed}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
