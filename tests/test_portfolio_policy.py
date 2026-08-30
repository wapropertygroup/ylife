"""
Unit tests for the position-limit policy — ``portfolio.normalise_policy`` and the
``exposure`` bridge.

No app, no network, no DynamoDB: only the pure normalisation and conversion are
exercised here. The store's read/write paths are covered end-to-end by
``tests/check_assets_endpoints.py``, which has a Flask app.

The tests that matter most are the ones asserting an **unset** limit stays unset.
An absent limit is load-bearing: ``exposure.assess`` emits no check for ``None``,
so a normaliser that helpfully defaulted it to some house number would silently
start reporting breaches against a limit the user never agreed to — or worse, pass
a portfolio against one.
"""

from __future__ import annotations

import unittest

from ystocker import portfolio
from ystocker.exposure import PortfolioPolicy, policy_from_stored


class TestNormalisePolicy(unittest.TestCase):

    def test_empty_input_is_all_unset(self):
        p = portfolio.normalise_policy({})
        self.assertIsNone(p["max_single_name_pct"])
        self.assertIsNone(p["max_issuer_pct"])
        self.assertIsNone(p["cash"])
        self.assertEqual(p["holding_types"], {})

    def test_non_dict_input_degrades_to_defaults(self):
        for bad in (None, [], "8", 8.0):
            with self.subTest(repr(bad)):
                self.assertEqual(portfolio.normalise_policy(bad),
                                 portfolio.DEFAULT_POLICY)

    def test_valid_limits_survive(self):
        p = portfolio.normalise_policy({"max_single_name_pct": 8,
                                        "max_issuer_pct": 12.5,
                                        "cash": 1234.567})
        self.assertEqual(p["max_single_name_pct"], 8.0)
        self.assertEqual(p["max_issuer_pct"], 12.5)
        self.assertEqual(p["cash"], 1234.57)

    def test_zero_limit_is_rejected_not_stored(self):
        # A 0% limit marks every position a breach and is never what somebody
        # typing into a form meant. Left unset, so no check is emitted.
        p = portfolio.normalise_policy({"max_single_name_pct": 0})
        self.assertIsNone(p["max_single_name_pct"])

    def test_out_of_range_limit_is_dropped(self):
        # A limit nobody can satisfy is worse than no limit: the page would be
        # permanently red with no way to clear it.
        for bad in (-5, 100.01, 1000, float("nan"), float("inf")):
            with self.subTest(repr(bad)):
                p = portfolio.normalise_policy({"max_single_name_pct": bad})
                self.assertIsNone(p["max_single_name_pct"])

    def test_hundred_percent_is_allowed(self):
        # The boundary is inclusive: "no more than all of it" is a legal, if inert,
        # statement and rejecting it would be surprising.
        p = portfolio.normalise_policy({"max_single_name_pct": 100})
        self.assertEqual(p["max_single_name_pct"], 100.0)

    def test_booleans_are_not_numbers(self):
        # float(True) == 1.0 would silently install a 1% limit.
        p = portfolio.normalise_policy({"max_single_name_pct": True, "cash": True})
        self.assertIsNone(p["max_single_name_pct"])
        self.assertIsNone(p["cash"])

    def test_negative_cash_is_unset_not_zero(self):
        # Unusable input means the balance was not stated. Zero would be a claim
        # that there is no cash, which fails every proposed buy.
        self.assertIsNone(portfolio.normalise_policy({"cash": -100})["cash"])

    def test_zero_cash_is_a_stated_value(self):
        # Distinct from silence: "I have no cash" is checkable, "I did not say" is
        # not, and only the first may fail a buy.
        self.assertEqual(portfolio.normalise_policy({"cash": 0})["cash"], 0.0)

    def test_unknown_keys_are_dropped(self):
        p = portfolio.normalise_policy({"max_single_name_pct": 8,
                                        "max_sector_pct": 35,
                                        "risk_budget": 1000,
                                        "__proto__": "x"})
        self.assertEqual(set(p), set(portfolio.DEFAULT_POLICY))

    def test_holding_types_are_allowlisted(self):
        p = portfolio.normalise_policy({"holding_types": {
            "aapl": "core", "soxq": "TACTICAL", "nvda": "speculative",
            "msft": "", "": "core"}})
        self.assertEqual(p["holding_types"], {"AAPL": "core", "SOXQ": "tactical"})

    def test_holding_type_symbols_are_normalised(self):
        # Same rewrite as a position, or a tag would never match its holding.
        p = portfolio.normalise_policy({"holding_types": {"brk.b": "core"}})
        self.assertEqual(p["holding_types"], {"BRK-B": "core"})

    def test_holding_types_are_capped(self):
        many = {f"SYM{i}": "core" for i in range(portfolio.MAX_HOLDING_TYPES + 50)}
        p = portfolio.normalise_policy({"holding_types": many})
        self.assertLessEqual(len(p["holding_types"]), portfolio.MAX_HOLDING_TYPES)

    def test_normalisation_is_idempotent(self):
        # It runs on read as well as write, so a second pass must not drift.
        once = portfolio.normalise_policy({"max_single_name_pct": 8.12345,
                                           "cash": 10.005,
                                           "holding_types": {"aapl": "core"}})
        self.assertEqual(portfolio.normalise_policy(once), once)

    def test_default_policy_is_not_shared_state(self):
        # A caller mutating what load_policy handed back must not change the
        # default for every later request in this worker. `dict(DEFAULT_POLICY)`
        # is a *shallow* copy and shared the nested holding_types dict, so one
        # user tagging a symbol polluted the default until the worker recycled.
        a = portfolio.normalise_policy({})
        a["holding_types"]["AAPL"] = "core"
        a["cash"] = 999.0
        self.assertEqual(portfolio.DEFAULT_POLICY["holding_types"], {})
        self.assertIsNone(portfolio.DEFAULT_POLICY["cash"])

    def test_two_fresh_policies_share_no_nested_objects(self):
        a = portfolio.normalise_policy({})
        b = portfolio.normalise_policy({})
        self.assertIsNot(a["holding_types"], b["holding_types"])
        self.assertIsNot(a["holding_types"], portfolio.DEFAULT_POLICY["holding_types"])


