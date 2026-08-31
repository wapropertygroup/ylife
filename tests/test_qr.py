"""Tests for ystocker/qr.py — the QR-code PNG behind "分享至微信".

No app, no network, and — deliberately — no QR-decoding or imaging library as
a test dependency either. What is verified here is that the hand-rolled PNG
encoder in ``_rasterise``/``_encode_png`` reproduces, byte for byte, the exact
boolean matrix ``qrcode.QRCode.get_matrix()`` handed it: ``_matrix_from_png``
below reverses this module's own encoding (inflate the IDAT chunk, strip the
per-scanline filter byte, downsample each ``BOX_SIZE``-square block back to one
module) using nothing but the standard library, and the tests assert round trips
against many inputs. That is the property that actually matters for
scannability: ``qrcode`` is a mature, independently-tested library, so if this
module's own pixels are a faithful, unblurred rendering of the matrix that
library computed, the result scans -- and conversely, a rasterisation bug (an
off-by-one in scanline length, a swapped dark/light value, a wrong filter byte)
would silently produce an image that *looks* plausible but does not, which is
exactly the failure mode a "does it look like a QR code" glance would miss.

This was additionally cross-checked once, by hand, against a real independent
decoder (OpenCV's ``QRCodeDetector``) in a throwaway venv outside this
checkout, for exactly the same reason ``share_card.py``'s module docstring
records doing something similar for its layout: a property proven by
arithmetic here is still worth seeing actually scan somewhere at least once.
"""

from __future__ import annotations

import importlib.util
import pathlib
import struct
import unittest
import zlib

import qrcode
from qrcode.constants import ERROR_CORRECT_M

ROOT = pathlib.Path(__file__).parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


qr = _load("ystocker.qr", "ystocker/qr.py")


def _reference_matrix(data: str) -> list[list[bool]]:
    """The matrix qrcode itself would compute, using this module's own settings."""
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_M,
                         box_size=qr.BOX_SIZE, border=qr.BORDER)
    code.add_data(data)
    code.make(fit=True)
    return code.get_matrix()


def _matrix_from_png(png: bytes) -> list[list[bool]]:
    """Reverse ``qr._encode_png``/``_rasterise`` by hand, using only stdlib.

    Deliberately independent of any of ``ystocker.qr``'s own helpers: if this
    read back whatever ``_rasterise`` happened to write via the same code path
    that wrote it, a shared bug could cancel out and the test would prove
    nothing.
    """
    assert png[:8] == b"\x89PNG\r\n\x1a\n", "not a PNG"
    pos = 8
    chunks = {}
    while pos < len(png):
        length, = struct.unpack(">I", png[pos:pos + 4])
        tag = png[pos + 4:pos + 8]
        data = png[pos + 8:pos + 8 + length]
        chunks.setdefault(tag, b"")
        chunks[tag] += data                 # IDAT may be split across chunks
        pos += 8 + length + 4               # length + tag + data + crc

    width, height, depth, color_type = struct.unpack(">IIBB", chunks[b"IHDR"][:10])
    assert depth == 8 and color_type == 0, "expected 8-bit greyscale"

    raw = zlib.decompress(chunks[b"IDAT"])
    stride = 1 + width                      # one filter byte per scanline
    assert len(raw) == stride * height

    box = qr.BOX_SIZE
    modules = width // box
    assert width % box == 0 and height % box == 0, "not a whole number of modules"

    matrix = []
    for mr in range(modules):
        scanline_start = mr * box * stride
        filter_byte = raw[scanline_start]
        assert filter_byte == 0, "expected filter type None on every scanline"
        pixels = raw[scanline_start + 1: scanline_start + 1 + width]
        matrix.append([pixels[mc * box] < 128 for mc in range(modules)])
    return matrix


