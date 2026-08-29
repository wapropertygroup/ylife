"""Guards for the light/dark theme wiring in the yStocker templates.

Needs no browser, no Flask app and no network -- it parses the templates, in the
same spirit as ``test_deferload_anchors.py``.

The failure this exists to catch is a silent one. Light-mode support turned every
hardcoded colour utility into a PAIR (``bg-slate-100 dark:bg-slate-800``). Any
place a template passed such a class to the DOM *as a token* rather than as an
attribute then broke:

  * ``classList.add`` / ``remove`` are variadic and take ONE token per argument.
  * ``classList.toggle`` / ``contains`` take exactly one token.
  * A CSS selector built from a class name (``closest('.bg-slate-950\\/50')``)
    silently stopped matching once the class was renamed.

All three throw only when the handler runs -- on a click, or inside an IIFE whose
``catch`` swallows it -- so nothing is visibly wrong on page load. One of these
(``closest`` in fed.html) killed that page's entire init block, and the only
symptom was a console line nobody was reading.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "ystocker" / "templates"

# /tv is a standalone kiosk template: it does not extend base.html, owns its own
# CSS variables, and is deliberately dark-only, so it is exempt throughout.
KIOSK = "tv.html"

QUOTED = re.compile(r"(['\"])([^'\"]*?)\1")
CLASSLIST_CALL = re.compile(r"classList\.(add|remove|toggle|contains)\(")
SELECTOR_CALL = re.compile(
    r"\.(closest|querySelector|querySelectorAll|matches)\((['\"])([^'\"]*)\2"
)


def templates() -> list[Path]:
    return sorted(p for p in TEMPLATES.glob("*.html") if p.name != KIOSK)


def _call_args(text: str, open_paren_end: int) -> str:
    """Return the raw argument text of a call whose '(' ends at *open_paren_end*.

    Scans for the matching close paren rather than using a regex, so a nested
    call such as ``toggle(sel(x), on)`` is not truncated at the inner ')'.
    """
    i, depth = open_paren_end, 1
    while i < len(text) and depth:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
        i += 1
    return text[open_paren_end : i - 1]


class TestDomTokenSafety(unittest.TestCase):
    def test_classlist_calls_pass_single_tokens(self) -> None:
        """No classList call may receive a string holding more than one class."""
        offenders: list[str] = []
        for path in templates():
            text = path.read_text(encoding="utf-8")
            for m in CLASSLIST_CALL.finditer(text):
                args = _call_args(text, m.end())
                for q in QUOTED.finditer(args):
                    token = q.group(2).strip()
                    if " " in token:
                        line = text[: m.start()].count("\n") + 1
                        offenders.append(
                            f"{path.name}:{line} classList.{m.group(1)}({q.group(0)})"
                        )
                        break
        self.assertEqual(
            offenders,
            [],
            "classList takes one class per token. Split into separate arguments "
            "for add/remove, or use the toggleClasses() helper in base.html:\n  "
            + "\n  ".join(offenders),
        )

    def test_selectors_do_not_name_a_theme_pair(self) -> None:
        """A selector must not contain a space-separated `dark:` class pair.

        `.foo dark:bar` parses as a *descendant* selector, so it either throws or
        silently matches nothing -- which is worse.
        """
        offenders: list[str] = []
        for path in templates():
            text = path.read_text(encoding="utf-8")
            for m in SELECTOR_CALL.finditer(text):
                sel = m.group(3)
                if " " in sel and "dark:" in sel:
                    line = text[: m.start()].count("\n") + 1
                    offenders.append(f"{path.name}:{line} .{m.group(1)}('{sel}')")
        self.assertEqual(
            offenders,
            [],
            "Selector names a theme class pair. Prefer a stable [data-*] hook, "
            "which restyling cannot invalidate:\n  " + "\n  ".join(offenders),
        )


class TestThemeWiring(unittest.TestCase):
    def setUp(self) -> None:
        self.base = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    def test_dark_is_the_default(self) -> None:
        """<html> ships with `dark`; the no-flash script only ever removes it."""
        self.assertRegex(self.base, r"<html[^>]*\bclass=\"[^\"]*\bdark\b")
        self.assertIn("classList.remove('dark')", self.base)

    def test_no_flash_script_precedes_the_stylesheet(self) -> None:
        """The class flip must run before any CSS, or a dark frame paints first."""
        flip = self.base.index("ystocker_theme")
        css = self.base.index("css/tailwind.css")
        self.assertLess(
            flip, css, "theme script must come before the Tailwind stylesheet"
        )

    def test_chart_theme_helper_is_defined_before_use(self) -> None:
        """CT must exist before any template's chart config calls CT.c()."""
        self.assertIn("window.CT", self.base)
        # base.html defines CT, so it must not itself call it: the MAP literals
        # sit in key position, and the two Chart.defaults assignments run inside
        # the IIFE, before CT has been assigned.
        body = self.base[self.base.index("window.CT") :]
        self.assertNotIn(
            "CT.c(",
            body.replace("ticks: { color: CT.c('#64748b') }", ""),  # the doc comment
            "base.html defines CT and must not call it",
        )

    def test_toggle_classes_helper_exists(self) -> None:
        self.assertIn("window.toggleClasses", self.base)

    def test_kiosk_is_untouched(self) -> None:
        """/tv stays dark-only: no `dark:` variants, no dependence on the toggle."""
        kiosk = (TEMPLATES / KIOSK).read_text(encoding="utf-8")
        self.assertNotIn("dark:", kiosk)
        self.assertNotIn("toggleTheme", kiosk)


class TestThemeStrings(unittest.TestCase):
    """Both languages must define the toggle's strings.

    Mirrors the EN/ZH parity assertion on report_email's _STR: a key present in
    one language and missing in the other renders as the raw key.
    """

    def test_theme_keys_have_both_languages(self) -> None:
        i18n = (
            TEMPLATES.parent / "static" / "i18n.js"
        ).read_text(encoding="utf-8")
        for key in ("nav.theme", "nav.theme_light", "nav.theme_dark"):
            m = re.search(
                r"'" + re.escape(key) + r"':\s*\{(.*?)\}", i18n, re.DOTALL
            )
            self.assertIsNotNone(m, f"{key} missing from i18n.js")
            entry = m.group(1)
            self.assertIn("en:", entry, f"{key} has no English string")
            self.assertIn("zh:", entry, f"{key} has no Chinese string")


if __name__ == "__main__":
    unittest.main()
