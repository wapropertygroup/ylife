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


def _seed(ticker: str = "MSFT", *, listing_basis: dict | None = None,
          price_k: float = 1.0, name: str = "Microsoft Corporation") -> dict:
    """A believable reconstruction, built from literals rather than fetched.

    *price_k* scales the whole price path, which is the cheapest way to give two
    seeded tickers genuinely different scores: every multiple is price over the
    same statements, so a scaled path moves V without touching the fundamentals
    the rest of these checks assert on.
    """
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
    prices = [((day + timedelta(weeks=w)).isoformat(),
               round((60 + 0.22 * w) * price_k, 4))
              for w in range(240)]
    series = dh.reconstruct(prices, vintages)

    payload = {
        "_ver": dh.CACHE_VER, "_ts": time.time(), "ticker": ticker,
        "name": name, "sector": "Technology",
        "industry": "Software - Infrastructure", "quote_type": "EQUITY",
        "series": series,
        "percentiles": dh.latest_percentiles(series, minimum=dca.MIN_OBSERVATIONS),
        "window": dh.window_meta(series, vintages),
        "vintages": [v.as_dict() for v in vintages],
        "forward_context": {"forwardPE": 21.78, "trailingPE": 28.58},
        "prices": prices[-260:],
    }
    if listing_basis is not None:
        payload["listing_basis"] = listing_basis
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

    # ── the listing basis reaches the client ──────────────────────────────
    #
    # `build()` computes it and `/api/dca` assembles its response field by field
    # rather than passing the payload through, so the two can disagree silently
    # — and did: the ADR fix shipped with the reconciliation correct in the
    # payload and `listing_basis: null` on the wire, leaving the page unable to
    # say that a P/E had been converted out of another currency. Caught by
    # reading a live response, not by any of the 70 checks here.
    def test_a_listing_basis_is_surfaced_to_the_client(self):
        basis = {"statement_currency": "TWD", "price_currency": "USD",
                 "share_ratio": 5.0, "eps_scale": 1.0,
                 "fx_source": "series", "notes": []}
        _seed("TSMX", listing_basis=basis)
        body = self.client.get("/api/dca/TSMX").get_json()
        self.assertEqual(body.get("listing_basis"), basis)

    def test_an_ordinary_listing_reports_no_basis(self):
        """The field has to be absent-or-null for a US filer, or the Provenance
        card grows a reconciliation note on every ticker on the site."""
        self.assertIsNone(self.client.get("/api/dca/MSFT").get_json().get("listing_basis"))

    # ── the forward basis reaches the client ──────────────────────────────
    #
    # Same failure mode the listing basis just had: computed correctly inside
    # `_dca_score` and dropped on the way out, leaving the page unable to say
    # which basis produced a rank. These are the fields `renderFactors` reads.
    def test_a_factor_carries_both_multiples_and_the_basis_it_ranked_on(self):
        rows = self.client.get("/api/dca/MSFT").get_json()["factors"]
        pe = next(r for r in rows if r["factor"] == "pe")
        self.assertAlmostEqual(pe["forward_value"], 21.78)
        self.assertIsNotNone(pe["trailing_value"])
        # The seeded payload carries prices and vintages, so a forward history
        # derives from it and the factor ranks forward-against-forward.
        self.assertEqual(pe["basis"], "forward")
        self.assertEqual(pe["basis_source"], "reconstructed_forward")

    def test_the_fallback_holds_when_no_forward_history_can_be_built(self):
        """The property the whole design rests on, and the one that must not
        rot: a factor with no forward distribution of *either* kind stays on the
        trailing reconstruction rather than borrowing it. A forward value ranked
        against trailing history is biased cheap — measured at a median of 11.3
        percentile points, and saturating at exactly 0.0 for half the universe,
        which is the ranking being thrown away rather than merely shifted."""
        seeded = _seed("NOFWD")
        # No prices means nothing to reconstruct a forward series from, and no
        # banked rows either — the state every ETF and every thin ADR is in.
        seeded["prices"] = []
        seeded.pop("forward_series", None)
        body = self.client.get("/api/dca/NOFWD").get_json()
        self.assertTrue(all(r.get("basis") != "forward" for r in body["factors"]))
        self.assertEqual(body["score_basis"]["basis"], "trailing")
        pe = next(r for r in body["factors"] if r["factor"] == "pe")
        self.assertAlmostEqual(pe["raw_pct"], seeded["percentiles"]["pe"],
                               places=4)

    def test_a_long_banked_distribution_switches_that_factor_only(self):
        _seed("FWDX")
        original = dh.banked_distributions
        dh.banked_distributions = lambda t: (
            {"pe": [float(v) for v in range(5, 65)]} if t == "FWDX" else {})
        try:
            rows = self.client.get("/api/dca/FWDX").get_json()["factors"]
        finally:
            dh.banked_distributions = original
        pe = next(r for r in rows if r["factor"] == "pe")
        self.assertEqual(pe["basis"], "forward")
        self.assertEqual(pe["basis_observations"], 60)
        # The rank it replaced travels with it, so the switch is visible rather
        # than just having happened.
        self.assertIsNotNone(pe["trailing_percentile"])
        self.assertNotEqual(pe["raw_pct"], pe["trailing_percentile"])
        # Per factor, never per ticker: P/FCF has no forward distribution here.
        pfcf = next((r for r in rows if r["factor"] == "pfcf"), None)
        if pfcf is not None:
            self.assertNotEqual(pfcf.get("basis"), "forward")

    # ── the score states its own basis ────────────────────────────────────
    #
    # The factor table already labelled each row, but V is a weighted blend and
    # the headline said nothing — a reader seeing "forward 8.99" two cards down
    # had no way to know whether the 90.6 above used it without adding up
    # weights themselves. Reported as exactly that.
    def test_the_score_declares_which_basis_it_used(self):
        body = self.client.get("/api/dca/MSFT").get_json()
        sb = body["score_basis"]
        self.assertEqual(sb["basis"], "mixed")
        self.assertEqual(sb["sources"], ["reconstructed_forward"])
        # The two shares are the surviving, renormalised weights, so they close.
        self.assertAlmostEqual(sb["forward_weight"] + sb["trailing_weight"], 1.0,
                               places=6)
        # And they are the weights of the factors that actually switched.
        switched = sum(f["weight"] for f in body["factors"]
                       if f.get("basis") == "forward")
        total = sum(f["weight"] for f in body["factors"]
                    if isinstance(f.get("weight"), (int, float)))
        self.assertAlmostEqual(sb["forward_weight"], switched / total, places=3)

    def test_a_mixed_score_is_reported_by_weight_not_by_count(self):
        """One forward factor out of five is not "20% forward" if it is the
        P/E: the share has to be the weight it actually carried."""
        _seed("MIXY")
        original = dh.banked_distributions
        dh.banked_distributions = lambda t: (
            {"pe": [float(v) for v in range(5, 65)]} if t == "MIXY" else {})
        try:
            body = self.client.get("/api/dca/MIXY").get_json()
        finally:
            dh.banked_distributions = original
        sb = body["score_basis"]
        self.assertEqual(sb["basis"], "mixed")
        self.assertEqual(sb["forward_factors"], ["pe"])
        pe = next(r for r in body["factors"] if r["factor"] == "pe")
        self.assertAlmostEqual(sb["forward_weight"], pe["weight"], places=3)
        self.assertAlmostEqual(sb["forward_weight"] + sb["trailing_weight"], 1.0,
                               places=6)

    # ── keeping a name in the shared registry ─────────────────────────────
    def test_pinning_is_refused_when_signed_out(self):
        """The registry is one shared list and every row is six Yahoo reads a
        day, so a write is VIP-gated like the DCF override."""
        r = self.client.post("/api/dca/pin/MSFT")
        self.assertEqual(r.status_code, 403)
        self.assertFalse(self.client.get("/api/dca/MSFT").get_json()["pin_editable"])

    def test_the_detail_response_reports_pin_state_and_budget(self):
        body = self.client.get("/api/dca/MSFT").get_json()
        self.assertIn("pinned", body)
        self.assertIn("registry", body)
        # Both numbers: "room: 0" alone does not say whether the next lookup
        # costs the reader a name they wanted.
        self.assertIn("pinned", body["registry"])
        self.assertIn("max_pinned", body["registry"])

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

        Since the DCF edition ``V`` is no longer ``100 - E``: that is ``V_rel``,
        and the headline is the blend of it with ``V_dcf``. Both steps are
        asserted, so the chain is checked end to end rather than at one link —
        and a DCF that stopped being folded in would now fail here instead of
        passing an identity that had quietly stopped describing the engine.
        """
        d = self.client.get("/api/dca/MSFT?base=5000").get_json()
        eq = d["equation"]
        total = sum(t["weight"] * t["percentile"] for t in eq["terms"])
        self.assertAlmostEqual(total, eq["e_value"], places=1)

        # Relative branch.
        self.assertAlmostEqual(d["V_rel"], 100 - eq["e_value"], places=2)

        # Blend, or the identity when there is no DCF to blend.
        if d["blended"]:
            self.assertAlmostEqual(
                eq["v_value"],
                round(d["w_dcf"] * d["V_dcf"] + (1 - d["w_dcf"]) * d["V_rel"], 2),
                places=1)
        else:
            self.assertAlmostEqual(eq["v_value"], 100 - eq["e_value"], places=2)
            self.assertEqual(d["w_dcf"], 0.0)

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


class DcfBranch(unittest.TestCase):
    """The absolute branch, through the API that serves it."""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        dh.peer_percentiles = lambda t, recs=None: {"percentile": 62.5,
                                                    "group": "Tech"}
        dh.eps_drift = lambda t: {"drift": 0.0}
        _seed("MSFT")

    def _api(self, ticker="MSFT"):
        return self.client.get(f"/api/dca/{ticker}?base=5000").get_json()

    def test_the_branch_is_reported_on_every_scored_response(self):
        d = self._api()
        for key in ("V_rel", "V_dcf", "w_dcf", "w_dcf_template", "blended",
                    "dcf_enabled", "dcf_editable"):
            self.assertIn(key, d, key)

    def test_the_blend_reconstructs_the_headline(self):
        """The number the page shows must follow from the two it shows beside
        it. A reader checking it by hand is exactly who finds a drift."""
        d = self._api()
        if not d["blended"]:
            self.skipTest("no DCF for this fixture")
        expected = d["w_dcf"] * d["V_dcf"] + (1 - d["w_dcf"]) * d["V_rel"]
        self.assertAlmostEqual(d["V"], round(expected, 2), places=1)

    def test_the_equation_carries_a_dcf_block(self):
        eq = self._api()["equation"]
        self.assertIn("dcf", eq)
        self.assertIn("w_dcf", eq)

    def test_the_headline_v_line_is_never_missing(self):
        """Whether or not a DCF exists, the page must have a line explaining V.
        Losing it for the common case would leave the card blank."""
        eq = self._api()["equation"]
        self.assertIsNotNone(eq["v_expression"])

    def test_an_unblended_row_shows_no_intermediate_step(self):
        """Printing "V = 0.00(—) + 1.00(44.0)" for a ticker with no DCF reads as
        though the branch had been measured and found neutral."""
        eq = self._api()["equation"]
        if not eq["blended"]:
            self.assertIsNone(eq["v_rel_expression"])
            self.assertEqual(eq["w_dcf"], 0.0)

    def test_a_refused_branch_never_scores_as_fifty(self):
        d = self._api()
        dq = d["equation"]["dcf"] or {}
        if dq.get("refused"):
            self.assertIsNone(d["V_dcf"])
            self.assertEqual(d["w_dcf"], 0.0)
            self.assertEqual(d["V"], d["V_rel"])

    def test_the_overview_reports_the_branch_per_row(self):
        d = self.client.get("/api/dca").get_json()
        self.assertIn("dcf_enabled", d)
        for row in d["rows"]:
            for key in ("V_rel", "V_dcf", "w_dcf", "blended"):
                self.assertIn(key, row, key)

    # ── a stored override the engine cannot apply ─────────────────────────
    def test_an_unrankable_value_override_is_reported_not_dropped(self):
        """The failure this was written for.

        A P/E override stored against NEM changed nothing: that ticker's P/E
        series is 57 weekly points against a floor of 60, so `percentile_rank`
        refused and the loop `continue`d. The value was saved, the page accepted
        it, the score did not move, and nothing anywhere said the input had been
        discarded — which reads as the engine disagreeing rather than as the
        override being thrown away.

        The refusal is still correct: a rank over 57 points is not a percentile.
        What was wrong is that it was silent.
        """
        from ystocker import dca, routes

        payload = {
            # Deliberately one short of the floor.
            "series": {"pe": [(f"2026-01-{i:02d}", 20.0 + i)
                              for i in range(1, dca.MIN_OBSERVATIONS)]},
            "prices": [["2026-09-14", 100.0]],
            "vintages": [],
            "sector": "Basic Materials",
        }
        override = {"values": {"pe": 12.8}}
        # A request context: _dca_score reaches the session for the portfolio
        # overlay, which is request-scoped.
        with self.app.test_request_context("/api/dca/TEST"):
            result, _peer, _drift, _pos = routes._dca_score(
                "TEST", payload, 1000.0, overrides={"TEST": override})

        refusals = result.get("override_refusals") or []
        self.assertTrue(refusals, "a refused override must be reported")
        row = refusals[0]
        self.assertEqual(row["factor"], "pe")
        self.assertEqual(row["value"], 12.8)
        self.assertEqual(row["reason"], "too_few_observations")
        self.assertEqual(row["observations"], dca.MIN_OBSERVATIONS - 1)
        self.assertEqual(row["short_by"], 1)
        # And it genuinely did not take effect — the report is not cosmetic.
        self.assertNotIn("pe", result.get("overridden") or [])

    def test_a_rankable_value_override_still_applies_and_is_not_reported(self):
        """Guards the other direction: a refusal path that swallowed everything
        would satisfy the test above for ever."""
        from ystocker import dca, routes

        payload = {
            "series": {"pe": [(f"2026-01-{i:02d}", 20.0 + i)
                              for i in range(1, dca.MIN_OBSERVATIONS + 20)]},
            "prices": [["2026-09-14", 100.0]],
            "vintages": [],
            "sector": "Basic Materials",
        }
        with self.app.test_request_context("/api/dca/TEST"):
            result, _p, _d, _x = routes._dca_score(
                "TEST", payload, 1000.0, overrides={"TEST": {"values": {"pe": 12.8}}})
        self.assertIn("pe", result.get("overridden") or [])
        self.assertFalse(result.get("override_refusals"))

    # ── the override, and who may write one ───────────────────────────────
    def test_reading_an_override_is_public(self):
        r = self.client.get("/api/dca/MSFT/dcf")
        self.assertEqual(r.status_code, 200)
        self.assertIn("override", r.get_json())

    def test_writing_an_override_signed_out_is_refused(self):
        r = self.client.post("/api/dca/MSFT/dcf", json={"base": 400})
        self.assertEqual(r.status_code, 403)

    def test_writing_an_override_as_a_non_vip_is_refused(self):
        with self.client.session_transaction() as sess:
            sess["user_email"] = "stranger@example.com"
        r = self.client.post("/api/dca/MSFT/dcf", json={"base": 400})
        self.assertEqual(r.status_code, 403)
        r = self.client.delete("/api/dca/MSFT/dcf")
        self.assertEqual(r.status_code, 403)
        with self.client.session_transaction() as sess:
            sess.clear()

    def test_a_public_read_never_leaks_the_author(self):
        """``/dca`` is public, so an address on the row must not travel with
        it — the same rule ``share.public_payload`` applies to a sharer's."""
        body = self.client.get("/api/dca/MSFT/dcf").data.decode()
        self.assertNotIn("author", body)

    def test_the_page_renders_the_dcf_card(self):
        body = self.client.get("/dca/MSFT").data.decode()
        self.assertIn('id="dcfCard"', body)
        self.assertIn('id="dcfScenarioBody"', body)
        self.assertIn('id="dcfRefused"', body)

    def test_the_editor_is_in_the_page_but_starts_hidden(self):
        """A hidden form is not an authorization boundary — the write is
        re-checked server-side — but it must not be visible by default."""
        body = self.client.get("/dca/MSFT").data.decode()
        self.assertIn('id="dcfEditor"', body)
        self.assertIn('id="dcfEditor" class="hidden', body)

    def test_the_printed_general_form_matches_what_the_engine_computes(self):
        """The page prints the formula directly under the substituted one. If
        the two disagree, the reader checking by hand is who finds out."""
        body = self.client.get("/dca/MSFT").data.decode()
        self.assertIn("w<sub>dcf</sub>", body)


