"""
Детерминированная валидация outline.json (выход агента 3-architect).

Зачем (6 июня 2026): раньше архитектор сам, в промпте, считал сумму длин блоков,
лемматизировал H2 на повтор корней и прикидывал «похожесть на прошлую статью».
LLM делает арифметику/лемматизацию/сравнение плохо и в режиме «проверь → если
плохо → переделай» зацикливается на outline без точки останова — это была
главная причина зависаний слота и недоборов 9/10 (само-признание в
3-architect.md: «три подряд hang 12 мая от переусложнения outline»).

Теперь архитектор только ГЕНЕРИРУЕТ, а эти три проверки делает детерминированный
скрипт ПОСЛЕ него:
  1. Сумма длин блоков + авто-подрезка (--fix) при превышении потолка.
  2. Повтор корней существительных в 2+ H2 (отчёт — переименование делает архитектор).
  3. Похожесть структуры на предыдущую статью той же категории (само-поиск соседа).

Запуск:
    python -m tools.outline_validate drafts/{slug}/outline.json
    python -m tools.outline_validate drafts/{slug}/outline.json --fix   # авто-подрезка длин
    python -m tools.outline_validate drafts/{slug}/outline.json --json

Возврат:
    0 — всё ок (или починено --fix), действий модели не нужно.
    1 — остались fixes_needed (повтор корней H2 / дубль соседа / длина без --fix):
        архитектору нужен ОДИН точечный проход правок по списку.
    2 — структурная ошибка (файл не найден / битый JSON).

Принцип: скрипт НИКОГДА не падает на содержимом outline — любые кривые/пустые
поля деградируют в пустой отчёт, а не в исключение (иначе уронит слот).
"""
from __future__ import annotations

import argparse
import json
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
DRAFTS_DIR = PROJECT_ROOT / "drafts"

# Переиспользуем лемматизатор и стоп-слова из quality_checks — единый источник.
try:
    from tools.quality_checks import simple_lemma, STOPWORDS  # type: ignore
except Exception:  # pragma: no cover — фоллбэк, чтобы скрипт не падал на импорте
    STOPWORDS = set()

    def simple_lemma(word: str) -> str:  # type: ignore
        return word.lower()

# Лимиты суммы длин блоков outline (знаки). Совпадают с целями 3-architect.md.
# default-черновик потом живёт в 6500-7500 (hard 8000), news — 4500-6500 (hard 6800);
# outline-сумму держим чуть ниже, чтобы писатель раскрыл связки без превышения.
LENGTH_LIMITS = {
    "default": {"min": 6000, "max": 8000, "target": 7000},
    "news": {"min": 4500, "max": 5500, "target": 5000},
}

WORD_RX = re.compile(r"[А-Яа-яЁёA-Za-z]{4,}")

# Generic-леммы, повтор которых в H2 не считаем проблемой (служебные/частотные).
H2_REPEAT_IGNORE = {
    simple_lemma(w) for w in (
        "когда", "какой", "какие", "сколько", "можно", "нужно", "если",
        "через", "после", "перед", "почему", "чтобы", "также", "этот",
    )
}


def _category_of(outline: dict, outline_path: Path) -> str:
    """Категория из outline.json, иначе из соседних brief.json / meta.json."""
    cat = (outline.get("category") or "").strip().lower()
    if cat:
        return cat
    for sibling in ("brief.json", "meta.json"):
        try:
            data = json.loads((outline_path.parent / sibling).read_text(encoding="utf-8"))
            c = (data.get("category") or "").strip().lower()
            if c:
                return c
        except Exception:
            continue
    return ""


def _kind(category: str) -> str:
    return "news" if category == "news" else "default"


def _h2_list(outline: dict) -> list[str]:
    out = []
    for b in outline.get("blocks", []) or []:
        h2 = b.get("h2")
        if isinstance(h2, str) and h2.strip():
            out.append(h2.strip())
    return out


def _content_lemmas(text: str) -> set[str]:
    return {
        simple_lemma(w) for w in WORD_RX.findall(text)
        if w.lower() not in STOPWORDS
    }


def _keyword_lemmas(outline: dict) -> set[str]:
    kb = outline.get("keyword_budget") or {}
    mk = kb.get("main_keyword") or outline.get("slug", "") or ""
    return _content_lemmas(mk.replace("-", " "))


