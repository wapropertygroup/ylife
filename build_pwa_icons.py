#!/usr/bin/env python3
"""Generate the app mark for every place it is needed, from one definition.

Run: venv/bin/python build_pwa_icons.py

Outputs:
  ystocker/static/img/pwa-icon-{180,192,512}.png      web manifest + apple-touch-icon
  ystocker/static/img/pwa-icon-maskable-512.png       Android maskable
  apps/TradeAgents/TradeAgents/Assets.xcassets/...    iOS (1024) + macOS icon set

Neither iOS's home screen nor the web app manifest will take an SVG, and both want
PNG at fixed pixel sizes. Rather than add a rasteriser dependency (cairosvg needs
the cairo system library), the shapes are drawn with Pillow, already a dependency
via yimage.

Five things here are not arbitrary:

* **Full bleed, no transparency — on iOS and the web.** iOS composites an icon onto
  black and applies its own corner rounding, so an icon with rounded corners of its
  own shows black wedges outside them. The background fills the canvas and the
  platform does the rounding.
* **macOS is the exact opposite, and needs its own art.** Nothing rounds a macOS app
  icon for you: the app supplies the shape, on a transparent canvas, inset inside the
  1024 square. Shipping the iOS full-bleed square for macOS puts a hard-edged tile in
  a Dock full of squircles — which is why ``render_macos`` exists rather than reusing
  ``render``. Geometry follows Apple's macOS template: an 824x824 plate centred in
  1024 with a 185px corner radius.
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
# `apps/`, not `ios/`: the Xcode project became multiplatform and was renamed. Left
# pointing at the old path this script would silently recreate a stale `ios/` tree
# and the real app icon would never change.
APP_OUT = (ROOT / "apps" / "TradeAgents" / "TradeAgents"
           / "Assets.xcassets" / "AppIcon.appiconset")

SS = 4                                  # supersample factor
GRAD_TOP = (17, 24, 46)                 # deep indigo-navy
GRAD_BOTTOM = (8, 13, 28)               # near slate-950

# Apple's macOS icon template, in the 1024 design space: an 824 plate centred with a
# 185 corner radius, leaving 100px of transparent margin. The margin is not padding
# to taste -- the Dock, Finder and Launchpad all size against the full 1024 canvas
# and expect the art to sit inside it.
MAC_PLATE = 824 / 1024
MAC_RADIUS = 185 / 1024

#: macOS wants every size explicitly, and reuses one file across two entries where
#: the pixel dimensions coincide (32px is both 16@2x and 32@1x).
MAC_ENTRIES = [
    (16, "1x", 16), (16, "2x", 32),
    (32, "1x", 32), (32, "2x", 64),
    (128, "1x", 128), (128, "2x", 256),
    (256, "1x", 256), (256, "2x", 512),
    (512, "1x", 512), (512, "2x", 1024),
]

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


def _bars(draw: ImageDraw.ImageDraw, span: float, off_x: float, off_y: float) -> None:
    """Draw the four bars into a ``span``-wide box at the given offset."""
    unit = span / 32.0
    for x, y, w, h, r, fill in BARS:
        draw.rounded_rectangle(
            [off_x + x * unit, off_y + y * unit,
             off_x + (x + w) * unit, off_y + (y + h) * unit],
            radius=r * unit, fill=fill,
        )


def render(size: int, content_scale: float = 1.0) -> Image.Image:
    """Draw the icon at ``size`` px, content occupying ``content_scale`` of it."""
    big = size * SS
    img = _gradient(big).convert("RGBA")
    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)

    span = big * content_scale
    off = (big - span) / 2.0
    _bars(d, span, off, off)

    img = Image.alpha_composite(img, layer)
    return img.resize((size, size), Image.LANCZOS).convert("RGB")


def render_macos(size: int) -> Image.Image:
    """Draw the macOS variant: an inset rounded plate on a transparent canvas.

    Returns RGBA, not RGB. The transparency is the point — macOS does not mask an
    app icon, so the margin around the plate has to actually be empty or the icon
    renders as a full-bleed square wherever the system draws it.
    """
    big = size * SS
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))

    plate = big * MAC_PLATE
    off = (big - plate) / 2.0
    radius = big * MAC_RADIUS

    # The gradient is generated at full canvas size and then masked to the plate,
    # rather than drawn into a smaller image, so the ramp runs over the same span it
    # does on the iOS icon and the two read as the same mark.
    mask = Image.new("L", (big, big), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [off, off, off + plate, off + plate], radius=radius, fill=255,
    )
    canvas.paste(_gradient(big).convert("RGBA"), (0, 0), mask)

    layer = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    # Bars at 72% of the plate. Filling it edge to edge crowds the corners once the
    # plate is already inset, and at 16px the outer bars merge into the rounding.
    span = plate * 0.72
    bar_off = (big - span) / 2.0
    _bars(ImageDraw.Draw(layer), span, bar_off, bar_off)

    canvas = Image.alpha_composite(canvas, layer)
    return canvas.resize((size, size), Image.LANCZOS)


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
    written.append(_write(APP_OUT / "AppIcon-1024.png", render(1024)))

    # macOS: every size spelled out. The single-size shortcut that works for iOS does
    # not apply here, and an AppIcon set with no `mac` idiom compiles perfectly
    # happily to *nothing* for a macOS target -- which is exactly how this app shipped
    # with a blank Contents/Resources and a generic Dock tile.
    images = [{
        "filename": "AppIcon-1024.png",
        "idiom": "universal",
        "platform": "ios",
        "size": "1024x1024",
    }]
    for px in sorted({px for _, _, px in MAC_ENTRIES}):
        written.append(_write(APP_OUT / f"AppIcon-mac-{px}.png", render_macos(px)))
    for pt, scale, px in MAC_ENTRIES:
        images.append({
            "filename": f"AppIcon-mac-{px}.png",
            "idiom": "mac",
            "scale": scale,
            "size": f"{pt}x{pt}",
        })

    (APP_OUT / "Contents.json").write_text(json.dumps({
        "images": images,
        "info": {"author": "xcode", "version": 1},
    }, indent=2) + "\n")
    (APP_OUT.parent / "Contents.json").write_text(json.dumps({
        "info": {"author": "xcode", "version": 1},
    }, indent=2) + "\n")
    written.append(APP_OUT / "Contents.json")

    for p in written:
        rel = p.relative_to(ROOT)
        size = f"{p.stat().st_size / 1024:.1f} KB"
        print(f"  {rel}  {size}")


if __name__ == "__main__":
    main()
