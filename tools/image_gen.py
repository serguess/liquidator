"""
Генерация обложки статьи: fal.ai text-to-image (nano-banana-2 от Google, 4K) →
наложение лого → загрузка в Cloudinary → запись cover_url в meta.json драфта.

Запускается агентом 7 (publisher) после того как он финализирует meta.json драфта.
Также может быть вызван bot/publisher.py при публикации (fallback для старых драфтов
без cover_url - см. soft-fallback ниже).

ENV переменные:
    FAL_KEY                       - ключ от fal.ai (ОБЯЗАТЕЛЬНО)
    FAL_MODEL                     - модель fal.ai (по умолчанию fal-ai/nano-banana-2)
    FAL_RESOLUTION                - разрешение для nano-banana (по умолчанию "4K")
    FAL_ASPECT_RATIO              - соотношение сторон (по умолчанию "16:9")
    CLOUDINARY_CLOUD_NAME         - идентификатор аккаунта Cloudinary (ОБЯЗАТЕЛЬНО)
    CLOUDINARY_API_KEY            - публичный ключ API (ОБЯЗАТЕЛЬНО)
    CLOUDINARY_API_SECRET         - приватный ключ API (ОБЯЗАТЕЛЬНО)
    CLOUDINARY_FOLDER             - папка в Cloudinary (по умолчанию "articles")
    CLOUDINARY_WEB_TRANSFORMATION - URL-трансформация для веб-версии
                                    (по умолчанию "f_auto,q_auto,w_1920")
    IMAGE_GEN_DEFAULT_COVER_URL   - URL дефолтной обложки на случай ошибки
                                    (если не задан - функция вернёт None при ошибке)

Архитектура промпта:
    BASE_STYLE  — фотореалистичный editorial top-down flat-lay (фиксирован в коде)
    SCENE       — конкретные предметы под тему статьи (формирует агент 7)
    STRICT      — запрет текста, людей, чистый угол под лого (фиксирован в коде)

Поведение при ошибках (soft fallback):
- Если ENV не настроены - возвращаем None, пишем warning в лог.
- Если fal.ai упал или таймаут - возвращаем IMAGE_GEN_DEFAULT_COVER_URL или None.
- Если Cloudinary upload упал - то же самое.
- Publisher должен корректно обработать None (не вставлять обложку, либо вставить
  заглушку из CSS). Pipeline не блокируется - статья всё равно может выйти.

Запуск как скрипта (для отладки и из агента 7):
    # Только slug - читает meta.json драфта (title, category, image_prompt/scene_objects)
    python -m tools.image_gen posledstviya-bankrotstva-fizicheskogo-lica

    # Передать сцену явно
    python -m tools.image_gen <slug> --scene "closed brown leather case folder, wooden gavel, ..."

    # Передать полный промпт целиком (override BASE+SCENE+STRICT)
    python -m tools.image_gen <slug> --prompt "<полный английский промпт>"

После успеха скрипт обновляет drafts/<slug>/meta.json:
    cover_url           - Cloudinary URL веб-версии (с трансформацией)
    cover_url_master    - Cloudinary URL мастер-файла 4K (без трансформации)
    image_prompt        - полный промпт, использованный для генерации
    cover_uploaded_at   - ISO timestamp
"""
from __future__ import annotations

import json
import logging
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("image_gen")

# Дефолты
DEFAULT_FAL_MODEL = os.getenv("FAL_MODEL", "fal-ai/nano-banana-2")
DEFAULT_FAL_RESOLUTION = os.getenv("FAL_RESOLUTION", "4K")
DEFAULT_FAL_ASPECT_RATIO = os.getenv("FAL_ASPECT_RATIO", "16:9")
DEFAULT_CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "articles")
DEFAULT_WEB_TRANSFORMATION = os.getenv("CLOUDINARY_WEB_TRANSFORMATION", "f_auto,q_auto,w_1920")
DEFAULT_COVER_URL = os.getenv("IMAGE_GEN_DEFAULT_COVER_URL", "").strip() or None
FAL_TIMEOUT_SEC = int(os.getenv("FAL_TIMEOUT_SEC", "120"))

# Корень проекта - для записи в drafts/<slug>/meta.json
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DRAFTS_DIR = PROJECT_ROOT / "drafts"


