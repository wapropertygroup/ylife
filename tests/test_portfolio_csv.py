"""
Unit tests for ystocker.portfolio_csv.

The fixtures below are modelled on the real exports, including the parts that
break naive parsers: Fidelity's footnote asterisks and trailing disclaimer
paragraphs, Schwab's quoted preamble and "Account Total" footer, Vanguard's
trailing empty column, IBKR's section-prefixed rows, Robinhood's "Instrument"
heading, and a GB18030-encoded Futu export.

No network, no app, no disk.
"""

from __future__ import annotations

import unittest

from ystocker.portfolio_csv import (
    CASH_SYMBOL,
    MAX_ROWS,
    TEMPLATE_CSV,
    merge_duplicates,
    normalise_symbol,
    parse,
    parse_number,
)

FIDELITY = """Account Number,Account Name,Symbol,Description,Quantity,Last Price,Last Price Change,Current Value,Today's Gain/Loss Dollar,Total Gain/Loss Dollar,Percent Of Account,Cost Basis Total,Average Cost Basis,Type
X12345678,ROTH IRA,VOO,VANGUARD 500 INDEX FUND ETF,25.000,$512.34,+$1.20,$12808.50,+$30.00,+$3308.50,45.12%,$9500.00,$380.00,Cash
X12345678,ROTH IRA,BRK.B,BERKSHIRE HATHAWAY INC CL B,12.000,$465.00,-$2.10,$5580.00,-$25.20,+$1080.00,19.65%,$4500.00,$375.00,Cash
X12345678,ROTH IRA,SPAXX**,FIDELITY GOVERNMENT MONEY MARKET,1500.000,$1.00,$0.00,$1500.00,$0.00,$0.00,5.28%,$1500.00,$1.00,Cash
,,,,,,,,,,,,,
"Brokerage services are provided by Fidelity Brokerage Services LLC, Member NYSE, SIPC.",,,,,,,,,,,,,
"Date downloaded 08/29/2026",,,,,,,,,,,,,
"""

SCHWAB = '''"Positions for account Individual ...123 as of 02:30 PM ET, 08/29/2026"
""
"Symbol","Description","Quantity","Price","Price Change %","Market Value","Day Change $","Cost Basis","Gain/Loss $","Security Type"
"AAPL","APPLE INC","40","$232.10","0.5%","$9,284.00","$45.00","$7,200.00","$2,084.00","Equity"
"QQQ","INVESCO QQQ TRUST","10","$488.20","-0.3%","$4,882.00","($15.00)","$4,200.00","$682.00","ETFs & Closed End Funds"
"Cash & Cash Investments","--","--","--","--","$2,500.00","--","--","--","Cash and Money Market"
"Account Total","","","","","$16,666.00","$30.00","$11,400.00","$2,766.00",""
'''

VANGUARD = """Account Number,Investment Name,Symbol,Shares,Share Price,Total Value,
12345678,Vanguard Total Stock Market Index Fund Admiral Shares,VTSAX,150.5,"$135.20","$20,347.60",
12345678,Vanguard Target Retirement 2060 Fund,VTTSX,300.0,"$48.15","$14,445.00",
"""

IBKR = """Positions,Header,DataDiscriminator,Asset Category,Currency,Symbol,Quantity,Mult,Cost Price,Cost Basis,Close Price,Value,Unrealized P/L
Positions,Data,Summary,Stocks,USD,NVDA,50,1,120.00,6000.00,178.50,8925.00,2925.00
Positions,Data,Summary,Stocks,USD,ASML.AS,10,1,650.00,6500.00,712.00,7120.00,620.00
Positions,Total,,Stocks,USD,,,,,12500.00,,16045.00,3545.00
"""

ROBINHOOD = """Instrument,Quantity,Average Cost,Total Return,Equity
TSLA,10,215.30,+450.00,2603.00
NVDA,25,95.00,+2087.50,4462.50
"""

FUTU = """代码,名称,持仓数量,成本价,现价,市值,盈亏比例
AAPL,苹果,40,180.00,232.10,9284.00,28.9%
NVDA,英伟达,25,95.00,178.50,4462.50,87.9%
"""

TEMPLATE = """symbol,quantity,cost_basis,account
VOO,25,9500,Taxable
QQQ,10,4200,Taxable
"""


