#!/usr/bin/env python3
"""
scripts/refresh_legal_whitelist.py — еженедельная проверка и пополнение
whitelist consultant.ru URL.

Запуск: systemd timer раз в неделю (вс 03:30 МСК).
Жёсткий таймаут на весь процесс: 10 минут (kill в systemd).

Что делает:
1. Health-check всех URL из ручного и автоматического whitelist:
   - HEAD-запрос с таймаутом 10 секунд на каждый.
   - Помечает мёртвые URL в логах (но не удаляет — сама не справится исправить).
2. Поиск свежих документов:
   - Парсинг RSS pravo.gov.ru (раздел «Федеральные законы»).
   - Парсинг страницы Постановлений Пленума ВС РФ (vsrf.ru).
   - Для каждого найденного документа — попытка определить cons_doc_LAW_XXXXX
     через consultant.ru поиск (поиск по точному названию).
3. Запись результата в data/legal_whitelist_auto.json.

Логи: stdout → journalctl -u liquidator-refresh-whitelist.

Принципы устойчивости:
- НИКАКИХ зависаний > 10 сек на URL: жёсткий timeout urllib.
- НИКАКИХ зависаний > 10 минут всего скрипта: systemd прибьёт.
- При ошибках — логируем и продолжаем (НЕ роняем процесс).
- Если парсер сломался (поменялась разметка сайта) — скрипт всё равно
  выполнит health-check существующих URL и обновит _last_run.
"""
from __future__ import annotations

import json
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANUAL_FILE = ROOT / "data" / "legal_whitelist.json"
AUTO_FILE = ROOT / "data" / "legal_whitelist_auto.json"

HTTP_TIMEOUT = 10        # секунд на HTTP-запрос
MAX_RUNTIME = 600        # 10 минут — общий бюджет скрипта
USER_AGENT = "Mozilla/5.0 (LiquidatorRefresh/1.0; +https://pravo.shop/)"


def log(msg: str) -> None:
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"[{ts}] {msg}", flush=True)