# ============ ПРОМПТ ============
#
# Архитектура: BASE_STYLE + SCENE_OBJECTS + STRICT_RULES
#
# - BASE_STYLE   — фотореалистичный editorial top-down flat-lay, palette,
#                  свет, премиум legal magazine. Фикс в коде.
# - SCENE        — конкретные предметы под тему статьи (3-7 элементов).
#                  Формирует агент 7 при финализации драфта. Если не передан -
#                  fallback на CATEGORY_SCENE_DEFAULT.
# - STRICT_RULES — запрет текста, людей, чистый правый нижний угол под лого.
#                  Фикс в коде.
#
# Промпты по-английски: модели генерации картинок сильно лучше понимают
# английский. Кириллица в стиль-инструкциях провоцирует псевдо-русские
# вывески в кадре.

BASE_STYLE = (
    "Photorealistic top-down flat-lay editorial photograph on a polished "
    "dark wooden desk, soft golden morning light from a window on the left "
    "casting a long warm diagonal across the desk, premium legal magazine "
    "aesthetic, shallow depth of field, neutral beige and graphite palette "
    "with warm gold accents, cinematic photorealistic detail"
)

STRICT_RULES = (
    "Strict requirements: completely wordless composition with absolutely "
    "no text, no inscriptions, no readable letters anywhere in the frame, "
    "no titles on books (only embossed decorative gilt patterns and emblems "
    "are allowed), no labels, no logos, no watermarks, no street signs, "
    "no banners. No people, no faces, no hands. "
    "Lower right area of the desk kept smooth, softly sunlit and empty "
    "for external logo placement. 16:9 horizontal aspect ratio."
)

NEGATIVE_PROMPT = (
    "text, letters, words, writing, typography, inscriptions, signs, signage, "
    "labels, captions, headlines, banners, plaques, posters, newspapers, "
    "documents with visible text, banknotes with visible numbers, screens "
    "with text, watermarks, fake text, gibberish text, cyrillic, latin script, "
    "chinese characters, japanese characters, runes, hieroglyphs, calligraphy, "
    "people, faces, hands, human figures"
)

# Дефолтные сцены по категориям - если агент не передал свой scene_objects.
# Каждая сцена = список из 3-7 предметов в одном кадре, без текстонесущих
# поверхностей (или с закрытыми обложками, гербами без надписей).
CATEGORY_SCENE_DEFAULT = {
    "fiz": (
        "a closed brown leather case folder placed slightly left of center "
        "with a wooden judges gavel resting diagonally on top, a stack of "
        "three closed law books with embossed gilt spines and a small "
        "national emblem, an elegant black fountain pen, a small white "
        "porcelain coffee cup on a saucer, a pair of reading glasses"
    ),
    "yur": (
        "a closed dark leather portfolio with a brass round company seal "
        "resting on top, a stack of corporate folders, a fountain pen, "
        "a vintage brass desk lamp, a closed laptop with brushed aluminum "
        "lid, a small white porcelain coffee cup"
    ),
    "vzysk": (
        "polished brass scales of justice with smooth blank pans on the "
        "left, a wooden judges gavel on a round sound block, a closed "
        "leather case folder, a fountain pen, a small white porcelain "
        "coffee cup, deep contemplative shadows on the right"
    ),
    "news": (
        "a closed newspaper folded in half with no visible headlines, "
        "a wooden judges gavel resting on a closed leather folder, "
        "a desk calendar showing only an abstract page with no readable "
        "dates, a fountain pen, a small white porcelain coffee cup, "
        "a pair of reading glasses"
    ),
}


def _build_prompt(scene: str) -> str:
    """
    Собирает финальный промпт из BASE_STYLE + SCENE + STRICT_RULES.

    scene - английская строка с описанием 3-7 предметов в кадре
            (формирует агент 7 под содержание конкретной статьи).
    """
    return f"{BASE_STYLE}. Scene: {scene}. {STRICT_RULES}"


def _scene_for_category(category: str) -> str:
    return CATEGORY_SCENE_DEFAULT.get(category, CATEGORY_SCENE_DEFAULT["fiz"])


# ============ FAL.AI ============

@dataclass
class FalResult:
    image_url: str
    prompt_used: str