def _by_symbol(result):
    return {r.symbol: r for r in result.rows}


class TestNumbers(unittest.TestCase):

    def test_currency_and_thousands(self):
        self.assertEqual(parse_number("$1,234.56"), 1234.56)
        self.assertEqual(parse_number("$12808.50"), 12808.50)
        self.assertEqual(parse_number("1 234.5"), 1234.5)

    def test_accounting_negative(self):
        self.assertEqual(parse_number("($15.00)"), -15.0)
        self.assertEqual(parse_number("-25.20"), -25.20)

    def test_leading_plus(self):
        self.assertEqual(parse_number("+$3308.50"), 3308.50)

    def test_blanks_are_none_not_zero(self):
        # Zero and "unknown" are different answers: a zero cost basis is a claim.
        for blank in ("", "--", "-", "n/a", "N/A", "None", "  ", "—"):
            self.assertIsNone(parse_number(blank), f"{blank!r} should be None")

    def test_percent_is_stripped(self):
        self.assertEqual(parse_number("28.9%"), 28.9)

    def test_junk_is_none(self):
        self.assertIsNone(parse_number("Cash"))
        self.assertIsNone(parse_number("ETFs & Closed End Funds"))

    def test_passthrough_numeric(self):
        self.assertEqual(parse_number(12.5), 12.5)
        self.assertIsNone(parse_number(None))


class TestSymbols(unittest.TestCase):

    def test_share_class_dot_becomes_hyphen(self):
        self.assertEqual(normalise_symbol("BRK.B"), "BRK-B")
        self.assertEqual(normalise_symbol("BF.B"), "BF-B")

    def test_exchange_suffix_survives(self):
        """The rewrite must not touch foreign listings look-through reaches."""
        for sym in ("ASML.AS", "0700.HK", "2330.TW", "ROP.SW", "HSBA.L", "005930.KQ"):
            self.assertEqual(normalise_symbol(sym), sym, f"{sym} was mangled")

    def test_single_letter_exchange_suffixes_are_not_share_classes(self):
        """The collision that "any single letter after a dot" gets wrong.

        ``.L``/``.T``/``.F``/``.V`` are London/Tokyo/Frankfurt/TSX-Venture, and
        ``7203.T`` is in this repo's own Nikkei peer group — rewriting it to
        ``7203-T`` makes a real holding unresolvable.
        """
        for sym in ("HSBA.L", "7203.T", "6758.T", "ADS.F", "ABC.V"):
            self.assertEqual(normalise_symbol(sym), sym, f"{sym} was mangled")

    def test_numeric_roots_are_never_rewritten(self):
        for sym in ("0700.A", "005930.B", "7203.C"):
            self.assertEqual(normalise_symbol(sym), sym, f"{sym} was mangled")

    def test_known_us_share_classes_are_rewritten(self):
        for raw, want in (("BRK.A", "BRK-A"), ("BRK.B", "BRK-B"),
                          ("BF.B", "BF-B"), ("LGF.A", "LGF-A"),
                          ("CWEN.A", "CWEN-A")):
            self.assertEqual(normalise_symbol(raw), want)

    def test_fidelity_footnote_markers_stripped(self):
        self.assertEqual(normalise_symbol("SPAXX**"), "SPAXX")
        self.assertEqual(normalise_symbol("FDRXX*"), "FDRXX")

    def test_case_and_whitespace(self):
        self.assertEqual(normalise_symbol("  voo  "), "VOO")
        self.assertEqual(normalise_symbol('"AAPL"'), "AAPL")

    def test_empty(self):
        self.assertEqual(normalise_symbol(""), "")
        self.assertEqual(normalise_symbol(None), "")