def check_length(outline: dict, kind: str, apply_fix: bool) -> dict:
    blocks = outline.get("blocks", []) or []
    lengths = []
    for b in blocks:
        v = b.get("length")
        lengths.append(int(v) if isinstance(v, (int, float)) else 0)
    total = sum(lengths)
    lim = LENGTH_LIMITS[kind]
    res = {
        "sum_blocks": total, "min": lim["min"], "max": lim["max"],
        "target": lim["target"], "status": "ok", "auto_trimmed": False,
    }

    if total > lim["max"]:
        if apply_fix and total > 0:
            factor = lim["target"] / total
            new_total = 0
            for b, ln in zip(blocks, lengths):
                nl = max(50, round(ln * factor))
                b["length"] = nl
                new_total += nl
            res.update(status="trimmed", auto_trimmed=True,
                       sum_blocks=new_total, sum_before=total)
        else:
            res["status"] = "too_long"
    elif total < lim["min"]:
        res["status"] = "too_short"
    return res


def check_h2_repeats(outline: dict) -> list[dict]:
    """Корень существительного в 2+ H2 (кроме лемм главного ключа и generic-слов)."""
    h2s = _h2_list(outline)
    kw = _keyword_lemmas(outline)
    lemma_to_h2: dict[str, list[int]] = {}
    for i, h2 in enumerate(h2s):
        seen_in_this = set()
        for w in WORD_RX.findall(h2):
            if w.lower() in STOPWORDS:
                continue
            lem = simple_lemma(w)
            if lem in kw or lem in H2_REPEAT_IGNORE or len(lem) < 4:
                continue
            if lem in seen_in_this:
                continue
            seen_in_this.add(lem)
            lemma_to_h2.setdefault(lem, []).append(i)
    conflicts = []
    for lem, idxs in lemma_to_h2.items():
        if len(idxs) >= 2:
            conflicts.append({"root": lem, "h2": [h2s[i] for i in idxs]})
    return conflicts


def _find_prev_outline(current_path: Path, kind_category: str,
                       current_slug: str) -> Path | None:
    """Самый свежий по mtime outline.json той же категории, кроме текущего."""
    if not DRAFTS_DIR.exists():
        return None
    best = None
    best_mtime = -1.0
    for p in DRAFTS_DIR.glob("*/outline.json"):
        try:
            if p.resolve() == current_path.resolve():
                continue
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if _category_of(data, p) != kind_category:
            continue
        if data.get("slug") == current_slug:
            continue
        m = p.stat().st_mtime
        if m > best_mtime:
            best_mtime = m
            best = p
    return best


def check_prev_similarity(outline: dict, current_path: Path, category: str) -> dict:
    slug = outline.get("slug", "")
    res = {"compared_with": None, "jaccard": 0.0, "exact_h2_matches": 0,
           "order_same": False, "conflict": False}
    if not category:
        return res
    prev_path = _find_prev_outline(current_path, category, slug)
    if not prev_path:
        return res
    try:
        prev = json.loads(prev_path.read_text(encoding="utf-8"))
    except Exception:
        return res

    cur_h2 = _h2_list(outline)
    prev_h2 = _h2_list(prev)
    res["compared_with"] = prev.get("slug") or prev_path.parent.name
    if not cur_h2 or not prev_h2:
        return res

    cur_lem = set()
    for h in cur_h2:
        cur_lem |= _content_lemmas(h)
    prev_lem = set()
    for h in prev_h2:
        prev_lem |= _content_lemmas(h)
    if cur_lem and prev_lem:
        inter = len(cur_lem & prev_lem)
        union = len(cur_lem | prev_lem)
        res["jaccard"] = round(inter / union, 3) if union else 0.0

    def _norm(s: str) -> str:
        return re.sub(r"\s+", " ", s.strip().lower())

    prev_norm = {_norm(h) for h in prev_h2}
    res["exact_h2_matches"] = sum(1 for h in cur_h2 if _norm(h) in prev_norm)
    res["order_same"] = [_norm(h) for h in cur_h2] == [_norm(h) for h in prev_h2]

    res["conflict"] = (
        res["jaccard"] >= 0.5
        or res["exact_h2_matches"] >= 2
        or res["order_same"]
    )
    return res


