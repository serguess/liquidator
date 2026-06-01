"""
Отчёт по расходу токенов Claude API на статьи.

Источник данных: meta.json в drafts/ и drafts/_archive/ (поле tokens_by_model).
Поле появляется с 1 июня 2026 (commit с tracking'ом в runner.py).

Запуск:
  python -m tools.tokens_report                     # сводка всё
  python -m tools.tokens_report --since 2026-06-01  # за период
  python -m tools.tokens_report --by-day            # разбивка по дням
  python -m tools.tokens_report --by-category       # по категориям
  python -m tools.tokens_report --slug some-slug    # одна статья детально
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = PROJECT_ROOT / "drafts"
ARCHIVE_DIR = DRAFTS_DIR / "_archive"


def _collect_articles(since: str | None = None, until: str | None = None) -> list[dict]:
    """Возвращает список словарей с полями slug, category, ready_at, tokens_*"""
    articles = []
    for base in [DRAFTS_DIR, ARCHIVE_DIR]:
        if not base.exists():
            continue
        # Архив имеет под-папки по месяцам (2026-05, 2026-06)
        if base == ARCHIVE_DIR:
            search_dirs = [d for d in base.iterdir() if d.is_dir()]
        else:
            search_dirs = [base]

        for sd in search_dirs:
            for slug_dir in sd.iterdir():
                if not slug_dir.is_dir() or slug_dir.name.startswith("_"):
                    continue
                meta_path = slug_dir / "meta.json"
                if not meta_path.exists():
                    continue
                try:
                    m = json.loads(meta_path.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                tokens_by_model = m.get("tokens_by_model")
                tokens_total = m.get("tokens_total")
                if not tokens_by_model:
                    continue
                ready_at = m.get("ready_at", "")
                if since and ready_at < since:
                    continue
                if until and ready_at >= until:
                    continue
                articles.append({
                    "slug": m.get("slug", slug_dir.name),
                    "category": m.get("category", "?"),
                    "ready_at": ready_at,
                    "retries": m.get("quality_gate_retry_count", 0),
                    "tokens_by_model": tokens_by_model,
                    "tokens_total": tokens_total or {},
                })
    articles.sort(key=lambda x: x["ready_at"])
    return articles


def _fmt_tokens(n: int) -> str:
    """123456 → 123.5K"""
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _summary(articles: list[dict]) -> dict:
    """Агрегирует токены по всем статьям."""
    total = {
        "articles": len(articles),
        "input": 0,
        "output": 0,
        "cache_read": 0,
        "cache_creation": 0,
        "billable": 0,  # input + output + cache_creation (cache_read дешевле)
    }
    by_model = {}  # opus / sonnet / haiku
    for a in articles:
        t = a.get("tokens_total") or {}
        total["input"] += t.get("input", 0)
        total["output"] += t.get("output", 0)
        total["cache_read"] += t.get("cache_read", 0)
        total["cache_creation"] += t.get("cache_creation", 0)
        total["billable"] += t.get("total", 0)
        for model, tm in (a.get("tokens_by_model") or {}).items():
            b = by_model.setdefault(model, {
                "input": 0, "output": 0, "cache_read": 0, "cache_creation": 0, "messages": 0,
            })
            b["input"] += tm.get("input_tokens", 0)
            b["output"] += tm.get("output_tokens", 0)
            b["cache_read"] += tm.get("cache_read_input_tokens", 0)
            b["cache_creation"] += tm.get("cache_creation_input_tokens", 0)
            b["messages"] += tm.get("messages", 0)
    return {"total": total, "by_model": by_model}


def _print_overall(articles: list[dict]):
    s = _summary(articles)
    t = s["total"]
    print(f"=== Сводка по {t['articles']} статьям ===")
    print(f"  Input:           {_fmt_tokens(t['input']):>10}")
    print(f"  Output:          {_fmt_tokens(t['output']):>10}")
    print(f"  Cache read:      {_fmt_tokens(t['cache_read']):>10}")
    print(f"  Cache creation:  {_fmt_tokens(t['cache_creation']):>10}")
    print(f"  Биллинговые:     {_fmt_tokens(t['billable']):>10} (input+output+cache_creation)")
    if t["articles"]:
        avg = t["billable"] // t["articles"]
        print(f"  Среднее/статью:  {_fmt_tokens(avg):>10}")

    print(f"\n=== По моделям ===")
    print(f"{'Модель':<8} {'Input':>8} {'Output':>8} {'CacheRd':>8} {'CacheWr':>8} {'Msgs':>6}")
    for model in ["opus", "sonnet", "haiku"]:
        b = s["by_model"].get(model)
        if not b:
            continue
        print(f"{model:<8} {_fmt_tokens(b['input']):>8} {_fmt_tokens(b['output']):>8} "
              f"{_fmt_tokens(b['cache_read']):>8} {_fmt_tokens(b['cache_creation']):>8} "
              f"{b['messages']:>6}")


def _print_by_day(articles: list[dict]):
    from collections import defaultdict
    by_day = defaultdict(list)
    for a in articles:
        day = a["ready_at"][:10] if a["ready_at"] else "unknown"
        by_day[day].append(a)
    print(f"=== По дням ===")
    print(f"{'Day':<12} {'Articles':>8} {'Billable':>12} {'Avg':>10}")
    for day in sorted(by_day):
        arts = by_day[day]
        bill = sum(a.get("tokens_total", {}).get("total", 0) for a in arts)
        avg = bill // len(arts) if arts else 0
        print(f"{day:<12} {len(arts):>8} {_fmt_tokens(bill):>12} {_fmt_tokens(avg):>10}")


def _print_by_category(articles: list[dict]):
    from collections import defaultdict
    by_cat = defaultdict(list)
    for a in articles:
        by_cat[a["category"]].append(a)
    print(f"=== По категориям ===")
    print(f"{'Cat':<6} {'Articles':>8} {'Billable':>12} {'Avg':>10}")
    for cat in sorted(by_cat):
        arts = by_cat[cat]
        bill = sum(a.get("tokens_total", {}).get("total", 0) for a in arts)
        avg = bill // len(arts) if arts else 0
        print(f"{cat:<6} {len(arts):>8} {_fmt_tokens(bill):>12} {_fmt_tokens(avg):>10}")


def _print_slug_detail(articles: list[dict], slug: str):
    matches = [a for a in articles if a["slug"] == slug]
    if not matches:
        print(f"Статья '{slug}' не найдена или нет tokens_by_model в meta.json")
        return
    a = matches[0]
    print(f"=== {a['slug']} ===")
    print(f"  category: {a['category']}")
    print(f"  ready_at: {a['ready_at']}")
    print(f"  retries: {a['retries']}")
    t = a.get("tokens_total", {})
    print(f"\n--- Сумма ---")
    print(f"  Input:           {_fmt_tokens(t.get('input', 0)):>10}")
    print(f"  Output:          {_fmt_tokens(t.get('output', 0)):>10}")
    print(f"  Cache read:      {_fmt_tokens(t.get('cache_read', 0)):>10}")
    print(f"  Cache creation:  {_fmt_tokens(t.get('cache_creation', 0)):>10}")
    print(f"  Биллинговые:     {_fmt_tokens(t.get('total', 0)):>10}")
    print(f"\n--- По моделям ---")
    print(f"{'Модель':<8} {'Input':>8} {'Output':>8} {'CacheRd':>8} {'CacheWr':>8} {'Msgs':>6}")
    for model, b in (a.get("tokens_by_model") or {}).items():
        print(f"{model:<8} "
              f"{_fmt_tokens(b.get('input_tokens', 0)):>8} "
              f"{_fmt_tokens(b.get('output_tokens', 0)):>8} "
              f"{_fmt_tokens(b.get('cache_read_input_tokens', 0)):>8} "
              f"{_fmt_tokens(b.get('cache_creation_input_tokens', 0)):>8} "
              f"{b.get('messages', 0):>6}")


def main() -> int:
    p = argparse.ArgumentParser(description="Отчёт по расходу токенов Claude")
    p.add_argument("--since", help="Только статьи с ready_at >= YYYY-MM-DD")
    p.add_argument("--until", help="Только статьи с ready_at < YYYY-MM-DD")
    p.add_argument("--by-day", action="store_true", help="Разбивка по дням")
    p.add_argument("--by-category", action="store_true", help="Разбивка по категориям")
    p.add_argument("--slug", help="Детально по одной статье")
    args = p.parse_args()

    articles = _collect_articles(since=args.since, until=args.until)
    if not articles:
        print("Нет статей с полем tokens_by_model в meta.json. Tracking появляется с 1 июня 2026.")
        return 0

    if args.slug:
        _print_slug_detail(articles, args.slug)
    elif args.by_day:
        _print_by_day(articles)
    elif args.by_category:
        _print_by_category(articles)
    else:
        _print_overall(articles)

    return 0


if __name__ == "__main__":
    sys.exit(main())
