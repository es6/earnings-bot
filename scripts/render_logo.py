"""Renders the First Print stopwatch mark to PNG with Pillow (4x supersampled), from the same
geometry as assets/first-print.svg (a 112-unit square). Pillow isn't a project dependency:

    uv run --no-project --with pillow python scripts/render_logo.py assets
"""

import sys
from pathlib import Path

from PIL import Image, ImageDraw

NAVY, LIGHT, MINT = "#0B1F3A", "#E8F1EC", "#2BD49A"


def render(size: int, square: bool) -> Image.Image:
    big = size * 4
    k = big / 112
    image = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    if square:
        d.rectangle([0, 0, big, big], fill=NAVY)
    else:
        d.ellipse([0, 0, big - 1, big - 1], fill=NAVY)
    d.rounded_rectangle([48 * k, 12 * k, 64 * k, 21 * k], radius=3 * k, fill=LIGHT)
    cx, cy, r, w = 56 * k, 62 * k, 30 * k, 7 * k
    d.pieslice([cx - r, cy - r, cx + r, cy + r], start=270, end=360, fill=MINT)
    d.ellipse(
        [cx - r - w / 2, cy - r - w / 2, cx + r + w / 2, cy + r + w / 2],
        outline=LIGHT,
        width=round(w),
    )
    for x1, y1, x2, y2 in ((18, 62, 94, 62), (56, 26, 56, 98)):
        # A line with round caps is a bar with fully rounded ends
        box = [x1 * k - w / 2, y1 * k - w / 2, x2 * k + w / 2, y2 * k + w / 2]
        d.rounded_rectangle(box, radius=w / 2, fill=LIGHT)
    return image.resize((size, size), Image.LANCZOS)


out = Path(sys.argv[1])
render(1024, square=True).save(out / "first-print-avatar.png")
render(512, square=False).save(out / "first-print.png")
print("ok")