def validate(path: Path, apply_fix: bool) -> dict:
    outline = json.loads(path.read_text(encoding="utf-8"))
    category = _category_of(outline, path)
    kind = _kind(category)

    length = check_length(outline, kind, apply_fix)
    h2_repeats = check_h2_repeats(outline)
    prev = check_prev_similarity(outline, path, category)

    fixes_needed: list[str] = []
    if length["status"] == "too_long":
        fixes_needed.append(
            f"Сумма длин блоков {length['sum_blocks']} > потолка {length['max']}: "
            f"сократи блоки до ~{length['target']} (или запусти с --fix для авто-подрезки)."
        )
    for c in h2_repeats:
        fixes_needed.append(
            f"Корень «{c['root']}» повторяется в H2: "
            + " | ".join(f"«{h}»" for h in c["h2"])
            + " — переименуй один через действие/сценарий."
        )
    if prev["conflict"]:
        fixes_needed.append(
            f"Структура похожа на соседа «{prev['compared_with']}» "
            f"(jaccard={prev['jaccard']}, точных совпадений H2={prev['exact_h2_matches']}, "
            f"порядок совпадает={prev['order_same']}): переформулируй H2, поменяй порядок/угол."
        )

    # length too_short — мягкое предупреждение, не блокер (писатель добьёт).
    warnings = []
    if length["status"] == "too_short":
        warnings.append(
            f"Сумма длин {length['sum_blocks']} < {length['min']} — писатель добьёт, "
            f"но лучше добавить материал в блоки."
        )

    if apply_fix and length["auto_trimmed"]:
        # Обновим length_check в самом outline и перезапишем файл.
        outline.setdefault("length_check", {})
        outline["length_check"].update({
            "sum_blocks": length["sum_blocks"],
            "limit_min": length["min"],
            "limit_max": length["max"],
            "target": length["target"],
            "passed": True,
            "auto_trimmed": True,
        })
        outline["target_total_chars"] = length["sum_blocks"]
        path.write_text(
            json.dumps(outline, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    return {
        "slug": outline.get("slug", path.parent.name),
        "kind": kind,
        "length": length,
        "h2_repeats": h2_repeats,
        "prev": prev,
        "warnings": warnings,
        "fixes_needed": fixes_needed,
        "ok": not fixes_needed,
    }


def _print_text(rep: dict) -> None:
    print(f"[outline_validate] {rep['slug']} (kind={rep['kind']})")
    L = rep["length"]
    print(f"  Длина: сумма блоков {L['sum_blocks']} "
          f"(коридор {L['min']}-{L['max']}, цель {L['target']}) — {L['status']}"
          + (f", авто-подрезано с {L.get('sum_before')}" if L.get("auto_trimmed") else ""))
    if rep["h2_repeats"]:
        print(f"  Повторы корней в H2: {len(rep['h2_repeats'])}")
        for c in rep["h2_repeats"]:
            print(f"    «{c['root']}»: " + " | ".join(c["h2"]))
    else:
        print("  Повторы корней в H2: нет")
    p = rep["prev"]
    if p["compared_with"]:
        print(f"  Сосед: «{p['compared_with']}» jaccard={p['jaccard']}, "
              f"точных H2={p['exact_h2_matches']}, порядок={p['order_same']}, "
              f"конфликт={p['conflict']}")
    else:
        print("  Сосед: не найден (нет предыдущей статьи категории)")
    for w in rep["warnings"]:
        print(f"  [WARN] {w}")
    if rep["fixes_needed"]:
        print("  [FIXES NEEDED] — один точечный проход архитектора:")
        for f in rep["fixes_needed"]:
            print(f"    - {f}")
    else:
        print("  [OK] действий модели не нужно")


def main() -> int:
    ap = argparse.ArgumentParser(description="Валидация outline.json (агент 3)")
    ap.add_argument("path", help="Путь к outline.json")
    ap.add_argument("--fix", action="store_true",
                    help="Авто-подрезка длин блоков при превышении потолка")
    ap.add_argument("--json", action="store_true", help="Вывод в JSON")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.is_absolute():
        path = (PROJECT_ROOT / args.path).resolve()
    if not path.exists():
        print(f"Не найден outline: {path}", file=sys.stderr)
        return 2
    try:
        rep = validate(path, apply_fix=args.fix)
    except json.JSONDecodeError as e:
        print(f"Битый JSON в {path}: {e}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        _print_text(rep)
    return 0 if rep["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
