"""
Финализатор драфта (детерминированная часть бывшего агента 7).

Запускается из шага 9 `.claude/commands/write-article.md` СРАЗУ после
агента 7-publisher. Агент 7 теперь делает только одно: подбирает
английскую scene-строку под смысл статьи и пишет её в
`drafts/{slug}/scene.txt` одной строкой. Всё остальное - этот скрипт.

Что делает:
    1. Валидирует обязательные файлы драфта (article.html, meta.json,
       quality_gate.json).
    2. Читает scene.txt - если файла нет или он пуст, передаёт в
       image_gen `scene=None` и тот сам берёт CATEGORY_SCENE_DEFAULT
       по `meta.category`. В лог пишется WARNING - чтобы было видно
       что агент 7 промазал.
    3. Зовёт `tools.image_gen.generate_and_upload_cover` напрямую через
       import (не subprocess). При None делает один retry без паузы.
       При повторном None - помечает meta.json `cover_generation_failed=true`,
       статья всё равно финализируется.
    4. Дописывает в meta.json: `ready_for_review`, `ready_at`,
       `publication_target`.
    5. Append в `drafts/_review_queue.json`: slug, category, title,
       added_at, char_count, cover_url, status, quality_gate.
    6. В stdout одной строкой: `publisher_done slug=... cover=ok|failed`.

Выходные коды:
    0  - драфт финализирован (даже при cover_generation_failed=true).
         Scheduler закоммитит и пушнёт. Бот покажет драфт заказчику.
    1  - структурная проблема (нет article.html / нет обязательных полей
         meta.json / quality_gate hard_failed). Slot уйдёт в failed_qa,
         коммита не будет.

Запуск:
    python -m articles_scheduler.finalize_draft <slug>
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = PROJECT_ROOT / "drafts"
REVIEW_QUEUE = DRAFTS_DIR / "_review_queue.json"

REQUIRED_META_FIELDS = ("slug", "category", "title", "description", "h1", "topic_action")
MIN_ARTICLE_BYTES = 5000

log = logging.getLogger("finalize_draft")


# ============ ВАЛИДАЦИЯ ============

def _validate_draft(slug: str) -> tuple[bool, str, dict]:
    """
    Проверяет что драфт готов к финализации. Возвращает (ok, reason, meta_dict).
    Если ok=False - выходим с rc=1, scheduler пометит failed_qa.
    """
    draft_dir = DRAFTS_DIR / slug
    if not draft_dir.is_dir():
        return False, f"draft dir не найден: {draft_dir}", {}

    article_path = draft_dir / "article.html"
    if not article_path.exists():
        return False, f"article.html отсутствует: {article_path}", {}
    article_size = article_path.stat().st_size
    if article_size < MIN_ARTICLE_BYTES:
        return False, f"article.html слишком короткий: {article_size} < {MIN_ARTICLE_BYTES} байт", {}

    meta_path = draft_dir / "meta.json"
    if not meta_path.exists():
        return False, f"meta.json отсутствует: {meta_path}", {}

    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return False, f"meta.json не парсится: {exc}", {}

    missing = [f for f in REQUIRED_META_FIELDS if not meta.get(f)]
    if missing:
        return False, f"в meta.json нет обязательных полей: {missing}", {}

    if meta.get("factcheck_passed") is False:
        return False, "factcheck_passed=false - драфт не финализируется", {}

    qg_path = draft_dir / "quality_gate.json"
    if qg_path.exists():
        try:
            qg = json.loads(qg_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return False, f"quality_gate.json не парсится: {exc}", {}
        # С 8 мая 2026: блокирует только hard_failed (структурный сбой).
        # Метрические fail не блокируют - заказчик решит сам в боте.
        if qg.get("hard_failed"):
            return False, f"quality_gate.hard_failed=true: {qg.get('blockers')}", {}

    return True, "ok", meta


# ============ SCENE ============

def _read_scene(slug: str) -> Optional[str]:
    """
    Читает drafts/{slug}/scene.txt. None если файла нет / пуст.
    Лог уровня WARNING если файла нет (агент 7 не отработал) -
    дальше image_gen возьмёт CATEGORY_SCENE_DEFAULT.
    """
    scene_path = DRAFTS_DIR / slug / "scene.txt"
    if not scene_path.exists():
        log.warning("scene.txt отсутствует для slug=%s - агент 7 не записал scene. "
                    "Используем CATEGORY_SCENE_DEFAULT по category.", slug)
        return None
    try:
        scene = scene_path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("scene.txt не читается для slug=%s: %s", slug, exc)
        return None
    if not scene:
        log.warning("scene.txt пустой для slug=%s - fallback на CATEGORY_SCENE_DEFAULT", slug)
        return None
    # Нормализуем многострочный файл в одну строку (если агент случайно вписал перевод строки)
    scene = " ".join(scene.split())
    return scene


# ============ ОБЛОЖКА ============

def _ensure_cover(slug: str, meta: dict) -> tuple[bool, dict]:
    """
    Гарантирует наличие cover_url в meta.json. Один retry при провале.
    Возвращает (cover_ok, fresh_meta).

    Логика:
        - если cover_url уже есть в meta (например, повторный запуск
          финализатора на том же драфте) - ничего не делаем, OK;
        - иначе зовём image_gen.generate_and_upload_cover;
        - перечитываем meta, если cover_url появился - OK;
        - если нет - retry один раз;
        - если опять нет - пишем cover_generation_failed=true,
          возвращаем cover_ok=False.
    """
    if meta.get("cover_url"):
        log.info("cover_url уже записан в meta.json (повторный запуск?) - пропускаем генерацию")
        return True, meta

    title = meta.get("title") or meta.get("h1") or slug
    category = meta.get("category") or "fiz"
    scene = _read_scene(slug)

    # Импорт здесь, чтобы скрипт можно было импортировать без fal/cloudinary
    # установленных (для тестов валидации).
    from tools import image_gen

    for attempt in (1, 2):
        log.info("image_gen attempt %d for slug=%s (scene=%s)",
                 attempt, slug, "custom" if scene else "category-fallback")
        try:
            url = image_gen.generate_and_upload_cover(
                slug=slug,
                title=title,
                category=category,
                scene=scene,
                write_meta=True,
            )
        except Exception as exc:
            log.exception("image_gen упал на попытке %d: %s", attempt, exc)
            url = None

        # Перечитываем meta.json - image_gen дописывает cover_url туда сам
        try:
            meta = json.loads((DRAFTS_DIR / slug / "meta.json").read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("meta.json не перечитывается после image_gen")

        if meta.get("cover_url"):
            log.info("cover готов на попытке %d: %s", attempt, meta["cover_url"])
            return True, meta

        log.warning("image_gen attempt %d не записал cover_url (returned %r)", attempt, url)

    # Обе попытки провалились - помечаем failed, но не блокируем pipeline
    log.error("Обложка не сгенерирована после 2 попыток для slug=%s, "
              "помечаем cover_generation_failed=true", slug)
    meta["cover_generation_failed"] = True
    meta["cover_generation_error"] = "image_gen returned None on both attempts"
    _write_meta(slug, meta)
    return False, meta


def _write_meta(slug: str, meta: dict) -> None:
    """Атомарная перезапись meta.json."""
    meta_path = DRAFTS_DIR / slug / "meta.json"
    tmp = meta_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(meta_path)


# ============ ФИНАЛИЗАЦИЯ ============

def _now_iso_microseconds() -> str:
    """ISO timestamp с микросекундами без TZ - формат как у существующих
    записей в _review_queue.json (см. drafts/_review_queue.json)."""
    return datetime.now().replace(tzinfo=None).isoformat()


def _now_iso_z() -> str:
    """ISO timestamp в UTC с суффиксом Z - формат meta-полей."""
    return (datetime.now(timezone.utc)
            .replace(microsecond=0).isoformat().replace("+00:00", "Z"))


def _stamp_ready_for_review(slug: str, meta: dict) -> dict:
    """
    Дописывает в meta.json публикационные поля.
    cover_url / cover_url_master / image_prompt / cover_uploaded_at
    уже записаны самим image_gen.py - их не трогаем.
    """
    meta["ready_for_review"] = True
    meta["ready_at"] = _now_iso_z()
    meta["publication_target"] = "telegram_review"
    _write_meta(slug, meta)
    return meta


def _append_to_review_queue(slug: str, meta: dict) -> None:
    """
    Добавляет запись в drafts/_review_queue.json. Если файла нет - создаёт
    как `{"items": []}`. Идемпотентен: если запись с этим slug уже есть -
    обновляет её на месте, не дублирует.
    """
    if REVIEW_QUEUE.exists():
        try:
            data = json.loads(REVIEW_QUEUE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            log.exception("_review_queue.json повреждён - пересоздаю")
            data = {"items": []}
    else:
        data = {"items": []}

    if not isinstance(data, dict) or "items" not in data or not isinstance(data["items"], list):
        data = {"items": []}

    qg = {
        "passed": bool(meta.get("quality_gate_passed")),
        "hard_failed": bool(meta.get("quality_gate_hard_failed")),
        "exit_code": meta.get("quality_gate_exit_code"),
        "note": meta.get("quality_gate_note"),
    }
    # Чистим None-поля чтобы не загромождать json
    qg = {k: v for k, v in qg.items() if v is not None}

    entry = {
        "slug": slug,
        "category": meta.get("category"),
        "title": meta.get("title"),
        "added_at": _now_iso_microseconds(),
        "char_count": meta.get("text_chars"),
        "cover_url": meta.get("cover_url"),
        "status": "ready_for_review",
        "quality_gate": qg,
    }
    if meta.get("cover_generation_failed"):
        entry["cover_generation_failed"] = True

    # Идемпотентность - удаляем старую запись с этим slug если была
    items = [it for it in data["items"] if it.get("slug") != slug]
    items.append(entry)
    data["items"] = items

    tmp = REVIEW_QUEUE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(REVIEW_QUEUE)
    log.info("review_queue: записан/обновлён slug=%s (всего items=%d)", slug, len(items))


# ============ MAIN ============

def finalize(slug: str) -> int:
    """
    Возвращает код возврата для CLI (0 - ok, 1 - структурная ошибка).
    """
    started = time.time()

    ok, reason, meta = _validate_draft(slug)
    if not ok:
        log.error("Валидация упала для slug=%s: %s", slug, reason)
        print(f"FAIL slug={slug} reason={reason}", file=sys.stderr)
        return 1

    cover_ok, meta = _ensure_cover(slug, meta)
    meta = _stamp_ready_for_review(slug, meta)
    _append_to_review_queue(slug, meta)

    duration = round(time.time() - started, 1)
    cover_status = "ok" if cover_ok else "failed"
    log.info("publisher_done slug=%s ready_for_review=true cover=%s duration=%.1fs",
             slug, cover_status, duration)
    print(f"publisher_done slug={slug} ready_for_review=true cover={cover_status}")
    return 0


def _cli() -> None:
    # .env подхватываем как и в image_gen - чтобы FAL_KEY/CLOUDINARY_*
    # были доступны при ручном запуске.
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(PROJECT_ROOT / ".env")
    except ImportError:
        pass

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Финализирует драфт после агента 7-publisher: "
                    "генерирует обложку, пишет ready_for_review, "
                    "добавляет в _review_queue.json."
    )
    parser.add_argument("slug", help="slug статьи (drafts/<slug>/)")
    args = parser.parse_args()

    sys.exit(finalize(args.slug))


if __name__ == "__main__":
    _cli()
