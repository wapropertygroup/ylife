"""Integrity checks for the installable-web-app assets.

These fail silently in the worst way. A manifest whose icon `src` 404s does not
throw anything: the browser simply declines to offer installation, and iOS falls
back to a screenshot of the page as the home-screen icon. Nothing appears in a
log, and the page itself is unaffected — so the only symptom is that "Add to Home
Screen" quietly produces the wrong thing, which is indistinguishable from not
having tried.

So this asserts the file-level wiring: every path the manifest, base.html and the
service worker reference actually resolves on disk, and the manifest carries the
fields a browser requires before it will treat the site as installable. No
browser, no app, no network.

Regenerate the icons with: venv/bin/python build_pwa_icons.py
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "ystocker" / "static"
MANIFEST = STATIC / "manifest.json"
BASE_HTML = ROOT / "ystocker" / "templates" / "base.html"
SW = STATIC / "sw.js"


def _static_path(url: str) -> Path:
    """Map a site-absolute /static/... URL onto the file that serves it."""
    return STATIC / url.split("?")[0].removeprefix("/static/")


class Manifest(unittest.TestCase):
    def setUp(self) -> None:
        self.m = json.loads(MANIFEST.read_text(encoding="utf-8"))

    def test_required_fields_for_installability(self) -> None:
        for key in ("name", "short_name", "start_url", "scope", "display", "icons"):
            self.assertIn(key, self.m, f"manifest is missing {key!r}")
        self.assertEqual(self.m["display"], "standalone")

    def test_scope_is_explicit_and_covers_start_url(self) -> None:
        """The manifest is served from /static/, so scope must be stated.

        An omitted scope defaults to the manifest's own directory — /static/ —
        which would not contain start_url and would make the manifest invalid.
        """
        self.assertEqual(self.m["scope"], "/")
        self.assertTrue(self.m["start_url"].startswith(self.m["scope"]))

    def test_short_name_fits_a_home_screen_label(self) -> None:
        self.assertLessEqual(
            len(self.m["short_name"]), 12,
            "iOS and Android truncate home-screen labels beyond ~12 characters",
        )

    def test_every_icon_file_exists(self) -> None:
        missing = [i["src"] for i in self.m["icons"]
                   if not _static_path(i["src"]).is_file()]
        self.assertFalse(missing, f"manifest icons do not resolve on disk: {missing}")

    def test_icon_pixel_sizes_match_their_declaration(self) -> None:
        """A wrong `sizes` is ignored silently and the icon goes unused."""
        try:
            from PIL import Image
        except ImportError:  # pragma: no cover
            self.skipTest("Pillow unavailable")
        for icon in self.m["icons"]:
            with Image.open(_static_path(icon["src"])) as im:
                self.assertEqual(
                    f"{im.width}x{im.height}", icon["sizes"],
                    f"{icon['src']} is {im.width}x{im.height}, declared {icon['sizes']}",
                )

    def test_has_both_any_and_maskable_purposes(self) -> None:
        purposes = {i.get("purpose", "any") for i in self.m["icons"]}
        self.assertIn("any", purposes)
        self.assertIn("maskable", purposes,
                      "without a maskable icon Android crops the square art")

    def test_a_512_icon_is_present(self) -> None:
        """Chrome requires >=512px before it will offer installation."""
        self.assertTrue(
            any(i["sizes"] == "512x512" for i in self.m["icons"]),
            "no 512x512 icon: Chrome will not treat the site as installable",
        )


class BaseTemplate(unittest.TestCase):
    def setUp(self) -> None:
        self.html = BASE_HTML.read_text(encoding="utf-8")

    def test_links_the_manifest(self) -> None:
        self.assertIn('rel="manifest"', self.html)

    def test_apple_touch_icon_exists_and_is_png(self) -> None:
        """iOS ignores the SVG favicon; without a PNG it screenshots the page."""
        m = re.search(r"""rel=["']apple-touch-icon["'][^>]*filename=['"]([^'"]+)['"]""",
                      self.html)
        self.assertIsNotNone(m, "no apple-touch-icon link in base.html")
        target = STATIC / m.group(1)
        self.assertTrue(target.is_file(), f"apple-touch-icon missing: {target}")
        self.assertEqual(target.suffix, ".png")

    def test_declares_standalone_and_a_title(self) -> None:
        for meta in ("apple-mobile-web-app-capable",
                     "apple-mobile-web-app-title",
                     "theme-color"):
            self.assertIn(meta, self.html, f"missing meta {meta!r}")

    def test_registers_the_worker_from_the_root(self) -> None:
        """A worker registered under /static/ could not see a navigation."""
        self.assertIn("serviceWorker.register('/sw.js')", self.html)


class ServiceWorker(unittest.TestCase):
    def setUp(self) -> None:
        self.js = SW.read_text(encoding="utf-8")

    def test_precached_files_exist(self) -> None:
        """install() rejects wholesale if any addAll() entry 404s."""
        block = re.search(r"const PRECACHE = \[(.*?)\]", self.js, re.S)
        self.assertIsNotNone(block)
        urls = re.findall(r"""['"](/static/[^'"]+)['"]""", block.group(1))
        self.assertTrue(urls, "PRECACHE parsed as empty — matcher is stale")
        missing = [u for u in urls if not _static_path(u).is_file()]
        self.assertFalse(missing, f"precached files do not exist: {missing}")

    def test_api_paths_are_bypassed(self) -> None:
        """The one rule that keeps stale market data off the screen."""
        self.assertIn("startsWith('/api/')", self.js,
                      "the /api/ bypass is gone; see tests/check_sw_routing.mjs")

    def test_offline_page_exists(self) -> None:
        m = re.search(r"""OFFLINE_URL\s*=\s*['"]([^'"]+)['"]""", self.js)
        self.assertIsNotNone(m)
        self.assertTrue(_static_path(m.group(1)).is_file())


if __name__ == "__main__":
    unittest.main()
