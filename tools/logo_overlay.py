"""
Наложение брендового водяного знака на сгенерированную обложку.

Композиция: тёмная градиентная подложка со скруглёнными углами →
белый лев (из assets/logo-watermark.png) → «ЛИКВИДАТОР» (Manrope) →
«pravo.shop» меньшим шрифтом под названием.

Используется в tools/image_gen.py между fal.ai (генерация) и Cloudinary
(загрузка): получаем bytes картинки, накладываем знак, отдаём готовые
bytes на загрузку.

ENV переменные:
    LOGO_OVERLAY_ENABLED       - "true"/"false", по умолчанию "true"
    LOGO_PATH                  - путь к PNG лева (с прозрачностью, без текста).
                                 По умолчанию assets/logo-watermark-lion.png.
    LOGO_FONT_PATH             - путь к TTF шрифту.
                                 По умолчанию assets/fonts/cormorant-garamond-700.ttf.
    LOGO_SIZE_RATIO            - доля высоты фото для высоты «знака»
                                 (лев + текст), по умолчанию 0.1235.
    LOGO_PADDING_RATIO         - отступ подложки от края фото (общий x/y),
                                 по умолчанию 0.04.
    LOGO_PADDING_X_RATIO       - отдельный отступ справа (если задан,
                                 переопределяет горизонтальный).
                                 По умолчанию 0.02.
    LOGO_PADDING_Y_RATIO       - отдельный отступ снизу (если задан,
                                 переопределяет вертикальный).
    LOGO_BACKDROP_TOP          - hex/«r,g,b» верхней границы градиента,
                                 по умолчанию "#0a0d12".
    LOGO_BACKDROP_BOTTOM       - hex/«r,g,b» нижней границы градиента,
                                 по умолчанию "#1a1f28".
    LOGO_BACKDROP_ALPHA        - 0..255, плотность плотного ядра подложки
                                 (по умолчанию 230).
    LOGO_BACKDROP_FADE_RATIO   - доля радиуса размытия от высоты подложки;
                                 чем больше, тем плавнее переход в прозрачность
                                 (по умолчанию 0.50).
    LOGO_OUTPUT_QUALITY        - JPEG quality 1..100, по умолчанию 92.

Поведение при ошибках:
- Если PIL/numpy не установлены или лого/шрифт не найдены - возвращаем
  оригинальные bytes без изменений (warning в лог). Пайплайн не падает.
- Если LOGO_OVERLAY_ENABLED=false - возвращаем оригинальные bytes сразу.
"""
from __future__ import annotations

import io
import logging
import os
from pathlib import Path
from typing import Optional

log = logging.getLogger("logo_overlay")


TITLE_TEXT = "ЛИКВИДАТОР"
SUBTITLE_TEXT = "pravo.shop"


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
    if s.startswith("#"):
        s = s[1:]
    if len(s) == 6 and all(c in "0123456789abcdefABCDEF" for c in s):
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    parts = [p.strip() for p in s.split(",")]
    if len(parts) == 3 and all(p.isdigit() for p in parts):
        return (int(parts[0]), int(parts[1]), int(parts[2]))
    log.warning("Цвет %r не распознан, использую дефолт %s", s, default)
    return default


def _resolve_path(env_name: str, default_rel: str) -> Optional[Path]:
    raw = os.getenv(env_name, default_rel).strip()
    p = Path(raw)
    if p.is_absolute() and p.exists():
        return p
    project_root = Path(__file__).resolve().parent.parent
    candidate = project_root / raw
    if candidate.exists():
        return candidate
    log.warning("%s=%r не найден ни как абсолютный, ни относительно %s", env_name, raw, project_root)
    return None


# ============ ХЕЛПЕРЫ ============

def _white_silhouette(logo_rgba):
    """RGBA → белая силуэт-копия с сохранённой альфой."""
    from PIL import Image  # type: ignore
    _, _, _, a = logo_rgba.split()
    white = Image.new("L", logo_rgba.size, 255)
    return Image.merge("RGBA", (white, white, white, a))


def _measure_tracked(text: str, font, tracking_px: int):
    """Длина строки c letter-spacing (учитывает пробелы между литерами)."""
    if not text:
        return 0, 0
    widths = []
    max_h = 0
    for ch in text:
        bbox = font.getbbox(ch)
        widths.append(bbox[2] - bbox[0])
        max_h = max(max_h, bbox[3] - bbox[1])
    total = sum(widths) + tracking_px * (len(text) - 1)
    return total, max_h


