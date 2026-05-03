"""
Генерация обложки статьи: fal.ai text-to-image (Seedream v4 от ByteDance) →
загрузка в Cloudinary → возврат публичного URL для вставки в HTML.

Используется в bot/publisher.py при нажатии «Опубликовать» в Telegram.

ENV переменные:
    FAL_KEY                       - ключ от fal.ai (ОБЯЗАТЕЛЬНО)
    FAL_MODEL                     - модель fal.ai (по умолчанию seedream v4 text-to-image)
    CLOUDINARY_CLOUD_NAME         - идентификатор аккаунта Cloudinary (ОБЯЗАТЕЛЬНО)
    CLOUDINARY_API_KEY            - публичный ключ API (ОБЯЗАТЕЛЬНО)
    CLOUDINARY_API_SECRET         - приватный ключ API (ОБЯЗАТЕЛЬНО)
    CLOUDINARY_FOLDER             - папка в Cloudinary (по умолчанию "articles")
    IMAGE_GEN_DEFAULT_COVER_URL   - URL дефолтной обложки на случай ошибки
                                    (если не задан - функция вернёт None при ошибке)

Поведение при ошибках:
- Если ENV не настроены - возвращаем None, пишем warning в лог.
- Если fal.ai упал или таймаут - возвращаем IMAGE_GEN_DEFAULT_COVER_URL или None.
- Если Cloudinary upload упал - то же самое.
- Publisher должен корректно обработать None (не вставлять обложку, либо вставить заглушку из CSS).

Запуск как скрипта (для отладки):
    python -m tools.image_gen "kak-zakryt-ooo-s-dolgami" "Как закрыть ООО с долгами в 2026 году" yur
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

log = logging.getLogger("image_gen")

# Дефолты
DEFAULT_FAL_MODEL = os.getenv("FAL_MODEL", "fal-ai/bytedance/seedream/v4/text-to-image")
DEFAULT_CLOUDINARY_FOLDER = os.getenv("CLOUDINARY_FOLDER", "articles")
DEFAULT_COVER_URL = os.getenv("IMAGE_GEN_DEFAULT_COVER_URL", "").strip() or None
FAL_TIMEOUT_SEC = int(os.getenv("FAL_TIMEOUT_SEC", "120"))


# ============ ПРОМПТ ============

# Категория → стилевая подсказка для генератора. Промпты по-английски,
# Seedream/SDXL-подобные модели лучше понимают английский, чем русский.
CATEGORY_STYLE = {
    "fiz": (
        "soft warm tones, beige and forest green palette, "
        "minimalist, abstract symbols of personal finance, "
        "calm reassuring atmosphere"
    ),
    "yur": (
        "corporate professional style, deep navy and slate gray palette, "
        "minimalist, abstract corporate documents and seals, "
        "structured composition"
    ),
    "vzysk": (
        "muted warm tones, dark amber and deep brown palette, "
        "minimalist, abstract scales of justice and contracts, "
        "serious focused atmosphere"
    ),
    "news": (
        "editorial newsroom style, cool neutral palette, "
        "minimalist, abstract calendar and document elements, "
        "informative composition"
    ),
}

# Базовый стиль - общий для всех. Без людей (deepfake-риски + сложность),
# без текста (Seedream плохо рендерит кириллицу), без логотипов.
BASE_STYLE = (
    "professional editorial illustration, flat vector style, "
    "no text, no people, no faces, no logos, "
    "clean composition with negative space, "
    "subtle gradient background, "
    "Russian legal and financial publication aesthetic"
)


def _build_prompt(title: str, category: str) -> str:
    cat_style = CATEGORY_STYLE.get(category, CATEGORY_STYLE["fiz"])
    # Title переводить на английский нет смысла: модели обычно понимают и не выдают
    # на нём текст в картинке (BASE_STYLE требует "no text").
    return (
        f"Editorial cover illustration for an article about: {title}. "
        f"Style: {BASE_STYLE}. "
        f"Mood: {cat_style}. "
        f"Aspect ratio 1200x630 OG image format."
    )


# ============ FAL.AI ============

@dataclass
class FalResult:
    image_url: str
    prompt_used: str


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

    try:
        result = fal_client.subscribe(
            DEFAULT_FAL_MODEL,
            arguments={
                "prompt": prompt,
                "image_size": {"width": 1200, "height": 630},
                "num_images": 1,
                "enable_safety_checker": True,
            },
            with_logs=False,
        )
    except Exception:
        log.exception("fal.ai: ошибка генерации")
        return None

    # Структура ответа Seedream/SDXL-подобных моделей: {"images": [{"url": "..."}]}
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
) -> Optional[str]:
    """
    Полный цикл: построить промпт → fal.ai → наложить лого → Cloudinary → secure_url.

    Promtp-стратегия:
        1. Если в `image_prompt` пришёл готовый промпт (его генерирует агент 6
           при написании статьи и кладёт в meta.json как поле `image_prompt`) -
           используем его. Это путь по умолчанию.
        2. Если поля нет (старая статья без image_prompt, или ошибка чтения meta) -
           падаем на простой шаблонный промпт _build_prompt(title, category).

    Возвращает:
        - URL обложки (https://res.cloudinary.com/...)
        - DEFAULT_COVER_URL если задан и любой шаг упал
        - None если задан DEFAULT_COVER_URL=None и шаг упал

    Publisher должен корректно обработать None: не вставлять обложку
    в HTML или использовать встроенный CSS-fallback.
    """
    if not slug or not title:
        log.warning("generate_and_upload_cover: пустой slug/title, пропуск")
        return DEFAULT_COVER_URL

    log.info("Генерация обложки: slug=%s category=%s title=%r", slug, category, title[:60])

    # 1. Промпт. Берём готовый из meta.json или падаем на шаблон.
    if image_prompt and image_prompt.strip():
        prompt = image_prompt.strip()
        log.info("Используем image_prompt из meta.json (%d символов)", len(prompt))
    else:
        prompt = _build_prompt(title=title, category=category)
        log.info("image_prompt не передан, использую шаблонный промпт по категории")

    fal_result = _fal_generate(prompt)
    if not fal_result:
        log.warning("fal.ai не вернул картинку, fallback на дефолтную обложку")
        return DEFAULT_COVER_URL

    # Скачиваем bytes с fal.ai - чтобы наложить лого перед загрузкой в Cloudinary.
    image_bytes = _download_image_bytes(fal_result.image_url)
    if image_bytes is None:
        log.warning("Не смог скачать с fal.ai, грузим в Cloudinary напрямую по URL (без лого)")
        upload_source: str | bytes = fal_result.image_url
    else:
        # Накладываем лого. Если что-то упадёт - модуль вернёт исходные bytes.
        try:
            from tools.logo_overlay import add_logo  # type: ignore
            upload_source = add_logo(image_bytes)
        except Exception:
            log.exception("logo_overlay.add_logo упал, грузим оригинал без лого")
            upload_source = image_bytes

    cloudinary_url = _cloudinary_upload(upload_source, slug=slug)
    if not cloudinary_url:
        log.warning("Cloudinary upload не сработал, fallback")
        return DEFAULT_COVER_URL

    log.info("Обложка готова: %s", cloudinary_url)
    return cloudinary_url


# ============ CLI для отладки ============

def _cli():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if len(sys.argv) < 4:
        print("Usage: python -m tools.image_gen <slug> <title> <category> [image_prompt]")
        print("Example: python -m tools.image_gen kak-zakryt-ooo "
              '"Как закрыть ООО" yur "Photorealistic flat-lay of..."')
        sys.exit(1)

    slug, title, category = sys.argv[1], sys.argv[2], sys.argv[3]
    image_prompt = sys.argv[4] if len(sys.argv) > 4 else None
    url = generate_and_upload_cover(
        slug=slug, title=title, category=category, image_prompt=image_prompt,
    )
    if url:
        print(f"OK: {url}")
        sys.exit(0)
    else:
        print("FAILED (см. лог выше)")
        sys.exit(1)


if __name__ == "__main__":
    _cli()
