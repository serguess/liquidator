"""
Batch-прогон N статей на GPT по плану категорий (3 fiz / 3 yur / 3 vzysk / 1 news
по умолчанию). Только ГЕНЕРАЦИЯ (в бот заказчице НЕ отправляет). Для каждой темы
запускает migration/pipeline_run.py отдельным процессом (изоляция), затем копирует
в slug-{gpt} для preview по домену.

Темы берёт из drafts/_topic-map/{category}.json прод-логикой (не rejected, не used,
для news — с валидацией event_date/news_zone/primary_source), не повторяя в рамках прогона.

Запуск (из projects/bankrotstvo):
    python migration/batch_run.py
    python migration/batch_run.py --plan fiz:3,yur:3,vzysk:3,news:1
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
DRAFTS = ROOT / "drafts"
TOPIC_MAP = DRAFTS / "_topic-map"


def pick_topics(category: str, n: int, taken: set[str]) -> list[dict]:
    """Берёт до n свободных тем категории (повтор прод-логики _pick_topic, но пачкой)."""
    from articles_scheduler.runner import _collect_used_slugs, _is_news_topic_valid
    used = _collect_used_slugs() | taken
    path = TOPIC_MAP / f"{category}.json"
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    out: list[dict] = []
    for t in data.get("topics", []) or []:
        if len(out) >= n:
            break
        if t.get("status") == "rejected":
            continue
        slug = t.get("slug")
        if not slug or slug in used:
            continue
        if category == "news":
            ok, _ = _is_news_topic_valid(t)
            if not ok:
                continue
        out.append(t)
        used.add(slug)
        taken.add(slug)
    return out


def run_one(topic: dict, category: str) -> dict:
    slug = topic["slug"]
    title = topic.get("title") or topic.get("topic_action") or slug
    cmd = [sys.executable, "-u", "migration/pipeline_run.py",
           "--slug", slug, "--category", category, "--topic", title]
    if category == "news":
        if topic.get("primary_source"):
            cmd += ["--primary-source", topic["primary_source"]]
        if topic.get("event_date"):
            cmd += ["--event-date", topic["event_date"]]
        if topic.get("news_zone"):
            cmd += ["--news-zone", topic["news_zone"]]
    t0 = time.time()
    res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    dt = round(time.time() - t0, 1)
    out = res.stdout or ""
    # вытащить итоговую стоимость и passed из вывода pipeline_run
    cost = ""
    for line in out.splitlines():
        if "ИТОГО" in line and "$" in line:
            cost = line.split("$")[-1].strip()
    gate = "ok" if "quality_gate] exit=0" in out else "FAIL"
    # preview-копия (slug с суффиксом -gpt, чтобы не занять тему и работал _safe_slug)
    work = DRAFTS / f"_mig-{slug}"
    prev = DRAFTS / f"{slug}-gpt"
    copied = False
    if work.exists():
        import shutil
        if prev.exists():
            shutil.rmtree(prev)
        shutil.copytree(work, prev)
        copied = True
    return {"slug": slug, "category": category, "title": title, "rc": res.returncode,
            "gate": gate, "cost": cost, "sec": dt, "preview": copied,
            "tail": out.strip().splitlines()[-1] if out.strip() else (res.stderr or "")[-200:]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", default="fiz:3,yur:3,vzysk:3,news:1")
    args = ap.parse_args()
    plan = []
    for part in args.plan.split(","):
        cat, _, num = part.partition(":")
        plan.append((cat.strip(), int(num)))

    # 1) собрать все темы заранее
    taken: set[str] = set()
    queue: list[tuple[str, dict]] = []
    for cat, n in plan:
        topics = pick_topics(cat, n, taken)
        if len(topics) < n:
            print(f"[warn] {cat}: нашлось {len(topics)}/{n} свободных тем")
        for t in topics:
            queue.append((cat, t))
    print(f"=== BATCH: {len(queue)} тем ===")
    for cat, t in queue:
        print(f"  {cat:6} {t['slug']}")
    print()

    # 2) прогон по одной
    results = []
    for i, (cat, t) in enumerate(queue, 1):
        print(f"\n########## [{i}/{len(queue)}] {cat} / {t['slug']} ##########", flush=True)
        r = run_one(t, cat)
        results.append(r)
        print(f"   rc={r['rc']} gate={r['gate']} cost=${r['cost']} {r['sec']}с preview={r['preview']}")

    # 3) сводка
    print("\n" + "=" * 80)
    print("СВОДКА BATCH")
    print("=" * 80)
    tot = 0.0
    for r in results:
        try:
            tot += float(r["cost"])
        except (ValueError, TypeError):
            pass
        print(f"  [{r['gate']:4}] {r['category']:6} {r['slug']:55} ${r['cost']}  {r['sec']}с")
    print(f"\nИтого статей: {len(results)}, суммарная стоимость ~${tot:.3f}")
    print("Preview-ссылки (подставь токен из bot_state.json:preview_token):")
    for r in results:
        if r["preview"]:
            print(f"  https://pravo.shop/p/{r['slug']}-gpt?t=<TOKEN>")
    (ROOT / "migration" / "_batch_report.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