def http_request(url: str, method: str = "GET", timeout: int = HTTP_TIMEOUT) -> tuple[int, bytes]:
    """Общая обёртка над urllib с user-agent и явным timeout.
    Возвращает (status_code, body). При ошибке поднимает urllib.error."""
    req = urllib.request.Request(
        url,
        method=method,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = b"" if method == "HEAD" else resp.read()
        return resp.status, body


def check_url_alive(url: str) -> tuple[bool, str]:
    """HEAD-запрос. Возвращает (alive, status_msg)."""
    try:
        status, _ = http_request(url, method="HEAD")
        return (200 <= status < 400), f"HTTP {status}"
    except urllib.error.HTTPError as e:
        # Некоторые сайты не отвечают на HEAD — пробуем GET с маленьким диапазоном.
        if e.code in (405, 501):
            try:
                status, _ = http_request(url, method="GET")
                return (200 <= status < 400), f"HTTP {status} (via GET fallback)"
            except Exception as exc:
                return False, f"GET fallback error: {exc}"
        return False, f"HTTPError {e.code}"
    except urllib.error.URLError as e:
        return False, f"URLError: {e.reason}"
    except (ConnectionError, TimeoutError) as e:
        return False, f"ConnectionError: {e}"
    except Exception as e:
        return False, f"Exception: {type(e).__name__}: {e}"


def health_check_whitelist(docs: list[dict], deadline: float) -> tuple[int, list[dict]]:
    """Проходим по всем документам whitelist и проверяем что URL живы.
    `deadline` — абсолютное время (time.time() + N), после которого выходим
    раньше срока, чтобы systemd не убил по жёсткому таймауту."""
    alive_count = 0
    dead: list[dict] = []
    for doc in docs:
        if time.time() > deadline:
            log("⏰ Бюджет времени исчерпан, прерываю health-check")
            break
        url = (doc or {}).get("url")
        doc_id = (doc or {}).get("id", "?")
        if not url:
            continue
        ok, status = check_url_alive(url)
        if ok:
            alive_count += 1
        else:
            dead.append({"id": doc_id, "url": url, "status": status})
            log(f"  [DEAD] {doc_id} = {url}: {status}")
    return alive_count, dead


# ============================================================
# Парсинг свежих документов (заглушки/каркас — расширять позже)
# ============================================================

# Источник 1: RSS pravo.gov.ru (свежие ФЗ).
# Реальный URL может потребовать уточнения после первого тестового запуска
# в production. RSS-фид: http://publication.pravo.gov.ru/api/Rss?lawClasses=...
PRAVO_GOV_RSS_URL = "http://publication.pravo.gov.ru/api/Rss"

# Источник 2: страница Постановлений Пленума ВС РФ.
VSRF_PLENUM_URL = "https://www.vsrf.ru/documents/own/?TYPE_CODE=4"


def fetch_recent_federal_laws(deadline: float) -> list[dict]:
    """Парсит RSS pravo.gov.ru и возвращает свежие ФЗ за последние 7 дней.
    На текущем этапе — заглушка, возвращает пустой список и пишет TODO.
    Реализация требует первого запуска на VPS для проверки актуальных
    URL и формата RSS."""
    log("TODO: fetch_recent_federal_laws — парсер pravo.gov.ru ещё не реализован.")
    log("      Health-check whitelist работает, новые ФЗ пока добавляются вручную.")
    return []


def fetch_recent_plenum_resolutions(deadline: float) -> list[dict]:
    """Парсит vsrf.ru/documents/own/?TYPE_CODE=4 и возвращает свежие
    Постановления Пленума ВС. Заглушка на старте."""
    log("TODO: fetch_recent_plenum_resolutions — парсер vsrf.ru ещё не реализован.")
    return []


def find_consultant_url_for_document(doc_name: str, deadline: float) -> str | None:
    """По названию документа («Постановление Пленума ВС № 50 от 15.11.2026»)
    пытается найти соответствующий cons_doc_LAW_XXXXX на consultant.ru.

    Стратегия (пока не реализована):
    1. Запрос на consultant.ru/search/?q=<название> с timeout 10 сек.
    2. Парсинг первого результата, извлечение URL вида /document/cons_doc_LAW_XXXXX/.
    3. WebFetch этого URL, проверка что title содержит ключевые слова из doc_name.

    Сейчас возвращает None — требует доработки на VPS с реальными запросами."""
    return None


# ============================================================
# Main
# ============================================================

def main() -> int:
    started = time.time()
    deadline = started + MAX_RUNTIME - 30  # 30 сек резерв на запись файла

    log("=== Запуск refresh_legal_whitelist ===")

    # 1. Загрузить оба whitelist
    if not MANUAL_FILE.exists():
        log(f"❌ Не найден {MANUAL_FILE}")
        return 1
    if not AUTO_FILE.exists():
        log(f"❌ Не найден {AUTO_FILE}")
        return 1

    try:
        manual = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
        auto = json.loads(AUTO_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log(f"❌ Ошибка чтения whitelist: {exc}")
        return 1

    manual_docs = list(manual.get("documents", []))
    auto_docs = list(auto.get("documents", []))
    all_docs = manual_docs + auto_docs
    log(f"Whitelist: ручной={len(manual_docs)}, авто={len(auto_docs)}, итого={len(all_docs)}")

    # 2. Health-check существующих URL
    log("--- Health-check существующих URL ---")
    alive, dead = health_check_whitelist(all_docs, deadline)
    log(f"Живых: {alive}/{len(all_docs)}, мёртвых: {len(dead)}")

    # 3. Поиск свежих документов (пока заглушка)
    log("--- Поиск свежих документов ---")
    new_laws = fetch_recent_federal_laws(deadline) if time.time() < deadline else []
    new_plenums = fetch_recent_plenum_resolutions(deadline) if time.time() < deadline else []
    log(f"Найдено новых ФЗ: {len(new_laws)}, ПП Пленумов: {len(new_plenums)}")

    # 4. Записать новые документы в auto-whitelist (когда парсер будет готов)
    added = 0
    for doc in new_laws + new_plenums:
        if time.time() > deadline:
            break
        # consultant_url = find_consultant_url_for_document(doc.get("name"), deadline)
        # if consultant_url:
        #     auto_docs.append({
        #         "id": doc.get("id"),
        #         "name": doc.get("name"),
        #         "cons_doc_id": ...,
        #         "url": consultant_url,
        #         "categories": ["news"],
        #         "added": datetime.now(timezone.utc).date().isoformat(),
        #         "verified": "auto: parsed from RSS",
        #     })
        #     added += 1
        pass

    # 5. Обновить _last_run и записать обратно
    auto["_last_run"] = datetime.now(timezone.utc).isoformat()
    auto["_last_status"] = {
        "alive": alive,
        "total_checked": len(all_docs),
        "dead": dead,
        "new_laws_found": len(new_laws),
        "new_plenums_found": len(new_plenums),
        "added_to_auto_whitelist": added,
        "duration_sec": round(time.time() - started, 1),
    }
    auto["documents"] = auto_docs

    try:
        AUTO_FILE.write_text(
            json.dumps(auto, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        log(f"✅ Обновлён {AUTO_FILE}")
    except OSError as exc:
        log(f"❌ Не удалось записать {AUTO_FILE}: {exc}")
        return 1

    log(f"=== Завершено за {round(time.time() - started, 1)}с ===")
    # Возвращаем 0 даже если есть мёртвые URL — это не повод для systemd считать
    # запуск проваленным. Юлия увидит факты в логах.
    return 0


if __name__ == "__main__":
    sys.exit(main())