class TestFidelity(unittest.TestCase):

    def setUp(self):
        self.r = parse(FIDELITY)

    def test_parses(self):
        self.assertTrue(self.r.ok, self.r.error)
        self.assertEqual(self.r.broker, "Fidelity")

    def test_all_three_positions(self):
        rows = _by_symbol(self.r)
        self.assertEqual(set(rows), {"VOO", "BRK-B", "SPAXX"})

    def test_values_and_costs(self):
        rows = _by_symbol(self.r)
        self.assertEqual(rows["VOO"].quantity, 25.0)
        self.assertEqual(rows["VOO"].market_value, 12808.50)
        self.assertEqual(rows["VOO"].cost_basis, 9500.00)
        self.assertEqual(rows["VOO"].account, "X12345678")

    def test_disclaimer_paragraphs_are_skipped_not_imported(self):
        reasons = {s.reason for s in self.r.skipped}
        self.assertTrue(self.r.skipped)
        self.assertNotIn("Brokerage", " ".join(r.symbol for r in self.r.rows))
        self.assertTrue(any("symbol" in x for x in reasons), reasons)

    def test_mapping_is_reported_for_review(self):
        self.assertEqual(self.r.mapping["symbol"], "Symbol")
        self.assertEqual(self.r.mapping["quantity"], "Quantity")
        self.assertEqual(self.r.mapping["market_value"], "Current Value")
        self.assertEqual(self.r.mapping["cost_basis"], "Cost Basis Total")

    def test_plain_last_price_wins_over_last_price_change(self):
        """First-match-wins matters: Fidelity has both columns."""
        self.assertEqual(self.r.mapping["price"], "Last Price")


class TestSchwab(unittest.TestCase):

    def setUp(self):
        self.r = parse(SCHWAB)

    def test_quoted_preamble_is_skipped_and_header_found(self):
        self.assertTrue(self.r.ok, self.r.error)
        self.assertEqual(self.r.header_line, 3)

    def test_account_total_footer_is_not_a_position(self):
        symbols = {r.symbol for r in self.r.rows}
        self.assertEqual(symbols, {"AAPL", "QQQ", CASH_SYMBOL})
        self.assertTrue(any("total" in s.reason for s in self.r.skipped))

    def test_cash_row_is_kept_with_its_value(self):
        cash = _by_symbol(self.r)[CASH_SYMBOL]
        self.assertEqual(cash.market_value, 2500.00)

    def test_totals_would_have_doubled_the_portfolio(self):
        total = sum(r.market_value or 0 for r in self.r.rows)
        self.assertAlmostEqual(total, 9284.00 + 4882.00 + 2500.00)

    def test_comma_thousands_in_quoted_cells(self):
        self.assertEqual(_by_symbol(self.r)["AAPL"].market_value, 9284.00)
        self.assertEqual(_by_symbol(self.r)["AAPL"].cost_basis, 7200.00)

    def test_broker_detected(self):
        self.assertEqual(self.r.broker, "Charles Schwab")

    def test_broker_detected_without_a_day_change_column(self):
        """A plain Schwab export still names Schwab, not a generic 'CSV'."""
        lean = ('"Symbol","Description","Quantity","Price","Market Value",'
                '"Cost Basis","Security Type"\n'
                '"VTI","VANGUARD TOTAL STOCK MKT","30","$298.40","$8,952.00",'
                '"$7,100.00","ETFs & Closed End Funds"\n')
        self.assertEqual(parse(lean).broker, "Charles Schwab")

    def test_blank_marker_is_not_used_as_a_cash_row_name(self):
        """Schwab writes "--" in the cash line's description."""
        cash = _by_symbol(self.r)[CASH_SYMBOL]
        self.assertEqual(cash.name, "Cash")
        self.assertNotIn("--", cash.name)


class TestVanguard(unittest.TestCase):

    def setUp(self):
        self.r = parse(VANGUARD)

    def test_shares_and_share_price_aliases(self):
        self.assertTrue(self.r.ok, self.r.error)
        rows = _by_symbol(self.r)
        self.assertEqual(rows["VTSAX"].quantity, 150.5)
        self.assertEqual(rows["VTSAX"].market_value, 20347.60)

    def test_broker_detected(self):
        self.assertEqual(self.r.broker, "Vanguard")

    def test_investment_name_captured(self):
        self.assertIn("Total Stock Market", _by_symbol(self.r)["VTSAX"].name)


class TestIbkr(unittest.TestCase):

    def setUp(self):
        self.r = parse(IBKR)

    def test_section_prefixed_rows_parse(self):
        self.assertTrue(self.r.ok, self.r.error)
        self.assertEqual({r.symbol for r in self.r.rows}, {"NVDA", "ASML.AS"})

    def test_foreign_symbol_preserved(self):
        self.assertEqual(_by_symbol(self.r)["ASML.AS"].market_value, 7120.00)

    def test_total_row_skipped(self):
        self.assertTrue(any("total" in s.reason or "symbol" in s.reason
                            for s in self.r.skipped))

    def test_broker_detected(self):
        self.assertEqual(self.r.broker, "Interactive Brokers")


