"""Иконка программы. Рисуется кодом — отдельные файлы с картинками не нужны."""
from __future__ import annotations

import base64
import io
import math
from pathlib import Path

from PIL import Image, ImageDraw

ACCENT = (139, 92, 246)  # фиолетовый — основной цвет иконки


def _ellipse(cx: float, cy: float, rx: float, ry: float, angle_deg: float, n: int = 72):
    a = math.radians(angle_deg)
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x, y = rx * math.cos(t), ry * math.sin(t)
        pts.append((cx + x * math.cos(a) - y * math.sin(a), cy + x * math.sin(a) + y * math.cos(a)))
    return pts


def make_icon(color: tuple = ACCENT, size: int = 64) -> Image.Image:
    """Цветной круг с белыми нотами. Рисуется крупно и уменьшается — края получаются гладкими."""
    big = max(size, 64) * 8
    k = big / 64
    img = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    white = (255, 255, 255, 255)

    def pts(points):
        return [(x * k, y * k) for x, y in points]

    d.ellipse((2 * k, 2 * k, 62 * k, 62 * k), fill=tuple(color) + (255,))
    for cx, cy in ((23.5, 44.5), (40.5, 40.5)):            # головки нот
        d.polygon(pts(_ellipse(cx, cy, 7.6, 5.6, -24)), fill=white)
    d.rectangle((28.2 * k, 19 * k, 31.4 * k, 44 * k), fill=white)   # штили
    d.rectangle((45.2 * k, 17 * k, 48.4 * k, 40 * k), fill=white)
    d.polygon(pts([(28.2, 19), (48.4, 14.6), (48.4, 21.6), (28.2, 26)]), fill=white)  # перекладина
    return img.resize((size, size), getattr(Image, "Resampling", Image).LANCZOS)


def png_bytes(color: tuple = ACCENT, size: int = 64) -> bytes:
    buf = io.BytesIO()
    make_icon(color, size).save(buf, format="PNG")
    return buf.getvalue()


def tk_png_data(color: tuple = ACCENT, size: int = 64) -> str:
    """PNG в base64 — годится для tkinter.PhotoImage(data=...)."""
    return base64.b64encode(png_bytes(color, size)).decode("ascii")


def save_files(folder: Path) -> None:
    """Пересоздаёт assets/app.png и assets/app.ico."""
    folder.mkdir(parents=True, exist_ok=True)
    make_icon(ACCENT, 512).save(folder / "app.png")
    make_icon(ACCENT, 256).save(
        folder / "app.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


if __name__ == "__main__":
    save_files(Path(__file__).resolve().parent.parent / "assets")
    print("assets/app.png и assets/app.ico обновлены")
