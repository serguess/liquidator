"""
Наложение логотипа-водяного знака на сгенерированную обложку статьи.

Используется в tools/image_gen.py между fal.ai (генерация) и Cloudinary
(загрузка): получаем bytes картинки от fal.ai, накладываем лого, отдаём
готовые bytes на загрузку.

Поведение:
- Лого темнее и контрастнее оригинала (брендовый оливковый #5B5632 на тёмном дереве
  иначе плохо читался).
- Под лого - круглая радиальная градиентная подложка кремового цвета (#FCF8EE),
  плавно затухающая к нулю задолго до границ холста (никаких квадратных краёв).
- Размер: 20% высоты фото, отступ 4% от края.
- Угол: правый нижний.

Параметры берутся из ENV (с разумными дефолтами) - можно переопределить
без правок кода.

ENV переменные:
    LOGO_OVERLAY_ENABLED       - "true"/"false", по умолчанию "true"
    LOGO_PATH                  - путь к PNG лого (с прозрачностью).
                                 По умолчанию assets/logo-watermark.png
                                 относительно корня проекта.
    LOGO_SIZE_RATIO            - доля высоты фото, по умолчанию 0.20
    LOGO_PADDING_RATIO         - отступ от края, доля ширины/высоты, по умолчанию 0.04
    LOGO_HALO_COLOR            - hex или "r,g,b" подложки, по умолчанию "252,248,238"
    LOGO_OUTPUT_QUALITY        - JPEG quality 1..100, по умолчанию 92

Поведение при ошибках:
- Если PIL/numpy не установлены или лого не найден - возвращаем оригинальные
  bytes без изменений (warning в лог). Пайплайн не падает.
- Если LOGO_OVERLAY_ENABLED=false - возвращаем оригинальные bytes сразу.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("logo_overlay")


# ============ КОНФИГ ============

def _env_bool(name: str, default: bool) -> bool:
    val = os.getenv(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def _env_float(name: str, default: float) -> float:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except ValueError:
        log.warning("ENV %s=%r не парсится как float, использую дефолт %s", name, val, default)
        return default


def _env_int(name: str, default: int) -> int:
    val = os.getenv(name, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except ValueError:
        log.warning("ENV %s=%r не парсится как int, использую дефолт %s", name, val, default)
        return default


def _parse_color(s: str, default: tuple[int, int, int]) -> tuple[int, int, int]:
    s = s.strip()
    if not s:
        return default
    # hex (#FCF8EE или FCF8EE)
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    # "r,g,b"
    parts = [p.strip() for p in s.split(",")]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    log.warning("LOGO_HALO_COLOR=%r не распознан, использую дефолт %s", s, default)
    return default


def _resolve_logo_path() -> Optional[Path]:
    """
    Возвращает абсолютный путь к лого. Ищем относительно корня проекта
    (двух уровней вверх от этого файла), либо абсолютный путь из ENV.
    """
    raw = os.getenv("LOGO_PATH", "assets/logo-watermark.png").strip()
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return p
    # Относительно корня проекта (tools/logo_overlay.py → корень)
    project_root = Path(__file__).resolve().parent.parent
    candidate = project_root / raw
    if candidate.exists():
        return candidate
    log.warning("LOGO_PATH=%r не найден ни как абсолютный, ни относительно %s", raw, project_root)
    return None


# ============ ЛОГИКА НАЛОЖЕНИЯ ============

def add_logo(image_bytes: bytes) -> bytes:
    """
    Накладывает лого-водяной знак на картинку и возвращает JPEG bytes.

    При любой ошибке (нет PIL, нет лого, битая картинка) возвращает исходные
    bytes без изменений - чтобы пайплайн генерации обложек не упал.

    Args:
        image_bytes: bytes исходной картинки (любой формат, который читает PIL).

    Returns:
        JPEG bytes с наложенным лого, либо исходные bytes при ошибке.
    """
    if not _env_bool("LOGO_OVERLAY_ENABLED", default=True):
        log.info("LOGO_OVERLAY_ENABLED=false, возвращаю картинку без лого")
        return image_bytes

    try:
        from PIL import Image, ImageEnhance, ImageFilter  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        log.exception("Pillow или numpy не установлены, пропускаю наложение лого")
        return image_bytes

    logo_path = _resolve_logo_path()
    if logo_path is None:
        return image_bytes

    try:
        photo = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")
    except Exception:
        log.exception("Не удалось открыть photo/logo, возвращаю исходные bytes")
        return image_bytes

    size_ratio = _env_float("LOGO_SIZE_RATIO", 0.20)
    padding_ratio = _env_float("LOGO_PADDING_RATIO", 0.04)
    halo_color = _parse_color(os.getenv("LOGO_HALO_COLOR", ""), default=(252, 248, 238))
    quality = _env_int("LOGO_OUTPUT_QUALITY", 92)

    try:
        # --- 1. Контраст лого: темнее + плотнее ---
        r, g, b, a = logo.split()
        rgb = Image.merge("RGB", (r, g, b))
        rgb = ImageEnhance.Brightness(rgb).enhance(0.45)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.7)
        r2, g2, b2 = rgb.split()
        a = a.point(lambda x: min(255, int(x * 1.9)))
        logo_boosted = Image.merge("RGBA", (r2, g2, b2, a))

        # --- 2. Размер лого ---
        target_h = int(photo.height * size_ratio)
        ratio = target_h / logo_boosted.height
        target_w = int(logo_boosted.width * ratio)
        logo_resized = logo_boosted.resize((target_w, target_h), Image.LANCZOS)

        # --- 3. Круглая радиальная подложка (квадратный холст, альфа в 0 у краёв) ---
        side = int(max(target_w, target_h) * 2.2)
        cx = cy = side / 2
        y_idx, x_idx = np.indices((side, side))
        dist = np.sqrt((x_idx - cx) ** 2 + (y_idx - cy) ** 2)
        max_r = side / 2
        norm = np.clip(dist / max_r, 0, 1)
        # easing 2.5: яркий центр, длинный мягкий хвост, точный 0 на границе
        alpha_map = (np.clip(1 - norm, 0, 1) ** 2.5) * 210
        alpha_map = alpha_map.astype(np.uint8)
        cream = np.full((side, side, 3), list(halo_color), dtype=np.uint8)
        halo_arr = np.dstack([cream, alpha_map])
        halo = Image.fromarray(halo_arr)
        # Доп. blur для отсутствия banding
        halo = halo.filter(ImageFilter.GaussianBlur(radius=side // 30))

        # --- 4. Координаты: правый низ ---
        pad_x = int(photo.width * padding_ratio)
        pad_y = int(photo.height * padding_ratio)
        logo_pos = (photo.width - target_w - pad_x, photo.height - target_h - pad_y)
        logo_cx = logo_pos[0] + target_w // 2
        logo_cy = logo_pos[1] + target_h // 2
        halo_pos = (logo_cx - side // 2, logo_cy - side // 2)

        # --- 5. Композ: фото → подложка → лого ---
        result = photo.copy()
        result.paste(halo, halo_pos, halo)
        result.paste(logo_resized, logo_pos, logo_resized)

        # --- 6. JPEG bytes ---
        out = io.BytesIO()
        result.convert("RGB").save(out, "JPEG", quality=quality)
        out.seek(0)
        return out.getvalue()
    except Exception:
        log.exception("Ошибка при наложении лого, возвращаю исходные bytes")
        return image_bytes


# ============ CLI для отладки ============

def _cli():
    import sys
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if len(sys.argv) < 3:
        print("Usage: python -m tools.logo_overlay <input_image> <output_image>")
        print("Example: python -m tools.logo_overlay photo.jpg photo-branded.jpg")
        sys.exit(1)

    in_path = Path(sys.argv[1])
    out_path = Path(sys.argv[2])

    if not in_path.exists():
        print(f"Не найден файл: {in_path}")
        sys.exit(1)

    src = in_path.read_bytes()
    result = add_logo(src)
    out_path.write_bytes(result)

    if result == src:
        print(f"WARNING: возвращены исходные bytes (см. лог) → {out_path}")
        sys.exit(2)
    print(f"OK: {out_path} ({len(result)} bytes)")


if __name__ == "__main__":
    _cli()