class TestRobinhood(unittest.TestCase):

    def setUp(self):
        self.r = parse(ROBINHOOD)

    def test_instrument_alias_and_equity_as_value(self):
        self.assertTrue(self.r.ok, self.r.error)
        rows = _by_symbol(self.r)
        self.assertEqual(rows["TSLA"].market_value, 2603.00)
        self.assertEqual(rows["TSLA"].quantity, 10.0)

    def test_cost_basis_derived_from_average_cost(self):
        # 25 shares x $95.00 average cost
        self.assertEqual(_by_symbol(self.r)["NVDA"].cost_basis, 2375.00)


class TestFutu(unittest.TestCase):

    def test_chinese_headers_utf8(self):
        r = parse(FUTU)
        self.assertTrue(r.ok, r.error)
        rows = _by_symbol(r)
        self.assertEqual(rows["AAPL"].quantity, 40.0)
        self.assertEqual(rows["AAPL"].market_value, 9284.00)
        self.assertEqual(rows["AAPL"].name, "苹果")

    def test_chinese_headers_gb18030(self):
        """A wrong codec makes headings mojibake, which reads as 'no header'."""
        r = parse(FUTU.encode("gb18030"))
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.encoding, "gb18030")
        self.assertEqual(_by_symbol(r)["NVDA"].name, "英伟达")

    def test_broker_detected(self):
        self.assertEqual(parse(FUTU).broker, "Futu / moomoo")


class TestTemplate(unittest.TestCase):

    def test_minimal_template_parses(self):
        r = parse(TEMPLATE)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(_by_symbol(r)["VOO"].cost_basis, 9500.0)
        self.assertEqual(_by_symbol(r)["VOO"].account, "Taxable")

    def test_shipped_template_is_importable(self):
        """The download offered to users must survive its own importer."""
        r = parse(TEMPLATE_CSV)
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len(r.rows), 5)
        self.assertEqual(r.skipped, [])

    def test_symbol_and_quantity_alone_is_enough(self):
        r = parse("symbol,quantity\nVOO,25\nAAPL,10\n")
        self.assertTrue(r.ok, r.error)
        self.assertEqual(len(r.rows), 2)
        self.assertIsNone(r.rows[0].market_value)


class TestEncodingAndFormat(unittest.TestCase):

    def test_bom_is_stripped_from_the_first_heading(self):
        r = parse(("symbol,quantity\nVOO,25\n").encode("utf-8-sig"))
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.mapping["symbol"], "symbol")
        self.assertEqual(r.rows[0].symbol, "VOO")

    def test_tab_separated(self):
        r = parse("symbol\tquantity\tcost_basis\nVOO\t25\t9500\n")
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.delimiter, "\t")
        self.assertEqual(r.rows[0].quantity, 25.0)

    def test_semicolon_separated(self):
        r = parse("symbol;quantity\nVOO;25\nAAPL;10\n")
        self.assertTrue(r.ok, r.error)
        self.assertEqual(r.delimiter, ";")
        self.assertEqual(len(r.rows), 2)

    def test_value_computed_when_only_quantity_and_price(self):
        r = parse("symbol,quantity,price\nVOO,10,500.00\n")
        self.assertEqual(r.rows[0].market_value, 5000.00)
        self.assertTrue(any("computed" in w for w in r.rows[0].warnings))


