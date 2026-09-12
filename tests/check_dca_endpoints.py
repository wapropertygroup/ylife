"""
End-to-end check of /dca and its API, driven through Flask's test client.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app (which
starts background threads). It needs no network — the reconstruction is
pre-seeded into ``dca_history``'s in-memory cache, and the peer and revision
lookups are stubbed.

matplotlib is stubbed before ``ystocker.routes`` is imported, for the broken
Homebrew pyexpat this repo's dev checkout has (see ``check_assets_endpoints.py``,
which does the same for the same reason). /dca draws in Chart.js and imports
nothing from matplotlib.

Run:  venv/bin/python -m tests.check_dca_endpoints
"""
from __future__ import annotations

import os
import sys
import types
import unittest


class _Any:
    """Accepts any attribute access, call, subscript or context-manager use."""
    def __getattr__(self, _name): return _Any()
    def __call__(self, *_a, **_k): return _Any()
    def __getitem__(self, _k): return _Any()
    def __setitem__(self, _k, _v): return None
    def __enter__(self): return _Any()
    def __exit__(self, *_a): return False
    def update(self, *_a, **_k): return None


for _name in ("matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
              "matplotlib.dates", "matplotlib.patches", "matplotlib.colors",
              "matplotlib.figure", "matplotlib.cm", "matplotlib.font_manager",
              "seaborn"):
    if _name not in sys.modules:
        _mod = types.ModuleType(_name)
        _mod.__getattr__ = lambda _attr: _Any()      # type: ignore[attr-defined]
        sys.modules[_name] = _mod

os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-dca-secret")
# The portfolio side of these checks writes real positions. Keep them in the
# local file store rather than the shared DynamoDB table -- a test run must not
# be able to edit somebody's actual holdings.
os.environ["ASSETS_LOCAL_STORE"] = "1"

import time                                                       # noqa: E402
import tempfile                                                   # noqa: E402
from datetime import date, timedelta                              # noqa: E402
from pathlib import Path                                          # noqa: E402

from ystocker import dca, dca_history as dh                       # noqa: E402
from ystocker import dca_universe as du                           # noqa: E402

# No background sweeps during a check run.
dh.start_background_thread = lambda: None                          # type: ignore[assignment]

# The registry is isolated to a scratch mirror with DynamoDB switched off.
# Without this the checks read and *write* the live ystocker-dca-universe table:
# a test that remembers or forgets a ticker would be editing what the deployed
# overview ranks, and a leftover row would then leak into the next run's
# assertions. Setting the unavailability deadline to infinity is what keeps
# _get_table() from ever connecting.
_REG_DIR = tempfile.TemporaryDirectory()
du.LOCAL_PATH = Path(_REG_DIR.name) / "dca_universe.json"
du._table = None
du._table_unavail_until = float("inf")

from ystocker import create_app                                    # noqa: E402


def _seed(ticker: str = "MSFT") -> dict:
    """A believable reconstruction, built from literals rather than fetched."""
    inc, bal, cfs = {}, {}, {}
    for i, year in enumerate((2020, 2021, 2022, 2023)):
        key = f"{year}-12-31"
        inc[key] = {"Total Revenue": 100.0 + 15 * i, "Diluted EPS": 4.0 + 0.6 * i,
                    "Net Income": 10.0 + 2 * i, "EBITDA": 20.0 + 3 * i}
        bal[key] = {"Ordinary Shares Number": 10.0, "Total Debt": 30.0,
                    "Cash And Cash Equivalents": 12.0, "Tangible Book Value": 40.0}
        cfs[key] = {"Free Cash Flow": 8.0 + i, "Depreciation And Amortization": 5.0}

    vintages = dh.build_vintages(inc, bal, cfs)
    day = date(2021, 1, 4)
    prices = [((day + timedelta(weeks=w)).isoformat(), round(60 + 0.22 * w, 4))
              for w in range(240)]
    series = dh.reconstruct(prices, vintages)

    payload = {
        "_ver": dh.CACHE_VER, "_ts": time.time(), "ticker": ticker,
        "name": "Microsoft Corporation", "sector": "Technology",
        "industry": "Software - Infrastructure", "quote_type": "EQUITY",
        "series": series,
        "percentiles": dh.latest_percentiles(series, minimum=dca.MIN_OBSERVATIONS),
        "window": dh.window_meta(series, vintages),
        "vintages": [v.as_dict() for v in vintages],
        "forward_context": {"forwardPE": 21.78, "trailingPE": 28.58},
        "prices": prices[-260:],
    }
    dh._mem[ticker] = (payload["_ts"], payload)
    return payload