def _build_fal_arguments(prompt: str, model: str) -> dict:
    """
    Собирает аргументы для fal_client.subscribe в зависимости от модели.

    nano-banana-2 (Google Gemini-based) — принимает aspect_ratio + resolution.
    seedream/v4 (ByteDance) — принимает image_size + negative_prompt.
    Остальные — общий fallback (image_size + aspect_ratio + negative_prompt;
    fal обычно игнорирует лишние ключи без ошибки).
    """
    base = {"prompt": prompt, "num_images": 1}
    m = (model or "").lower()
    if "nano-banana" in m:
        return {
            **base,
            "aspect_ratio": DEFAULT_FAL_ASPECT_RATIO,
            "resolution": DEFAULT_FAL_RESOLUTION,
        }
    if "seedream" in m:
        return {
            **base,
            "image_size": {"width": 1200, "height": 630},
            "negative_prompt": NEGATIVE_PROMPT,
            "enable_safety_checker": True,
        }
    return {
        **base,
        "image_size": {"width": 1200, "height": 630},
        "aspect_ratio": DEFAULT_FAL_ASPECT_RATIO,
        "negative_prompt": NEGATIVE_PROMPT,
    }


def _fal_generate(prompt: str) -> Optional[FalResult]:
    """
    Вызывает fal.ai с указанным промптом, возвращает URL сгенерированной
    картинки. None при любой ошибке.
    """
    fal_key = os.getenv("FAL_KEY", "").strip()
    if not fal_key:
        log.warning("FAL_KEY не задан, пропускаю генерацию обложки")
        return None

    try:
        # fal-client ожидает FAL_KEY именно в env
        os.environ["FAL_KEY"] = fal_key
        import fal_client  # type: ignore
    except ImportError:
        log.exception("fal-client не установлен (pip install fal-client)")
        return None

    arguments = _build_fal_arguments(prompt, DEFAULT_FAL_MODEL)
    log.info("fal.ai: model=%s args.keys=%s", DEFAULT_FAL_MODEL,
             [k for k in arguments.keys() if k != "prompt"])

    try:
        result = fal_client.subscribe(
            DEFAULT_FAL_MODEL,
            arguments=arguments,
            with_logs=False,
        )
    except Exception:
        log.exception("fal.ai: ошибка генерации")
        return None

    # Структура ответа: {"images": [{"url": "..."}], ...} - общая для всех моделей
    images = (result or {}).get("images") or []
    if not images:
        log.warning("fal.ai: пустой ответ images=%r", result)
        return None

    image_url = (images[0] or {}).get("url")
    if not image_url:
        log.warning("fal.ai: нет url в images[0]=%r", images[0])
        return None

    return FalResult(image_url=image_url, prompt_used=prompt)


# ============ CLOUDINARY ============

def _cloudinary_configured() -> bool:
    return bool(
        os.getenv("CLOUDINARY_CLOUD_NAME", "").strip()
        and os.getenv("CLOUDINARY_API_KEY", "").strip()
        and os.getenv("CLOUDINARY_API_SECRET", "").strip()
    )


def _cloudinary_setup() -> bool:
    """
    Конфигурирует cloudinary SDK. Возвращает True если всё ок.
    """
    if not _cloudinary_configured():
        log.warning("Cloudinary ENV не заполнены, пропускаю загрузку")
        return False

    try:
        import cloudinary  # type: ignore
    except ImportError:
        log.exception("cloudinary не установлен (pip install cloudinary)")
        return False

    cloudinary.config(
        cloud_name=os.getenv("CLOUDINARY_CLOUD_NAME"),
        api_key=os.getenv("CLOUDINARY_API_KEY"),
        api_secret=os.getenv("CLOUDINARY_API_SECRET"),
        secure=True,
    )
    return True


def _download_image_bytes(url: str) -> Optional[bytes]:
    """
    Скачивает картинку с указанного URL (fal.ai), возвращает её bytes.
    Нужно, чтобы между fal.ai и Cloudinary вклинить шаг наложения лого.
    """
    try:
        import httpx  # type: ignore
    except ImportError:
        log.exception("httpx не установлен (pip install httpx)")
        return None

    try:
        with httpx.Client(timeout=FAL_TIMEOUT_SEC) as client:
            r = client.get(url)
            r.raise_for_status()
            return r.content
    except Exception:
        log.exception("Не удалось скачать картинку с %s", url)
        return None