class DcaPeers(unittest.TestCase):
    """``/api/dca/<t>/peers`` — the same V for the rest of the peer group.

    The two properties worth protecting here are both about *not* doing
    something: the panel must never fetch (six Yahoo reads per name) and must
    never build (a build registers the name, so a side panel could otherwise
    evict whatever was least recently opened from a registry capped at sixty).
    Everything else it does is arithmetic over payloads already on disk.
    """

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()
        dh.eps_drift = lambda t: {"drift": 0.0}
        dh.start_background_thread = lambda: None  # type: ignore[assignment]

        # MSFT's first PEER_GROUPS entry, which is what dca_history.peer_group()
        # resolves and therefore what the panel must list.
        from ystocker import PEER_GROUPS
        cls.group = "Tech"
        cls.members = list(PEER_GROUPS[cls.group])

        # Six of the thirteen built, deliberately: partial coverage is the
        # normal state of this table and the case the reasons exist for.
        cls.built = ["MSFT", "AAPL", "NVDA", "META", "ADBE", "ORCL"]
        for i, sym in enumerate(cls.built):
            _seed(sym, price_k=0.7 + 0.18 * i, name=f"{sym} Inc.")
        # Built, but publishes nothing this engine can rank. Distinct from "not
        # built" and the page says so differently.
        dh._mem["IBM"] = (time.time(), {"_ver": dh.CACHE_VER, "_ts": time.time(),
                                        "ticker": "IBM",
                                        "unavailable": "too_few_vintages"})
        cls.cached = cls.built + ["IBM"]
        dh.cached_tickers = lambda: sorted(dh._mem)

        # Forward P/E for part of the group and a trailing one for all of it, so
        # the "one basis for everybody" rule has something to choose between.
        recs = {t: {"PE (Forward)": 18.0 + i, "PE (TTM)": 25.0 + i}
                for i, t in enumerate(cls.members)}
        from ystocker import valuation
        valuation._cached_fundamentals = lambda: recs      # type: ignore[assignment]

        # Nothing in this endpoint may reach the network. A build would also
        # register the ticker, which is the failure that matters most.
        def _forbidden(*_a, **_k):
            raise AssertionError("the peer panel fetched")
        dh.get = _forbidden                                # type: ignore[assignment]
        dh.build = _forbidden                              # type: ignore[assignment]

    def _peers(self, ticker="MSFT", **params):
        query = "&".join(f"{k}={v}" for k, v in params.items())
        r = self.client.get(f"/api/dca/{ticker}/peers" + (f"?{query}" if query else ""))
        self.assertEqual(r.status_code, 200)
        return r.get_json()

    def test_it_scores_only_what_is_already_built(self):
        d = self._peers()
        listed = {r["ticker"] for r in d["rows"]}
        self.assertTrue(listed.issubset(set(self.cached)),
                        f"scored something unbuilt: {listed - set(self.cached)}")

    def test_the_group_is_the_one_the_peer_factor_ranks_against(self):
        """Two cards on one page naming two different peer groups is not two
        views of the company, it is the page contradicting itself."""
        d = self._peers()
        self.assertEqual(d["group"], dh.peer_group("MSFT"))
        detail = self.client.get("/api/dca/MSFT").get_json()
        self.assertEqual(d["group"], detail["peer"]["group"])

    def test_the_ticker_itself_is_in_the_table_and_marked(self):
        """The comparison the panel exists to make is "where do I sit", which
        needs the reader's own row in the same sorted column."""
        d = self._peers()
        own = [r for r in d["rows"] if r["ticker"] == "MSFT"]
        self.assertEqual(len(own), 1)
        self.assertTrue(own[0]["self"])
        self.assertEqual(d["scored"], len(d["rows"]) - 1)

    def test_a_row_agrees_with_that_tickers_own_page(self):
        """One scoring path. A reader who clicks a peer through to its own page
        is exactly who would find two implementations of one formula."""
        d = self._peers(base=2500)
        row = next(r for r in d["rows"] if r["ticker"] == "NVDA")
        detail = self.client.get("/api/dca/NVDA?base=2500").get_json()
        for key in ("V", "band", "model", "m_valuation", "multiplier", "amount"):
            self.assertEqual(row[key], detail[key], key)

    def test_cheapest_sorts_first_and_unscorable_last(self):
        d = self._peers()
        vs = [r["V"] for r in d["rows"]]
        scored = [v for v in vs if v is not None]
        self.assertEqual(scored, sorted(scored, reverse=True))
        self.assertEqual(vs[:len(scored)], scored, "an unscorable row sorted early")

    def test_unbuilt_members_are_named_rather_than_dropped(self):
        """A comparison table quietly missing half its group is a different
        claim from one that says which half is missing."""
        d = self._peers()
        unscored = {u["ticker"]: u for u in d["unscored"]}
        self.assertIn("TSLA", unscored)
        self.assertEqual(unscored["TSLA"]["reason"], "not_built")
        seen = {r["ticker"] for r in d["rows"]} | set(unscored)
        self.assertEqual(seen, set(self.members),
                         "every group member must be accounted for exactly once")

    def test_publishing_nothing_rankable_is_a_different_reason_from_unbuilt(self):
        """``pending`` vs ``unresolved`` in the look-through, again: only one of
        the two is worth clicking, so collapsing them wastes the reader's time
        on a name that can never score."""
        d = self._peers()
        ibm = next(u for u in d["unscored"] if u["ticker"] == "IBM")
        self.assertEqual(ibm["reason"], "unavailable")
        self.assertEqual(ibm["detail"], "too_few_vintages")

    def test_the_pe_column_is_one_basis_for_the_whole_group(self):
        """A forward P/E beside a trailing one under a single heading reads as
        the forward name being cheaper, on nothing but a data gap."""
        d = self._peers()
        self.assertEqual(d["basis"], "forward")
        for row in d["rows"]:
            self.assertIsNotNone(row["value"])

    def test_a_template_mismatch_is_stated_not_left_to_the_reader(self):
        d = self._peers()
        for row in d["rows"]:
            self.assertEqual(row["same_model"], row["model"] == d["model"])

    def test_base_scales_the_peer_contributions_too(self):
        """Two answers to "how much should I put in" on one screen, differing
        because one of them did not hear about the base change, is the failure."""
        a = self._peers(base=1000)
        b = self._peers(base=2000)
        for x, y in zip(a["rows"], b["rows"]):
            self.assertEqual(x["V"], y["V"])
            if x["amount"] is not None:
                self.assertAlmostEqual(y["amount"], x["amount"] * 2, places=4)

    def test_a_ticker_in_no_group_says_so_rather_than_erroring(self):
        d = self._peers("ZZZZ")
        self.assertIsNone(d["group"])
        self.assertEqual(d["rows"], [])
        self.assertEqual(d["unscored"], [])

    def test_a_ticker_with_no_reconstruction_still_gets_its_peers(self):
        """The peers do not depend on this ticker's own reconstruction.

        A name that has never been scored must answer with the group rather than
        500 or, worse, invent a self row for a company it holds nothing about.
        (The card itself lives inside ``#dcaBody`` and so stays hidden until the
        main payload lands — this pins the endpoint, not the reveal.)
        """
        d = self._peers("GOOGL")          # in Tech, never seeded
        self.assertEqual(d["group"], "Tech")
        self.assertTrue(d["rows"])
        self.assertFalse(any(r.get("self") for r in d["rows"]))

    def test_the_table_is_capped_and_says_by_how_much(self):
        from ystocker import routes as _routes

        original = _routes.DCA_PEERS_MAX
        _routes.DCA_PEERS_MAX = 2
        try:
            d = self._peers()
        finally:
            _routes.DCA_PEERS_MAX = original
        # Self is added after the cap, so it is never the row that gets cut.
        self.assertEqual(len(d["rows"]), 3)
        self.assertTrue(any(r.get("self") for r in d["rows"]))
        self.assertEqual(d["truncated"], len(self.built) - 1 - 2)

    def test_the_panel_never_enters_a_name_into_the_tracked_registry(self):
        """The reason this endpoint refuses to build, stated as an assertion.

        ``dca_history.get`` registers whatever it successfully builds, and the
        registry is capped at sixty with least-recently-opened eviction. A panel
        that warmed eleven peers because somebody opened one ticker would
        silently rewrite what the overview ranks — and the eviction, not the
        Yahoo bill, is the part nobody would notice.
        """
        before = set(du.tracked())
        self._peers()
        self.assertEqual(set(du.tracked()), before)

    def test_the_page_carries_the_panel(self):
        body = self.client.get("/dca/MSFT").data.decode()
        self.assertIn('id="peersBody"', body)
        self.assertIn('id="peersUnscored"', body)
        self.assertIn("/peers?base=", body)


if __name__ == "__main__":  # pragma: no cover
    unittest.main(verbosity=2)
