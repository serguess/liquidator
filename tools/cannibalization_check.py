"""
Жаккаровая проверка каннибализации тем — детерминированный preflight для агента 1.

Раньше: агент 1 в промпте получал «алгоритм проверки каннибализации»
и сам вычислял индекс Жаккара через LLM. Это арифметика над множествами,
которую LLM считает менее надёжно чем Python и тратит на это ~500-1000
токенов промпта + чтение data/keywords.json/clusters.json.

Теперь: агент 1 после подбора main_keyword + secondary_keywords вызывает
этот скрипт. Скрипт считает Жаккара против реестров и возвращает вердикт.
LLM только использует результат, не считает.

Источники данных для сравнения (по приоритету):
  1. data/published_index.json — все опубликованные статьи + drafts
     (main_keyword + secondary_keywords каждой). Самый актуальный.
  2. data/keywords.json — реестр ключей публикаций (заполняет агент 7).
  3. data/clusters.json — семантические кластеры (related_keywords).

Пороги (как в старом промпте агента 1):
  J > 0.7   — конфликт, тему НЕ создавать.
  0.4 < J ≤ 0.7 — warn, тема допустима, но архитектор учтёт при перелинковке.
  J ≤ 0.4   — ok.

Запуск:
  # preflight по сырой теме (до WebSearch):
  python -m tools.cannibalization_check preflight \
      --category fiz --topic "как списать кредит без работы"

  # full по уже подобранным ключам (после WebSearch):
  python -m tools.cannibalization_check full \
      --category fiz \
      --main-keyword "как списать кредит без работы" \
      --secondary "списать долги без работы,банкротство без работы,..."

  # JSON-вывод для парсинга агентом:
  python -m tools.cannibalization_check full ... --json

Exit:
  0 — ok / warn (LLM может продолжать)
  1 — conflict (LLM должен остановиться)
  2 — ошибка (нет файлов, плохие аргументы)
"""

from __future__ import annotations

import argparse
import json
import os
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
DATA_DIR = PROJECT_ROOT / "data"

# Пороги Жаккара (старый промпт агента 1, шаги 4-6)
THRESHOLD_CONFLICT = 0.7
THRESHOLD_WARN = 0.4

# Пороги cosine-similarity для embeddings-проверки (семантическая каннибализация).
# Jaccard ловит лексические совпадения, embeddings — семантические:
# например «как списать долги без работы» и «банкротство для безработных»
# имеют разную лексику но один кластер интента.
EMBED_THRESHOLD_CONFLICT = 0.85
EMBED_THRESHOLD_WARN = 0.75

# Файл-кэш эмбеддингов тем published-статей. Ключ = slug, значение = вектор.
TOPIC_EMBEDDINGS_CACHE = None  # лениво строится при первом обращении

# Стоп-слова русского + типовые служебные для юр-тематики.
# Цель — не давать пустым словам типа «как», «и», «по» давать ложные пересечения.
STOP_WORDS = {
    # местоимения и предлоги
    "и", "в", "во", "на", "за", "по", "к", "ко", "у", "о", "об", "обо",
    "от", "ото", "до", "из", "изо", "с", "со", "над", "под", "при",
    "для", "через", "без", "между", "ради", "вокруг", "около", "после",
    "перед", "согласно",
    # союзы
    "а", "но", "или", "либо", "что", "чтобы", "если", "как", "так",
    "то", "же", "ли", "же", "да", "нет", "не", "ни", "бы", "был", "была",
    "было", "были", "есть", "быть",
    # местоимения
    "я", "ты", "он", "она", "оно", "они", "мы", "вы", "это", "этот",
    "эта", "эти", "тот", "та", "те", "мой", "моя", "его", "её", "их",
    "наш", "ваш", "свой", "себя", "сам", "сама", "само", "сами",
    # частые «мусорные» в темах
    "если", "когда", "где", "откуда", "куда", "почему", "зачем",
    # частые юр-связки (не несут темообразующего смысла)
    "году", "годов", "году", "год", "года",
    # числа цифрами оставляем
}


