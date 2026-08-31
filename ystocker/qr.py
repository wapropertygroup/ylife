"""ystocker.qr
~~~~~~~~~~~~~~
Minimal QR-code PNG rendering, for "scan this to open the link" — the one
mechanism that actually works for handing a share URL to WeChat from a plain
website. WeChat has no ``sms:``-equivalent URL scheme a page can invoke to hand
off a share, and registering as a WeChat Official Account to use its JS-SDK
share hooks (``wx.updateAppMessageShareData``) is an external business process
— Tencent verification, a bound and verified domain, a per-request signed
``wx.config()`` call — that this box has no part of and a family site has no
reason to acquire. A QR code the reader scans with WeChat's own built-in
scanner needs none of that, and is already the standard pattern every Chinese
website uses for exactly this gap — the ubiquitous "长按识别二维码".

Encoding is delegated to the ``qrcode`` package. Reed-Solomon error correction
and the QR version/mode/mask tables are not something to hand-roll here: a
subtly wrong implementation produces a code that *looks* right and does not
scan, which is a worse failure than most bugs since nothing short of a real
scanner catches it. Only the *rendering* is done in this module, deliberately
without Pillow — ``qrcode.QRCode.get_matrix()`` returns a plain boolean grid
with no imaging library involved, and ``_rasterise`` below turns that grid
straight into a minimal PNG by hand (``zlib`` + ``struct``, both stdlib).

Not routed through matplotlib either, unlike ``share_card.py``. matplotlib is
the right tool there because it is already drawing antialiased text and
shapes; it would be the wrong one here, because ``imshow()``'s default
interpolation blurs the crisp module edges a scanner depends on, and getting
``interpolation="nearest"`` plus the dpi/figsize arithmetic exactly right is a
footgun worth avoiding entirely when a solid-block rasteriser has no
interpolation setting to get wrong in the first place.
"""

from __future__ import annotations

import logging
import struct
import zlib
from typing import Optional

import qrcode
from qrcode.constants import ERROR_CORRECT_M

log = logging.getLogger(__name__)

#: Modules per side of one QR "pixel". 10 keeps a typical share link's ~35-45
#: module code (including the quiet zone) comfortably scannable on a phone
#: screen without producing a needlessly large PNG.
BOX_SIZE = 10

#: Quiet zone, in modules, on all four sides. The QR spec's own minimum is 4;
#: cropping it tighter is a common real-world reason an otherwise-correct code
#: fails to scan, so this is never trimmed just to shrink the PNG.
BORDER = 4


def render(data: str) -> Optional[bytes]:
    """A PNG of a QR code encoding ``data``, or None if it could not be built.

    None rather than a raised exception: this backs an image route
    (``routes.api_agents_shared_qr``), and a broken QR image is a strictly
    better failure than a 500 that takes an otherwise-working share page's
    request thread down with it. ``data`` is expected to be a URL this process
    minted (``share.share_url()``'s output), not arbitrary caller-supplied
    text — see that route for why the input is never taken from a query
    string.
    """
    if not data:
        return None
    try:
        qr = qrcode.QRCode(error_correction=ERROR_CORRECT_M,
                           box_size=BOX_SIZE, border=BORDER)
        qr.add_data(data)
        qr.make(fit=True)
        matrix = qr.get_matrix()          # list[list[bool]]; True = dark module
    except Exception as exc:  # noqa: BLE001 - encoder limits, malformed input, etc.
        log.warning("qr: could not encode %r (%s)", data[:60], exc)
        return None
    try:
        return _rasterise(matrix)
    except Exception as exc:  # noqa: BLE001 - a rendering bug must not 500 the route
        log.warning("qr: could not rasterise a %d-module code (%s)",
                    len(matrix), exc)
        return None


def _rasterise(matrix: list[list[bool]]) -> bytes:
    """``matrix`` (True = dark module) to an 8-bit greyscale PNG.

    Each module becomes a solid ``BOX_SIZE`` x ``BOX_SIZE`` block of pure black
    (0) or white (255) — no antialiasing, no resampling, so a scanner sees
    exactly the crisp edges the QR spec assumes.
    """
    side = len(matrix) * BOX_SIZE
    scanlines = bytearray()
    for module_row in matrix:
        row = bytearray()
        for dark in module_row:
            row.extend((0 if dark else 255,) * BOX_SIZE)
        for _ in range(BOX_SIZE):
            scanlines.append(0)              # per-scanline filter type: None
            scanlines.extend(row)
    return _encode_png(side, side, bytes(scanlines))


def _chunk(tag: bytes, data: bytes) -> bytes:
    """One length-prefixed, CRC-suffixed PNG chunk."""
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data)))


def _encode_png(width: int, height: int, raw_scanlines: bytes) -> bytes:
    """Assemble a minimal 8-bit greyscale (colour type 0) PNG.

    ``raw_scanlines`` must already be filter-tagged (one leading 0x00 byte per
    scanline, meaning "no filtering") — IDAT is nothing more than that stream,
    deflate-compressed.
    """
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    idat = zlib.compress(bytes(raw_scanlines), 9)
    return (signature + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat)
            + _chunk(b"IEND", b""))