class DcaEndpoints(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        # Neither of these may touch the network from a request.
        dh.peer_percentiles = lambda t, recs=None: {"percentile": 62.5, "group": "Tech",
                                                    "basis": "forward", "value": 21.8,
                                                    "peers": 11, "median": 24.0}
        dh.eps_drift = lambda t: {"drift": 0.031, "current": 14.2, "prior": 13.77,
                                  "period": "+1y", "lookback_days": 90}
        _seed("MSFT")

    # ── the page ──────────────────────────────────────────────────────────
    def test_page_renders_for_a_known_ticker(self):
        r = self.client.get("/dca/MSFT")
        self.assertEqual(r.status_code, 200)
        body = r.data.decode()
        self.assertIn('const TICKER = "MSFT"', body)
        self.assertIn("DCA Valuation Engine", body)

    def test_page_renders_for_an_unknown_ticker(self):
        """Matches /history: the page paints, the API reports what it found."""
        self.assertEqual(self.client.get("/dca/ZZZZ").status_code, 200)

    def test_page_lowercases_are_normalised(self):
        self.assertIn('const TICKER = "MSFT"', self.client.get("/dca/msft").data.decode())

    def test_history_page_links_to_dca(self):
        body = self.client.get("/history/MSFT").data.decode()
        self.assertIn("/dca/MSFT", body)

    # ── the API ───────────────────────────────────────────────────────────
    def test_api_returns_a_score_and_an_equation(self):
        r = self.client.get("/api/dca/MSFT?base=5000")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertEqual(d["status"], "ok")
        self.assertEqual(d["model"], "compounder")
        self.assertIsNotNone(d["V"])
        self.assertTrue(0 <= d["V"] <= 100)
        eq = d["equation"]
        for key in ("e_expression", "v_expression", "m_expression", "dca_expression"):
            self.assertTrue(eq[key], f"{key} missing from the equation block")

    def test_the_equation_evaluates_to_its_own_answer(self):
        """The page prints both the terms and the total; they must agree.

        This is the check that a rounding change in one place cannot silently
        make the shown arithmetic disagree with the shown result.
        """
        d = self.client.get("/api/dca/MSFT?base=5000").get_json()
        eq = d["equation"]
        total = sum(t["weight"] * t["percentile"] for t in eq["terms"])
        self.assertAlmostEqual(total, eq["e_value"], places=1)
        self.assertAlmostEqual(eq["v_value"], 100 - eq["e_value"], places=2)
        self.assertAlmostEqual(eq["m_value"], 0.5 + eq["v_value"] / 100, places=3)

    def test_base_scales_only_the_final_line(self):
        a = self.client.get("/api/dca/MSFT?base=1000").get_json()
        b = self.client.get("/api/dca/MSFT?base=2000").get_json()
        self.assertEqual(a["V"], b["V"])
        self.assertEqual(a["m_valuation"], b["m_valuation"])
        # delta, not places: each amount is rounded to the cent independently, so
        # doubling the base can legitimately differ by one cent from doubling the
        # rounded result.
        self.assertAlmostEqual(b["amount"], a["amount"] * 2, delta=0.02)

    def test_a_nonsense_base_falls_back_rather_than_failing(self):
        for bad in ("abc", "-50", "0", "999999999"):
            r = self.client.get(f"/api/dca/MSFT?base={bad}")
            self.assertEqual(r.status_code, 200, bad)
            self.assertEqual(r.get_json()["base_dca"], 1000.0, bad)

    def test_the_peer_factor_reaches_the_score(self):
        d = self.client.get("/api/dca/MSFT").get_json()
        self.assertIn("peer", [f["factor"] for f in d["factors"]])
        self.assertEqual(d["peer"]["percentile"], 62.5)

    def test_the_v_line_excludes_peer_and_says_so_by_omission(self):
        d = self.client.get("/api/dca/MSFT").get_json()
        self.assertTrue(d["v_history"])
        self.assertNotIn("peer", d["series"])

    def test_the_window_states_its_own_evidence(self):
        w = self.client.get("/api/dca/MSFT").get_json()["window"]
        self.assertEqual(w["basis"], "trailing")
        self.assertGreater(w["observations"], 0)
        self.assertGreater(w["vintages"], 0)

    def test_the_banked_series_is_reported_even_when_empty(self):
        """A daily series that stopped being written looks like one that never
        started, and this one cannot be backfilled — so zero has to be visible."""
        b = self.client.get("/api/dca/MSFT").get_json()["banked"]
        self.assertEqual(b["basis"], "forward")
        self.assertIn("count", b)
        self.assertIn("rankable", b)
        self.assertEqual(b["min_observations"], dca.MIN_OBSERVATIONS)

    def test_the_banked_series_is_never_merged_into_the_reconstruction(self):
        """Two bases, reported side by side, never spliced."""
        d = self.client.get("/api/dca/MSFT").get_json()
        self.assertEqual(d["window"]["basis"], "trailing")
        self.assertEqual(d["banked"]["basis"], "forward")
        self.assertNotEqual(d["window"]["basis"], d["banked"]["basis"])

    def test_forward_context_is_present_and_never_percentiled(self):
        d = self.client.get("/api/dca/MSFT").get_json()
        self.assertEqual(d["forward_context"]["forwardPE"], 21.78)
        self.assertNotIn("forwardPE", [f["factor"] for f in d["factors"]])

    def test_a_cold_ticker_answers_warming_not_an_error(self):
        r = self.client.get("/api/dca/NOTSEEDED")
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.get_json()["status"], "warming")

    def test_signed_out_leaves_the_portfolio_overlay_neutral_and_labelled(self):
        d = self.client.get("/api/dca/MSFT").get_json()
        self.assertEqual(d["m_portfolio"], 1.0)
        self.assertEqual(d["portfolio_band"], "unknown")
        self.assertEqual(d["portfolio"]["reason"], "signed_out")

    def test_a_burst_of_distinct_cold_tickers_is_throttled(self):
        """The regression: per-symbol de-duplication bounds nothing here.

        Twenty *different* symbols is twenty six-read bursts with nothing in
        between, which is what one person clicking through ticker pages actually
        produced. Every request must still answer 202 — the reader is not shown
        an error for browsing — but only the budget's worth may start work.
        """
        import ystocker.dca_history as dhm

        dhm._inflight = 0
        dhm._last_build_start = 0.0
        started = []
        real_get = dhm.get
        dhm.get = lambda sym, **k: (started.append(sym), real_get(sym, **k))[1]
        try:
            codes = []
            for i in range(12):
                r = self.client.get(f"/api/dca/COLD{i}")
                codes.append(r.status_code)
                self.assertEqual(r.get_json()["status"], "warming")
            self.assertEqual(set(codes), {202},
                             "a throttled rebuild is still a 202, never an error")
        finally:
            dhm.get = real_get
            dhm._inflight = 0
            dhm._last_build_start = 0.0
        self.assertLessEqual(
            len(started), dhm.MAX_INFLIGHT_BUILDS,
            f"12 distinct cold tickers started {len(started)} rebuilds; the "
            f"global budget allows {dhm.MAX_INFLIGHT_BUILDS}")

    def test_a_throttled_request_says_it_is_queued(self):
        """"Queued" and "running" are different states and the page says which."""
        import ystocker.dca_history as dhm

        dhm._inflight = dhm.MAX_INFLIGHT_BUILDS      # nothing available
        try:
            d = self.client.get("/api/dca/ALSOCOLD").get_json()
            self.assertTrue(d["queued"])
            self.assertEqual(d["budget"]["max_inflight"], dhm.MAX_INFLIGHT_BUILDS)
        finally:
            dhm._inflight = 0

    def test_the_refresh_route_redirects_back_to_the_page(self):
        r = self.client.get("/dca/MSFT/refresh")
        self.assertEqual(r.status_code, 302)
        self.assertIn("/dca/MSFT", r.headers["Location"])

    def test_an_unscorable_ticker_answers_200_not_500(self):
        """Too little of a template surviving must not crash the equation builder.

        ``expensiveness`` deliberately leaves ``weight``/``contribution`` unset
        when it refuses to renormalise, and formatting ``None`` with ``:.2f``
        raises ``TypeError``. This is not an edge case — a bank with no tangible
        book value, or an ADR with thin statements, lands here routinely.
        """
        thin = _seed("THIN")
        # Keep one low-weight factor so there are rows but not enough weight.
        thin["series"] = {"pe": thin["series"]["pe"]}
        thin["percentiles"] = {"pe": 55.0}
        dh._mem["THIN"] = (thin["_ts"], thin)
        try:
            dh.peer_percentiles = lambda t, recs=None: {"percentile": None, "group": None,
                                                        "reason": "no_group"}
            r = self.client.get("/api/dca/THIN")
            self.assertEqual(r.status_code, 200)
            d = r.get_json()
            self.assertIsNone(d["V"], "a refused score must be null, not invented")
            self.assertIsNone(d["amount"])
            self.assertIsNone(d["equation"]["e_expression"])
            self.assertTrue(d["dropped"], "the page still needs to say what was missing")
        finally:
            dh.peer_percentiles = lambda t, recs=None: {"percentile": 62.5, "group": "Tech",
                                                        "basis": "forward", "value": 21.8,
                                                        "peers": 11, "median": 24.0}


