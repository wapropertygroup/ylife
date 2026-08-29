"""
Tests for the index universes added to /multiples — SOX and the Nikkei 225 —
plus the currency conversion they forced into data.py.

No app, no network, no disk. Every test either monkeypatches
``valuation._cached_fundamentals`` or builds a record dict by hand, because the
real one reads ``cache/ticker_cache.json`` and its contents differ per machine.

What is actually worth testing here is not the arithmetic — it is the four ways
this feature fails *silently*:

* A universe listed in valuation but not in PEER_GROUPS aggregates nothing,
  because PEER_GROUPS is the only thing that populates the ticker cache. The
  page renders, the tile is simply absent, and nothing logs an error.
* A currency scale factor that does not cancel would move every foreign
  multiple by ~150x while still landing inside the sanity band.
* A weighting caveat whose number is hardcoded in a translation string goes
  stale within a quarter and nobody notices, because it still reads plausibly.
* A trailing proxy that grows a forward-looking field becomes indistinguishable
  from the forward tiles it sits below.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest import mock

from ystocker import PEER_GROUPS, valuation

ROOT = Path(__file__).resolve().parent.parent


def rec(fwd=None, cap=None, ttm=None, price=None):
    """One ticker_cache.json-shaped record."""
    return {"PE (Forward)": fwd, "Market Cap ($B)": cap,
            "PE (TTM)": ttm, "Current Price": price}


class UniverseWiring(unittest.TestCase):
    """The trap: a universe is only real if PEER_GROUPS also lists it."""

    def test_sox30_is_fully_covered_by_peer_groups(self):
        held = {t for group in PEER_GROUPS.values() for t in group}
        missing = [t for t in valuation.SOX30 if t not in held]
        self.assertEqual(missing, [], "SOX30 names absent from PEER_GROUPS are "
                                     "never fetched, so they silently drop out "
                                     "of the aggregate")

    def test_n225_is_fully_covered_by_peer_groups(self):
        held = {t for group in PEER_GROUPS.values() for t in group}
        missing = [t for t in valuation.N225 if t not in held]
        self.assertEqual(missing, [], "N225 names absent from PEER_GROUPS are "
                                      "never fetched")

    def test_proxy_symbols_are_covered_by_peer_groups(self):
        held = {t for group in PEER_GROUPS.values() for t in group}
        for spec in valuation.PROXY_TRAILING:
            self.assertIn(spec["symbol"], held,
                          f"{spec['symbol']} must be in PEER_GROUPS or its row "
                          f"never appears")

    def test_no_duplicates_in_the_new_tuples(self):
        for name, uni in (("SOX30", valuation.SOX30), ("N225", valuation.N225)):
            self.assertEqual(len(uni), len(set(uni)), f"{name} has a duplicate")

    def test_every_source_resolves_to_a_non_empty_universe(self):
        for etf, meta in valuation.INDEX_UNIVERSE.items():
            if meta["source"] == "sp500":
                continue  # imports breadth; covered by its own tests
            self.assertTrue(valuation._constituents(meta["source"]),
                            f"{etf}: source {meta['source']!r} resolved empty — "
                            f"_constituents fell through to its default")

    def test_sox_is_not_an_etf_holding_its_own_constituents(self):
        # SOXX/SMH in the Semiconductors group would double-count every name
        # they hold. They belong in Sector ETFs.
        for sym in ("SOXX", "SMH"):
            self.assertNotIn(sym, valuation.SOX30)
            self.assertNotIn(sym, PEER_GROUPS["Semiconductors"])


class ForwardAggregation(unittest.TestCase):

    def test_cap_weighted_harmonic_mean_not_arithmetic(self):
        # Two names, equal cap, P/E 10 and 30. Harmonic mean is 15, arithmetic
        # would be 20. Averaging P/Es is the classic wrong statistic.
        recs = {"A": rec(10.0, 100.0), "B": rec(30.0, 100.0)}
        with mock.patch.object(valuation, "_cached_fundamentals", lambda: recs), \
             mock.patch.object(valuation, "_constituents", lambda s: ("A", "B")):
            out = valuation._fetch_forward_pe(
                "X", {"label": "X", "source": "x", "min_constituents": 2})
        self.assertAlmostEqual(out["forward_pe"], 15.0, places=2)

    def test_multiple_is_invariant_to_a_currency_scale_factor(self):
        """The load-bearing claim behind the Nikkei figure.

        Σcap / Σ(cap/PE) is linear in cap top and bottom, so scaling every cap
        by one factor cancels exactly. This is why N225 needs no FX conversion
        to be correct, and why a future 'fix' that converts caps inside the
        aggregation would be a no-op at best.
        """
        base = {"A": rec(10.0, 100.0), "B": rec(30.0, 250.0), "C": rec(18.0, 40.0)}
        scaled = {k: rec(v["PE (Forward)"], v["Market Cap ($B)"] * 152.7)
                  for k, v in base.items()}
        uni = ("A", "B", "C")
        meta = {"label": "X", "source": "x", "min_constituents": 3}

        def run(recs):
            with mock.patch.object(valuation, "_cached_fundamentals", lambda: recs), \
                 mock.patch.object(valuation, "_constituents", lambda s: uni):
                return valuation._fetch_forward_pe("X", meta)

        self.assertAlmostEqual(run(base)["forward_pe"],
                               run(scaled)["forward_pe"], places=6)

    def test_per_universe_min_constituents_overrides_the_global_floor(self):
        # 20 names is under the global 40 but over SOX's own 15.
        recs = {f"T{i}": rec(20.0, 100.0) for i in range(20)}
        uni = tuple(recs)
        with mock.patch.object(valuation, "_cached_fundamentals", lambda: recs), \
             mock.patch.object(valuation, "_constituents", lambda s: uni):
            self.assertIsNotNone(valuation._fetch_forward_pe(
                "SOX", {"label": "SOX", "source": "sox30", "min_constituents": 15}))
            # …and the global default still rejects it when no override is set.
            self.assertIsNone(valuation._fetch_forward_pe(
                "SPY", {"label": "SPY", "source": "sp500"}))

    def test_top_weight_is_measured_from_the_payload(self):
        recs = {"BIG": rec(20.0, 700.0), "S1": rec(20.0, 200.0), "S2": rec(20.0, 100.0)}
        with mock.patch.object(valuation, "_cached_fundamentals", lambda: recs), \
             mock.patch.object(valuation, "_constituents", lambda s: tuple(recs)):
            out = valuation._fetch_forward_pe(
                "X", {"label": "X", "source": "x", "min_constituents": 3})
        self.assertEqual(out["top_name"], "BIG")
        self.assertAlmostEqual(out["top_weight_pct"], 70.0, places=1)

    def test_weighting_note_is_a_token_not_prose(self):
        """The page owns the wording so it can be translated.

        A sentence returned from here would ship English into the zh page.
        """
        recs = {f"T{i}": rec(20.0, 100.0) for i in range(20)}
        with mock.patch.object(valuation, "_cached_fundamentals", lambda: recs), \
             mock.patch.object(valuation, "_constituents", lambda s: tuple(recs)):
            out = valuation._fetch_forward_pe(
                "SOX", {"label": "SOX", "source": "sox30",
                        "min_constituents": 15, "weighting_note": "cap_vs_capped"})
        self.assertEqual(out["weighting_note"], "cap_vs_capped")
        self.assertNotIn(" ", out["weighting_note"])

    def test_insane_multiple_is_discarded_rather_than_charted(self):
        # This is the guard that catches Korea. It must keep catching it.
        recs = {f"T{i}": rec(3.5, 100.0) for i in range(20)}
        with mock.patch.object(valuation, "_cached_fundamentals", lambda: recs), \
             mock.patch.object(valuation, "_constituents", lambda s: tuple(recs)):
            out = valuation._fetch_forward_pe(
                "KOSPI", {"label": "KOSPI", "source": "x", "min_constituents": 15})
        self.assertIsNone(out, "a 3.5x index multiple is a data problem, not a "
                               "market event, and must not be recorded")

    def test_negative_and_missing_multiples_are_excluded(self):
        recs = {"OK1": rec(20.0, 100.0), "OK2": rec(20.0, 100.0),
                "NEG": rec(-8.0, 500.0), "NONE": rec(None, 500.0),
                "NOCAP": rec(20.0, None)}
        with mock.patch.object(valuation, "_cached_fundamentals", lambda: recs), \
             mock.patch.object(valuation, "_constituents", lambda s: tuple(recs)):
            out = valuation._fetch_forward_pe(
                "X", {"label": "X", "source": "x", "min_constituents": 2})
        self.assertEqual(out["constituents_used"], 2)
        self.assertAlmostEqual(out["forward_pe"], 20.0, places=2)


class ProxyTrailing(unittest.TestCase):

    def test_reads_trailing_and_derives_eps_per_share(self):
        recs = {"SOXX": rec(ttm=40.0, price=200.0)}
        rows = valuation._proxy_trailing(recs)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["symbol"], "SOXX")
        self.assertAlmostEqual(rows[0]["trailing_pe"], 40.0)
        self.assertAlmostEqual(rows[0]["trailing_eps_share"], 5.0)

    def test_never_emits_a_forward_field(self):
        """These sit on a page full of forward multiples.

        Any key with 'forward' in it here would be picked up as comparable.
        """
        recs = {s["symbol"]: rec(ttm=20.0, price=100.0)
                for s in valuation.PROXY_TRAILING}
        for row in valuation._proxy_trailing(recs):
            for key in row:
                self.assertNotIn("forward", key.lower())
            self.assertEqual(row["basis"], "trailing")

    def test_skips_absent_or_nonpositive_rather_than_shipping_none(self):
        recs = {"SOXX": rec(ttm=0.0), "EWY": rec(ttm=None)}
        self.assertEqual(valuation._proxy_trailing(recs), [])

    def test_carries_both_languages(self):
        for spec in valuation.PROXY_TRAILING:
            for key in ("index", "index_zh", "proxy", "proxy_zh"):
                self.assertTrue(spec.get(key), f"{spec['symbol']} missing {key}")

    def test_japan_is_not_a_proxy_row(self):
        """Japan has a real forward figure; a trailing 19.8x beside a forward
        19.4x is exactly the confusion this page exists to prevent."""
        self.assertNotIn("EWJ", [s["symbol"] for s in valuation.PROXY_TRAILING])
        self.assertIn("N225", valuation.INDEX_UNIVERSE)


class CurrencyConversion(unittest.TestCase):
    """data.usd_rate — the only reason the $B column is not a 150x lie."""

    def setUp(self):
        from ystocker import data
        self.data = data
        data._fx_cache.clear()

    def test_usd_and_missing_currency_cost_no_request(self):
        with mock.patch.object(self.data.yf, "Ticker",
                               side_effect=AssertionError("must not fetch")):
            self.assertEqual(self.data.usd_rate("USD"), 1.0)
            self.assertEqual(self.data.usd_rate("usd"), 1.0)
            self.assertEqual(self.data.usd_rate(None), 1.0)
            self.assertEqual(self.data.usd_rate(""), 1.0)

    def test_reads_the_direct_pair_and_caches_it(self):
        tk = mock.Mock()
        tk.info = {"regularMarketPrice": 0.0065}
        with mock.patch.object(self.data.yf, "Ticker", return_value=tk) as m:
            self.assertAlmostEqual(self.data.usd_rate("JPY"), 0.0065)
            self.assertAlmostEqual(self.data.usd_rate("JPY"), 0.0065)
        m.assert_called_once_with("JPYUSD=X")

    def test_failure_caches_none_so_the_batch_does_not_retry(self):
        with mock.patch.object(self.data.yf, "Ticker",
                               side_effect=RuntimeError("no such pair")) as m:
            self.assertIsNone(self.data.usd_rate("ZZZ"))
            self.assertIsNone(self.data.usd_rate("ZZZ"))
        self.assertEqual(m.call_count, 1)

    def test_unpriceable_pair_returns_none_not_one(self):
        """Returning 1.0 on failure would pass the local-currency number
        through as dollars, which is the bug this exists to prevent."""
        tk = mock.Mock()
        tk.info = {}
        with mock.patch.object(self.data.yf, "Ticker", return_value=tk):
            self.assertIsNone(self.data.usd_rate("JPY"))

    def test_usd_path_does_not_round_or_alter_prices(self):
        """The ~230 existing USD tickers must come through byte-identical.

        A sub-cent price rounded to 0.00 reads as free and makes the
        `price / multiple` derivations a division by zero.
        """
        info = {"currency": "USD", "currentPrice": 35.2799, "marketCap": 4.2e9,
                "targetMeanPrice": 0.0031, "trailingPE": 12.0}
        with mock.patch.object(self.data, "usd_rate", return_value=1.0), \
             mock.patch.object(self.data.yf, "Ticker") as tk, \
             mock.patch.object(self.data.fetchguard, "guard"):
            tk.return_value.info = info
            out = self.data.fetch_ticker_data("X")
        self.assertEqual(out["Current Price"], 35.2799)
        self.assertEqual(out["Target Price"], 0.0031)
        self.assertEqual(out["Market Cap ($B)"], 4.2)

    def test_non_usd_fields_are_converted(self):
        info = {"currency": "JPY", "currentPrice": 3116.0,
                "marketCap": 36899154558976, "trailingPE": 8.87,
                "forwardPE": 9.61}
        with mock.patch.object(self.data, "usd_rate", return_value=0.00625), \
             mock.patch.object(self.data.yf, "Ticker") as tk, \
             mock.patch.object(self.data.fetchguard, "guard"):
            tk.return_value.info = info
            out = self.data.fetch_ticker_data("7203.T")
        self.assertAlmostEqual(out["Current Price"], 19.475, places=3)
        self.assertAlmostEqual(out["Market Cap ($B)"], 230.6, places=1)
        # Ratios must NOT be touched — they already cancel the currency.
        self.assertEqual(out["PE (TTM)"], 8.87)
        self.assertEqual(out["PE (Forward)"], 9.61)

    def test_unpriceable_currency_blanks_dollar_fields_rather_than_lying(self):
        info = {"currency": "ZZZ", "currentPrice": 3116.0,
                "marketCap": 36899154558976, "trailingPE": 8.87}
        with mock.patch.object(self.data, "usd_rate", return_value=None), \
             mock.patch.object(self.data.yf, "Ticker") as tk, \
             mock.patch.object(self.data.fetchguard, "guard"):
            tk.return_value.info = info
            out = self.data.fetch_ticker_data("X.ZZ")
        self.assertIsNone(out["Market Cap ($B)"])
        self.assertIsNone(out["Current Price"])
        self.assertEqual(out["PE (TTM)"], 8.87, "the multiple is still valid")


class TemplateContract(unittest.TestCase):
    """Keys the page asks for must exist, in both languages.

    Same shape as tests/test_deferload_anchors.py: a missing translation key
    renders as an empty string, so the caveat simply vanishes rather than
    erroring — invisible in exactly the case it matters.
    """

    @classmethod
    def setUpClass(cls):
        cls.i18n = (ROOT / "ystocker" / "static" / "i18n.js").read_text(encoding="utf-8")
        cls.html = (ROOT / "ystocker" / "templates" / "multiples.html").read_text(encoding="utf-8")

    def _assert_key(self, key):
        m = re.search(r"'" + re.escape(key) + r"':\s*\{(.*?)\}\s*,\n",
                      self.i18n, re.S)
        self.assertIsNotNone(m, f"{key} missing from i18n.js")
        body = m.group(1)
        self.assertIn("en:", body, f"{key} has no en")
        self.assertIn("zh:", body, f"{key} has no zh")

    def test_every_weighting_note_token_has_a_translation(self):
        tokens = {meta["weighting_note"]
                  for meta in valuation.INDEX_UNIVERSE.values()
                  if meta.get("weighting_note")}
        self.assertTrue(tokens, "expected at least one weighting_note")
        for tok in tokens:
            self._assert_key(f"mult.basis_{tok}")

    def test_weighting_note_strings_use_placeholders_not_baked_numbers(self):
        """A hardcoded '36%' reads plausibly forever after it stops being true."""
        for meta in valuation.INDEX_UNIVERSE.values():
            tok = meta.get("weighting_note")
            if not tok:
                continue
            m = re.search(r"'mult\.basis_" + re.escape(tok) + r"':\s*\{(.*?)\}\s*,\n",
                          self.i18n, re.S)
            body = m.group(1)
            self.assertIn("{top}", body, f"basis_{tok} must substitute {{top}}")
            self.assertIn("{pct}", body, f"basis_{tok} must substitute {{pct}}")

    def test_every_forward_index_has_a_kpi_tile_and_label(self):
        for etf in valuation.INDEX_UNIVERSE:
            key = f"{etf.lower()}_forward_pe"
            self.assertIn(f"'{key}'", self.html,
                          f"{etf} has no KPI tile, so its figure is computed "
                          f"and then never shown")
        for suffix in ("spy", "qqq", "sox", "n225"):
            self._assert_key(f"mult.kpi_{suffix}")

    def test_proxy_card_keys_exist(self):
        for key in ("mult.sec_proxy", "mult.proxy_title", "mult.proxy_desc",
                    "mult.proxy_korea", "mult.proxy_index", "mult.proxy_via",
                    "mult.proxy_pe", "mult.proxy_eps"):
            self._assert_key(key)

    def test_one_ordering_source_not_four(self):
        """The four ['SPY','QQQ'] arrays had already drifted once."""
        self.assertEqual(self.html.count("['SPY', 'QQQ']"), 0,
                         "a hardcoded SPY/QQQ order array is back; use IDX")
        self.assertIn("const IDX = ", self.html)

    def test_short_ranges_are_offered_on_the_long_series(self):
        """1Y/2Y — the whole point of the range buttons for a 155-year chart."""
        m = re.search(r"const autoRanges = span => \((.*?)\);", self.html, re.S)
        self.assertIsNotNone(m)
        for branch in m.group(1).split(":"):
            if "[" not in branch:
                continue
            self.assertIn("12", branch, "every range set must offer 1Y")
            self.assertIn("24", branch, "every range set must offer 2Y")


class RequiredGroupReconciliation(unittest.TestCase):
    """The deploy trap: cache/peer_groups.json replaces PEER_GROUPS wholesale.

    A box that has ever used the /groups UI holds a saved copy, and before
    REQUIRED_GROUPS existed that copy won at startup — so extending a group in
    code shipped nothing. /multiples would drop the SOX and Nikkei tiles and the
    page would look untouched, with no error and no log line.

    These drive the real :func:`ystocker.merge_saved_groups`, which is the same
    function ``routes._load_groups`` calls; it lives in ``__init__`` precisely so
    this can be tested without importing ``routes`` and its matplotlib chain.
    """

    def setUp(self):
        from ystocker import PEER_GROUPS as PG, REQUIRED_GROUPS, merge_saved_groups
        self.defaults = {k: list(v) for k, v in PG.items()}
        self.REQUIRED = REQUIRED_GROUPS
        self.merge = merge_saved_groups

    def _run_load(self, saved):
        return self.merge(saved, self.defaults)

    def test_required_groups_survive_a_stale_saved_file(self):
        """An old saved file with the 8-name semis group must not win."""
        stale = {"Semiconductors": ["NVDA", "AMD", "TSM", "AVGO",
                                    "ASML", "INTC", "QCOM", "MU"],
                 "Tech": ["MSFT", "AAPL"]}
        got = self._run_load(stale)
        for t in valuation.SOX30:
            self.assertIn(t, got["Semiconductors"],
                          f"{t} lost to a stale peer_groups.json")

    def test_a_required_group_absent_from_the_saved_file_is_restored(self):
        """Japan (Nikkei) will be missing from every existing box's file."""
        got = self._run_load({"Tech": ["MSFT"]})
        self.assertIn("Japan (Nikkei)", got)
        for t in valuation.N225:
            self.assertIn(t, got["Japan (Nikkei)"])

    def test_user_additions_to_a_required_group_are_kept(self):
        got = self._run_load({"Semiconductors": list(valuation.SOX30) + ["CRUS"]})
        self.assertIn("CRUS", got["Semiconductors"])
        self.assertIn("NVDA", got["Semiconductors"])

    def test_non_required_groups_are_still_fully_user_owned(self):
        """The override behaviour must not change for everything else."""
        got = self._run_load({"Tech": ["ONLY_THIS"]})
        self.assertEqual(got["Tech"], ["ONLY_THIS"])

    def test_no_duplicates_after_merging(self):
        got = self._run_load({"Semiconductors": ["NVDA", "NVDA", "AMD"]})
        group = got["Semiconductors"]
        self.assertEqual(len(group), len(set(group)))

    def test_every_required_group_actually_exists_in_the_defaults(self):
        """A typo in REQUIRED_GROUPS would be a silent no-op."""
        for name in self.REQUIRED:
            self.assertIn(name, self.defaults,
                          f"REQUIRED_GROUPS names {name!r}, which is not a "
                          f"PEER_GROUPS key — the reconciliation does nothing")

    def test_proxy_and_universe_symbols_all_reachable_after_a_hostile_merge(self):
        """End to end: an empty saved file must still leave every symbol
        /multiples needs present."""
        got = self._run_load({})
        held = {t for group in got.values() for t in group}
        for t in list(valuation.SOX30) + list(valuation.N225):
            self.assertIn(t, held)
        for spec in valuation.PROXY_TRAILING:
            self.assertIn(spec["symbol"], held)


if __name__ == "__main__":
    unittest.main()
