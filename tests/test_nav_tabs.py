"""The tabs across the top of every dashboard -- no app, no network.

There are two bars: the yStocker header (base.html's ``_nav``) and
trade-agents.com's Markets bar (_ta_markets_bar.html's ``_links``). Each is
built from its own list, so a page added to one and not the other goes
missing from a tab row without anything failing, and a page that sits "under"
another tab (as Sectors, Earnings, Insiders and Smart money did until
2026-10-08) is simply absent from the row. These pin the two lists together,
and each page to the one tab that marks it.

Run:  venv/bin/python -m unittest tests.test_nav_tabs
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TPL = ROOT / "ystocker" / "templates"

_TUPLE = re.compile(
    r"\('(main\.[a-z_]+)',\s*'([a-z_.]+)',\s*'([^']+)',\s*'([a-z_.]*)',\s*'([^']*)',\s*\(([^)]*)\)\)")


def _tabs(name: str, var: str) -> list[tuple]:
    text = (TPL / name).read_text()
    block = text[text.index("{% set " + var + " = ["):]
    block = block[:block.index("] %}")]
    rows = []
    for ep, key, label, tip_key, tip, here in _TUPLE.findall(block):
        rows.append((ep, key, label, tip_key, tip, tuple(re.findall(r"'(main\.[a-z_]+)'", here))))
    return rows


class TabListTests(unittest.TestCase):
    def setUp(self):
        self.header = _tabs("base.html", "_nav")
        self.bar = _tabs("_ta_markets_bar.html", "_links")

    def test_both_bars_list_the_same_tabs_in_the_same_order(self):
        self.assertEqual(len(self.header), 17)
        self.assertEqual(self.header, self.bar)

    def test_the_pages_that_used_to_sit_under_another_tab_have_their_own(self):
        eps = [row[0] for row in self.header]
        for page, after in (("main.sectors_page", "main.markets"),
                            ("main.earnings_page", "main.companies"),
                            ("main.insiders_page", "main.earnings_page"),
                            ("main.smart_money_page", "main.thirteenf")):
            self.assertIn(page, eps)
            self.assertEqual(eps.index(page), eps.index(after) + 1, f"{page} sits after {after}")

    def test_each_page_marks_one_tab_and_its_own(self):
        owner: dict[str, str] = {}
        for ep, *_rest, here in self.header:
            self.assertIn(ep, here, f"{ep} does not mark itself")
            for page in here:
                self.assertNotIn(page, owner, f"{page} marks both {owner.get(page)} and {ep}")
                owner[page] = ep

    def test_every_tab_names_a_route(self):
        routes = (ROOT / "ystocker" / "routes.py").read_text()
        for ep, *_rest, here in self.header:
            for name in {ep, *here}:
                self.assertRegex(routes, r"\ndef " + re.escape(name.split(".", 1)[1]) + r"\(", name)

    def test_the_phone_menu_reaches_every_tab(self):
        base = (TPL / "base.html").read_text()
        drawer = base[base.index('<div data-nav="drawer"'):base.index("</header>")]
        for ep, *_rest in self.header:
            self.assertIn(f"url_for('{ep}')", drawer, ep)


class LabelTests(unittest.TestCase):
    SRC = (ROOT / "ystocker" / "static" / "i18n.js").read_text()

    def _entry(self, key: str) -> None:
        q = r"'((?:[^'\\]|\\.)*)'"
        found = re.findall(r"^\s*'" + re.escape(key) + r"'\s*:\s*\{\s*en:\s*" + q + r"\s*,\s*zh:\s*" + q,
                           self.SRC, re.M)
        self.assertEqual(len(found), 1, f"{key}: {len(found)} definitions with en and zh")
        self.assertTrue(all(found[0]), f"empty string for {key}")

    def test_every_label_and_tooltip_exists_once_in_both_languages(self):
        for _ep, key, _label, tip_key, _tip, _here in _tabs("base.html", "_nav"):
            self._entry(key)
            if tip_key:
                self._entry(tip_key)


if __name__ == "__main__":
    unittest.main()
