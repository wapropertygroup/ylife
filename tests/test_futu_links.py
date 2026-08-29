"""
Tests for the Futu (futunn.com) quote link on /history/<ticker>.

No app, no network, no disk — ``_futu_symbol`` is pure string work and the rest
is read out of the template and i18n.js as text.

The link sits next to the TradingView one, but it cannot be built the same way.
TradingView tolerates a bare symbol; Futu makes the market part of the path
mandatory, so every failure here is a 404 the reader only discovers by clicking:

* ``/stock/TSM`` is a 404 where ``/stock/TSM-US`` is not, so a passed-through
  ticker is always wrong.
* Hong Kong codes are zero-padded to five digits by Futu and four by Yahoo.
  ``0700-HK`` 404s, ``00700-HK`` is Tencent — verified against the live site.
* Yahoo spells US share classes with a hyphen (``BRK-B``), Futu with a dot
  (``BRK.B``).
* Futu lists far more venues than the four mapped here, each with its own code
  format. Guessing ``.TO``/``.L``/``.T`` yields a dead link, so those must map to
  None and drop the anchor entirely — no link beats a broken one.

The template contract matters for a subtler reason: ``{% if futu_symbol %}`` on an
undefined variable is quietly falsy in Jinja, so dropping the context key from
``history()`` would remove the link for *every* ticker with no error anywhere.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from ystocker.routes import _futu_symbol

ROOT = Path(__file__).resolve().parent.parent


class SymbolMapping(unittest.TestCase):
    """Yahoo-style ticker -> Futu's SYMBOL-MARKET path segment."""

    def test_us_listings_get_the_us_suffix(self):
        self.assertEqual(_futu_symbol("TSM"), "TSM-US")
        self.assertEqual(_futu_symbol("AAPL"), "AAPL-US")

    def test_us_share_classes_switch_hyphen_to_dot(self):
        """Yahoo BRK-B is Futu BRK.B; the hyphenated form is not a Futu symbol."""
        self.assertEqual(_futu_symbol("BRK-B"), "BRK.B-US")
        self.assertEqual(_futu_symbol("BF-B"), "BF.B-US")

    def test_hong_kong_codes_are_zero_padded_to_five_digits(self):
        """The whole point: 0700-HK 404s, 00700-HK is Tencent."""
        self.assertEqual(_futu_symbol("0700.HK"), "00700-HK")
        self.assertEqual(_futu_symbol("9988.HK"), "09988-HK")

    def test_already_padded_hong_kong_codes_are_left_alone(self):
        self.assertEqual(_futu_symbol("00700.HK"), "00700-HK")

    def test_shanghai_is_sh_not_ss(self):
        """Yahoo says .SS, Futu says -SH. Passing .SS straight through 404s."""
        self.assertEqual(_futu_symbol("600519.SS"), "600519-SH")
        self.assertEqual(_futu_symbol("000905.SS"), "000905-SH")

    def test_shenzhen_keeps_sz(self):
        self.assertEqual(_futu_symbol("000001.SZ"), "000001-SZ")

    def test_unverified_markets_yield_no_link_rather_than_a_dead_one(self):
        for ticker in ("SHOP.TO", "VOD.L", "7203.T", "SAP.DE", "FOO.XYZ"):
            self.assertIsNone(_futu_symbol(ticker),
                              f"{ticker}: an unmapped venue must drop the link, "
                              f"not guess a code format and 404")

    def test_junk_input_is_none_not_a_malformed_url(self):
        for ticker in ("", "   ", ".HK", None):
            self.assertIsNone(_futu_symbol(ticker))

    def test_input_is_normalised_like_the_route_does(self):
        """history() upper-cases and strips; the helper must not disagree."""
        self.assertEqual(_futu_symbol("  tsm  "), "TSM-US")
        self.assertEqual(_futu_symbol("0700.hk"), "00700-HK")

    def test_no_result_ever_contains_a_yahoo_suffix(self):
        """A leaked '.SS'/'.HK' in the path is the 404 this mapping exists to stop."""
        for ticker in ("TSM", "BRK-B", "0700.HK", "600519.SS", "000001.SZ"):
            sym = _futu_symbol(ticker)
            self.assertNotIn(".HK", sym)
            self.assertNotIn(".SS", sym)
            self.assertNotIn(".SZ", sym)


class TemplateContract(unittest.TestCase):
    """The link is only real if the route feeds it and i18n names it."""

    @classmethod
    def setUpClass(cls):
        cls.html = (ROOT / "ystocker" / "templates" / "history.html").read_text(encoding="utf-8")
        cls.i18n = (ROOT / "ystocker" / "static" / "i18n.js").read_text(encoding="utf-8")
        cls.routes = (ROOT / "ystocker" / "routes.py").read_text(encoding="utf-8")

    def test_route_passes_futu_symbol_to_the_template(self):
        """Jinja treats an undefined var as falsy, so losing this key hides the
        link for every ticker without raising anything."""
        self.assertRegex(self.routes, r"futu_symbol\s*=\s*_futu_symbol\(ticker\)")

    def test_template_guards_the_anchor_on_the_symbol(self):
        self.assertIn("{% if futu_symbol %}", self.html)

    def test_anchor_interpolates_the_mapped_symbol_not_the_raw_ticker(self):
        m = re.search(r'href="(https://www\.futunn\.com/[^"]*)"', self.html)
        self.assertIsNotNone(m, "no futunn.com link found in history.html")
        href = m.group(1)
        self.assertIn("{{ futu_symbol }}", href,
                      "the anchor must use the mapped symbol; '{{ ticker }}' "
                      "would 404 for HK and A-shares")
        self.assertNotIn("{{ ticker }}", href)

    def test_external_link_is_opened_safely(self):
        """Matches the TradingView anchor beside it."""
        m = re.search(r'<a href="https://www\.futunn\.com/[^>]*>', self.html, re.S)
        self.assertIsNotNone(m)
        tag = m.group(0)
        self.assertIn('target="_blank"', tag)
        self.assertIn('rel="noopener"', tag)

    def test_label_key_has_both_languages(self):
        m = re.search(r"'history\.futu':\s*\{(.*?)\}\s*,\n", self.i18n, re.S)
        self.assertIsNotNone(m, "history.futu missing from i18n.js")
        body = m.group(1)
        self.assertIn("en:", body, "history.futu has no en")
        self.assertIn("zh:", body, "history.futu has no zh")

    def test_label_is_wired_to_that_key(self):
        self.assertIn('data-i18n="history.futu"', self.html)


if __name__ == "__main__":
    unittest.main()
