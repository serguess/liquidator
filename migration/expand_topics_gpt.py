"""
GPT-замена slash-команды /expand-topics (миграция с Anthropic на OpenAI).

Генерит N новых тем в drafts/_topic-map/{category}.json через один вызов
gpt-5-mini вместо `claude -p "/expand-topics {category}"`. Запускается
автономным scheduler-ом (articles_scheduler/runner.py) в GPT-режиме, когда
свободные темы категории закончились — это блокер автономности.

Что делает:
  1. Собирает «занятые» slug-и и main_keyword-и (drafts/, published_index,
     все topic-map) — новые темы не должны пересекаться.
  2. Строит system из той же спеки .claude/commands/expand-topics.md +
     стайл-гайдов (yandex-quality, news-sourcing для news) + формат-образца.
  3. Просит модель вернуть JSON {"topics":[...]} по схеме 1-semantics.
  4. Постобработка: dedup, нумерация id, длины, dedash, для news — валидация
     event_date/news_zone/primary_source (как _is_news_topic_valid).
  5. Дописывает валидные темы в массив topics (не трогая существующие).

ВАЖНО про news: чистый chat.completions не умеет WebSearch/WebFetch, поэтому
свежие новости с проверенными URL надёжно не генерит (knowledge cutoff). Для
fiz/yur/vzysk (вечнозелёные брифы, 9 из 10 статей/день) работает полноценно.
news остаётся на еженедельном news_topup + sanitize отбракует невалидное.

Запуск (из projects/bankrotstvo):
    python migration/expand_topics_gpt.py --category fiz
    python migration/expand_topics_gpt.py --category yur --count 10 --dry-run
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))                            # tools.* / articles_scheduler.*
sys.path.insert(0, str(Path(__file__).resolve().parent))  # writer_gpt_test
import writer_gpt_test as W  # noqa: E402

DRAFTS = ROOT / "drafts"
TOPIC_MAP = DRAFTS / "_topic-map"
STYLE = ROOT / ".claude" / "style"
AGENTS = ROOT / ".claude" / "agents"
COMMANDS = ROOT / ".claude" / "commands"
PUBLISHED_INDEX = ROOT / "data" / "published_index.json"

DEFAULT_MODEL = "gpt-5-mini"
VALID_CATEGORIES = ("fiz", "yur", "vzysk", "news")


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def dedash(s: str) -> str:
    """Длинное/среднее тире → дефис (критичный AI-маркер + правило стиля)."""
    if not isinstance(s, str):
        return s
    return s.replace(" — ", ": ").replace("—", "-").replace("–", "-")


def _chat(client, model: str, system: str, user: str,
          reasoning: str = "low", max_tokens: int = 16000) -> tuple[str, dict]:
    from openai import APIConnectionError, APITimeoutError, RateLimitError
    messages = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
    kwargs = {"model": model, "messages": messages,
              "max_completion_tokens": max_tokens,
              "response_format": {"type": "json_object"},
              "reasoning_effort": reasoning}
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
    content = (resp.choices[0].message.content or "").strip()
    u = resp.usage
    usage = {"prompt": getattr(u, "prompt_tokens", 0) or 0,
             "completion": getattr(u, "completion_tokens", 0) or 0,
             "total": getattr(u, "total_tokens", 0) or 0,
             "finish": resp.choices[0].finish_reason}
    return content, usage


def collect_taken() -> tuple[set[str], set[str]]:
    """Занятые slug-и и main_keyword-и по всему проекту (для дедупа)."""
    try:
        from articles_scheduler.runner import _collect_used_slugs
        slugs = set(_collect_used_slugs())
    except Exception:
        slugs = set()
        if DRAFTS.exists():
            for d in DRAFTS.iterdir():
                if d.is_dir() and not d.name.startswith("_"):
                    slugs.add(d.name)
    keywords: set[str] = set()
    # все topic-map: и slug, и main_keyword
    for cat in VALID_CATEGORIES:
        p = TOPIC_MAP / f"{cat}.json"
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        for t in data.get("topics", []) or []:
            if t.get("slug"):
                slugs.add(t["slug"])
            if t.get("main_keyword"):
                keywords.add(t["main_keyword"].strip().lower())
    # published_index
    if PUBLISHED_INDEX.exists():
        try:
            pi = json.loads(PUBLISHED_INDEX.read_text(encoding="utf-8"))
            for e in pi.get("articles", []) or []:
                if e.get("slug"):
                    slugs.add(e["slug"])
                if e.get("main_keyword"):
                    keywords.add(e["main_keyword"].strip().lower())
        except (json.JSONDecodeError, OSError):
            pass
    return slugs, keywords


def _next_id_start(topics: list[dict], category: str) -> int:
    """Максимальный N среди id вида {category}-NN, +1."""
    mx = 0
    for t in topics:
        tid = str(t.get("id") or "")
        if tid.startswith(f"{category}-"):
            tail = tid[len(category) + 1:]
            if tail.isdigit():
                mx = max(mx, int(tail))
    return mx + 1


def build_system(category: str) -> str:
    spec = W.strip_frontmatter(_read(COMMANDS / "expand-topics.md"))
    parts = [
        "Ты SEO-стратег проекта о банкротстве. Генерируешь брифы новых тем "
        "(НЕ пишешь статьи) строго по спецификации ниже.\n",
        "=== СПЕЦИФИКАЦИЯ /expand-topics ===\n" + spec,
    ]
    yq = _read(STYLE / "yandex-quality.md")
    if yq:
        parts.append("\n=== СТАЙЛ-ГАЙД yandex-quality (Баден-Баден) ===\n" + yq)
    if category == "news":
        ns = _read(STYLE / "news-sourcing.md")
        if ns:
            parts.append("\n=== news-sourcing (6 зон, окно 30 дней) ===\n" + ns)
    sem = W.strip_frontmatter(_read(AGENTS / "1-semantics.md"))
    if sem:
        parts.append("\n=== Формат brief-полей (1-semantics) ===\n" + sem[:4000])
    parts.append(
        "\n=== РЕЖИМ API (не Claude Code) ===\n"
        "Ты работаешь через прямой API-вызов. НЕ пишешь файлы, НЕ запускаешь "
        "bash/WebSearch. Просто верни JSON по схеме ниже. Любые инструкции в "
        "спеке про запись файла / Write / heartbeat ИГНОРИРУЙ — запись делает "
        "вызывающий Python-код.\n"
        "Длинное тире (—) ЗАПРЕЩЕНО во всех полях — только дефис (-) или "
        "двоеточие. Кавычки-ёлочки «...»."
    )
    return "\n".join(parts)


def build_user(category: str, count: int, sample: list[dict],
               taken_slugs: set[str], taken_kw: set[str]) -> str:
    sample_json = json.dumps(sample[:2], ensure_ascii=False, indent=2)
    # короткий список занятого (срез — модели хватит понять паттерн дублей)
    busy_slugs = sorted(taken_slugs)
    busy_kw = sorted(taken_kw)
    schema_fields = (
        "id, slug, title, title_length, description, description_length, h1, "
        "main_keyword, main_keyword_density_target, secondary_keywords (6 шт), "
        "intent, funnel_stage, article_type, offer, frequency_estimate, "
        "rationale, expected_length_chars, topic_action"
    )
    if category == "news":
        schema_fields += ", event_date (YYYY-MM-DD, ≤30 дней), news_zone, primary_source (URL)"
    return (
        f"Категория: {category}. Нужно ровно {count} НОВЫХ тем.\n\n"
        f"Верни JSON-объект строго вида: {{\"topics\": [ {{...}}, ... ]}}.\n"
        f"Каждый объект — ровно эти поля: {schema_fields}.\n"
        "Поле status НЕ добавляй. id оставь пустым или любым — Python "
        "перенумерует. title_length/description_length посчитай как длину строки.\n\n"
        "ОБРАЗЕЦ ФОРМАТА (2 существующие темы, копируй структуру, НЕ содержание):\n"
        f"{sample_json}\n\n"
        f"НЕЛЬЗЯ дублировать эти slug-и (всего {len(busy_slugs)}):\n"
        f"{', '.join(busy_slugs)}\n\n"
        f"НЕЛЬЗЯ дублировать эти main_keyword (всего {len(busy_kw)}):\n"
        f"{', '.join(busy_kw)}\n\n"
        "Распредели по интентам: 3-4 commercial/problem-aware (decision), "
        "3-4 solution-aware (consideration), 2-3 informational (awareness). "
        "slug — латиница kebab-case, 30-60 знаков, без стоп-слов."
    )


def normalize_topic(raw: dict, category: str, new_id: str,
                    taken_slugs: set[str], taken_kw: set[str]) -> tuple[dict | None, str]:
    """Чистит/валидирует одну тему. Возвращает (topic|None, reason)."""
    slug = (raw.get("slug") or "").strip()
    if not slug:
        return None, "no_slug"
    if slug in taken_slugs:
        return None, f"dup_slug:{slug}"
    kw = (raw.get("main_keyword") or "").strip()
    if kw and kw.lower() in taken_kw:
        return None, f"dup_keyword:{kw}"

    title = dedash((raw.get("title") or "").strip())
    desc = dedash((raw.get("description") or "").strip())
    h1 = dedash((raw.get("h1") or title).strip())
    if not title:
        return None, "no_title"

    sec = raw.get("secondary_keywords") or []
    if isinstance(sec, str):
        sec = [sec]
    sec = [dedash(str(x).strip()) for x in sec if str(x).strip()][:6]

    topic = {
        "id": new_id,
        "slug": slug,
        "title": title,
        "title_length": len(title),
        "description": desc,
        "description_length": len(desc),
        "h1": h1,
        "main_keyword": dedash(kw),
        "main_keyword_density_target": raw.get("main_keyword_density_target") or "≤2.5%",
        "secondary_keywords": sec,
        "intent": raw.get("intent") or "problem-aware",
        "funnel_stage": raw.get("funnel_stage") or "consideration",
        "article_type": raw.get("article_type") or "law-explanation",
        "offer": raw.get("offer") or "proverka-spisaniya",
        "frequency_estimate": raw.get("frequency_estimate") or "medium",
        "rationale": dedash((raw.get("rationale") or "").strip()),
        "expected_length_chars": int(raw.get("expected_length_chars") or 7000),
        "client_notes": "",
        "topic_action": dedash((raw.get("topic_action") or title).strip()),
    }
    if category == "news":
        for f in ("event_date", "news_zone", "primary_source"):
            if raw.get(f):
                topic[f] = raw[f]
        try:
            from articles_scheduler.runner import _is_news_topic_valid
            ok, why = _is_news_topic_valid(topic)
            if not ok:
                return None, f"news_invalid:{why}"
        except Exception:
            # без валидатора news пропускаем только с обязательными полями
            if not (topic.get("event_date") and topic.get("news_zone")
                    and topic.get("primary_source")):
                return None, "news_missing_fields"
    return topic, "ok"


def main() -> int:
    ap = argparse.ArgumentParser(description="GPT-замена /expand-topics")
    ap.add_argument("--category", required=True, choices=VALID_CATEGORIES)
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true",
                    help="не писать в topic-map, только показать что сгенерилось")
    args = ap.parse_args()

    category = args.category
    path = TOPIC_MAP / f"{category}.json"
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        data = {"category": category, "category_label": category,
                "generated_at": "", "topics": []}
    existing = data.get("topics", []) or []
    sample = [t for t in existing if t.get("status") != "rejected"] or existing

    taken_slugs, taken_kw = collect_taken()
    print(f"[expand-gpt] category={category} count={args.count} "
          f"model={args.model} занято slug={len(taken_slugs)} kw={len(taken_kw)}")

    from openai import OpenAI
    client = OpenAI(api_key=W.load_api_key(), timeout=200.0, max_retries=0)
    system = build_system(category)
    user = build_user(category, args.count, sample, taken_slugs, taken_kw)

    content, usage = _chat(client, args.model, system, user,
                           reasoning="low", max_tokens=16000)
    print(f"[expand-gpt] tokens prompt={usage['prompt']} "
          f"completion={usage['completion']} finish={usage['finish']}")
    if usage["finish"] == "length":
        print("[expand-gpt] WARN: ответ обрезан по длине — часть тем может пропасть")

    try:
        parsed = json.loads(content)
    except json.JSONDecodeError as e:
        (TOPIC_MAP / f"_FAILED_expand_{category}.txt").write_text(content, encoding="utf-8")
        print(f"[ERR] невалидный JSON ({e}). Сырьё в _FAILED_expand_{category}.txt")
        return 1
    raw_topics = parsed.get("topics") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_topics, list):
        print("[ERR] в ответе нет массива topics")
        return 1

    next_id = _next_id_start(existing, category)
    added: list[dict] = []
    skipped: list[str] = []
    for raw in raw_topics:
        if not isinstance(raw, dict):
            continue
        new_id = f"{category}-{next_id:02d}"
        topic, reason = normalize_topic(raw, category, new_id, taken_slugs, taken_kw)
        if topic is None:
            skipped.append(reason)
            continue
        added.append(topic)
        taken_slugs.add(topic["slug"])
        if topic.get("main_keyword"):
            taken_kw.add(topic["main_keyword"].lower())
        next_id += 1

    print(f"[expand-gpt] валидных тем: {len(added)}, пропущено: {len(skipped)}")
    for s in skipped:
        print(f"   skip: {s}")
    for t in added:
        print(f"   + {t['id']:10} {t['slug']:50} | {t['main_keyword']}")

    if args.dry_run:
        print("[expand-gpt] DRY-RUN — в файл не пишу")
        return 0 if added else 1

    if not added:
        print("[expand-gpt] нечего добавлять (0 валидных тем) — exit 1")
        return 1

    data["topics"] = existing + added
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[expand-gpt] added={len(added)} total={len(data['topics'])} → {path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
