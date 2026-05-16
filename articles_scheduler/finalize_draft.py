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
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo  # py 3.9+
    _MSK_TZ = ZoneInfo("Europe/Moscow")
except Exception:
    _MSK_TZ = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = PROJECT_ROOT / "drafts"
REVIEW_QUEUE = DRAFTS_DIR / "_review_queue.json"
COVER_SCENES_MD = PROJECT_ROOT / ".claude" / "style" / "cover-scenes.md"

REQUIRED_META_FIELDS = ("slug", "category", "title", "description", "h1", "topic_action")
MIN_ARTICLE_BYTES = 5000

# Регекс 4-значного года 19xx-20xx как отдельного слова. Используется
# хард-валидатором ниже: для category=news в title и h1 год запрещён
# (зафиксировано заказчиком 9 мая 2026 — статья с годом мгновенно
# выглядит «старой» в поиске и через год). Документы можно упоминать
# только без года: «обзор Верховного суда», «ФЗ-259», «постановление
# Пленума №40» — без «от 17.12.2024» и без «№5/2026».
_YEAR_IN_HEADLINE_RE = re.compile(r"\b(19|20)\d{2}\b")

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

    # Хард-проверка: для category=news запрещён 4-значный год в title и h1.
    # Заказчик зафиксировала правило 9 мая 2026 после кейса
    # «Обзор ВС РФ №5/2026 по банкротству» — статья с годом сразу
    # выглядит устаревшей. Если правило нарушено — слот падает,
    # scheduler возьмёт следующую тему. Это страховка от ошибок
    # агента 1 / 6, которые формируют title и h1.
    if (meta.get("category") or "").lower() == "news":
        offenders = []
        for field_name in ("title", "h1"):
            value = meta.get(field_name) or ""
            match = _YEAR_IN_HEADLINE_RE.search(value)
            if match:
                offenders.append(f"{field_name}={value!r} содержит год {match.group()!r}")
        if offenders:
            return False, (
                "category=news запрещает 4-значный год в title/h1; найдено: "
                + "; ".join(offenders)
                + ". Правило: см. .claude/agents/6-seo-editor.md §2.1, "
                ".claude/agents/1-semantics.md шаг 10, "
                ".claude/commands/expand-topics.md."
            ), {}

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

def _read_scene_template_id(slug: str) -> Optional[int]:
    """
    Читает drafts/{slug}/scene_template.txt (формат `template_id=N`).
    None если файла нет / парсится плохо.
    """
    path = DRAFTS_DIR / slug / "scene_template.txt"
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    m = re.search(r"template_id\s*=\s*(\d+)", raw)
    if not m:
        return None
    try:
        tid = int(m.group(1))
    except ValueError:
        return None
    if 1 <= tid <= 30:
        return tid
    return None