def _draw_tracked(draw, xy, text: str, font, fill, tracking_px: int):
    """Рисует текст с явным letter-spacing символ за символом."""
    x, y = xy
    for ch in text:
        bbox = font.getbbox(ch)
        draw.text((x - bbox[0], y), ch, font=font, fill=fill)
        x += (bbox[2] - bbox[0]) + tracking_px


# ============ ОСНОВНАЯ ЛОГИКА ============

def add_logo(image_bytes: bytes) -> bytes:
    """
    Накладывает брендовый водяной знак и возвращает JPEG bytes.

    При любой ошибке возвращает исходные bytes без изменений - пайплайн
    генерации обложек не должен падать из-за визуального оверлея.
    """
    if not _env_bool("LOGO_OVERLAY_ENABLED", default=True):
        log.info("LOGO_OVERLAY_ENABLED=false, возвращаю картинку без знака")
        return image_bytes

    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont  # type: ignore
        import numpy as np  # type: ignore
    except ImportError:
        log.exception("Pillow или numpy не установлены, пропускаю наложение знака")
        return image_bytes

    logo_path = _resolve_path("LOGO_PATH", "assets/logo-watermark-lion.png")
    font_path = _resolve_path("LOGO_FONT_PATH", "assets/fonts/cormorant-garamond-700.ttf")
    if logo_path is None or font_path is None:
        return image_bytes

    try:
        photo = Image.open(io.BytesIO(image_bytes)).convert("RGBA")
        logo = Image.open(logo_path).convert("RGBA")
    except Exception:
        log.exception("Не удалось открыть photo/logo, возвращаю исходные bytes")
        return image_bytes

    size_ratio = _env_float("LOGO_SIZE_RATIO", 0.1235)
    padding_ratio = _env_float("LOGO_PADDING_RATIO", 0.04)
    padding_x_ratio = _env_float("LOGO_PADDING_X_RATIO", 0.005)
    padding_y_ratio = _env_float("LOGO_PADDING_Y_RATIO", padding_ratio)
    backdrop_top = _parse_color(os.getenv("LOGO_BACKDROP_TOP", ""), default=(10, 13, 18))
    backdrop_bot = _parse_color(os.getenv("LOGO_BACKDROP_BOTTOM", ""), default=(26, 31, 40))
    backdrop_alpha = max(0, min(255, _env_int("LOGO_BACKDROP_ALPHA", 230)))
    fade_ratio = _env_float("LOGO_BACKDROP_FADE_RATIO", 0.50)
    quality = _env_int("LOGO_OUTPUT_QUALITY", 92)

    try:
        # --- 1. Базовая «дизайн-высота» (для отступов и подзаголовка) ---
        wm_h = max(48, int(photo.height * size_ratio))

        # --- 2. Белый лев ---
        white_logo = _white_silhouette(logo)
        lion_h = max(24, int(wm_h * 0.55))
        lion_w = int(white_logo.width * (lion_h / white_logo.height))
        lion = white_logo.resize((lion_w, lion_h), Image.LANCZOS)

        # --- 3. Текст: ЛИКВИДАТОР (компактный) + pravo.shop (без изменений) ---
        title_size = max(12, int(wm_h * 0.46 * 0.4))
        sub_size = max(8, int(wm_h * 0.20))
        title_font = ImageFont.truetype(str(font_path), title_size)
        sub_font = ImageFont.truetype(str(font_path), sub_size)

        title_tracking = max(1, int(title_size * 0.06))
        sub_tracking = max(1, int(sub_size * 0.10))

        title_w, _ = _measure_tracked(TITLE_TEXT, title_font, title_tracking)
        sub_w, _ = _measure_tracked(SUBTITLE_TEXT, sub_font, sub_tracking)
        text_w = max(title_w, sub_w)

        # Реальные bbox видимой части глифов (через временный draw для точных метрик)
        _probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
        title_bbox = _probe.textbbox((0, 0), TITLE_TEXT, font=title_font)
        sub_bbox = _probe.textbbox((0, 0), SUBTITLE_TEXT, font=sub_font)
        title_h = title_bbox[3] - title_bbox[1]
        sub_h = sub_bbox[3] - sub_bbox[1]
        # сдвиги, чтобы при рисовании от точки (x, y) верх глифа оказался ровно на y
        title_yo = -title_bbox[1]
        sub_yo = -sub_bbox[1]

        gap_text = max(1, int(wm_h * 0.03))
        text_block_h = title_h + gap_text + sub_h

        # --- 4. Размер подложки ---
        # core - плотная зона за контентом; fade - радиус мягкого затухания вокруг.
        gap_lion_text = max(4, int(wm_h * 0.08))
        core_pad_x = max(12, int(wm_h * 0.32))
        core_pad_y = max(10, int(wm_h * 0.24))
        content_w = lion_w + gap_lion_text + text_w
        content_h = max(lion_h, text_block_h)
        core_w = content_w + 2 * core_pad_x
        core_h = content_h + 2 * core_pad_y

        fade_px = max(8, int(core_h * fade_ratio))
        bg_w = core_w + 2 * fade_px
        bg_h = core_h + 2 * fade_px
        radius = max(8, int(core_h * 0.22))

        # --- 5. Градиентная RGBA-подложка с плавным фейдом в прозрачность ---
        # Цвет: вертикальный линейный градиент по всей подложке.
        yy = np.linspace(0.0, 1.0, bg_h, dtype=np.float32).reshape(bg_h, 1, 1)
        top_arr = np.array(backdrop_top, dtype=np.float32).reshape(1, 1, 3)
        bot_arr = np.array(backdrop_bot, dtype=np.float32).reshape(1, 1, 3)
        grad_rgb = (top_arr * (1 - yy) + bot_arr * yy)
        grad_rgb = np.broadcast_to(grad_rgb, (bg_h, bg_w, 3)).astype(np.uint8).copy()

        # Альфа: ядро = solid rounded-rect, затем сильное Gaussian размытие,
        # которое уносит края в полную прозрачность.
        mask = Image.new("L", (bg_w, bg_h), 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            (fade_px, fade_px, bg_w - 1 - fade_px, bg_h - 1 - fade_px),
            radius=radius,
            fill=backdrop_alpha,
        )
        mask = mask.filter(ImageFilter.GaussianBlur(radius=fade_px * 0.55))

        backdrop = Image.fromarray(
            np.dstack([grad_rgb, np.array(mask, dtype=np.uint8)])
        )

        # --- 6. Композ контента поверх подложки ---
        wm = backdrop.copy()
        # Лев (по центру вертикали подложки), координаты от внешнего края bg.
        lion_x = fade_px + core_pad_x
        lion_y = (bg_h - lion_h) // 2
        wm.paste(lion, (lion_x, lion_y), lion)

        # Текстовый блок справа от льва, по центру вертикали.
        # У «pravo.shop» есть выносной элемент 'p', который тянет геометрический
        # центр bbox вниз, а визуальный — наоборот. Компенсируем сдвигом вниз
        # на половину descent подзаголовка.
        draw = ImageDraw.Draw(wm)
        text_x = lion_x + lion_w + gap_lion_text
        _, sub_descent = sub_font.getmetrics()
        visual_offset = sub_descent // 2
        text_block_y = (bg_h - text_block_h) // 2 + visual_offset

        # title_yo / sub_yo выравнивают точку рисования по верху видимого глифа
        _draw_tracked(
            draw, (text_x, text_block_y + title_yo),
            TITLE_TEXT, title_font, (255, 255, 255, 255), title_tracking,
        )
        sub_y = text_block_y + title_h + gap_text + sub_yo
        _draw_tracked(
            draw, (text_x, sub_y),
            SUBTITLE_TEXT, sub_font, (255, 255, 255, 240), sub_tracking,
        )

        # --- 7. Размещение знака в правом нижнем углу фото ---
        # margin отсчитывается от ВИДИМОГО края ядра, halo с фейдом может «уходить»
        # ближе к самому краю фото - и это норм, он там и так прозрачный.
        margin_x = int(photo.width * padding_x_ratio)
        margin_y = int(photo.height * padding_y_ratio)
        wm_x = photo.width - margin_x - bg_w + fade_px
        wm_y = photo.height - margin_y - bg_h + fade_px

        result = photo.copy()
        result.paste(wm, (wm_x, wm_y), wm)

        # --- 8. JPEG bytes ---
        out = io.BytesIO()
        result.convert("RGB").save(out, "JPEG", quality=quality)
        out.seek(0)
        return out.getvalue()
    except Exception:
        log.exception("Ошибка при наложении знака, возвращаю исходные bytes")
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