def _cloudinary_upload(source: str | bytes, slug: str) -> Optional[str]:
    """
    Загружает картинку в Cloudinary, возвращает её secure_url.

    source может быть:
        - str: URL (Cloudinary сам тянет по HTTP)
        - bytes: уже скачанная и обработанная картинка

    public_id фиксированный по slug (с префиксом папки) - повторная загрузка
    с overwrite=True перезапишет старую обложку для того же slug.
    """
    if not _cloudinary_setup():
        return None

    try:
        import cloudinary.uploader  # type: ignore
    except ImportError:
        log.exception("cloudinary.uploader не установлен")
        return None

    public_id = f"{slug}-cover"

    try:
        # uploader.upload принимает как URL (str), так и file-like (bytes через BytesIO)
        if isinstance(source, bytes):
            import io as _io
            upload_source = _io.BytesIO(source)
        else:
            upload_source = source

        result = cloudinary.uploader.upload(
            upload_source,
            folder=DEFAULT_CLOUDINARY_FOLDER,
            public_id=public_id,
            overwrite=True,
            resource_type="image",
            # Автоматическая оптимизация: webp/avif при поддержке + ресайз через URL.
            transformation=[
                {"quality": "auto:good", "fetch_format": "auto"},
            ],
        )
    except Exception:
        log.exception("cloudinary upload упал")
        return None

    return result.get("secure_url")


def _build_web_url(secure_url: str, transformation: Optional[str] = None) -> str:
    """
    Превращает Cloudinary secure_url мастер-файла в URL веб-версии с
    трансформацией.

    Пример входа:
      https://res.cloudinary.com/<cloud>/image/upload/v123/articles/x-cover.jpg
    Пример выхода (transformation="f_auto,q_auto,w_1920"):
      https://res.cloudinary.com/<cloud>/image/upload/f_auto,q_auto,w_1920/v123/articles/x-cover.jpg

    Cloudinary понимает трансформацию вставленную сразу после "/upload/".
    """
    transformation = transformation or DEFAULT_WEB_TRANSFORMATION
    if not secure_url or "/upload/" not in secure_url or not transformation:
        return secure_url
    return secure_url.replace("/upload/", f"/upload/{transformation}/", 1)


def _update_meta_with_cover(slug: str, fields: dict) -> bool:
    """
    Дописывает поля в drafts/<slug>/meta.json. Возвращает True если получилось.
    Не падает если файла нет - просто warning.
    """
    meta_path = DRAFTS_DIR / slug / "meta.json"
    if not meta_path.exists():
        log.warning("meta.json драфта не найден: %s", meta_path)
        return False
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        log.exception("meta.json драфта битый: %s", meta_path)
        return False
    meta.update(fields)
    try:
        meta_path.write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except OSError:
        log.exception("Не смог записать meta.json: %s", meta_path)
        return False
    return True


def delete_cover(slug: str) -> bool:
    """
    Удаляет обложку из Cloudinary (если, например, статья отозвана).
    Возвращает True если удалили или картинки и так не было.
    """
    if not _cloudinary_setup():
        return False
    try:
        import cloudinary.uploader  # type: ignore
        public_id = f"{DEFAULT_CLOUDINARY_FOLDER}/{slug}-cover"
        result = cloudinary.uploader.destroy(public_id, resource_type="image")
        return (result or {}).get("result") in ("ok", "not found")
    except Exception:
        log.exception("cloudinary delete упал для slug=%s", slug)
        return False


# ============ ОСНОВНАЯ ФУНКЦИЯ ============