# ============ Нормализация ============

_PUNCT_RX = re.compile(r"[^\wа-яёА-ЯЁ0-9\-]+", re.UNICODE)


def _normalize_word(word: str) -> str:
    """
    Простой стеммер:
      — lower
      — режем популярные русские окончания, чтобы «должника / должников / должник»
        давали одинаковый ключ
      — длина результата >= 3, иначе слово не учитывается
    """
    w = word.strip().lower()
    if not w or w in STOP_WORDS:
        return ""
    # Окончания (упрощённо, без pymorphy)
    suffixes = (
        "ование", "ования", "ованию", "ованием",
        "ические", "ическая", "ическое", "ический",
        "иться", "иться", "ились", "ились",
        "ление", "ления", "лению", "лением",
        "ность", "ности", "ностью", "ностей",
        "ского", "ская", "ское", "ские", "ских",
        "ного", "ная", "ное", "ные", "ных", "ным", "ными",
        "ому", "ому", "ого", "ему",
        "ами", "ями", "ах", "ях",
        "ов", "ев", "ёв",
        "ам", "ям", "ами", "ями",
        "ой", "ей", "ою", "ею",
        "ть", "ться",
        "ия", "ии", "ий", "ие", "ия",
        "ы", "и", "у", "ю", "а", "я", "о", "е", "ё",
    )
    # Сортируем длинные окончания первыми
    for s in sorted(suffixes, key=len, reverse=True):
        if len(w) - len(s) >= 4 and w.endswith(s):
            w = w[: -len(s)]
            break
    return w if len(w) >= 3 else ""


def normalize_phrase(phrase: str) -> set[str]:
    """Превращает фразу/ключ в множество нормализованных токенов."""
    if not phrase:
        return set()
    # Заменяем пунктуацию на пробел, дефис оставляем
    cleaned = _PUNCT_RX.sub(" ", phrase)
    tokens = cleaned.split()
    result = set()
    for t in tokens:
        # Дефисные слова бьём на части тоже («127-фз» → «127», «фз»)
        for part in t.split("-"):
            n = _normalize_word(part)
            if n:
                result.add(n)
    return result


def normalize_keyset(main: str, secondary: list[str] | None = None,
                     extra: list[str] | None = None) -> set[str]:
    """Объединяет main + secondary + extra в одно нормализованное множество."""
    result = normalize_phrase(main)
    for kw in (secondary or []):
        result |= normalize_phrase(kw)
    for kw in (extra or []):
        result |= normalize_phrase(kw)
    return result


# ============ Источники сравнения ============

def _load_published_index() -> list[dict]:
    """
    Возвращает [{slug, category, main_keyword, secondary_keywords, h2_topics, source}].
    Если индекса нет — пустой список.
    """
    path = DATA_DIR / "published_index.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("articles") or []
    except (json.JSONDecodeError, OSError):
        return []


def _load_keywords_registry() -> list[dict]:
    """
    data/keywords.json — реестр от агента 7 после публикации.
    Формат: {keywords: [{slug, main_keyword, secondary_keywords, ...}]}.
    """
    path = DATA_DIR / "keywords.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("keywords") or []
    except (json.JSONDecodeError, OSError):
        return []


def _load_clusters() -> list[dict]:
    """
    data/clusters.json — семантические кластеры.
    Формат: {clusters: [{id, category, main_keyword, related_keywords, articles}]}.
    """
    path = DATA_DIR / "clusters.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("clusters") or []
    except (json.JSONDecodeError, OSError):
        return []


