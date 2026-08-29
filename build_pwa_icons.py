#!/usr/bin/env python3
"""Generate the app mark for every place it is needed, from one definition.

Run: venv/bin/python build_pwa_icons.py

Outputs:
  ystocker/static/img/pwa-icon-{180,192,512}.png      web manifest + apple-touch-icon
  ystocker/static/img/pwa-icon-maskable-512.png       Android maskable
  ios/TradeAgents/TradeAgents/Assets.xcassets/...     iOS app icon (1024, universal)

Neither iOS's home screen nor the web app manifest will take an SVG, and both want
PNG at fixed pixel sizes. Rather than add a rasteriser dependency (cairosvg needs
the cairo system library), the shapes are drawn with Pillow, already a dependency
via yimage.

Four things here are not arbitrary:

* **Full bleed, no transparency.** iOS composites an icon onto black and applies its
  own corner rounding, so an icon with rounded corners of its own shows black wedges
  outside them. The background fills the canvas and the platform does the rounding.
* **The fourth bar is opaque.** favicon.svg draws it at 0.6 alpha, which is fine at
  32px in a browser tab but disappears entirely in a 40px home-screen icon. An app
  icon is read at a glance and at a small size, so every element has to carry.
* **The background is a gradient, not the flat slate of the favicon.** A flat dark
  square reads as a missing icon on a home screen full of gradients; the diagonal
  gives it depth without adding a shape to interpret.
* **The maskable variant is drawn smaller.** Android may crop it to any shape within
  the central 80%, so its content is scaled to 60% and centred. Shipping the same art
  for both purposes gets the bars shaved off on some launchers.

Supersampled 4x and downsampled because Pillow's rounded_rectangle does not
antialias.
"""

from __future__ import annotations

import json
import pathlib

from PIL import Image, ImageDraw

ROOT = pathlib.Path(__file__).resolve().parent
WEB_OUT = ROOT / "ystocker" / "static" / "img"
IOS_OUT = (ROOT / "ios" / "TradeAgents" / "TradeAgents"
           / "Assets.xcassets" / "AppIcon.appiconset")

SS = 4                                  # supersample factor
GRAD_TOP = (17, 24, 46)                 # deep indigo-navy
GRAD_BOTTOM = (8, 13, 28)               # near slate-950

# (x, y, w, h, radius, fill) in a 32x32 design space, matching favicon.svg's
# composition so the browser tab, the installed web app and the iOS app agree.
BARS = [
    (4,  18, 5, 10, 1.5, (99, 102, 241, 255)),    # indigo-500
    (11, 12, 5, 16, 1.5, (56, 189, 248, 255)),    # sky-400
    (18,  7, 5, 21, 1.5, (52, 211, 153, 255)),    # emerald-400
    (25, 14, 3, 14, 1.5, (129, 140, 248, 255)),   # indigo-400, opaque (see docstring)
]


def _gradient(size: int) -> Image.Image:
    """Vertical gradient, drawn once per row rather than per pixel."""
    img = Image.new("RGB", (1, size))
    d = ImageDraw.Draw(img)
    for y in range(size):
        t = y / max(size - 1, 1)
        d.point((0, y), fill=tuple(
            round(a + (b - a) * t) for a, b in zip(GRAD_TOP, GRAD_BOTTOM)
        ))
    return img.resize((size, size), Image.BILINEAR)


def render(size: int, content_scale: float = 1.0) -> Image.Image:
    """Draw the icon at ``size`` px, content occupying ``content_scale`` of it."""
    big = size * SS
    img = _gradient(big).convert("RGBA")
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

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


def _write(path: pathlib.Path, img: Image.Image) -> pathlib.Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, "PNG", optimize=True)
    return path


def main() -> None:
    written = []

    # Web: manifest icons plus the iOS home-screen icon for the installed web app.
    for size in (180, 192, 512):
        written.append(_write(WEB_OUT / f"pwa-icon-{size}.png", render(size)))
    written.append(_write(WEB_OUT / "pwa-icon-maskable-512.png",
                          render(512, content_scale=0.60)))

    # iOS: a single 1024 universal entry is all modern Xcode needs; it derives the
    # rest at build time, so there is no list of a dozen sizes to keep in step.
    written.append(_write(IOS_OUT / "AppIcon-1024.png", render(1024)))
    (IOS_OUT / "Contents.json").write_text(json.dumps({
        "images": [{
            "filename": "AppIcon-1024.png",
            "idiom": "universal",
            "platform": "ios",
            "size": "1024x1024",
        }],
        "info": {"author": "xcode", "version": 1},
    }, indent=2) + "\n")
    (IOS_OUT.parent / "Contents.json").write_text(json.dumps({
        "info": {"author": "xcode", "version": 1},
    }, indent=2) + "\n")
    written.append(IOS_OUT / "Contents.json")

    for p in written:
        rel = p.relative_to(ROOT)
        size = f"{p.stat().st_size / 1024:.1f} KB"
        print(f"  {rel}  {size}")


if __name__ == "__main__":
    main()