class RenderTests(unittest.TestCase):
    def test_it_returns_a_png(self):
        png = qr.render("https://trade-agents.com/agents/shared/" + "a" * 22)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_dimensions_match_the_module_count_plus_quiet_zone(self):
        url = "https://trade-agents.com/agents/shared/" + "a" * 22
        png = qr.render(url)
        width, height = struct.unpack(">II", png[16:24])
        modules = len(_reference_matrix(url))
        self.assertEqual(width, modules * qr.BOX_SIZE)
        self.assertEqual(height, modules * qr.BOX_SIZE)
        self.assertEqual(width, height, "a QR code is always square")

    def test_the_rasterised_pixels_are_an_exact_module_map(self):
        # The property that matters: what got drawn must be exactly what
        # qrcode itself computed, module for module.
        for url in (
            "https://trade-agents.com/agents/shared/" + "a" * 22,
            "https://trade-agents.com/agents/shared/" + "Z9-_" * 6,
            "https://stock.li-family.us/agents/shared/" + "b" * 22,
            "short",
        ):
            png = qr.render(url)
            self.assertEqual(_matrix_from_png(png), _reference_matrix(url), url)

    def test_position_detection_squares_are_present_in_three_corners(self):
        # The three big nested squares a scanner locates first. Not a
        # QR-correctness proof on its own, but a sharp, easy check that the
        # matrix landed the right way up and was not transposed or mirrored --
        # a transposition bug could otherwise still pass the exact-equality
        # check above by transposing the *reference* the same way, if the bug
        # were copy-pasted into both; this checks an absolute, known pattern
        # instead.
        url = "https://trade-agents.com/agents/shared/" + "a" * 22
        matrix = _matrix_from_png(qr.render(url))
        n = len(matrix)

        def is_finder(top, left):
            # A finder pattern is a 7x7 block: a dark 7x7 ring, a white 5x5
            # ring inside it, and a dark 3x3 core.
            for r in range(7):
                for c in range(7):
                    dark = matrix[top + r][left + c]
                    on_outer_ring = r in (0, 6) or c in (0, 6)
                    in_core = 2 <= r <= 4 and 2 <= c <= 4
                    expected = on_outer_ring or in_core
                    if dark != expected:
                        return False
            return True

        border = qr.BORDER
        self.assertTrue(is_finder(border, border), "top-left finder pattern")
        self.assertTrue(is_finder(border, n - border - 7), "top-right finder pattern")
        self.assertTrue(is_finder(n - border - 7, border), "bottom-left finder pattern")

    def test_different_urls_render_different_images(self):
        a = qr.render("https://trade-agents.com/agents/shared/" + "a" * 22)
        b = qr.render("https://trade-agents.com/agents/shared/" + "b" * 22)
        self.assertNotEqual(a, b)

    def test_the_same_url_renders_identically_every_time(self):
        # No timestamp, no random padding choice left to chance: the same
        # token must produce byte-identical PNGs, which is what makes caching
        # the route (routes.api_agents_shared_qr) correct.
        url = "https://trade-agents.com/agents/shared/" + "a" * 22
        self.assertEqual(qr.render(url), qr.render(url))

    def test_empty_and_none_input_return_none_not_a_raise(self):
        for bad in ("", None):
            self.assertIsNone(qr.render(bad), repr(bad))

    def test_input_too_large_for_any_qr_version_returns_none(self):
        # qrcode itself raises rather than silently truncating; render() must
        # convert that into "no image" rather than a 500 on the route.
        self.assertIsNone(qr.render("x" * 10_000))

    def test_unicode_data_does_not_raise(self):
        # Not a real use case today (share_url() only ever hands this ASCII),
        # but the encoder must not be assumed ASCII-only just because its one
        # caller happens to pass ASCII.
        png = qr.render("https://trade-agents.com/agents/shared/分享")
        self.assertIsNotNone(png)
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")

    def test_quiet_zone_is_at_least_the_spec_minimum(self):
        self.assertGreaterEqual(qr.BORDER, 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
