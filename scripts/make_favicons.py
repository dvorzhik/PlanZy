"""Генерация растровых favicon для PlanZy без внешних зависимостей.

Рисует шахматного коня на фирменном синем фоне (brand-500 #3b6fe0)
и сохраняет PNG (32x32, 180x180, 192x192) и ICO (16/32/48)
в app/web/static/.

Используется только стандартная библиотека: zlib + struct.

Запуск:
    python -m scripts.make_favicons
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

# Цвета фирменного стиля (совпадают с Tailwind-конфигом в шаблонах).
BRAND = (0x3B, 0x6F, 0xE0)
WHITE = (0xFF, 0xFF, 0xFF)

STATIC_DIR = Path(__file__).resolve().parent.parent / "app" / "web" / "static"

# --- Силуэт коня 16x16 (1 — закрашенный пиксель) ---
# Низкополигональный профиль: уши, грива, морда, шея, грудь, основание.
KNIGHT_16 = [
    "................",
    ".......##.......",
    "......####...##.",
    ".....#####..####",
    "....######.#####",
    "....############",
    "....############",
    "...#####....####",
    "...####......###",
    "..####........##",
    "..###.........##",
    ".####.........##",
    ".###############",
    ".###############",
    "................",
    "................",
]

# --- Силуэт коня 32x32: плотный узнаваемый профиль ---
# Уши, чёлка, морда, ганаш, шея, грудь и основание — как у шахматного коня.
KNIGHT_32 = [
    "................................",
    "..............##................",
    ".............####...............",
    "............#####...............",
    "...........######....#####......",
    "...........#######.########.....",
    "..........#####################.",
    "..........#####################.",
    ".........######################.",
    ".........######################.",
    ".........######################.",
    ".........#########....#########.",
    ".........#######........########",
    ".........#######.........#######",
    "........########.........######.",
    "........########..........#####.",
    ".......#########..........######",
    ".......########.............####",
    ".......#######..............####",
    "......########..............####",
    "......########..............####",
    ".....#########..............####",
    ".....###########################",
    ".....###########################",
    ".....###########################",
    "................................",
    "................................",
    "................................",
    "................................",
    "................................",
    "................................",
    "................................",
]


def _parse(rows: list[str]) -> list[list[bool]]:
    """Преобразует строковую матрицу в булеву."""
    return [[ch == "#" for ch in row] for row in rows]


def _is_corner(x: int, y: int, size: int) -> bool:
    """Проверяет, попадает ли пиксель в срезанный угол (скругление)."""
    radius = max(2, size // 8)
    cx = radius - 1 if x < radius else (size - radius if x >= size - radius else x)
    cy = radius - 1 if y < radius else (size - radius if y >= size - radius else y)
    if (x < radius or x >= size - radius) and (y < radius or y >= size - radius):
        dx = x - cx
        dy = y - cy
        return dx * dx + dy * dy > radius * radius
    return False


def _render(
    mask: list[list[bool]], size: int, rounded: bool = True, samples: int = 3
) -> list[list[tuple[int, int, int]]]:
    """Собирает пиксели: фирменный фон + белый силуэт коня.

    Args:
        mask: Булева матрица силуэта.
        size: Размер итогового изображения в пикселях.
        rounded: Скруглять ли углы.
        samples: Множитель суперсемплинга (сглаживание краёв).
            При значении >1 результат усредняется по подпикселям.
    """
    src_h = len(mask)
    src_w = len(mask[0])
    pixels: list[list[tuple[int, int, int]]] = []

    for y in range(size):
        row: list[tuple[int, int, int]] = []
        for x in range(size):
            if rounded and _is_corner(x, y, size):
                row.append((0, 0, 0))
                continue

            # Суперсемплинг: усредняем цвета подпикселей.
            acc_r = acc_g = acc_b = 0
            for sy_ in range(samples):
                for sx_ in range(samples):
                    fx = x + (sx_ + 0.5) / samples
                    fy = y + (sy_ + 0.5) / samples
                    src_x = min(src_w - 1, int(fx * src_w / size))
                    src_y = min(src_h - 1, int(fy * src_h / size))
                    color = WHITE if mask[src_y][src_x] else BRAND
                    acc_r += color[0]
                    acc_g += color[1]
                    acc_b += color[2]

            total = samples * samples
            row.append((acc_r // total, acc_g // total, acc_b // total))
        pixels.append(row)
    return pixels


def _png_bytes(pixels: list[list[tuple[int, int, int]]]) -> bytes:
    """Собирает RGBA PNG-изображение в байтах."""
    height = len(pixels)
    width = len(pixels[0])

    raw = bytearray()
    for row in pixels:
        raw.append(0)  # фильтр 0 (None)
        for r, g, b in row:
            raw += bytes((r, g, b, 255))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", ihdr)
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    """Собирает один чанк PNG."""
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def _write_png(path: Path, pixels: list[list[tuple[int, int, int]]]) -> None:
    """Записывает RGBA PNG-файл."""
    path.write_bytes(_png_bytes(pixels))


def _write_ico(path: Path, images: list[tuple[int, list[list[tuple[int, int, int]]]]]) -> None:
    """Собирает ICO из PNG-изображений (ICO поддерживает встраивание PNG)."""
    blobs = [(size, _png_bytes(pixels)) for size, pixels in images]

    header = struct.pack("<HHH", 0, 1, len(blobs))
    offset = len(header) + 16 * len(blobs)

    directory = b""
    for size, png in blobs:
        dim = 0 if size >= 256 else size
        directory += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        offset += len(png)

    path.write_bytes(header + directory + b"".join(png for _size, png in blobs))


def main() -> None:
    """Собирает все favicon-файлы."""
    STATIC_DIR.mkdir(parents=True, exist_ok=True)

    mask16 = _parse(KNIGHT_16)
    mask32 = _parse(KNIGHT_32)

    _write_png(STATIC_DIR / "favicon-32.png", _render(mask32, 32))
    _write_png(STATIC_DIR / "favicon-192.png", _render(mask32, 192))
    _write_png(STATIC_DIR / "apple-touch-icon.png", _render(mask32, 180))

    _write_ico(
        STATIC_DIR / "favicon.ico",
        [
            (16, _render(mask16, 16)),
            (32, _render(mask32, 32)),
            (48, _render(mask32, 48)),
        ],
    )

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