def _pick_scene_template_fallback(slug: str, category: Optional[str]) -> Optional[int]:
    """
    Если scene_template.txt отсутствует (например, /finish-article запустился
    после ручного восстановления), зовём pick_scene_template как страховку.
    Возвращает выбранный template_id или None при сбое.
    """
    import subprocess
    try:
        res = subprocess.run(
            [sys.executable, "-m", "articles_scheduler.pick_scene_template",
             slug, category or ""],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("pick_scene_template fallback упал для slug=%s: %s", slug, exc)
        return None
    if res.returncode != 0:
        log.warning("pick_scene_template fallback rc=%d для slug=%s: %s",
                    res.returncode, slug, res.stderr.strip())
        return None
    return _read_scene_template_id(slug)


_TEMPLATE_HEADER_RX = re.compile(r"^###\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE)


def _read_template_scene_from_catalog(template_id: int) -> Optional[str]:
    """
    Извлекает блок `**Template:** ...` из `.claude/style/cover-scenes.md`
    для указанного template_id. Возвращает английскую строку scene.
    None если каталог не читается или нет такого ID.
    """
    if not COVER_SCENES_MD.exists():
        log.warning("cover-scenes.md не найден: %s", COVER_SCENES_MD)
        return None
    try:
        text = COVER_SCENES_MD.read_text(encoding="utf-8")
    except OSError as exc:
        log.warning("cover-scenes.md не читается: %s", exc)
        return None

    headers = list(_TEMPLATE_HEADER_RX.finditer(text))
    target_idx = None
    for i, m in enumerate(headers):
        if int(m.group(1)) == template_id:
            target_idx = i
            break
    if target_idx is None:
        return None

    start = headers[target_idx].end()
    end = headers[target_idx + 1].start() if target_idx + 1 < len(headers) else len(text)
    section = text[start:end]

    # Ищем строку `**Template:** ...` и захватываем её до следующего `**Best for:**`
    # или пустой строки за абзацем.
    m = re.search(r"\*\*Template:\*\*\s*(.+?)(?:\n\n|\n\s*\*\*Best for:)", section, re.DOTALL)
    if not m:
        return None
    scene = " ".join(m.group(1).split())
    return scene or None


def _read_scene(slug: str, category: Optional[str] = None) -> Optional[str]:
    """
    Возвращает английскую scene-строку для image_gen.

    Приоритет источников (с 16 мая 2026):
      1. drafts/{slug}/scene.txt - адаптированная LLM-агентом 7 сцена
         (объекты из allowed pool подогнаны под смысл статьи).
      2. drafts/{slug}/scene_template.txt - детерминированно выбранный
         pick_scene_template.py template_id (1-30). Достаём `**Template:**`
         из .claude/style/cover-scenes.md.
      3. Если scene_template.txt тоже нет — на лету зовём
         pick_scene_template.py и берём результат.
      4. None — image_gen возьмёт CATEGORY_SCENE_DEFAULT (последний рубеж).

    Раньше шагов 2-3 не было: при отсутствии scene.txt сразу падали в
    4 категорийных дефолта (fiz=10, yur=25, vzysk=3, news=12), что
    превращало 30 сцен каталога в 4 повторяющихся. Зафиксировано
    16 мая 2026 на свежих fiz-статьях, у всех был flat-lay шаблон 10.
    """
    scene_path = DRAFTS_DIR / slug / "scene.txt"
    if scene_path.exists():
        try:
            scene = scene_path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            log.warning("scene.txt не читается для slug=%s: %s", slug, exc)
            scene = ""
        if scene:
            return " ".join(scene.split())
        log.warning("scene.txt пустой для slug=%s - падаем на template-каталог", slug)
    else:
        log.warning("scene.txt отсутствует для slug=%s - агент 7 не записал. "
                    "Падаем на template-каталог.", slug)

    template_id = _read_scene_template_id(slug)
    if template_id is None:
        log.warning("scene_template.txt отсутствует для slug=%s - зовём pick_scene_template",
                    slug)
        template_id = _pick_scene_template_fallback(slug, category)

    if template_id is not None:
        scene = _read_template_scene_from_catalog(template_id)
        if scene:
            log.info("scene из cover-scenes.md template_id=%d для slug=%s",
                     template_id, slug)
            return scene
        log.warning("Template #%d не извлёкся из cover-scenes.md - fallback "
                    "на CATEGORY_SCENE_DEFAULT", template_id)

    return None


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
    scene = _read_scene(slug, category=category)

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


def _today_msk_iso() -> str:
    """Сегодняшняя дата в Москве (YYYY-MM-DD). Решает проблему когда
    Claude (агент 6) писал в meta.json date_published на день вперёд из-за
    TZ контейнера или injected currentDate. Зафиксировано 9 мая 2026."""
    if _MSK_TZ is not None:
        return datetime.now(_MSK_TZ).date().isoformat()
    return datetime.now().date().isoformat()


def _stamp_ready_for_review(slug: str, meta: dict) -> dict:
    """
    Дописывает в meta.json публикационные поля.
    cover_url / cover_url_master / image_prompt / cover_uploaded_at
    уже записаны самим image_gen.py - их не трогаем.

    Дату публикации перезаписываем на сегодняшнюю по МСК — даже если
    агент 6 что-то записал в date_published, мы её принудительно меняем
    на детерминированное значение. Это решает проблему «писалась 8 мая,
    в HTML стоит 9 мая» (зафиксировано 9 мая 2026 для статьи
    kak-zakryt-ooo-s-dolgami).
    """
    today_msk = _today_msk_iso()
    meta["date_published"] = today_msk
    meta["date_modified"] = today_msk
    meta["ready_for_review"] = True
    meta["ready_at"] = _now_iso_z()
    meta["publication_target"] = "telegram_review"
    _write_meta(slug, meta)

    # После записи правильной даты — пересобираем article.html, чтобы
    # дата попала в видимый блок и в JSON-LD. Если inject_boilerplate
    # упадёт — оставляем уже сгенерированный HTML, не блокируем pipeline.
    try:
        from tools import inject_boilerplate
        result = inject_boilerplate.process(
            DRAFTS_DIR / slug,
            body_filename="body.html",
            out_filename="article.html",
        )
        if not result.get("ok"):
            log.warning("inject_boilerplate не пересобрал article.html "
                        "после правки даты для %s: %s", slug, result)
        else:
            log.info("article.html пересобран с date_published=%s", today_msk)
    except Exception as exc:
        log.exception("inject_boilerplate упал при пересборке после правки даты: %s", exc)

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
