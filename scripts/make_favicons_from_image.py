"""Генерация favicon PlanZy из готового изображения (JPG/PNG).

Скрипт ``scripts/make_favicons.py`` рисует иконку из символьной матрицы,
поэтому не подходит, когда логотип нарисован дизайнером. Здесь исходником
служит растровое изображение, а результатом — тот же набор файлов в
``app/web/static/``, который подключают шаблоны ``base.html`` и
``admin/base.html``:

    * ``favicon-32.png``        — 32x32 со скруглёнными углами;
    * ``favicon-192.png``       — 192x192 для Android / PWA;
    * ``apple-touch-icon.png``  — 180x180 для iOS;
    * ``favicon.ico``           — 16/32/48 с встроенными PNG (legacy);
    * ``favicon.svg``           — обёртка с тем же растром в base64 для
      современных браузеров.

Требуется Pillow (в проект он не входит, поэтому скрипт считается разовым
инструментом и запускается в окружении, где Pillow установлен):

    pip install pillow
    python -m scripts.make_favicons_from_image planzy_favicon.jpg
"""

from __future__ import annotations

import argparse
import base64
import io
import struct
from pathlib import Path

from PIL import Image, ImageDraw

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"

# Размеры артефактов совпадают с прежними названиями файлов.
PNG_SIZES = {
    "favicon-32.png": 32,
    "favicon-192.png": 192,
    "apple-touch-icon.png": 180,
}
ICO_SIZES = (16, 32, 48)
# Размер растра, встраиваемого в favicon.svg.
SVG_EMBED_SIZE = 64
# Доля стороны, приходящаяся на радиус скругления в PNG-иконке.
CORNER_RADIUS_RATIO = 0.22


def _load_square(source: Path) -> Image.Image:
    """Читает изображение и приводит его к квадрату RGB.

    Исходник может быть неквадратным: лишние края обрезаются по центру,
    чтобы логотип не искажался при масштабировании.

    Args:
        source: Путь к исходному изображению.

    Returns:
        Квадратное изображение в режиме RGB.

    Raises:
        FileNotFoundError: Если файл-источник не найден.
    """
    if not source.exists():
        raise FileNotFoundError(f"Не найден исходный файл: {source}")

    with Image.open(source) as opened:
        image = opened.convert("RGB")

    width, height = image.size
    side = min(width, height)
    if width != height:
        left = (width - side) // 2
        top = (height - side) // 2
        image = image.crop((left, top, left + side, top + side))
    return image


def _resize(image: Image.Image, size: int) -> Image.Image:
    """Уменьшает изображение методом LANCZOS (без потери резкости)."""
    return image.resize((size, size), Image.LANCZOS)


def _round_corners(image: Image.Image, radius_ratio: float = CORNER_RADIUS_RATIO) -> Image.Image:
    """Скругляет углы и делает фон за ними прозрачным.

    Args:
        image: Квадратное изображение RGB.
        radius_ratio: Доля стороны, приходящаяся на радиус скругления.

    Returns:
        Изображение RGBA со скруглёнными углами.
    """
    size = image.size[0]
    result = image.convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size - 1, size - 1),
        radius=max(1, round(size * radius_ratio)),
        fill=255,
    )
    result.putalpha(mask)
    return result


def _png_bytes(image: Image.Image) -> bytes:
    """Сериализует изображение в PNG внутри памяти."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _write_png(path: Path, image: Image.Image) -> None:
    """Записывает PNG-файл."""
    path.write_bytes(_png_bytes(image))


def _write_ico(path: Path, images: list[tuple[int, Image.Image]]) -> None:
    """Собирает ICO из PNG-изображений (ICO поддерживает встраивание PNG).

    Args:
        path: Куда записать файл.
        images: Пары «размер — изображение». Формат ICO разрешает не более
            256x256, поэтому список ограничен значениями ``ICO_SIZES``.
    """
    blobs = [(size, _png_bytes(image)) for size, image in images]

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = len(header) + 16 * len(blobs)

    directory = b""
    for size, png in blobs:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)

    path.write_bytes(header + directory + b"".join(png for _size, png in blobs))


def _write_svg(path: Path, image: Image.Image) -> None:
    """Генерирует favicon.svg с растром, встроенным в base64.

    Полноценный вектор из растра не получить, но обёртка нужна: шаблоны
    подключают ``favicon.svg`` первым, и без него браузер показал бы
    старую иконку.

    Args:
        path: Куда записать SVG.
        image: Изображение, которое встраивается в виде data-URI.
    """
    encoded = base64.b64encode(_png_bytes(image)).decode("ascii")
    data_uri = f"data:image/png;base64,{encoded}"
    size = image.size[0]
    svg = (
        '<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        'role="img" aria-label="PlanZy">\n'
        "  <title>PlanZy</title>\n"
        f'  <image width="{size}" height="{size}" xlink:href="{data_uri}" href="{data_uri}"/>\n'
        "</svg>\n"
    )
    path.write_text(svg, encoding="utf-8")


def main() -> None:
    """Собирает все favicon-файлы из исходного изображения."""
    parser = argparse.ArgumentParser(description="Генерация favicon PlanZy из картинки")
    parser.add_argument(
        "source",
        nargs="?",
        default="planzy_favicon.jpg",
        help="Путь к исходному изображению (по умолчанию planzy_favicon.jpg в корне проекта)",
    )
    args = parser.parse_args()

    source = Path(args.source)
    if not source.is_absolute():
        source = Path(__file__).resolve().parent.parent / source

    base = _load_square(source)
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    for name, size in PNG_SIZES.items():
        _write_png(STATIC_DIR / name, _round_corners(_resize(base, size)))

    # ICO: мелкие размеры без прозрачности по углам выглядят чище.
    _write_ico(
        STATIC_DIR / "favicon.ico",
        [(size, _resize(base, size)) for size in ICO_SIZES],
    )

    _write_svg(STATIC_DIR / "favicon.svg", _resize(base, SVG_EMBED_SIZE))

    print(f"Исходник: {source}")
    print("Сгенерированы favicon:")
    for name in (
        "favicon.svg",
        "favicon.ico",
        "favicon-32.png",
        "favicon-192.png",
        "apple-touch-icon.png",
    ):
        path = STATIC_DIR / name
        if path.exists():
            print(f"  {name:24} {path.stat().st_size:7} bytes")
        else:
            print(f"  {name:24} MISSING")


if __name__ == "__main__":
    main()

