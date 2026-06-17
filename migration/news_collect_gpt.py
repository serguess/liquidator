"""
Еженедельный сбор свежих news-тем через OpenAI web_search (миграция с claude).

Заменяет `claude -p /expand-topics news` в scripts/news_topup.py. Чистый
chat.completions новости не умеет (нет браузинга, knowledge cutoff), но
Responses API с инструментом web_search — умеет: gpt-5-mini реально ищет
свежие события на vsrf.ru / pravo.gov.ru / fedresurs и возвращает URL.

Защита от галлюцинаций: КАЖДЫЙ primary_source проверяется HTTP-запросом
(URL должен резолвиться) + _is_news_topic_valid (event_date ≤30 дней,
обязательные поля) + запрет года в title/h1 (иначе finalize_draft валит слот).

Пишет до N валидных news-тем в drafts/_topic-map/news.json на неделю вперёд.

Запуск (из projects/bankrotstvo):
    python migration/news_collect_gpt.py            # реальный сбор + запись
    python migration/news_collect_gpt.py --dry-run  # показать, не писать
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import writer_gpt_test as W  # noqa: E402

TOPIC_MAP = ROOT / "drafts" / "_topic-map"
STYLE = ROOT / ".claude" / "style"
NEWS_JSON = TOPIC_MAP / "news.json"

DEFAULT_MODEL = "gpt-5-mini"
NEWS_ZONES = ("vs_practice", "vs_plenum", "legislation", "moratorium", "initiative", "finance")
SOURCES = "vsrf.ru, pravo.gov.ru, fedresurs.ru, consultant.ru, garant.ru, klerk.ru, pravo.ru"
YEAR_RX = re.compile(r"\b(19|20)\d{2}\b")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8") if p.exists() else ""


def dedash(s) -> str:
    if not isinstance(s, str):
        return s
    return s.replace(" — ", ": ").replace("—", "-").replace("–", "-")


def url_ok(url: str) -> bool:
    """URL резолвится в реальную страницу. 2xx/3xx — ок; 401/403/405/429 —
    страница есть, но бота блокируют (тоже считаем валидной); 404/сеть — нет."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= getattr(r, "status", 200) < 400
    except urllib.error.HTTPError as e:
        return e.code in (401, 403, 405, 406, 429)
    except Exception:
        return False


def collect_taken() -> tuple[set[str], set[str]]:
    """Занятые slug + main_keyword (для дедупа), как в expand_topics_gpt."""
    try:
        from articles_scheduler.runner import _collect_used_slugs
        slugs = set(_collect_used_slugs())
    except Exception:
        slugs = set()
    keywords: set[str] = set()
    for cat in ("fiz", "yur", "vzysk", "news"):
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
    return slugs, keywords


def _next_id_start(topics: list[dict]) -> int:
    mx = 0
    for t in topics:
        tid = str(t.get("id") or "")
        if tid.startswith("news-"):
            tail = tid[5:]
            if tail.isdigit():
                mx = max(mx, int(tail))
    return mx + 1


def build_input(n: int, today: str, taken_slugs: set[str]) -> str:
    news_spec = _read(STYLE / "news-sourcing.md")
    busy = ", ".join(sorted(taken_slugs))
    return (
        f"Сегодня {today}. Найди через веб-поиск до {n} РЕАЛЬНЫХ свежих новостей "
        f"(событие за последние 30 дней) по банкротству в России. Ищи на официальных "
        f"источниках: {SOURCES}.\n\n"
        "Для КАЖДОЙ новости дай конкретное событие с датой и РАБОЧИМ URL источника "
        "(прямая ссылка на новость/документ, которую ты реально открыл в поиске). "
        "Не выдумывай URL. Если реального события с датой и ссылкой нет — НЕ добавляй "
        "(лучше меньше тем, но реальных).\n\n"
        f"news_zone — одно из: {', '.join(NEWS_ZONES)}.\n"
        "ЗАПРЕЩЕНО: год (2024/2025/2026 и т.п.) в полях title и h1 (год можно только "
        "в description/rationale/event_date). Длинное тире (—) запрещено везде.\n\n"
        "Верни СТРОГО JSON-объект: {\"topics\": [ {...}, ... ]}. Поля каждого объекта:\n"
        "slug (латиница kebab-case 30-60, без года), title (50-60 знаков, без года), "
        "description (130-160), h1 (50-70, без года), main_keyword, "
        "secondary_keywords (6 шт), intent, funnel_stage, article_type, offer "
        "(proverka-spisaniya|otmena-prikaza|snyatie-aresta|raschet-stoimosti), "
        "frequency_estimate, rationale, expected_length_chars (число), topic_action, "
        "event_date (YYYY-MM-DD, фактическая дата события), news_zone, primary_source (URL).\n\n"
        + (f"НЕ дублируй эти slug: {busy}\n" if busy else "")
        + (("\n=== news-sourcing (6 зон, окно 30 дней) ===\n" + news_spec[:3500])
           if news_spec else "")
    )


