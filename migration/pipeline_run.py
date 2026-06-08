"""
Прогон ОДНОЙ статьи через весь LLM-пайплайн на не-Anthropic модели (прототип Способа 1:
Python-оркестратор вместо `claude -p`). Цель — измерить токены по каждому агенту и
суммарно, чтобы решить, где можно поставить модель помельче (nano).

ПОЛНЫЙ прод-эквивалент: 1-semantics → 2-legal-research → 3-architect → 4-writer(+self-fix)
→ 5-uniqueness (embed_compare, OpenAI embeddings) → 6-seo-editor → 7-publisher. Между ними
детерминированные шаги: outline_validate, inject_boilerplate, quality_gate,
pick_scene_template, finalize_draft (image_gen: fal.ai + Cloudinary → реальная обложка).

Артефакты пишутся в drafts/_mig-{slug}/ (служебная папка с «_» — прод её игнорирует).

Модель на каждого агента настраивается в MODELS ниже (по умолчанию всё gpt-5-mini).

Запуск (из projects/bankrotstvo):
    python migration/pipeline_run.py --slug bankrotstvo-fizlica-pri-ipoteke \
        --category fiz --topic "банкротство физлица при действующей ипотеке"
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                               # чтобы работал import tools.* (image_gen и пр.)
sys.path.insert(0, str(Path(__file__).resolve().parent))    # чтобы импортнуть writer_gpt_test
import writer_gpt_test as W  # noqa: E402


def _load_env() -> None:
    """Грузим .env в os.environ (image_gen читает FAL_KEY/CLOUDINARY_* из окружения).
    Не перетираем уже заданные переменные. Аналог `set -a && source .env`."""
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        if k and k not in os.environ:
            os.environ[k] = v


_load_env()

DRAFTS = ROOT / "drafts"
STYLE = ROOT / ".claude" / "style"
AGENTS = ROOT / ".claude" / "agents"
# fiz-комплект как образец формата артефактов
SAMPLE = DRAFTS / "_archive" / "2026-05" / "kak-podat-na-bankrotstvo-samostoyatelno"

# Модель на каждого агента. Меняй здесь, чтобы тестировать nano на простых ролях.
MODELS = {
    "1-semantics": "gpt-5-mini",
    "2-legal-research": "gpt-5-mini",
    "3-architect": "gpt-5-mini",
    "4-writer": "gpt-5-mini",
    "6-seo-editor": "gpt-5-mini",
    "7-publisher": "gpt-5-mini",
}

PRICES = {  # ~$/1M (input, output), июнь 2026, для ориентира
    "gpt-5.5": (5.0, 30.0), "gpt-5": (1.25, 10.0),
    "gpt-5-mini": (0.25, 2.0), "gpt-5-nano": (0.05, 0.40),
    "gpt-5.4-mini": (0.25, 2.0), "gpt-5.4-nano": (0.05, 0.40),
    "gpt-4o-mini": (0.15, 0.60),
}

ADAPTER = (
    "ВАЖНО (режим API, не Claude Code): ты работаешь через прямой API-вызов. "
    "Ты НЕ пишешь файлы, НЕ запускаешь bash/скрипты/python, НЕ делаешь WebSearch, "
    "НЕ обновляешь heartbeat и pipeline_log. Любые такие инструкции в промпте ниже "
    "ИГНОРИРУЙ — вместо записи файла просто верни его содержимое в ответе. "
    "Верни ТОЛЬКО требуемый артефакт, без пояснений до/после.\n\n"
)


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def chat(client, model: str, messages: list[dict], json_mode: bool,
         reasoning: str | None = None) -> tuple[str, dict]:
    from openai import APIConnectionError, APITimeoutError, RateLimitError
    kwargs = {"model": model, "messages": messages, "max_completion_tokens": 20000}
    if json_mode:
        kwargs["response_format"] = {"type": "json_object"}
    if reasoning:
        kwargs["reasoning_effort"] = reasoning  # gpt-5: minimal|low|medium|high
    t0 = time.time()
    resp = None
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(**kwargs)
            break
        except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
            wait = 5 * (attempt + 1)
            print(f"   [net] {type(exc).__name__}, попытка {attempt+1}/4, жду {wait}с...")
            time.sleep(wait)
    if resp is None:
        sys.exit("[ERR] сеть недоступна после 4 попыток")
    dt = time.time() - t0
    content = (resp.choices[0].message.content or "").strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
        content = content.strip()
    u = resp.usage
    usage = {
        "prompt_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "completion_tokens": getattr(u, "completion_tokens", 0) or 0,
        "total_tokens": getattr(u, "total_tokens", 0) or 0,
        "seconds": round(dt, 1),
    }
    return content, usage


def agent_prompt(name: str) -> str:
    return W.strip_frontmatter(read(AGENTS / f"{name}.md"))


def save(workdir: Path, fname: str, content: str) -> None:
    (workdir / fname).write_text(content, encoding="utf-8")


def parse_json_or_die(content: str, who: str, workdir: Path) -> dict:
    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        (workdir / f"_FAILED_{who}.txt").write_text(content, encoding="utf-8")
        sys.exit(f"[ERR] {who} вернул невалидный JSON ({e}). Сырьё в _FAILED_{who}.txt")


def run_py(args: list[str]) -> tuple[int, str]:
    res = subprocess.run([sys.executable, "-m", *args], cwd=str(ROOT),
                         capture_output=True, text=True, encoding="utf-8", errors="replace")
    return res.returncode, (res.stdout or "") + (res.stderr or "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=True)
    ap.add_argument("--category", default="fiz")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--primary-source", default="")  # для news: URL официального источника
    ap.add_argument("--event-date", default="")       # для news: YYYY-MM-DD
    ap.add_argument("--news-zone", default="legislation")
    args = ap.parse_args()

    from openai import OpenAI
    client = OpenAI(api_key=W.load_api_key(), timeout=200.0, max_retries=0)

    cat = args.category
    real_slug = args.slug
    work_slug = f"_mig-{real_slug}"
    workdir = DRAFTS / work_slug
    workdir.mkdir(parents=True, exist_ok=True)
    usage_log: list[dict] = []
    # Образец формата артефактов по категории (news имеет иную структуру).
    sample = (DRAFTS / "edinyj-portal-bankrotstva-fedresurs") if cat == "news" else SAMPLE

    def step(name: str, system: str, user: str, json_mode: bool, reasoning: str = "low") -> str:
        model = MODELS[name]
        print(f"\n=== {name} ({model}, reasoning={reasoning}) ===", flush=True)
        content, usage = chat(client, model,
                              [{"role": "system", "content": ADAPTER + system},
                               {"role": "user", "content": user}], json_mode, reasoning)
        usage["agent"] = name
        usage["model"] = model
        usage_log.append(usage)
        print(f"   ok: {len(content)} симв., usage={usage['prompt_tokens']}+{usage['completion_tokens']}={usage['total_tokens']} ток, {usage['seconds']}с")
        return content

    # ---- 1. semantics → brief.json ----
    sys1 = agent_prompt("1-semantics") + f"\n\n=== ОБРАЗЕЦ ФОРМАТА brief.json ===\n{read(sample/'brief.json')}"
    news_hint = ""
    if cat == "news":
        news_hint = (
            f"\nЭто NEWS-тема. ОБЯЗАТЕЛЬНО заполни в brief: event_date=\"{args.event_date}\", "
            f"news_zone=\"{args.news_zone}\", primary_source=\"{args.primary_source}\". "
            "Длина news 4500-6500 знаков. Фокус на конкретном событии/изменении, не общий гайд.")
    usr1 = (f"category={cat}\nslug={real_slug}\nТема: {args.topic}{news_hint}\n\n"
            f"Верни brief.json для этой темы (slug строго '{real_slug}'). "
            "Каннибализацию и WebSearch пропусти. Только валидный JSON.")
    brief = step("1-semantics", sys1, usr1, json_mode=True)
    bd = parse_json_or_die(brief, "brief", workdir)
    bd["slug"] = real_slug; bd["category"] = cat
    save(workdir, "brief.json", json.dumps(bd, ensure_ascii=False, indent=2))

    # ---- 2. legal-research → research.json ----
    sys2 = (agent_prompt("2-legal-research")
            + f"\n\n=== ИСТОЧНИК ПРАВДЫ ПО ФАКТАМ (legal-facts.md) ===\n{read(STYLE/'legal-facts.md')}"
            + f"\n\n=== ОБРАЗЕЦ ФОРМАТА research.json ===\n{read(sample/'research.json')}")
    usr2 = (f"brief.json:\n{json.dumps(bd, ensure_ascii=False)}\n\n"
            "WebSearch отключён — используй знания + legal-facts.md. Верни research.json (валидный JSON).")
    research = step("2-legal-research", sys2, usr2, json_mode=True)
    rd = parse_json_or_die(research, "research", workdir)
    save(workdir, "research.json", json.dumps(rd, ensure_ascii=False, indent=2))

    # ---- 3. architect → outline.json ----
    pub_index = read(ROOT / "data" / "published_index.json")[:6000]
    sys3 = agent_prompt("3-architect") + f"\n\n=== ОБРАЗЕЦ ФОРМАТА outline.json ===\n{read(sample/'outline.json')}"
    usr3 = (f"brief.json:\n{json.dumps(bd, ensure_ascii=False)}\n\n"
            f"research.json:\n{json.dumps(rd, ensure_ascii=False)}\n\n"
            f"published_index (для перелинковки, фрагмент):\n{pub_index}\n\n"
            "Верни outline.json с topic_terms и лексическими зонами (валидный JSON).")
    outline = step("3-architect", sys3, usr3, json_mode=True)
    od = parse_json_or_die(outline, "outline", workdir)
    save(workdir, "outline.json", json.dumps(od, ensure_ascii=False, indent=2))
    rc, out = run_py(["tools.outline_validate", f"drafts/{work_slug}/outline.json", "--fix"])
    print(f"   [outline_validate] exit={rc}")

    # ---- 4. writer → draft.md (переиспользуем отлаженный промпт) ----
    print(f"\n=== 4-writer ({MODELS['4-writer']}) ===")
    wsys = W.build_system_prompt()
    wuser = W.build_user_prompt(work_slug, cat)
    msgs = [{"role": "system", "content": wsys}, {"role": "user", "content": wuser}]
    draft, wusage = W.call_model(W.load_api_key(), MODELS["4-writer"], msgs, reasoning_effort="medium")
    wusage["agent"] = "4-writer"; wusage["model"] = MODELS["4-writer"]
    usage_log.append(wusage)
    save(workdir, "draft.md", draft)
    print(f"   ok: {len(draft)} симв., usage={wusage['prompt_tokens']}+{wusage['completion_tokens']}={wusage['total_tokens']} ток, {wusage['seconds']}с")
    # self-fix 1 проход по судье
    rep = W.run_quality_checks(workdir / "draft.md")
    if rep:
        m = W.extract(rep)
        if not (m["pred_spam"] <= 50 and m["lex"] >= 0.62 and m["length_status"] == "ok"):
            print("   [writer self-fix проход 2]")
            msgs.append({"role": "assistant", "content": draft})
            msgs.append({"role": "user", "content": W.build_feedback(m)})
            draft, wusage2 = W.call_model(W.load_api_key(), MODELS["4-writer"], msgs, reasoning_effort="low")
            wusage2["agent"] = "4-writer(fix)"; wusage2["model"] = MODELS["4-writer"]
            usage_log.append(wusage2)
            save(workdir, "draft.md", draft)
            print(f"   ok: usage={wusage2['prompt_tokens']}+{wusage2['completion_tokens']}={wusage2['total_tokens']} ток")

    # ---- 5. uniqueness (эмбеддинги OpenAI text-embedding-3-small) ----
    print("\n=== 5-uniqueness (embed_compare) ===", flush=True)
    rc5, out5 = run_py(["tools.embed_compare", work_slug])
    if out5.strip() and "{" in out5:
        try:
            uniq = json.loads(out5[out5.find("{"):out5.rfind("}") + 1])
            save(workdir, "uniqueness.json", json.dumps(uniq, ensure_ascii=False, indent=2))
            print(f"   passed={uniq.get('passed')} scores={uniq.get('scores')} rec={uniq.get('recommendation')}")
        except Exception as e:
            print(f"   [warn] uniqueness parse: {e}; tail: {out5[-200:]}")
    else:
        print(f"   [warn] embed_compare без JSON (rc={rc5}); tail: {out5[-200:]}")

    # ---- 6. seo-editor → body.html + meta.json ----
    sys6 = (agent_prompt("6-seo-editor")
            + f"\n\n=== editor-cheatsheet.md ===\n{read(STYLE/'editor-cheatsheet.md')}"
            + f"\n\n=== ОБРАЗЕЦ ФОРМАТА meta.json ===\n{read(sample/'meta.json')}")
    usr6 = (f"draft.md:\n{read(workdir/'draft.md')}\n\n"
            f"brief.json:\n{json.dumps(bd, ensure_ascii=False)}\n\n"
            f"research.json:\n{json.dumps(rd, ensure_ascii=False)}\n\n"
            "Сконвертируй draft в HTML тела статьи и заполни meta. Верни ОДИН JSON-объект: "
            '{"meta": {...все поля meta.json...}, "body_html": "<...HTML тела...>"}. '
            "Валидный JSON.\n\n"
            "ПРАВИЛА body_html (СТРОГО, частые дефекты):\n"
            "- ТОЛЬКО теги тела: <p>, <h2>, <h3>, <ul>/<ol>/<li>, <strong>, <a>. "
            "БЕЗ <html>/<head>/<body>/<article>/<header>/<footer> — это даст boilerplate.\n"
            "- НЕ добавляй и УДАЛИ из тела, если есть в draft: дисклеймер, копирайт "
            "«© ООО», блок «Об авторе», блок «Читайте также», готовые HTML-кнопки CTA. "
            "Всё это вставляет boilerplate автоматически — дубли запрещены.\n"
            "- BP-маркеры из draft.md (<!--BP:CTA-TOP-->, <!--BP:CTA-MID-->, "
            "<!--BP:CTA-BOTTOM-->, <!--BP:DISCLAIMER-->) перенеси на их места как "
            "HTML-комментарии БЕЗ изменений — НЕ заменяй их текстом/кнопками.\n"
            "- Сохрани структуру H2/H3 из draft КАК ЕСТЬ: где в draft есть H3-подразделы, "
            "оставь их H3, не схлопывай в список и не повышай до H2.\n"
            "- Сохрани все авторские вставки «мы» («по нашему опыту» и т.п.) из draft "
            "дословно — не вычищай их.\n"
            "- Сохрани ПОРЯДОК BP-маркеров как в draft: CTA-BOTTOM и DISCLAIMER идут "
            "ДО блока «Частые вопросы», FAQ — последний раздел. Не переставляй их после FAQ.")
    seo = step("6-seo-editor", sys6, usr6, json_mode=True)
    sd = parse_json_or_die(seo, "seo", workdir)

    # Детерминированная зачистка длинных/средних тире (— –) → короткое (-).
    # Мини-модель упорно ставит «—» (главный AI-маркер) вопреки запрету в промпте,
    # а autofix gate не достаёт их в FAQ из meta.json. Чиним надёжно здесь.
    def dedash(o):
        if isinstance(o, str):
            return o.replace("—", "-").replace("–", "-").replace("―", "-")
        if isinstance(o, list):
            return [dedash(x) for x in o]
        if isinstance(o, dict):
            return {k: dedash(v) for k, v in o.items()}
        return o

    sd = dedash(sd)
    meta = sd.get("meta", {}) if isinstance(sd, dict) else {}
    meta["slug"] = real_slug; meta["category"] = cat
    save(workdir, "meta.json", json.dumps(meta, ensure_ascii=False, indent=2))
    save(workdir, "body.html", sd.get("body_html", ""))

    # ---- 7b. pick_scene_template (детерминированная ротация обложек) ----
    rc, _ = run_py(["articles_scheduler.pick_scene_template", work_slug, cat])
    print(f"   [pick_scene_template] exit={rc}")

    # ---- 7. publisher → scene.txt (адаптирует выбранный шаблон) ----
    tmpl = read(workdir / "scene_template.txt").strip()
    sys7 = agent_prompt("7-publisher") + f"\n\n=== cover-scenes.md ===\n{read(STYLE/'cover-scenes.md')}"
    usr7 = (f"Тема статьи: {bd.get('title') or args.topic}, категория {cat}.\n"
            f"Выбранный шаблон: {tmpl or '(нет — выбери уместный сам)'}\n"
            "Найди этот template_id в cover-scenes.md, адаптируй его под смысл статьи "
            "(подбери 3-7 предметов из allowed pool). Верни ТОЛЬКО английскую scene-строку "
            "для генерации обложки (без JSON, без пояснений).")
    scene = step("7-publisher", sys7, usr7, json_mode=False)
    save(workdir, "scene.txt", scene)

    # ---- ОБЛОЖКА: image_gen напрямую (fal.ai + лого + Cloudinary), ДО inject ----
    # Прямой вызов вместо finalize_draft, чтобы НЕ писать в прод _review_queue.json
    # (это шаг доставки заказчице — для теста не нужен). write_meta=True кладёт
    # cover_url в meta.json, дальше inject подставит реальную обложку в article.html.
    print("\n=== обложка (image_gen: fal.ai + Cloudinary) ===", flush=True)
    try:
        from tools.image_gen import generate_and_upload_cover
        cover = generate_and_upload_cover(
            slug=work_slug, title=meta.get("title") or args.topic,
            category=cat, scene=(scene.strip() or None), write_meta=True)
        print(f"   cover_url: {cover}")
    except Exception as e:
        print(f"   [image_gen failed] {type(e).__name__}: {e}")

    # ---- inject_boilerplate (теперь meta.cover_url есть → обложка в article.html) ----
    rc, out = run_py(["tools.inject_boilerplate", f"drafts/{work_slug}/", "--body", "body.html", "--out", "article.html"])
    print(f"   [inject_boilerplate] exit={rc}")

    # ---- quality_gate (детерминированный, после сборки article.html) ----
    rc, gate_out = run_py(["tools.quality_gate", f"drafts/{work_slug}/article.html", "--json", "--save-report"])
    print(f"\n[quality_gate] exit={rc} (0=passed)")

    # ====== ОТЧЁТ ПО ТОКЕНАМ ======
    print("\n" + "=" * 78)
    print(f"ОТЧЁТ ПО ТОКЕНАМ — статья '{real_slug}' ({cat})")
    print("=" * 78)
    print(f"{'агент':<20}{'модель':<14}{'in':>9}{'out':>8}{'итого':>9}{'$':>10}")
    tot_in = tot_out = 0
    tot_cost = 0.0
    for u in usage_log:
        pin, pout = PRICES.get(u["model"], (5.0, 30.0))
        cost = u["prompt_tokens"] / 1e6 * pin + u["completion_tokens"] / 1e6 * pout
        tot_in += u["prompt_tokens"]; tot_out += u["completion_tokens"]; tot_cost += cost
        print(f"{u['agent']:<20}{u['model']:<14}{u['prompt_tokens']:>9}{u['completion_tokens']:>8}"
              f"{u['total_tokens']:>9}{('$'+format(cost,'.4f')):>10}")
    print("-" * 78)
    print(f"{'ИТОГО':<20}{'':<14}{tot_in:>9}{tot_out:>8}{tot_in+tot_out:>9}{('$'+format(tot_cost,'.4f')):>10}")
    print(f"\nПроекция на 10 статей/день × 30 дней (300 статей): "
          f"~{(tot_in+tot_out)*300//1000}K ток/мес, ~${tot_cost*300:.1f}/мес")
    print("Не учтено: 5-uniqueness (эмбеддинги, ~копейки), image_gen (fal.ai, не LLM).")
    print(f"\nАртефакты: drafts/{work_slug}/  | quality_gate exit={rc}")
    save(workdir, "_token_report.json", json.dumps(usage_log, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