def generate_and_upload_cover(
    slug: str,
    title: str,
    category: str,
    image_prompt: Optional[str] = None,
    scene: Optional[str] = None,
    write_meta: bool = True,
) -> Optional[str]:
    """
    Полный цикл: построить промпт → fal.ai → наложить лого → Cloudinary →
    обновить drafts/<slug>/meta.json → вернуть URL веб-версии (с трансформацией).

    Promtp-стратегия (по приоритету):
        1. image_prompt — готовый полный промпт целиком (используется как есть).
           Если агент 7 сам сформировал английский промпт и записал в meta -
           это самый прямой путь.
        2. scene — только описание сцены (3-7 предметов на английском).
           Тогда BASE_STYLE и STRICT_RULES добавляются автоматически.
           Это рекомендуемый путь для агента 7.
        3. Если ни того ни другого - fallback на CATEGORY_SCENE_DEFAULT[category]
           + BASE_STYLE + STRICT_RULES.

    write_meta:
        True (по умолчанию) - после успешной загрузки в Cloudinary дописывает
                             в drafts/<slug>/meta.json:
                                cover_url           - URL веб-версии (с трансформацией)
                                cover_url_master    - URL мастер-файла без трансформации
                                image_prompt        - полный промпт, использованный
                                cover_uploaded_at   - ISO timestamp
        False - не трогать meta.json (для разовых вызовов из скриптов).

    Возвращает:
        - URL веб-версии (с CLOUDINARY_WEB_TRANSFORMATION). Это то что должно
          подставляться в HTML <img> и og:image.
        - DEFAULT_COVER_URL если задан и любой шаг упал.
        - None если DEFAULT_COVER_URL не задан и шаг упал.

    Publisher должен корректно обработать None: не вставлять обложку
    в HTML или использовать встроенный CSS-fallback.
    """
    if not slug or not title:
        log.warning("generate_and_upload_cover: пустой slug/title, пропуск")
        return DEFAULT_COVER_URL

    log.info("Генерация обложки: slug=%s category=%s title=%r", slug, category, title[:60])

    # 1. Сборка промпта по приоритету.
    if image_prompt and image_prompt.strip():
        prompt = image_prompt.strip()
        log.info("Используем готовый image_prompt (%d символов)", len(prompt))
    else:
        scene_text = (scene or "").strip() or _scene_for_category(category)
        prompt = _build_prompt(scene_text)
        log.info("Собран промпт из BASE+SCENE+STRICT (scene=%d символов)", len(scene_text))

    fal_result = _fal_generate(prompt)
    if not fal_result:
        log.warning("fal.ai не вернул картинку, fallback на дефолтную обложку")
        return DEFAULT_COVER_URL

    # 2. Скачиваем bytes с fal.ai - чтобы наложить лого перед загрузкой в Cloudinary.
    image_bytes = _download_image_bytes(fal_result.image_url)
    if image_bytes is None:
        log.warning("Не смог скачать с fal.ai, грузим в Cloudinary напрямую по URL (без лого)")
        upload_source: str | bytes = fal_result.image_url
    else:
        # 3. Накладываем лого. Если что-то упадёт - модуль вернёт исходные bytes.
        try:
            from tools.logo_overlay import add_logo  # type: ignore
            upload_source = add_logo(image_bytes)
        except Exception:
            log.exception("logo_overlay.add_logo упал, грузим оригинал без лого")
            upload_source = image_bytes

    # 4. Cloudinary upload - получаем secure_url мастер-файла.
    master_url = _cloudinary_upload(upload_source, slug=slug)
    if not master_url:
        log.warning("Cloudinary upload не сработал, fallback")
        return DEFAULT_COVER_URL

    # 5. Web URL с трансформацией (q_auto,f_auto,w_1920) - для og:image и сайта.
    web_url = _build_web_url(master_url, DEFAULT_WEB_TRANSFORMATION)

    # 6. Записываем в meta.json драфта.
    if write_meta:
        ok = _update_meta_with_cover(slug, {
            "cover_url": web_url,
            "cover_url_master": master_url,
            "image_prompt": prompt,
            "cover_uploaded_at": datetime.now(timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        })
        if ok:
            log.info("meta.json обновлён cover_url для slug=%s", slug)

    log.info("Обложка готова: master=%s web=%s", master_url, web_url)
    return web_url


def upload_existing_cover(
    slug: str,
    file_path: str | Path,
    write_meta: bool = True,
    image_prompt: Optional[str] = None,
) -> Optional[str]:
    """
    Загружает уже существующий локальный файл в Cloudinary как обложку драфта
    (без вызова fal.ai). Полезно когда обложка сгенерирована вручную через
    отладочный скрипт и её нужно просто залить.

    Параметры:
        slug         - идентификатор статьи, public_id будет <slug>-cover.
        file_path    - путь к локальному файлу JPEG/PNG.
        write_meta   - дописать ли поля в drafts/<slug>/meta.json.
        image_prompt - использованный промпт (запишется в meta).

    Возвращает web URL (с трансформацией) или None.
    """
    p = Path(file_path)
    if not p.exists():
        log.error("upload_existing_cover: файл не найден: %s", p)
        return None
    image_bytes = p.read_bytes()
    master_url = _cloudinary_upload(image_bytes, slug=slug)
    if not master_url:
        log.warning("Cloudinary upload не сработал")
        return DEFAULT_COVER_URL
    web_url = _build_web_url(master_url, DEFAULT_WEB_TRANSFORMATION)
    if write_meta:
        fields = {
            "cover_url": web_url,
            "cover_url_master": master_url,
            "cover_uploaded_at": datetime.now(timezone.utc)
                .replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        if image_prompt:
            fields["image_prompt"] = image_prompt
        _update_meta_with_cover(slug, fields)
    log.info("Existing cover uploaded: master=%s web=%s", master_url, web_url)
    return web_url


# ============ CLI ============
#
# Интерфейс командной строки. Используется агентом 7 (publisher) после
# финализации meta.json драфта.
#
# Базовый сценарий (агент 7):
#     python -m tools.image_gen <slug> --scene "<3-7 предметов на английском>"
#
# Если scene не передан - используется fallback по category из meta.json.
# Если slug передан без других аргументов - title и category читаются из
# drafts/<slug>/meta.json.
#
# Также можно передать готовый полный промпт целиком:
#     python -m tools.image_gen <slug> --prompt "<full english prompt>"
#
# Backward compat для старого вызова из bot/publisher.py:
#     python -m tools.image_gen <slug> <title> <category> [image_prompt]
# (распознаётся по позиции - если 3-4 позиционных аргумента без --флагов).

def _cli():
    import argparse

    # Подтягиваем .env автоматически - чтобы агент 7 мог запускать
    # `python -m tools.image_gen ...` без ручного export ENV. На сервере
    # Timeweb Cloud Apps ENV приходят из настроек deployment, .env там нет -
    # тогда load_dotenv просто ничего не сделает (нечего загружать).
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
        description="Генерация обложки статьи через fal.ai + Cloudinary"
    )
    parser.add_argument("slug", help="slug статьи (drafts/<slug>/)")
    parser.add_argument("title_pos", nargs="?", default=None,
                        help="(legacy) title статьи если не указан в meta.json")
    parser.add_argument("category_pos", nargs="?", default=None,
                        help="(legacy) category статьи (fiz/yur/vzysk/news)")
    parser.add_argument("legacy_prompt", nargs="?", default=None,
                        help="(legacy) готовый image_prompt 4-м позиционным")
    parser.add_argument("--scene", default=None,
                        help="Описание сцены на английском (3-7 предметов). "
                             "Используется агентом 7.")
    parser.add_argument("--prompt", default=None,
                        help="Готовый полный английский промпт целиком "
                             "(override BASE+SCENE+STRICT)")
    parser.add_argument("--no-meta-write", action="store_true",
                        help="Не записывать cover_url в meta.json")
    parser.add_argument("--upload-only", default=None,
                        help="Не вызывать fal.ai — просто залить указанный "
                             "локальный файл в Cloudinary")
    args = parser.parse_args()

    # upload-only режим: загрузка готового локального файла без fal.ai
    if args.upload_only:
        url = upload_existing_cover(
            slug=args.slug,
            file_path=args.upload_only,
            write_meta=not args.no_meta_write,
            image_prompt=args.prompt or args.legacy_prompt,
        )
        if url:
            print(f"OK: {url}")
            sys.exit(0)
        print("FAILED (см. лог выше)")
        sys.exit(1)

    # Резолвим title/category — приоритет: legacy позиционные → meta.json драфта
    title = args.title_pos
    category = args.category_pos
    if not title or not category:
        meta_path = DRAFTS_DIR / args.slug / "meta.json"
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                title = title or meta.get("title") or meta.get("h1") or args.slug
                category = category or meta.get("category") or "fiz"
            except (json.JSONDecodeError, OSError):
                log.exception("Не смог прочитать meta.json драфта %s", args.slug)
                sys.exit(2)
        else:
            print(f"FAIL: не найден meta.json драфта и не передан title/category. "
                  f"Ожидался файл: {meta_path}", file=sys.stderr)
            sys.exit(2)

    image_prompt = args.prompt or args.legacy_prompt
    scene = args.scene

    url = generate_and_upload_cover(
        slug=args.slug,
        title=title,
        category=category,
        image_prompt=image_prompt,
        scene=scene,
        write_meta=not args.no_meta_write,
    )
    if url:
        print(f"OK: {url}")
        sys.exit(0)
    print("FAILED (см. лог выше)")
    sys.exit(1)


if __name__ == "__main__":
    _cli()