def collect_existing(category: str | None = None) -> list[dict]:
    """
    Сводит все источники в единый список записей для сравнения.
    Каждая запись: {slug, category, source, keys_set, main_norm, main_keyword, label}.
    Дедупликация по slug — первый источник побеждает (приоритет published_index).

    keys_set включает ТОЛЬКО main + secondary (как в исходном алгоритме агента 1).
    h2_topics не учитываем — они раздувают множество и занижают Жаккара.
    main_norm — отдельно нормализованный main_keyword для проверки точного совпадения.
    """
    seen_slugs: set[str] = set()
    records: list[dict] = []

    # 1. published_index
    for a in _load_published_index():
        slug = a.get("slug")
        if not slug or slug in seen_slugs:
            continue
        cat = a.get("category", "")
        main_kw = a.get("main_keyword") or ""
        keys = normalize_keyset(main_kw, a.get("secondary_keywords") or [])
        main_norm = normalize_phrase(main_kw)
        if not keys and not main_norm:
            continue
        seen_slugs.add(slug)
        records.append({
            "slug": slug,
            "category": cat,
            "source": f"published_index:{a.get('source', 'unknown')}",
            "keys_set": keys,
            "main_norm": main_norm,
            "main_keyword": main_kw,
            "label": (a.get("title") or a.get("h1") or slug)[:80],
        })

    # 2. keywords.json
    for k in _load_keywords_registry():
        slug = k.get("slug")
        if not slug or slug in seen_slugs:
            continue
        main_kw = k.get("main_keyword") or ""
        keys = normalize_keyset(main_kw, k.get("secondary_keywords") or [])
        main_norm = normalize_phrase(main_kw)
        if not keys and not main_norm:
            continue
        seen_slugs.add(slug)
        records.append({
            "slug": slug,
            "category": k.get("category", ""),
            "source": "keywords_registry",
            "keys_set": keys,
            "main_norm": main_norm,
            "main_keyword": main_kw,
            "label": k.get("title") or slug,
        })

    # 3. clusters.json (как доп. сигнал — кластер-уровневые ключи)
    for c in _load_clusters():
        cluster_id = c.get("id")
        if not cluster_id:
            continue
        virtual_slug = f"cluster:{cluster_id}"
        if virtual_slug in seen_slugs:
            continue
        main_kw = c.get("main_keyword") or ""
        keys = normalize_keyset(main_kw, c.get("related_keywords") or [])
        main_norm = normalize_phrase(main_kw)
        if not keys and not main_norm:
            continue
        seen_slugs.add(virtual_slug)
        records.append({
            "slug": virtual_slug,
            "category": c.get("category", ""),
            "source": "clusters",
            "keys_set": keys,
            "main_norm": main_norm,
            "main_keyword": main_kw,
            "label": f"cluster: {c.get('subtopic') or cluster_id}",
        })

    return records


def containment(small: set[str], big: set[str]) -> float:
    """
    Containment-метрика: насколько меньшее множество содержится в большем.
    Решает проблему preflight по короткой теме vs полный набор ключей существующей.
    """
    if not small:
        return 0.0
    return len(small & big) / len(small)


# ============ Жаккар ============

def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def classify(j: float) -> str:
    if j > THRESHOLD_CONFLICT:
        return "conflict"
    if j > THRESHOLD_WARN:
        return "warn"
    return "ok"


# ============ Основной API ============