def web_search_topics(model: str, prompt: str) -> tuple[list, str]:
    """Один вызов Responses API с web_search. Возвращает (raw_topics, raw_text)."""
    from openai import OpenAI
    client = OpenAI(api_key=W.load_api_key(), timeout=240.0, max_retries=1)
    resp = client.responses.create(
        model=model,
        tools=[{"type": "web_search"}],
        input=prompt + "\n\nОтвет — только JSON, без markdown-обёрток и пояснений.",
    )
    text = (getattr(resp, "output_text", "") or "").strip()
    # вырезаем JSON из возможной обёртки
    if "```" in text:
        m = re.search(r"```(?:json)?\s*(.+?)```", text, re.DOTALL)
        if m:
            text = m.group(1).strip()
    # на случай текста вокруг JSON — берём от первой { до последней }
    if not text.startswith("{"):
        i, j = text.find("{"), text.rfind("}")
        if i != -1 and j != -1:
            text = text[i:j + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return [], text
    topics = data.get("topics") if isinstance(data, dict) else data
    return (topics if isinstance(topics, list) else []), text


def normalize(raw: dict, new_id: str, taken_slugs: set[str], taken_kw: set[str]):
    slug = (raw.get("slug") or "").strip()
    if not slug or slug in taken_slugs:
        return None, f"dup_or_no_slug:{slug}"
    kw = (raw.get("main_keyword") or "").strip()
    if kw and kw.lower() in taken_kw:
        return None, f"dup_keyword:{kw}"
    title = dedash((raw.get("title") or "").strip())
    h1 = dedash((raw.get("h1") or title).strip())
    if not title:
        return None, "no_title"
    if YEAR_RX.search(title) or YEAR_RX.search(h1):
        return None, "year_in_title_or_h1"
    src = (raw.get("primary_source") or "").strip()
    if not url_ok(src):
        return None, f"url_unreachable:{src[:60]}"
    sec = raw.get("secondary_keywords") or []
    if isinstance(sec, str):
        sec = [sec]
    sec = [dedash(str(x).strip()) for x in sec if str(x).strip()][:6]
    topic = {
        "id": new_id, "slug": slug,
        "title": title, "title_length": len(title),
        "description": dedash((raw.get("description") or "").strip()),
        "description_length": len(dedash((raw.get("description") or "").strip())),
        "h1": h1,
        "main_keyword": dedash(kw),
        "main_keyword_density_target": raw.get("main_keyword_density_target") or "≤2.5%",
        "secondary_keywords": sec,
        "intent": raw.get("intent") or "informational",
        "funnel_stage": raw.get("funnel_stage") or "awareness",
        "article_type": raw.get("article_type") or "law-explanation",
        "offer": raw.get("offer") or "proverka-spisaniya",
        "frequency_estimate": raw.get("frequency_estimate") or "medium",
        "rationale": dedash((raw.get("rationale") or "").strip()),
        "expected_length_chars": int(raw.get("expected_length_chars") or 6000),
        "client_notes": "",
        "topic_action": dedash((raw.get("topic_action") or title).strip()),
        "event_date": (raw.get("event_date") or "").strip(),
        "news_zone": raw.get("news_zone") if raw.get("news_zone") in NEWS_ZONES else "legislation",
        "primary_source": src,
    }
    # финальная валидация свежести/полей как в проде
    try:
        from articles_scheduler.runner import _is_news_topic_valid
        ok, why = _is_news_topic_valid(topic)
        if not ok:
            return None, f"news_invalid:{why}"
    except Exception:
        if not topic["event_date"]:
            return None, "no_event_date"
    return topic, "ok"


def verify_existing() -> int:
    """Проверяет URL у всех активных news-тем; недоступные/фейковые → rejected.
    Чистит галлюцинации из claude-эпохи (например vsrf.ru/.../doc/1234567),
    которые проходят _is_news_topic_valid (там только проверка наличия полей)."""
    if not NEWS_JSON.exists():
        print("[news-verify] нет news.json")
        return 0
    data = json.loads(NEWS_JSON.read_text(encoding="utf-8"))
    rejected = 0
    for t in data.get("topics", []) or []:
        if t.get("status") == "rejected":
            continue
        src = (t.get("primary_source") or "").strip()
        if not src or not url_ok(src):
            t["status"] = "rejected"
            t["_rejected_reason"] = "url_unreachable_or_missing"
            rejected += 1
            print(f"   reject {t.get('id')} {t.get('slug')}: src={src[:60]}")
    NEWS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[news-verify] помечено rejected: {rejected}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Еженедельный сбор news через web_search")
    ap.add_argument("--count", type=int, default=10)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--verify-existing", action="store_true",
                    help="проверить URL активных news-тем, недоступные → rejected (без сбора)")
    args = ap.parse_args()

    if args.verify_existing:
        return verify_existing()

    # Нормальный сбор: сперва бракуем фейковые/мёртвые URL среди существующих
    # тем (галлюцинации claude-эпохи), затем добираем свежие через web_search.
    print("[news-gpt] проверка URL существующих тем...")
    verify_existing()

    today = datetime.now().strftime("%Y-%m-%d")
    data = json.loads(NEWS_JSON.read_text(encoding="utf-8")) if NEWS_JSON.exists() else \
        {"category": "news", "category_label": "Новости", "generated_at": today, "topics": []}
    existing = data.get("topics", []) or []
    taken_slugs, taken_kw = collect_taken()
    print(f"[news-gpt] today={today} count={args.count} model={args.model} "
          f"занято slug={len(taken_slugs)}")

    raw_topics, raw_text = web_search_topics(args.model, build_input(args.count, today, taken_slugs))
    print(f"[news-gpt] модель вернула сырых тем: {len(raw_topics)}")
    if not raw_topics:
        (TOPIC_MAP / "_FAILED_news_collect.txt").write_text(raw_text, encoding="utf-8")
        print("[news-gpt] 0 тем (сырьё в _FAILED_news_collect.txt) — exit 1")
        return 1

    next_id = _next_id_start(existing)
    added, skipped = [], []
    for raw in raw_topics:
        if not isinstance(raw, dict):
            continue
        topic, reason = normalize(raw, f"news-{next_id:02d}", taken_slugs, taken_kw)
        if topic is None:
            skipped.append(reason)
            continue
        added.append(topic)
        taken_slugs.add(topic["slug"])
        if topic.get("main_keyword"):
            taken_kw.add(topic["main_keyword"].lower())
        next_id += 1

    print(f"[news-gpt] валидных (URL проверен, свежесть ок): {len(added)}, отсеяно: {len(skipped)}")
    for s in skipped:
        print(f"   skip: {s}")
    for t in added:
        print(f"   + {t['id']:9} {t['event_date']} [{t['news_zone']:12}] {t['slug']}")
        print(f"       src: {t['primary_source']}")

    if args.dry_run:
        print("[news-gpt] DRY-RUN — в файл не пишу")
        return 0 if added else 1
    if not added:
        print("[news-gpt] 0 валидных тем — exit 1")
        return 1

    data["topics"] = existing + added
    data["generated_at"] = today
    NEWS_JSON.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[news-gpt] added={len(added)} total={len(data['topics'])} → news.json")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
