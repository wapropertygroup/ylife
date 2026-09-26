"""/markets is grouped into sections, and the jump bar has to keep naming them.

The page is ~14,000px of panels, so it carries a sticky bar of chips, one per
<section class="mk-section">, that jumps to a section and lights the one being
read. Everything that can break that is structural and silent — the page still
renders, and the chip simply does nothing or lights the wrong thing:

* **A chip without its section, or the reverse.** The script derives its list of
  sections from the chips' ``data-mk-target``, and returns early if any target is
  missing, so a renamed section id disables the whole bar with no console line.
  And a section with no chip is unreachable from the bar.
* **A panel dropped between two sections.** It belongs to neither. The spy
  attributes it to the section above, whose banner says something else.
* **An id that is a JavaScript identifier.** An element id becomes a property of
  ``window`` by named access, so ``id="global"`` would quietly replace any code's
  reference to a global of that name. Hyphenated ids cannot be identifiers.
* **An id the page already routes on.** ``/markets#ixic`` selects the Nasdaq tab;
  a section called ``ixic`` would make that link do two things.
* **A label with no Chinese.** The chips and banners are ``data-i18n`` text, and
  an empty or missing ``zh`` renders the English fallback on a Chinese page.

Needs no browser, app or network.
"""
from __future__ import annotations

import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATE = ROOT / "ystocker" / "templates" / "markets.html"
I18N = ROOT / "ystocker" / "static" / "i18n.js"

_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
         "meta", "source", "track", "wbr"}


class _TopLevel(HTMLParser):
    """Records the direct children of the fragment it is fed."""

    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.children: list[tuple[str, dict[str, str]]] = []

    def handle_starttag(self, tag, attrs):
        if tag in _VOID:
            return
        if self.depth == 0:
            self.children.append((tag, {k: v or "" for k, v in attrs}))
        self.depth += 1

    def handle_endtag(self, tag):
        if tag not in _VOID:
            self.depth -= 1


class MarketsSectionTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tpl = TEMPLATE.read_text()
        cls.i18n = I18N.read_text()
        start = cls.tpl.index('<div id="marketsContent">') + len('<div id="marketsContent">')
        end = cls.tpl.index("</div><!-- #marketsContent -->")
        parser = _TopLevel()
        parser.feed(cls.tpl[start:end])
        parser.close()
        cls.balanced = parser.depth == 0
        cls.children = parser.children
        cls.sections = [a.get("id", "") for t, a in cls.children if t == "section"]
        nav = re.search(r'<nav id="mkJump".*?</nav>', cls.tpl, re.S)
        cls.nav = nav.group(0) if nav else ""
        cls.chips = re.findall(r'data-mk-target="([^"]+)"', cls.nav)

    def test_the_content_region_parses_as_balanced_markup(self):
        # Guards the checks below: an unclosed tag would shift every depth.
        self.assertTrue(self.balanced)
        self.assertGreaterEqual(len(self.sections), 5)

    def test_every_panel_sits_inside_a_section(self):
        for tag, attrs in self.children:
            self.assertEqual(tag, "section", f"<{tag} id={attrs.get('id')!r}> outside any section")
            self.assertIn("mk-section", attrs.get("class", "").split())

    def test_chips_and_sections_match_one_to_one_in_page_order(self):
        self.assertEqual(self.chips, self.sections)
        for sid in self.chips:
            self.assertIn(f'href="#{sid}"', self.nav, f"chip for {sid} links elsewhere")

    def test_every_section_after_the_first_has_a_labelled_banner(self):
        # The first is the summary, which the page's own <h1> heads.
        for sid in self.sections[1:]:
            self.assertIn(f'aria-labelledby="{sid}-h"', self.tpl, sid)
            self.assertRegex(self.tpl, rf'<h2 id="{sid}-h" class="mk-banner-title"', sid)

    def test_section_ids_cannot_shadow_a_global_or_an_index_tab(self):
        keys = re.search(r"const IDX_ALL_KEYS = \[(.*?)\];", self.tpl, re.S)
        self.assertIsNotNone(keys, "IDX_ALL_KEYS moved; update this test")
        tabs = set(re.findall(r"'([a-z0-9]+)'", keys.group(1)))
        self.assertIn("ixic", tabs)
        for sid in self.sections:
            self.assertIn("-", sid, f"{sid} is a valid JS identifier")
            self.assertNotIn(sid, tabs)

    def test_every_label_exists_in_both_languages(self):
        used = set(re.findall(r'data-i18n(?:-title)?="(markets\.(?:jump|sec)_[a-z_]+)"', self.tpl))
        self.assertGreaterEqual(len(used), 2 * len(self.sections))
        q = r"'((?:[^'\\]|\\.)*)'"
        for key in sorted(used):
            m = re.search(r"'" + re.escape(key) + r"'\s*:\s*\{\s*en:\s*" + q + r"\s*,\s*zh:\s*" + q,
                          self.i18n, re.S)
            self.assertIsNotNone(m, f"missing i18n key {key}, or it lacks en/zh")
            self.assertTrue(m.group(1) and m.group(2), f"empty string for {key}")


if __name__ == "__main__":
    unittest.main()
