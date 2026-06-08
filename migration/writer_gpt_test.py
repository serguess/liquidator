"""
Локальный тест писателя (агент 4) на не-Anthropic LLM (по умолчанию GPT-5.5).

Зачем: проверить ДО любой миграции, тянет ли OpenAI-модель русский YMYL-копирайтинг
под планку text.ru (заспам <=50%), на ТОМ ЖЕ входе, что и прод. Claude-эталон не
трогаем: входы (brief/research/outline) и судья (tools.quality_checks) те же.

Что делает:
  1. Читает OPENAI_API_KEY из projects/bankrotstvo/.env.
  2. Берёт готовый drafts/{slug}/{brief,research,outline}.json (вход писателя).
  3. Собирает system-prompt из тех же файлов, что читает писатель в проде
     (.claude/agents/4-writer.md + style/*.md).
  4. Один вызов модели (Pass A + Pass B, как инструктирует промпт писателя).
  5. Сохраняет drafts/_migration_test/{slug}__{model}/draft.md (оригинал не трогаем).
  6. Прогоняет tools.quality_checks (детерминированный судья) и печатает метрики
     рядом с Claude-эталоном из оригинального meta.json.

Запуск (из корня projects/bankrotstvo):
    python migration/writer_gpt_test.py --slug vzyskanie-dolga-po-raspiske
    python migration/writer_gpt_test.py --slug vzyskanie-dolga-po-raspiske --model gpt-5.5
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

# Windows-консоль по умолчанию cp1251 — принудительно UTF-8, иначе падаем на «≈»/«—».
for _s in (sys.stdout, sys.stderr):
    if hasattr(_s, "reconfigure"):
        try:
            _s.reconfigure(encoding="utf-8")
        except Exception:
            pass

ROOT = Path(__file__).resolve().parent.parent
DRAFTS = ROOT / "drafts"
STYLE = ROOT / ".claude" / "style"
AGENTS = ROOT / ".claude" / "agents"
OUT_ROOT = DRAFTS / "_migration_test"


# ---------- .env ----------

def load_api_key() -> str:
    env_path = ROOT / ".env"
    if not env_path.exists():
        sys.exit(f"[ERR] .env не найден: {env_path}")
    key = None
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, val = line.partition("=")
        name = name.strip()
        val = val.strip().strip('"').strip("'")
        if name in ("OPENAI_API_KEY", "OPENAI_KEY"):
            key = val
    if not key:
        sys.exit("[ERR] OPENAI_API_KEY не найден в .env. Добавь строку:\n"
                 "      OPENAI_API_KEY=sk-...")
    return key


# ---------- prompt building ----------

def strip_frontmatter(text: str) -> str:
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            return text[end + 4:].lstrip()
    return text


def read_file(path: Path, label: str) -> str:
    if not path.exists():
        print(f"[warn] нет файла {label}: {path}")
        return ""
    return path.read_text(encoding="utf-8")


def build_system_prompt() -> str:
    parts = []
    role = strip_frontmatter(read_file(AGENTS / "4-writer.md", "4-writer.md"))
    parts.append("# ТВОЯ РОЛЬ И ИНСТРУКЦИЯ (агент 4, писатель)\n\n" + role)
    # порядок по важности — как в самом промпте писателя
    style_files = [
        ("few-shot-exemplars.md", "ЭТАЛОНЫ (главное — учись по примерам)"),
        ("category-periph.md", "ПЕРИФРАЗ-ИНВЕНТАРЬ ПО КАТЕГОРИЯМ"),
        ("writer-cheatsheet.md", "МЕТОДОЛОГИЯ/МЕТРИКИ/ЗАПРЕТЫ"),
        ("anti-spam-playbook.md", "ANTI-SPAM PLAYBOOK"),
        ("legal-facts.md", "ИСТОЧНИК ПРАВДЫ ПО ФАКТАМ"),
    ]
    for fname, label in style_files:
        body = read_file(STYLE / fname, fname)
        if body:
            parts.append(f"\n\n===== {label} ({fname}) =====\n\n{body}")
    return "\n".join(parts)


def build_user_prompt(slug: str, category: str) -> str:
    d = DRAFTS / slug
    brief = read_file(d / "brief.json", "brief.json")
    research = read_file(d / "research.json", "research.json")
    outline = read_file(d / "outline.json", "outline.json")
    prev = read_file(ROOT / "data" / f"_prev_summary_{category}.json", "prev_summary")

    instr = (
        "Напиши черновик статьи `draft.md` строго по своему промпту писателя "
        "(см. system). Это ОДИН вызов: выполни Pass A (плотный черновик) и Pass B "
        "(перифраз горячих лемм + token-cap + ритм) внутри одного ответа, как "
        "инструктирует роль.\n\n"
        "ЖЁСТКИЕ ТРЕБОВАНИЯ:\n"
        "- Верни ТОЛЬКО содержимое файла draft.md: frontmatter (--- slug/title/"
        "description/h1/topic_action ---) + markdown тело с H2/H3. Без ```-обёрток, "
        "без комментариев до/после, без пояснений.\n"
        "- Тело 6500-7500 знаков (news: 4500-6500). Hard cap 8000.\n"
        "- Главный рычаг заспама: lexical_diversity >= 0.62 (лучше 0.65). "
        "Зонируй лексику по H2, веди лестницы перифраз для горячих лемм.\n"
        "- «ст.» НЕ разворачивать в «статья N». «руб» = 0 (только «рублей»). "
        "Цитаты закона перифразировать.\n"
        "- 4 BP-маркера обязательны: <!--BP:CTA-TOP-->, <!--BP:CTA-MID-->, "
        "<!--BP:CTA-BOTTOM-->, <!--BP:DISCLAIMER-->.\n"
        "- Тире только короткие (-), кавычки «ёлочки», голос «мы», без эмодзи.\n\n"
        "ЧАСТЫЕ ДЕФЕКТЫ — НЕ ДОПУСКАТЬ:\n"
        "1. НЕ копируй в текст служебные формулировки из outline (thesis, "
        "must_include_facts, must_avoid) дословно — это указания ДЛЯ ТЕБЯ, а не "
        "текст статьи. Запрещены фразы вида «В первые предложения:», «Первый экран "
        "заканчивается CTA», «основные риски - ...». Первый абзац — живой бытовой "
        "зачин от лица читателя в стрессе, без пересказа структуры статьи.\n"
        "2. H2 КОРОТКИЕ (3-6 слов), живые, БЕЗ двоеточий и перечислений в самом "
        "заголовке. Плохо: «Какие варианты сохранить жильё: реструктуризация, "
        "мировое, реализация». Хорошо: «Два способа сохранить жильё».\n"
        "3. Где в блоке несколько вариантов/способов/шагов — раскрывай их через "
        "H3-подзаголовки внутри H2 (H3 «Способ 1. ...», H3 «Способ 2. ...»), а не "
        "одним сплошным списком. Структуру не уплощай.\n"
        "4. ОБЯЗАТЕЛЬНО минимум 2 авторские вставки голосом «мы»: «по нашему опыту», "
        "«в нашей практике», «мы видим», «мы советуем». Без них AI-детектор растёт. "
        "Это hard-требование (gate блокирует author_markers < 2).\n"
        "5. НЕ пиши в тексте сам дисклеймер, копирайт «© ООО», блок «Об авторе» и "
        "готовые HTML-кнопки CTA — вместо них ставь только BP-маркеры. Эти блоки "
        "подставит boilerplate автоматически. Дублирование запрещено.\n\n"
    )
    blocks = [instr, f"## ВХОД. brief.json\n```json\n{brief}\n```"]
    blocks.append(f"## ВХОД. outline.json (структура, длины, лексические зоны, keyword_budget)\n```json\n{outline}\n```")
    blocks.append(f"## ВХОД. research.json (фактбаза, не отступать)\n```json\n{research}\n```")
    if prev:
        blocks.append(f"## Антишаблонность по соседу: _prev_summary_{category}.json\n```json\n{prev}\n```")
    return "\n\n".join(blocks)


# ---------- model call ----------

def call_model(api_key: str, model: str, messages: list[dict],
               reasoning_effort: str | None = None) -> tuple[str, dict]:
    from openai import OpenAI
    client = OpenAI(api_key=api_key, timeout=200.0, max_retries=0)
    kwargs = {"model": model, "messages": messages, "max_completion_tokens": 20000}
    if reasoning_effort:
        kwargs["reasoning_effort"] = reasoning_effort
    from openai import APIConnectionError, APITimeoutError, RateLimitError
    t0 = time.time()
    resp = None
    for attempt in range(4):
        try:
            resp = client.chat.completions.create(**kwargs)
            break
        except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
            wait = 5 * (attempt + 1)
            print(f"[net] {type(exc).__name__}, попытка {attempt + 1}/4, жду {wait}с...")
            time.sleep(wait)
        except Exception as exc:
            msg = str(exc)
            if "model" in msg.lower() and ("not found" in msg.lower() or "does not exist" in msg.lower()):
                print(f"[ERR] модель '{model}' недоступна на этом ключе. Доступные gpt-модели:")
                try:
                    for m in client.models.list().data:
                        if "gpt" in m.id.lower():
                            print("   ", m.id)
                except Exception as e2:
                    print("   (не удалось получить список:", e2, ")")
                sys.exit(1)
            raise
    if resp is None:
        sys.exit("[ERR] сеть недоступна после 4 попыток (getaddrinfo/timeout). Повтори позже.")
    dt = time.time() - t0
    content = (resp.choices[0].message.content or "").strip()
    # снять случайные ```-обёртки
    if content.startswith("```"):
        content = content.split("\n", 1)[1] if "\n" in content else content
        if content.endswith("```"):
            content = content[: content.rfind("```")]
        content = content.strip()
    u = resp.usage
    usage = {
        "prompt_tokens": getattr(u, "prompt_tokens", None),
        "completion_tokens": getattr(u, "completion_tokens", None),
        "total_tokens": getattr(u, "total_tokens", None),
        "seconds": round(dt, 1),
    }
    return content, usage


# ---------- judge ----------

def run_quality_checks(draft_path: Path) -> dict | None:
    cmd = [sys.executable, "-m", "tools.quality_checks", str(draft_path), "--json"]
    res = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True,
                         encoding="utf-8", errors="replace")
    if not res.stdout:
        print("[warn] quality_checks без вывода. stderr:", (res.stderr or "")[-400:])
        return None
    try:
        data = json.loads(res.stdout)
        return data[0] if isinstance(data, list) else data
    except json.JSONDecodeError:
        print("[warn] quality_checks: не JSON:", res.stdout[-400:])
        return None


def predict_spam(lex_div: float, top10_share: float, ngram3: float,
                 targeted: list[dict]) -> int:
    """Та же таблица, что в tools.quality_checks.predict_textru_metrics (spam-часть)."""
    if lex_div >= 0.73:
        spam = 38
    elif lex_div >= 0.70:
        spam = 42
    elif lex_div >= 0.65:
        spam = 47
    elif lex_div >= 0.62:
        spam = 49
    elif lex_div >= 0.59:
        spam = 53
    else:
        spam = 58
    if top10_share > 0.115:
        spam += 2
    if ngram3 > 0.035:
        spam += 2
    over_hard = sum(max(0, h.get("count", 0) - h.get("limit", 0))
                    for h in targeted if h.get("severity", "hard") == "hard")
    over_soft = sum(max(0, h.get("count", 0) - h.get("limit", 0))
                    for h in targeted if h.get("severity") == "soft")
    spam += min(15, over_hard // 12)
    spam += min(8, over_soft // 20)
    return min(max(spam, 25), 90)


# ---------- metrics / feedback ----------

def extract(rep: dict) -> dict:
    spam = rep.get("spam") or {}
    targeted = rep.get("targeted_tokens") or []
    lex = spam.get("lexical_diversity", 0.0)
    top10 = spam.get("top10_share", 0.0)
    ng3 = spam.get("ngram3_repeat_share", 0.0)
    return {
        "chars": rep.get("text_chars"),
        "length_status": rep.get("length_status"),
        "passed": rep.get("passed"),
        "lex": lex, "top10": top10, "ng3": ng3,
        "top1": spam.get("top1_count", 0),
        "pred_spam": predict_spam(lex, top10, ng3, targeted),
        "author": rep.get("author_markers_count"),
        "author_min": rep.get("author_markers_min"),
        "top_words": spam.get("top10_words", [])[:6],
        "over_hard": [h for h in targeted if h.get("over_limit") and h.get("severity", "hard") == "hard"],
        "over_soft": [h for h in targeted if h.get("over_limit") and h.get("severity") == "soft"],
        "word_warnings": spam.get("word_warnings") or [],
    }


def build_feedback(m: dict) -> str:
    lines = ["Судья quality_checks вернул [RISK]. Сделай ТОЧЕЧНУЮ правку (не переписывай "
             "статью целиком), верни ПОЛНЫЙ обновлённый draft.md тем же форматом. Проблемы:"]
    if m["length_status"] == "too_long":
        lines.append(f"- ДЛИНА {m['chars']} знаков > cap 8000. Сократи тело до 6500-7500 "
                     "(news 4500-6500): убери воду, слей дублирующие абзацы. Это критично.")
    elif m["length_status"] == "too_short":
        lines.append(f"- ДЛИНА {m['chars']} < минимума. Дополни фактами из research.")
    if m["lex"] < 0.62:
        lines.append(f"- lexical_diversity {m['lex']} < 0.62 (ГЛАВНЫЙ рычаг заспама). Подними "
                     "разнообразие лемм: лестницы перифраз для горячих слов, замени повторы "
                     "синонимами/фактами, разбей однотипные формулировки.")
    if m["top1"] > 12:
        lines.append(f"- top1-лемма {m['top1']} вхождений (cap 12). Самые частые: "
                     f"{m['top_words'][:5]}. Проредить перифразами.")
    if m["top10"] > 0.115:
        lines.append(f"- top10_share {m['top10']*100:.1f}% > 11.5%. Разнеси частотную лексику по зонам H2.")
    if m["word_warnings"]:
        lines.append(f"- Леммы для прореживания (count>12): {', '.join(m['word_warnings'])}.")
    if m["over_hard"]:
        lines.append("- HARD-токены сверх cap: " +
                     ", ".join(f"{h['token']}={h['count']}/{h['limit']}" for h in m["over_hard"]))
    if m["over_soft"]:
        lines.append("- soft-токены сверх cap: " +
                     ", ".join(f"{h['token']}={h['count']}/{h['limit']}" for h in m["over_soft"]))
    if (m["author"] or 0) < (m["author_min"] or 0):
        lines.append(f"- авторских вставок «мы» {m['author']} < {m['author_min']}. Добавь «по нашему опыту»/«в нашей практике».")
    return "\n".join(lines)


def print_report(m: dict, meta: dict, category: str, label: str) -> None:
    def row(name, gpt, claude, target):
        return f"  {name:<22} GPT={gpt!s:<11} Claude={claude!s:<11} цель: {target}"
    print(f"\n================= {label} (GPT vs Claude-эталон) =================")
    print(f"  Категория: {category}   Длина GPT: {m['chars']} ({m['length_status']}), Claude: {meta.get('text_chars')}")
    print(f"  Gate passed (числовой судья): GPT={m['passed']}  "
          f"Claude_gate={meta.get('quality_gate_passed')} (retry={meta.get('quality_gate_retry_count')})")
    print()
    print(row("lexical_diversity", m["lex"], meta.get("local_lexical_diversity"), ">=0.62 (лучше 0.65)"))
    print(row("top10_share", f"{m['top10']*100:.1f}%", f"{(meta.get('local_spam_top10_share') or 0)*100:.1f}%", "<=11.5%"))
    print(row("ngram3_repeat", f"{m['ng3']*100:.1f}%", f"{(meta.get('local_spam_ngram3_repeat') or 0)*100:.1f}%", "<=5.5%"))
    print(row("top1_lemma_count", m["top1"], "-", "<=12"))
    print(row("predicted_spam ~", f"{m['pred_spam']}%", f"{meta.get('predicted_spam_pct')}%", "<=50%"))
    if m["over_hard"]:
        print("  [HARD over-limit]:", ", ".join(f"{h['token']}={h['count']}/{h['limit']}" for h in m["over_hard"]))
    if m["over_soft"]:
        print("  [soft over-limit]:", ", ".join(f"{h['token']}={h['count']}/{h['limit']}" for h in m["over_soft"]))
    print(f"  author_markers «мы»: {m['author']} (min {m['author_min']})")
    if m["top_words"]:
        print(f"  топ лемм: {m['top_words']}")
    print("=" * 67)
    ok = m["pred_spam"] <= 50 and m["lex"] >= 0.62 and m["length_status"] == "ok"
    print(f"  ВЕРДИКТ по числам: {'ТЯНЕТ планку' if ok else 'НЕ дотянул'} "
          f"(spam~{m['pred_spam']}%, lex={m['lex']}, длина {m['length_status']})")


# ---------- main ----------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slug", required=False)
    ap.add_argument("--model", default="gpt-5.5")
    ap.add_argument("--fix-iters", type=int, default=1,
                    help="доп. проходов self-fix через судью (прод: cap=2, т.е. 1 доп.)")
    ap.add_argument("--list-models", action="store_true",
                    help="показать доступные на ключе модели и выйти")
    args = ap.parse_args()

    if args.list_models:
        from openai import OpenAI
        client = OpenAI(api_key=load_api_key())
        ids = sorted(m.id for m in client.models.list().data)
        print("Доступные модели на ключе (gpt/o-серии):")
        for mid in ids:
            if any(t in mid.lower() for t in ("gpt", "o1", "o3", "o4", "chat")):
                print("   ", mid)
        return 0
    if not args.slug:
        sys.exit("[ERR] укажи --slug или --list-models")

    slug = args.slug
    src = DRAFTS / slug
    if not src.exists():
        sys.exit(f"[ERR] нет драфта {src}")
    meta = json.loads((src / "meta.json").read_text(encoding="utf-8")) if (src / "meta.json").exists() else {}
    category = meta.get("category") or "fiz"

    api_key = load_api_key()
    print(f"== Тест писателя: slug={slug} category={category} model={args.model} ==")

    system = build_system_prompt()
    user = build_user_prompt(slug, category)
    print(f"[i] system ~{len(system)} симв., user ~{len(user)} симв.")

    out_dir = OUT_ROOT / f"{slug}__{args.model.replace('/', '_')}"
    out_dir.mkdir(parents=True, exist_ok=True)
    draft_path = out_dir / "draft.md"

    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "seconds": 0.0}
    last_m = None

    for it in range(args.fix_iters + 1):
        print(f"\n[i] Проход {it + 1}/{args.fix_iters + 1}: вызываю {args.model}...")
        content, usage = call_model(api_key, args.model, messages)
        for k in total_usage:
            total_usage[k] += usage.get(k) or 0
        print(f"[i] ответ: {len(content)} симв., usage={usage}")
        draft_path.write_text(content, encoding="utf-8")
        rep = run_quality_checks(draft_path)
        if not rep:
            return 1
        m = extract(rep)
        last_m = m
        label = f"ПРОХОД {it + 1}"
        print_report(m, meta, category, label)
        passed_target = m["pred_spam"] <= 50 and m["lex"] >= 0.62 and m["length_status"] == "ok"
        if passed_target or it == args.fix_iters:
            break
        # готовим self-fix: история + конкретный фидбэк судьи
        messages.append({"role": "assistant", "content": content})
        messages.append({"role": "user", "content": build_feedback(m)})

    total_usage["seconds"] = round(total_usage["seconds"], 1)
    (out_dir / "_usage.json").write_text(json.dumps(total_usage, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n[i] draft: {draft_path.relative_to(ROOT)}")
    print(f"[i] суммарно usage: {total_usage}")
    # приблизительные цены за 1M токенов (input/output), июнь 2026 — для ориентира
    PRICES = {
        "gpt-5.5": (5.0, 30.0), "gpt-5.5-pro": (15.0, 120.0),
        "gpt-5.4": (3.0, 24.0), "gpt-5.4-mini": (0.25, 2.0), "gpt-5.4-nano": (0.05, 0.40),
        "gpt-5": (1.25, 10.0), "gpt-5-mini": (0.25, 2.0), "gpt-5-nano": (0.05, 0.40),
        "gpt-4.1-mini": (0.40, 1.60), "gpt-4o-mini": (0.15, 0.60),
    }
    pin, pout = PRICES.get(args.model, (5.0, 30.0))
    cost = (total_usage["prompt_tokens"] / 1e6) * pin + (total_usage["completion_tokens"] / 1e6) * pout
    note = "" if args.model in PRICES else " (цена неизвестна, взял gpt-5.5)"
    print(f"[i] ~стоимость прогона на {args.model}: ${cost:.4f}{note} "
          f"(оценка, без кэша; с кэшем входа ниже)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