class TestPolicyKeys(unittest.TestCase):
    """The policy lives on its own row, and that is load-bearing."""

    def test_policy_key_differs_from_the_positions_key(self):
        # save() is a whole-item put_item, so sharing a row would let the next CSV
        # import erase the limits -- successfully, and without a word.
        self.assertNotEqual(portfolio._key("a@b.com"),
                            portfolio._policy_key("a@b.com"))

    def test_keys_are_case_insensitive_and_trimmed(self):
        self.assertEqual(portfolio._policy_key("  A@B.COM "),
                         portfolio._policy_key("a@b.com"))


class TestPolicyBridge(unittest.TestCase):
    """``exposure.policy_from_stored`` must not invent a limit."""

    def test_unset_limits_stay_none(self):
        p = policy_from_stored(portfolio.DEFAULT_POLICY)
        self.assertIsNone(p.max_single_name_pct)
        self.assertIsNone(p.max_issuer_pct)

    def test_round_trip_preserves_every_field(self):
        stored = portfolio.normalise_policy({
            "max_single_name_pct": 8, "max_issuer_pct": 12,
            "cash": 5000, "holding_types": {"aapl": "core"}})
        p = policy_from_stored(stored)
        self.assertIsInstance(p, PortfolioPolicy)
        self.assertEqual(p.max_single_name_pct, 8.0)
        self.assertEqual(p.max_issuer_pct, 12.0)
        self.assertEqual(p.cash, 5000.0)
        self.assertEqual(p.type_of("AAPL"), "core")

    def test_missing_keys_do_not_raise(self):
        p = policy_from_stored({})
        self.assertIsNone(p.max_single_name_pct)
        self.assertIsNone(p.cash)

    def test_holding_types_are_copied_not_aliased(self):
        stored = portfolio.normalise_policy({"holding_types": {"aapl": "core"}})
        p = policy_from_stored(stored)
        stored["holding_types"]["MSFT"] = "tactical"
        self.assertEqual(p.type_of("MSFT"), "")

    def test_stated_tags_match_the_store_allowlist(self):
        # exposure names the two tags for consumers to compare against; a drift
        # between the two lists would let the UI offer a tag the store discards.
        from ystocker import exposure

        self.assertEqual({exposure.HOLDING_CORE, exposure.HOLDING_TACTICAL},
                         set(portfolio.HOLDING_TYPES))


