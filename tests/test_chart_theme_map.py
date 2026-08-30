"""The chart-colour theme table, and the three ways it goes wrong quietly.

A canvas takes no CSS, so `CT.c()` in base.html is the only thing standing
between a dashboard's ~490 Chart.js colour literals and a white background.
Every failure mode here is silent — the page renders, the chart draws, and the
line is simply the wrong colour or invisible:

  * **Drift between the table and the migrator.** `migrate_chart_colors.py`
    mirrors base.html's MAP keys *by hand*, and its own docstring notes that a
    mismatch "degrades to no theming rather than to a wrong colour". True, and
    that is exactly why nothing reports it: a key in the MAP that the migrator
    lacks leaves call sites unwrapped, and a key in the migrator that the MAP
    lacks wraps literals that then pass straight through. Either way the dark
    value stays on a white card.

  * **A mapped colour that is still illegible.** Dropping Tailwind 400 to 600 is
    the house rule, but it is not sufficient for every hue: yellow-600 (#ca8a04)
    is 2.94:1 on white, so a yellow series line mapped "correctly" was still
    unreadable. Only measuring catches that.

  * **A wrap on a page that has no CT.** tv.html does not extend base.html, so
    `CT` there is undefined rather than merely unmapped, and a wrap would raise
    ReferenceError and take out the surrounding init block — on the one page with
    nobody watching it fail.

3:1 is the bar (WCAG 1.4.11, non-text contrast) because these are graphical
marks a reader has to distinguish, not body text. Grid lines and the panel fill
are exempt: they are deliberately faint scaffolding, and a gridline that met 3:1
would compete with the data drawn on top of it.

No browser, no app, no network.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from migrate_chart_colors import LITERALS, SKIP  # noqa: E402

TEMPLATES = ROOT / "ystocker" / "templates"
BASE_HTML = TEMPLATES / "base.html"

SCRIPT_BLOCK = re.compile(r"<script\b[^>]*>.*?</script>", re.DOTALL | re.IGNORECASE)

# Faint by design: grid lines, borders and the fill behind a chart. These are
# scaffolding, not marks the reader reads a value off.
#
# rgba(148,163,184,·) is deliberately NOT here. It looks like scaffolding and was
# originally filed as such, but every use of it is a comparison *line* — the
# sector median on /multiples, the target-range step on /fedwatch, the national
# series on /housing — so it is held to the mark bar like any other series.
CHROME_KEYS = {
    "#0f172a", "#1e293b", "#334155", "#475569", "#64748b", "#94a3b8",
    "#cbd5e1", "#e2e8f0", "rgba(51,65,85,0.3)",
}


def _map_from_base() -> dict[str, str]:
    """Read window.CT's MAP out of base.html as a dict, preserving duplicates."""
    src = BASE_HTML.read_text(encoding="utf-8")
    block = re.search(r"const MAP = \{(.*?)\n    \};", src, re.S)
    assert block, "could not locate window.CT's MAP in base.html"
    pairs = re.findall(r"'([^']+)':\s*'([^']+)'", block.group(1))
    return dict(pairs), [k for k, _ in pairs]


def _rgba(colour: str) -> tuple[float, float, float, float] | None:
    c = colour.strip().lower()
    if c.startswith("#"):
        h = c[1:]
        if len(h) == 3:
            h = "".join(ch * 2 for ch in h)
        if len(h) < 6:
            return None
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16), 1.0
    m = re.match(r"rgba?\(([^)]*)\)", c)
    if not m:
        return None
    parts = [p.strip() for p in m.group(1).split(",")]
    try:
        r, g, b = (float(p) for p in parts[:3])
    except ValueError:
        return None
    return r, g, b, float(parts[3]) if len(parts) > 3 else 1.0


def _contrast_on_white(colour: str) -> float | None:
    """WCAG contrast ratio against white, compositing any alpha over white."""
    parsed = _rgba(colour)
    if parsed is None:
        return None
    r, g, b, a = parsed
    r, g, b = (v * a + 255 * (1 - a) for v in (r, g, b))

    def lin(v: float) -> float:
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    lum = 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)
    return round(1.05 / (lum + 0.05), 2)