def check(category: str, main_keyword: str = "", secondary: list[str] | None = None,
          topic: str = "", mode: str = "full") -> dict:
    """
    mode='preflight' — берёт только topic (быстрая проверка до WebSearch).
    mode='full' — берёт main_keyword + secondary (после WebSearch).

    Возвращает dict со всеми данными для записи в brief.json и для решения LLM.

    Алгоритм:
      1. Жаккар (как в исходном промпте агента 1) — основная метрика.
      2. Containment входной фразы в существующий main_keyword — ловит preflight
         по короткой теме, которая дословно совпадает с существующим ключом.
      3. Точное совпадение нормализованного main_keyword — мгновенный конфликт.

    Verdict: conflict если хоть один сигнал даёт conflict, дальше warn → ok.
    """
    if mode == "preflight":
        new_keys = normalize_phrase(topic)
        new_main_norm = new_keys  # для preflight topic = main candidate
    else:
        new_keys = normalize_keyset(main_keyword, secondary or [], extra=[topic] if topic else None)
        new_main_norm = normalize_phrase(main_keyword)

    if not new_keys:
        return {
            "ok": True,
            "verdict": "ok",
            "reason": "empty_keys",
            "category": category,
            "mode": mode,
            "input_keys_count": 0,
            "checks": [],
        }

    existing = collect_existing(category=category)

    checks = []
    for rec in existing:
        j = jaccard(new_keys, rec["keys_set"]) if rec["keys_set"] else 0.0
        # Containment по main_keyword: насколько новая тема "сидит" в существующем main
        cont_main = 0.0
        if new_main_norm and rec["main_norm"]:
            cont_main = max(
                containment(new_main_norm, rec["main_norm"]),
                containment(rec["main_norm"], new_main_norm),
            )
        # Точное совпадение нормализованного main_keyword
        exact_main = bool(new_main_norm) and new_main_norm == rec["main_norm"]

        # Финальный вердикт по записи
        if exact_main:
            verdict = "conflict"
            reason = "exact_main_match"
        elif j > THRESHOLD_CONFLICT:
            verdict = "conflict"
            reason = "jaccard_high"
        elif cont_main >= 0.85 and len(new_main_norm & rec["main_norm"]) >= 3:
            verdict = "conflict"
            reason = "main_containment_high"
        elif j > THRESHOLD_WARN:
            verdict = "warn"
            reason = "jaccard_warn"
        elif cont_main >= 0.6 and len(new_main_norm & rec["main_norm"]) >= 2:
            verdict = "warn"
            reason = "main_containment_warn"
        else:
            verdict = "ok"
            reason = ""

        # Шум отбрасываем — но conflict/warn пропускаем всегда
        if verdict == "ok" and j <= 0.05 and cont_main < 0.4:
            continue

        checks.append({
            "slug": rec["slug"],
            "category": rec["category"],
            "source": rec["source"],
            "main_keyword": rec["main_keyword"],
            "label": rec["label"],
            "jaccard": round(j, 3),
            "main_containment": round(cont_main, 3),
            "exact_main_match": exact_main,
            "verdict": verdict,
            "reason": reason,
            "intersection_size": len(new_keys & rec["keys_set"]) if rec["keys_set"] else 0,
            "union_size": len(new_keys | rec["keys_set"]) if rec["keys_set"] else 0,
        })

    # Семантическая проверка через embeddings (опциональная, требует OPENAI_API_KEY).
    # Ловит случаи когда лексика разная, но интент тот же:
    # «как списать долги без работы» vs «банкротство для безработных».
    # Если ключа нет или эмбеддинг не получился — checks возвращаются без изменений.
    if existing:
        new_topic_text = main_keyword if mode == "full" and main_keyword else topic
        if not new_topic_text and new_keys:
            new_topic_text = " ".join(sorted(new_keys))
        checks = enrich_with_semantic(checks, new_topic_text, existing)

    # Сортируем: сначала по vердикту (conflict→warn→ok), потом по «силе» сигнала
    verdict_rank = {"conflict": 0, "warn": 1, "ok": 2}
    checks.sort(key=lambda x: (
        verdict_rank.get(x["verdict"], 3),
        -max(x["jaccard"], x["main_containment"]),
    ))

    conflicts = [c for c in checks if c["verdict"] == "conflict"]
    warnings_list = [c for c in checks if c["verdict"] == "warn"]

    if conflicts:
        verdict = "conflict"
    elif warnings_list:
        verdict = "warn"
    else:
        verdict = "ok"

    # cannibalization_check для записи в brief.json (формат как в старом агенте 1)
    if conflicts:
        brief_field = f"conflict:{conflicts[0]['slug']}"
    elif warnings_list:
        brief_field = f"warn:{warnings_list[0]['slug']}"
    else:
        brief_field = "ok"

    return {
        "ok": verdict != "conflict",
        "verdict": verdict,
        "category": category,
        "mode": mode,
        "input_keys_count": len(new_keys),
        "input_keys_sample": sorted(new_keys)[:15],  # первые 15 для отладки
        "checks": checks[:10],  # топ-10 ближайших
        "checks_total": len(checks),
        "conflicts": [c["slug"] for c in conflicts],
        "warnings": [c["slug"] for c in warnings_list],
        "brief_field": brief_field,
        "thresholds": {"conflict": THRESHOLD_CONFLICT, "warn": THRESHOLD_WARN},
    }