class TestConstraintsBlock(unittest.TestCase):
    """``assets._constraints`` must stay silent when nothing was asked."""

    def _result(self):
        from tests.test_exposure import resolver
        from ystocker.lookthrough import Position, analyse

        return analyse([Position("VOO", 10_000.0)], resolver)

    def test_no_policy_yields_none(self):
        from ystocker import assets

        self.assertIsNone(assets._constraints(self._result(), None, []))

    def test_policy_with_no_limits_yields_none(self):
        # Not an empty passing structure: a limit nobody set has not been
        # satisfied, and "0 breaches" would say the opposite.
        from ystocker import assets

        self.assertIsNone(
            assets._constraints(self._result(), portfolio.DEFAULT_POLICY, []))

    def test_policy_with_a_limit_yields_verdicts(self):
        from ystocker import assets

        stored = portfolio.normalise_policy({"max_single_name_pct": 8})
        blob = assets._constraints(self._result(), stored, [])
        self.assertIsNotNone(blob)
        self.assertIn("verdict", blob)
        self.assertGreater(blob["check_count"], 0)

    def test_cash_alone_does_not_trigger_a_check(self):
        # Cash bounds a proposed buy, and there is no trade on this path.
        from ystocker import assets

        stored = portfolio.normalise_policy({"cash": 10_000})
        self.assertIsNone(assets._constraints(self._result(), stored, []))


if __name__ == "__main__":
    unittest.main()


class TestUnstatedCash(unittest.TestCase):
    """An unstated balance must not read as no cash.

    The regression this pins: ``cash`` defaulted to 0.0, so a user who never
    opened the limits form had every proposed buy come back a liquidity BREACH —
    and because ``Assessment.verdict`` folds liquidity in, the whole assessment
    read BREACH for a $100 trade with no concentration issue at all.
    """

    def _assess(self, policy):
        from tests.test_exposure import resolver
        from ystocker.exposure import Trade, assess
        from ystocker.lookthrough import Position

        return assess([Position("AAPL", 1_000.0)], policy, resolver,
                      trade=Trade("MSFT", 100.0))

    def test_unstated_cash_is_not_checked(self):
        from ystocker.exposure import PortfolioPolicy

        a = self._assess(PortfolioPolicy())
        self.assertIsNone(a.liquidity)

    def test_stated_zero_cash_does_fail_a_buy(self):
        from ystocker.exposure import VERDICT_BREACH, PortfolioPolicy

        a = self._assess(PortfolioPolicy(cash=0.0))
        self.assertEqual(a.liquidity.verdict, VERDICT_BREACH)

    def test_sufficient_stated_cash_passes(self):
        from ystocker.exposure import VERDICT_PASS, PortfolioPolicy

        a = self._assess(PortfolioPolicy(cash=500.0))
        self.assertEqual(a.liquidity.verdict, VERDICT_PASS)

    def test_unstated_cash_does_not_poison_the_overall_verdict(self):
        from ystocker.exposure import VERDICT_PASS, PortfolioPolicy

        a = self._assess(PortfolioPolicy(max_single_name_pct=95.0))
        self.assertEqual(a.verdict, VERDICT_PASS)