class ThemeTable(unittest.TestCase):
    def setUp(self) -> None:
        self.map, self.keys = _map_from_base()

    def test_no_duplicate_keys(self) -> None:
        """A repeated key is legal JS: the last wins, silently."""
        dupes = sorted({k for k in self.keys if self.keys.count(k) > 1})
        self.assertFalse(dupes, f"duplicate MAP keys, later entry silently wins: {dupes}")

    def test_map_and_migrator_agree(self) -> None:
        only_map = sorted(set(self.map) - set(LITERALS))
        only_mig = sorted(set(LITERALS) - set(self.map))
        self.assertFalse(
            only_map,
            "in base.html's MAP but not in migrate_chart_colors.LITERALS, so these "
            f"call sites never get wrapped: {only_map}",
        )
        self.assertFalse(
            only_mig,
            "in migrate_chart_colors.LITERALS but not in base.html's MAP, so these "
            f"get wrapped and then pass through unmapped: {only_mig}",
        )

    def test_every_data_colour_is_legible_on_white(self) -> None:
        """The light value must clear 3:1, or the mapping achieved nothing."""
        failures = []
        for key, light in self.map.items():
            if key in CHROME_KEYS:
                continue
            ratio = _contrast_on_white(light)
            self.assertIsNotNone(ratio, f"unparseable light value for {key}: {light}")
            if ratio < 3.0:
                failures.append(f"{key} -> {light} is {ratio}:1")
        self.assertFalse(
            failures,
            "mapped light-mode colours below 3:1 on white (WCAG 1.4.11): "
            + "; ".join(failures),
        )

    def test_one_colour_maps_to_one_colour(self) -> None:
        """Two spellings of the same dark colour must not disagree.

        Lookup normalises case and whitespace but does not rewrite ".55" into
        "0.55", so both spellings are separate keys describing one colour. When
        they mapped to different light values, /fedwatch's comparison line and
        /multiples' median line — the same grey in dark mode — came out two
        different greys in light mode, on pages a reader flips between.
        """
        by_colour: dict[tuple[float, float, float, float], set[str]] = {}
        for key, light in self.map.items():
            src = _rgba(key)
            if src is None:
                continue
            by_colour.setdefault(src, set()).add(light.replace(" ", "").lower())
        clashes = {
            k: sorted(v) for k, v in by_colour.items() if len(v) > 1
        }
        self.assertFalse(
            clashes,
            "one dark colour mapped to more than one light value: "
            + "; ".join(f"rgba{k} -> {v}" for k, v in clashes.items()),
        )

    def test_alpha_ordering_survives_the_mapping(self) -> None:
        """Same hue, more alpha in dark must not mean less alpha in light.

        The slate family spans 0.3 to 0.9 in the templates — a zero line, a
        muted comparison series and a point marker. Mapping them all to the
        single lowest alpha that clears 3:1 would flatten three deliberately
        different weights into one grey, which reads as a bug in the chart.
        """
        by_hue: dict[tuple[float, float, float], list[tuple[float, float, str]]] = {}
        for key, light in self.map.items():
            src, dst = _rgba(key), _rgba(light)
            if src is None or dst is None or src[3] == 1.0:
                continue
            by_hue.setdefault(src[:3], []).append((src[3], dst[3], key))
        for hue, entries in by_hue.items():
            entries.sort()
            alphas = [dst for _, dst, _ in entries]
            self.assertEqual(
                alphas, sorted(alphas),
                f"rgb{hue}: light alphas {alphas} do not follow the dark ordering "
                f"{[s for s, _, _ in entries]}",
            )


class MigrationCoverage(unittest.TestCase):
    def test_kiosk_is_skipped(self) -> None:
        """tv.html has no CT at all — a wrap there is a ReferenceError."""
        self.assertIn("tv.html", SKIP)
        kiosk = (TEMPLATES / "tv.html").read_text(encoding="utf-8")
        self.assertNotIn("CT.c(", kiosk, "tv.html cannot call CT: it has no base.html")

    def test_no_mapped_literal_is_left_unwrapped(self) -> None:
        """Catches a new chart added with a known colour and no migration run.

        Scope mirrors the migrator: single-quoted, inside <script> only. The same
        hex values appear in <style> blocks and style= attributes as raw CSS,
        where CT.c() would be a syntax error rather than a colour.
        """
        mapped = set(LITERALS)
        misses: list[str] = []
        for path in sorted(TEMPLATES.glob("*.html")):
            if path.name in SKIP:
                continue
            src = path.read_text(encoding="utf-8")
            for block in SCRIPT_BLOCK.finditer(src):
                text = block.group(0)
                for m in re.finditer(r"'(#[0-9a-fA-F]{3,8}|rgba?\([^)]*\))'", text):
                    if m.group(1) not in mapped:
                        continue
                    if text[max(0, m.start() - 5):m.start()] == "CT.c(":
                        continue
                    line = src[:block.start() + m.start()].count("\n") + 1
                    misses.append(f"{path.name}:{line} {m.group(0)}")
        self.assertFalse(
            misses,
            "chart colour literals that CT knows about but which are not wrapped "
            "— run: venv/bin/python migrate_chart_colors.py ystocker/templates/*.html"
            f"\n  " + "\n  ".join(misses[:25]),
        )


if __name__ == "__main__":
    unittest.main()