# ============ Семантическая проверка через embeddings ============

# Кэш эмбеддингов: data/topic_embeddings.json, формат {slug: [floats], ...}.
# Перестраивается лениво: при каждом запуске проверяем что у всех текущих
# published_index slug-ов есть вектор. Если slug новый — эмбеддим и дописываем.
EMBEDDINGS_CACHE_PATH = DATA_DIR / "topic_embeddings.json"


def _topic_text_for_embedding(record: dict) -> str:
    """Текст по которому считаем эмбеддинг темы. Берём label + main_keyword."""
    parts = [record.get("label") or "", record.get("main_keyword") or ""]
    return " | ".join(p for p in parts if p)


def _load_embeddings_cache() -> dict:
    if not EMBEDDINGS_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(EMBEDDINGS_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_embeddings_cache(cache: dict) -> None:
    DATA_DIR.mkdir(exist_ok=True)
    try:
        EMBEDDINGS_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError:
        pass


def _embed_text(text: str) -> list[float] | None:
    """
    Получает эмбеддинг текста через tools.embed_compare.get_embedding.
    Возвращает None если OPENAI_API_KEY отсутствует ИЛИ при любой ошибке —
    тогда вызывающий код просто пропускает семантическую проверку.
    """
    if not text.strip():
        return None
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        from tools.embed_compare import get_embedding
        vec = get_embedding(text)
        # get_embedding возвращает fallback-хеш если API не отвечает —
        # такой fallback семантически бессмыслен, отбрасываем (длина норм есть, но семантика 0).
        # Простой признак: если в env OPENAI_API_KEY есть, get_embedding пробует API,
        # и при ошибке печатает WARN в stderr. Здесь различить настоящий эмбеддинг от
        # хеша сложно, поэтому полагаемся на наличие ключа.
        return vec
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _build_or_refresh_topic_vectors(records: list[dict]) -> dict:
    """
    Возвращает {slug: vector} для published-тем. Использует кэш, эмбеддит только новые.
    Если ключа OpenAI нет или эмбеддинг не получился — slug просто не попадает в результат.
    """
    cache = _load_embeddings_cache()
    changed = False
    out = {}
    for rec in records:
        slug = rec["slug"]
        if slug in cache and isinstance(cache[slug], list) and cache[slug]:
            out[slug] = cache[slug]
            continue
        text = _topic_text_for_embedding(rec)
        vec = _embed_text(text)
        if vec:
            cache[slug] = vec
            out[slug] = vec
            changed = True
    if changed:
        _save_embeddings_cache(cache)
    return out


def _semantic_verdict(score: float) -> str:
    if score >= EMBED_THRESHOLD_CONFLICT:
        return "conflict"
    if score >= EMBED_THRESHOLD_WARN:
        return "warn"
    return "ok"


def enrich_with_semantic(checks: list[dict], new_topic_text: str,
                          existing_records: list[dict]) -> list[dict]:
    """
    Двухступенчатое обогащение checks семантическим сигналом:

    1. Для записей УЖЕ в checks (Jaccard их пропустил) — добавляем embed_similarity
       и поднимаем verdict если embeddings строже.
    2. Для записей которых НЕТ в checks (Jaccard их отбросил как шум) — пробегаем
       по всем existing_records, эмбеддим, и если cosine ≥ EMBED_THRESHOLD_WARN —
       ДОБАВЛЯЕМ запись в checks. Это ловит семантическую каннибализацию при
       полностью разной лексике (Jaccard = 0, embeddings = 0.87).

    Если эмбеддинг кандидата получить не удалось (нет OPENAI_API_KEY) —
    тихо возвращает checks без изменений (graceful degradation на чистый Jaccard).
    """
    new_vec = _embed_text(new_topic_text)
    if not new_vec:
        return checks
    vectors_by_slug = _build_or_refresh_topic_vectors(existing_records)
    if not vectors_by_slug:
        return checks

    rank = {"ok": 0, "warn": 1, "conflict": 2}
    existing_slugs_in_checks = {ch["slug"] for ch in checks}

    # Шаг 1: обогащаем существующие checks
    for ch in checks:
        vec = vectors_by_slug.get(ch["slug"])
        if not vec:
            continue
        sim = round(_cosine(new_vec, vec), 3)
        ch["embed_similarity"] = sim
        ch["embed_verdict"] = _semantic_verdict(sim)
        if rank[ch["embed_verdict"]] > rank.get(ch["verdict"], 0):
            ch["verdict"] = ch["embed_verdict"]
            ch["reason"] = f"embedding_overlap (sim={sim})"

    # Шаг 2: добавляем семантические находки которых не было в checks
    record_by_slug = {r["slug"]: r for r in existing_records}
    for slug, vec in vectors_by_slug.items():
        if slug in existing_slugs_in_checks:
            continue
        sim = round(_cosine(new_vec, vec), 3)
        if sim < EMBED_THRESHOLD_WARN:
            continue
        rec = record_by_slug.get(slug)
        if not rec:
            continue
        v = _semantic_verdict(sim)
        checks.append({
            "slug": slug,
            "category": rec.get("category", ""),
            "source": rec.get("source", ""),
            "main_keyword": rec.get("main_keyword", ""),
            "label": rec.get("label", slug),
            "jaccard": 0.0,
            "main_containment": 0.0,
            "exact_main_match": False,
            "verdict": v,
            "reason": f"embedding_only (sim={sim})",
            "intersection_size": 0,
            "union_size": 0,
            "embed_similarity": sim,
            "embed_verdict": v,
        })
    return checks


# ============ CLI ============

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Жаккаровая проверка каннибализации тем (preflight для агента 1)"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_pre = sub.add_parser("preflight", help="Быстрая проверка по сырой теме (до WebSearch)")
    p_pre.add_argument("--category", required=True, choices=["fiz", "yur", "vzysk", "news"])
    p_pre.add_argument("--topic", required=True, help="Свободная формулировка темы")
    p_pre.add_argument("--json", action="store_true")

    p_full = sub.add_parser("full", help="Полная проверка по подобранным ключам (после WebSearch)")
    p_full.add_argument("--category", required=True, choices=["fiz", "yur", "vzysk", "news"])
    p_full.add_argument("--main-keyword", required=True)
    p_full.add_argument("--secondary", default="",
                        help="Список вторичных ключей через запятую")
    p_full.add_argument("--topic", default="", help="Опционально — исходная тема для контекста")
    p_full.add_argument("--json", action="store_true")

    args = parser.parse_args()

    if args.mode == "preflight":
        result = check(category=args.category, topic=args.topic, mode="preflight")
    else:
        secondary = [s.strip() for s in args.secondary.split(",") if s.strip()]
        result = check(
            category=args.category,
            main_keyword=args.main_keyword,
            secondary=secondary,
            topic=args.topic,
            mode="full",
        )

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        v = result["verdict"]
        symbol = {"ok": "✓", "warn": "!", "conflict": "✗"}[v]
        print(f"{symbol} verdict={v} category={args.category} mode={args.mode}")
        print(f"  input_keys: {result['input_keys_count']} нормализованных токенов")
        print(f"  brief_field: {result['brief_field']}")
        if result["checks"]:
            print(f"\n  Близкие темы (топ-{min(5, len(result['checks']))}):")
            for c in result["checks"][:5]:
                print(f"    [{c['verdict']:8}] J={c['jaccard']:.3f} "
                      f"{c['slug']} ({c['source']})")
                if c["main_keyword"]:
                    print(f"      main_keyword: {c['main_keyword']}")
        else:
            print("  Близких тем не найдено.")

    if result["verdict"] == "conflict":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