class DcaOverview(unittest.TestCase):
    """The /dca ranked overview."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        dh.peer_percentiles = lambda t, recs=None: {"percentile": 40.0, "group": "Tech"}
        dh.eps_drift = lambda t: {"drift": 0.0}
        # Two names cached, the rest of the universe deliberately not, so the
        # partial-coverage path is what gets exercised.
        cls.built = ["MSFT", "NVDA"]
        for sym in cls.built:
            _seed(sym)
        # Reflect whatever has been seeded rather than a frozen list, so a test
        # that seeds a new ticker sees it as cached — which is what the real
        # cached_tickers() (a glob of the cache dir) would do.
        dh.cached_tickers = lambda: sorted(dh._mem)
        # The warm kick must never fire a real sweep from a test.
        dh.warm_universe = lambda *a, **k: 0

    def test_page_renders(self):
        r = self.client.get("/dca")
        self.assertEqual(r.status_code, 200)
        self.assertIn("DCA Valuation Engine", r.data.decode())

    def test_it_ranks_only_what_is_already_built(self):
        """A ranked table must never trigger a fan-out of six reads per name."""
        d = self.client.get("/api/dca").get_json()
        ranked = {r["ticker"] for r in d["rows"]}
        self.assertTrue(set(self.built).issubset(ranked))
        # Everything ranked must have a payload; nothing was fetched to build it.
        self.assertTrue(ranked.issubset(set(dh.cached_tickers())))
        self.assertGreater(d["universe"], d["scored"])

    def test_partial_coverage_is_stated_not_hidden(self):
        """A league table silently missing entries is worse than an honest gap."""
        d = self.client.get("/api/dca").get_json()
        self.assertTrue(d["pending"])
        self.assertNotIn("MSFT", d["pending"])

    def test_cheapest_sorts_first(self):
        d = self.client.get("/api/dca").get_json()
        vs = [r["V"] for r in d["rows"] if r["V"] is not None]
        self.assertEqual(vs, sorted(vs, reverse=True))

    def test_rows_agree_with_the_detail_endpoint(self):
        """One scoring path, so a row and its detail page cannot disagree.

        A reader comparing the table against a ticker's own page is exactly who
        would find a drift between two implementations of the same formula.
        """
        d = self.client.get("/api/dca?base=2500").get_json()
        row = next(r for r in d["rows"] if r["ticker"] == "MSFT")
        detail = self.client.get("/api/dca/MSFT?base=2500").get_json()
        for key in ("V", "E", "band", "model", "m_valuation", "m_earnings",
                    "m_portfolio", "multiplier", "amount"):
            self.assertEqual(row[key], detail[key], f"{key} differs between list and detail")

    def test_base_scales_every_row(self):
        a = self.client.get("/api/dca?base=1000").get_json()
        b = self.client.get("/api/dca?base=2000").get_json()
        self.assertEqual(b["base_dca"], 2000.0)
        for ra, rb in zip(a["rows"], b["rows"]):
            self.assertEqual(ra["V"], rb["V"])
            self.assertAlmostEqual(rb["amount"], ra["amount"] * 2, delta=0.02)

    def test_a_nonsense_base_falls_back(self):
        for bad in ("abc", "-1", "0"):
            self.assertEqual(
                self.client.get(f"/api/dca?base={bad}").get_json()["base_dca"], 1000.0)

    def test_the_universe_always_contains_the_seed(self):
        """The registry grows, but the framework's named set is the floor.

        A burst of lookups must not be able to push MSFT out of the table the
        overview exists to show.
        """
        self.assertTrue(du.seed().issubset(set(dh.universe())))
        self.assertNotIn("GOOG", dh.universe(), "GOOG and GOOGL are one company")

    def test_nav_links_to_the_overview(self):
        self.assertIn('href="/dca"', self.client.get("/dca/MSFT").data.decode())

    def test_the_overview_carries_a_ticker_search(self):
        """Any listed symbol must be reachable, not just the ranked universe."""
        body = self.client.get("/dca").data.decode()
        self.assertIn('id="tickerSearch"', body)
        self.assertIn("/api/search", body)

    def test_the_search_backend_answers(self):
        r = self.client.get("/api/search?q=NVD")
        self.assertEqual(r.status_code, 200)
        self.assertIn("NVDA", [m["ticker"] for m in r.get_json()])

    def test_an_off_universe_symbol_still_has_a_page(self):
        """The search leads with whatever was typed, so that page must exist."""
        self.assertEqual(self.client.get("/dca/GDX").status_code, 200)

    def test_a_scored_ticker_joins_the_tracked_list(self):
        """Searching a name that scores must add it to the ranked table.

        Registration lives in dca_history.get() so it happens once, on a
        successful build, rather than at each of the several places a ticker can
        be reached from.
        """
        payload = _seed("SHOP")
        self.assertFalse(payload.get("unavailable"))
        du.remember("SHOP")
        try:
            self.assertIn("SHOP", du.all_tickers())
            self.assertIn("SHOP", [r["ticker"] for r in
                                   self.client.get("/api/dca").get_json()["rows"]])
        finally:
            du.forget("SHOP")

    def test_a_name_that_cannot_score_is_not_tracked(self):
        """An ETF has no statements: a permanently blank row that still costs
        six Yahoo reads a day to re-confirm."""
        self.assertNotIn("GDX", du.all_tickers())

    def test_untracking_removes_a_name(self):
        du.remember("SHOP")
        try:
            r = self.client.delete("/api/dca/track/SHOP")
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.get_json()["removed"])
            self.assertNotIn("SHOP", du.all_tickers())
        finally:
            du.forget("SHOP")

    def test_a_seed_name_cannot_be_untracked(self):
        """It would reappear on the next sweep, which reads as a bug."""
        r = self.client.delete("/api/dca/track/MSFT")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.get_json()["reason"], "seed")
        self.assertIn("MSFT", du.all_tickers())

    def test_the_list_reports_registry_capacity(self):
        reg = self.client.get("/api/dca").get_json()["registry"]
        self.assertIn("max", reg)
        self.assertIn("effective", reg)
        self.assertLessEqual(reg["effective"], reg["max"])

    def test_viewing_an_already_cached_ticker_registers_it(self):
        """The gap that made the registry miss everything opened before it.

        dca_history.get() only registers on a *build*, so a payload already on
        disk would never enter the registry — which is every name looked up
        before this existed, and any name untracked and then opened again.
        """
        _seed("SHOP")                        # cached, but not registered
        du.forget("SHOP")
        self.assertNotIn("SHOP", du.all_tickers())
        try:
            self.assertEqual(self.client.get("/api/dca/SHOP").status_code, 200)
            self.assertIn("SHOP", du.all_tickers())
        finally:
            du.forget("SHOP")

    def test_viewing_an_unscorable_cached_ticker_does_not_register_it(self):
        payload = _seed("ETFISH")
        payload["unavailable"] = "too_few_vintages"
        payload["series"] = {}
        dh._mem["ETFISH"] = (payload["_ts"], payload)
        try:
            self.assertEqual(self.client.get("/api/dca/ETFISH").status_code, 200)
            self.assertNotIn("ETFISH", du.all_tickers())
        finally:
            du.forget("ETFISH")
            dh._mem.pop("ETFISH", None)


#: A fund and the companies inside it, so the look-through has something real to
#: penetrate. Without this the portfolio panel yields no rows at all and every
#: assertion about 穿透 would pass vacuously — the exact shape of test that looks
#: like coverage and is not.
_FUND_UNIVERSE = {
    "VOO": {"symbol": "VOO", "name": "Vanguard S&P 500", "kind": "fund",
            "price": 512.00, "quote_type": "ETF",
            "holdings": [{"symbol": "MSFT", "name": "Microsoft Corp", "weight": 0.07},
                         {"symbol": "NVDA", "name": "NVIDIA Corp", "weight": 0.06}],
            "asset_classes": {"stock": 0.999, "bond": 0.0, "cash": 0.001},
            "sectors": {"technology": 0.35}},
    "MSFT": {"symbol": "MSFT", "name": "Microsoft Corp", "kind": "equity",
             "price": 505.00, "quote_type": "EQUITY", "holdings": [],
             "asset_classes": {}, "sectors": {}, "sector": "Technology"},
    "NVDA": {"symbol": "NVDA", "name": "NVIDIA Corp", "kind": "equity",
             "price": 178.50, "quote_type": "EQUITY", "holdings": [],
             "asset_classes": {}, "sectors": {}, "sector": "Technology"},
}


def _seed_funds() -> None:
    from ystocker import funddata

    now = time.time()
    with funddata._lock:                                   # noqa: SLF001
        funddata._loaded = True                            # noqa: SLF001
        for symbol, rec in _FUND_UNIVERSE.items():
            full = dict(rec)
            full.update({"quote_at": now, "comp_at": now, "read_at": now,
                         "currency": "USD"})
            full.setdefault("sector", "")
            funddata._mem[symbol] = full                   # noqa: SLF001


class DcaPortfolio(unittest.TestCase):
    """/api/dca/portfolio — the multiplier applied to real holdings.

    Uses the local file store (``ASSETS_LOCAL_STORE``) so no DynamoDB portfolio
    is touched, and seeds funddata with a synthetic universe so the look-through
    resolves without a network call.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        dh.peer_percentiles = lambda t, recs=None: {"percentile": 50.0, "group": "Tech"}
        dh.eps_drift = lambda t: {"drift": 0.0}
        _seed("MSFT")
        _seed("NVDA")
        _seed_funds()

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess["user_email"] = "dca-check@example.com"

    def _positions(self, rows):
        from ystocker import portfolio

        portfolio.save("dca-check@example.com", rows)

    def test_signed_out_is_refused(self):
        anon = self.app.test_client()
        self.assertEqual(anon.get("/api/dca/portfolio").status_code, 401)

    def test_an_empty_portfolio_says_so(self):
        self._positions([])
        d = self.client.get("/api/dca/portfolio").get_json()
        self.assertEqual(d["rows"], [])
        self.assertEqual(d["reason"], "no_positions")

    def test_a_held_equity_is_sized(self):
        self._positions([{"symbol": "MSFT", "quantity": 10}])
        d = self.client.get("/api/dca/portfolio?base=1000").get_json()
        row = next(r for r in d["rows"] if r["ticker"] == "MSFT")
        self.assertEqual(row["status"], "ok")
        self.assertIsNotNone(row["m_valuation"])
        self.assertIsNotNone(row["amount"])
        self.assertEqual(d["base_dca"], 1000.0)

    def test_the_multiplier_is_the_product_of_its_three_terms(self):
        """The page prints all four numbers; they have to reconcile by hand."""
        self._positions([{"symbol": "MSFT", "quantity": 10}])
        row = next(r for r in self.client.get("/api/dca/portfolio").get_json()["rows"]
                   if r["ticker"] == "MSFT")
        product = row["m_valuation"] * row["m_earnings"] * row["m_portfolio"]
        expected = min(product, dca.MAX_TOTAL_MULTIPLIER)
        self.assertAlmostEqual(row["multiplier"], expected, places=3)

    def test_a_fund_is_penetrated_into_its_companies(self):
        """Holding only an ETF must still produce company rows.

        The whole point of running the panel off `exposures` rather than
        `positions`: a reader holding VOO owns the businesses inside it, and
        only a business has a valuation. Before this the row read
        "fund — not scored" and the tab was empty for an index investor.
        """
        self._positions([{"symbol": "VOO", "quantity": 20}])
        d = self.client.get("/api/dca/portfolio").get_json()
        tickers = {r["ticker"] for r in d["rows"]}
        self.assertNotIn("VOO", tickers, "the fund itself is not a unit of analysis")
        self.assertTrue({"MSFT", "NVDA"} & tickers,
                        f"VOO did not penetrate into its holdings: {tickers}")
        self.assertNotIn("fund", [r["status"] for r in d["rows"]])

    def test_exposure_through_a_fund_is_scored(self):
        """A company reached only through an ETF still gets a multiplier."""
        self._positions([{"symbol": "VOO", "quantity": 20}])
        row = next((r for r in self.client.get("/api/dca/portfolio").get_json()["rows"]
                    if r["ticker"] == "MSFT"), None)
        self.assertIsNotNone(row)
        self.assertEqual(row["status"], "ok")
        self.assertGreater(row["position_pct"], 0)

    def test_direct_and_fund_exposure_to_one_name_combine(self):
        """3% held directly plus an index fund is more than 3%.

        This is what makes M_portfolio mean something, and it is invisible on
        any per-line view.
        """
        self._positions([{"symbol": "VOO", "quantity": 20}])
        via_fund = next(r for r in
                        self.client.get("/api/dca/portfolio").get_json()["rows"]
                        if r["ticker"] == "MSFT")["position_pct"]
        self._positions([{"symbol": "VOO", "quantity": 20},
                         {"symbol": "MSFT", "quantity": 10}])
        combined = next(r for r in
                        self.client.get("/api/dca/portfolio").get_json()["rows"]
                        if r["ticker"] == "MSFT")["position_pct"]
        self.assertGreater(combined, via_fund,
                           "direct and indirect exposure must add up")

    def test_exposure_is_the_look_through_weight(self):
        """The reason this lives on /assets rather than /dca."""
        self._positions([{"symbol": "MSFT", "quantity": 10}])
        row = next(r for r in self.client.get("/api/dca/portfolio").get_json()["rows"]
                   if r["ticker"] == "MSFT")
        self.assertIsNotNone(row["position_pct"])
        self.assertIn("route_count", row)
        self.assertNotEqual(row["portfolio_band"], "unknown",
                            "a signed-in reader with holdings must get a real band")

    def test_rows_agree_with_the_per_ticker_endpoint(self):
        """One scoring path, so /assets and /dca/<ticker> cannot disagree."""
        self._positions([{"symbol": "MSFT", "quantity": 10}])
        row = next(r for r in
                   self.client.get("/api/dca/portfolio?base=2500").get_json()["rows"]
                   if r["ticker"] == "MSFT")
        detail = self.client.get("/api/dca/MSFT?base=2500").get_json()
        for key in ("V", "band", "model", "m_valuation", "m_earnings",
                    "m_portfolio", "multiplier", "amount"):
            self.assertEqual(row[key], detail[key], f"{key} differs")

    def test_the_portfolio_multiplier_is_exposure_weighted(self):
        """A 12% position and a 0.3% one must not get equal say."""
        self._positions([{"symbol": "MSFT", "quantity": 10}])
        d = self.client.get("/api/dca/portfolio").get_json()
        scored = [r for r in d["rows"] if r.get("amount") is not None]
        if not scored:
            self.skipTest("nothing scored in this fixture")
        value = sum(r["value"] or 0.0 for r in scored)
        expected = sum(r["multiplier"] * (r["value"] or 0.0) for r in scored) / value
        self.assertAlmostEqual(d["weighted_multiplier"], expected, places=3)

    def test_the_weighted_figures_state_what_share_they_cover(self):
        """A weighted V over a third of the equity must not read as the whole."""
        self._positions([{"symbol": "MSFT", "quantity": 10}])
        d = self.client.get("/api/dca/portfolio").get_json()
        self.assertIn("scored_share_pct", d)
        self.assertIn("coverage_pct", d)
        self.assertLessEqual(d["scored_share_pct"], 100.0)

    def test_truncation_is_reported_never_silent(self):
        """A ranked table missing names is honest only if it says how many."""
        self._positions([{"symbol": "MSFT", "quantity": 10}])
        d = self.client.get("/api/dca/portfolio").get_json()
        self.assertIn("not_ranked", d)
        self.assertIn("not_ranked_value", d)
        self.assertEqual(d["ranked"] + d["not_ranked"], d["exposure_count"])

    def test_totals_count_only_scored_rows(self):
        """Counting an unscored name at 1.0x would understate what the engine
        actually moved."""
        self._positions([{"symbol": "MSFT", "quantity": 10},
                         {"symbol": "NOSUCHTICKER", "quantity": 5}])
        d = self.client.get("/api/dca/portfolio?base=1000").get_json()
        self.assertLessEqual(d["scored"], d["ranked"])
        self.assertAlmostEqual(d["flat_total"], d["scored"] * 1000.0, places=2)

    def test_an_unscorable_name_has_no_amount_not_a_zero(self):
        self._positions([{"symbol": "NOSUCHTICKER", "quantity": 5}])
        d = self.client.get("/api/dca/portfolio").get_json()
        for r in d["rows"]:
            if r["status"] != "ok":
                self.assertIsNone(r.get("amount"))

    def test_the_assets_page_carries_the_dca_tab(self):
        body = self.client.get("/assets").data.decode()
        self.assertIn('data-tab="dca"', body)
        self.assertIn('data-panel="dca"', body)
        self.assertIn("/api/dca/portfolio", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