class TestFailureModes(unittest.TestCase):

    def test_empty_file(self):
        r = parse("")
        self.assertFalse(r.ok)
        self.assertIn("empty", r.error.lower())

    def test_no_recognisable_header(self):
        r = parse("alpha,beta,gamma\n1,2,3\n")
        self.assertFalse(r.ok)
        self.assertIn("header", r.error.lower())

    def test_header_but_no_positions_explains_itself(self):
        r = parse("symbol,quantity\nAccount Total,\n")
        self.assertFalse(r.ok)
        self.assertIn("no positions", r.error.lower())

    def test_oversize_is_refused_not_truncated(self):
        r = parse(b"x" * (3 * 1024 * 1024))
        self.assertFalse(r.ok)
        self.assertIn("limit", r.error.lower())

    def test_row_cap_is_reported_not_silent(self):
        body = "".join(f"SYM{i},1\n" for i in range(60))
        r = parse("symbol,quantity\n" + body, max_rows=10)
        self.assertEqual(len(r.rows), 10)
        self.assertTrue(any("Stopped at 10" in w for w in r.warnings), r.warnings)

    def test_negative_position_is_skipped_with_a_reason(self):
        r = parse("symbol,quantity,market_value\nVOO,25,12000\nSHORT,-5,-2000\n")
        self.assertEqual({x.symbol for x in r.rows}, {"VOO"})
        self.assertTrue(any("negative" in s.reason for s in r.skipped))

    def test_row_with_no_quantity_or_value_is_skipped(self):
        r = parse("symbol,quantity,market_value\nVOO,25,12000\nJUNK,,\n")
        self.assertEqual({x.symbol for x in r.rows}, {"VOO"})
        self.assertTrue(any("no quantity or value" in s.reason for s in r.skipped))

    def test_ragged_row_is_kept_but_flagged(self):
        r = parse("symbol,quantity,cost_basis\nVOO,25,9500\nAAPL,10\n")
        rows = _by_symbol(r)
        self.assertIn("AAPL", rows)
        self.assertTrue(any("columns" in w for w in rows["AAPL"].warnings))

    def test_prose_in_the_symbol_column_is_rejected(self):
        r = parse("symbol,quantity\n"
                  "\"Past performance is no guarantee of future results\",\n")
        self.assertFalse(r.ok)
        self.assertTrue(any("not a symbol" in s.reason or "no quantity" in s.reason
                            for s in r.skipped))


class TestDuplicatesAndMerge(unittest.TestCase):

    SPLIT = ("symbol,quantity,market_value,cost_basis,account\n"
             "VOO,25,12000,9500,Taxable\n"
             "VOO,10,4800,4000,IRA\n"
             "AAPL,40,9284,7200,Taxable\n")

    def test_duplicates_are_reported_not_silently_merged(self):
        r = parse(self.SPLIT)
        self.assertEqual(len(r.rows), 3)
        self.assertTrue(any("more than once" in w for w in r.warnings), r.warnings)

    def test_merge_sums_quantity_value_and_cost(self):
        merged = {x.symbol: x for x in merge_duplicates(parse(self.SPLIT).rows)}
        self.assertEqual(len(merged), 2)
        self.assertEqual(merged["VOO"].quantity, 35.0)
        self.assertEqual(merged["VOO"].market_value, 16800.0)
        self.assertEqual(merged["VOO"].cost_basis, 13500.0)

    def test_merge_keeps_both_account_names(self):
        merged = {x.symbol: x for x in merge_duplicates(parse(self.SPLIT).rows)}
        self.assertEqual(merged["VOO"].account, "Taxable + IRA")
        self.assertTrue(any("merged" in w for w in merged["VOO"].warnings))

    def test_merge_does_not_mutate_the_parsed_rows(self):
        parsed = parse(self.SPLIT)
        merge_duplicates(parsed.rows)
        self.assertEqual(parsed.rows[0].quantity, 25.0)

    def test_merge_handles_missing_values(self):
        r = parse("symbol,quantity\nVOO,25\nVOO,10\n")
        merged = merge_duplicates(r.rows)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].quantity, 35.0)
        self.assertIsNone(merged[0].market_value)


class TestAliasHygiene(unittest.TestCase):
    """Guards against an alias table that quietly stops covering the template."""

    def test_no_alias_is_claimed_by_two_fields(self):
        from ystocker.portfolio_csv import _ALIASES
        seen: dict[str, str] = {}
        for field_name, aliases in _ALIASES.items():
            for alias in aliases:
                self.assertNotIn(alias, seen,
                                 f"{alias!r} claimed by {seen.get(alias)} "
                                 f"and {field_name}")
                seen[alias] = field_name

    def test_template_headings_are_all_aliases(self):
        from ystocker.portfolio_csv import _ALIASES, _squash
        every = {a for aliases in _ALIASES.values() for a in aliases}
        for heading in TEMPLATE_CSV.splitlines()[0].split(","):
            self.assertIn(_squash(heading), every, f"{heading!r} is not an alias")

    def test_max_rows_default_is_sane(self):
        self.assertGreaterEqual(MAX_ROWS, 500)


if __name__ == "__main__":
    unittest.main()
