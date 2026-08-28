#!/usr/bin/env python3
"""Generate the PWA / apple-touch icons from the favicon design.

Run: venv/bin/python build_pwa_icons.py

The favicon is an SVG, which neither iOS's home screen nor the web app manifest
will take -- both want PNG at fixed pixel sizes. Rather than add a rasteriser
dependency (cairosvg needs the cairo system library), the same five shapes are
drawn with Pillow, which is already a dependency via yimage.

Two things here are not arbitrary:

* **Full bleed, no transparency.** iOS composites an apple-touch-icon onto black
  and applies its own corner rounding. An icon with rounded corners of its own
  therefore shows black wedges outside them, so the background fills the canvas
  and iOS does the rounding.
* **The maskable variant is drawn smaller.** Android may crop a maskable icon to
  any shape within the central 80%, so its content is scaled to 60% and centred,
  leaving a margin that can be cropped away without clipping a bar. Shipping the
  same art for both purposes gets the bars shaved off on some launchers.

Supersampled 4x and downsampled because Pillow's rounded_rectangle does not
antialias.
"""

from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

OUT = pathlib.Path(__file__).resolve().parent / "ystocker" / "static" / "img"
BG = (30, 41, 59)          # #1e293b, matches favicon.svg and the page chrome
SS = 4                     # supersample factor

# (x, y, w, h, radius, fill) in the favicon's 32x32 coordinate space.
BARS = [
    (4,  18, 5, 10, 1.5, (99, 102, 241, 255)),    # #6366f1
    (11, 12, 5, 16, 1.5, (56, 189, 248, 255)),    # #38bdf8
    (18,  7, 5, 21, 1.5, (52, 211, 153, 255)),    # #34d399
    (25, 14, 3, 14, 1.5, (99, 102, 241, 153)),    # #6366f1 at 0.6 alpha
]


def render(size: int, content_scale: float = 1.0) -> Image.Image:
    """Draw the icon at ``size`` px, content occupying ``content_scale`` of it."""
    big = size * SS
    img = Image.new("RGBA", (big, big), BG + (255,))
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    # Map the 32-unit design space onto the scaled content box, centred.
    span = big * content_scale
    unit = span / 32.0
    off = (big - span) / 2.0
    for x, y, w, h, r, fill in BARS:
        d.rounded_rectangle(
            [off + x * unit, off + y * unit,
             off + (x + w) * unit, off + (y + h) * unit],
            radius=r * unit, fill=fill,
        )

    img = Image.alpha_composite(img, layer)
    return img.resize((size, size), Image.LANCZOS).convert("RGB")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    written = []
    # purpose "any" + the iOS home screen icon. iOS only reads the 180.
    for size in (180, 192, 512):
        p = OUT / f"pwa-icon-{size}.png"
        render(size).save(p, "PNG", optimize=True)
        written.append(p)
    # purpose "maskable": same art, inset so a launcher can crop it to a circle.
    p = OUT / "pwa-icon-maskable-512.png"
    render(512, content_scale=0.60).save(p, "PNG", optimize=True)
    written.append(p)

    for p in written:
        print(f"  {p.relative_to(OUT.parents[2])}  {p.stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
