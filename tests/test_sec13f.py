"""Tests for ystocker/sec13f.py -- no network, no app, no AWS.

What they pin, and the measurements behind each (EDGAR, 2026-10-04):

* **Value units.** Filings made on or after 2023-01-03 report <value> in whole
  dollars, and this module read everything as thousands: Berkshire's
  2026-06-30 AAPL line (65,950,296,923 for 227,917,808 shares -- $289.36, that
  day's close) was cached as $65.95 trillion. But T. Rowe Price Associates,
  Baupost and Duquesne still filed thousands for 2026-06-30, so the unit is
  decided per filing. The rows below are copied from those filings.
* **Locating the infotable.** The fixtures in ``fixtures/sec13f/`` are real
  EDGAR pages. ``index_x01_*`` are 2019 and 2022 filings whose rendered views
  sit under ``xslForm13F_X01/``, which the old filter (``"xslForm13F_X02/"``)
  let through; ``index_x02_*`` is a 2023 one; ``index_txt_pabrai_2011q4.html``
  is a pre-2013 text filing with no XML at all.
  ``rendered_x01_barber_2022q3_head.html`` is the first 4 KB of what the old
  code handed the XML parser ("mismatched tag: line 33, column 2"), and the
  ``infotable_*.xml`` files are the raw tables it should have fetched. Barber
  Financial Group (CIK 1624865) and Marcato Capital (CIK 1541996) are the
  filers FUNDS used to list as "Jane Street" and "Altimeter".
* **Amendments.** ``cover_*.xml`` are real 13F-HR/A cover pages: Vanguard
  Capital Management's 2026-03-31 restatement and NEW HOLDINGS amendment
  (filed the same day), Berkshire's 2025-03-31 NEW HOLDINGS amendment (four
  rows, $1.1B -- what the old code showed as the whole quarter), and Jane
  Street Group's 2025-03-31 restatement.
* **Reported value.** Options rows are not holdings but are in the cover total:
  Jane Street Group's 2025-06-30 filing has $62.0B of positions and $443.7B of
  options against a $505.6B cover total, to the dollar.
* **Filers.** Predecessor routing (Pershing Square's cutover), the Vanguard
  co-filer sum, aliases, and refresh_cache keeping what finished.

XML parsing needs pyexpat, which this checkout's Homebrew Python cannot load
(built against a newer libexpat than macOS ships). Those few tests skip here
with that reason; run with ``DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib`` to
include them. The rest -- the co-filer merge, routing, refresh and the picker
included -- stub the parse and run anywhere.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest import mock

from ystocker import sec13f as s

FIXTURES = Path(__file__).parent / "fixtures" / "sec13f"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _expat_works() -> bool:
    import xml.etree.ElementTree as ET
    try:
        ET.fromstring("<a/>")
    except ImportError:
        return False
    return True


needs_expat = unittest.skipUnless(
    _expat_works(),
    "pyexpat cannot load in this interpreter; rerun with "
    "DYLD_LIBRARY_PATH=/opt/homebrew/opt/expat/lib to include XML parsing",
)


class _Resp:
    """The parts of a requests.Response the module reads."""

    def __init__(self, text: str, content_type: str = "text/xml") -> None:
        self.text = text
        self.headers = {"content-type": content_type}
        self.status_code = 200


def _row(value, shares, share_type="SH"):
    return {"value": value, "shares": shares, "share_type": share_type}


def _filing(cik, period, accession, form="13F-HR", filed="2026-08-14",
            primary_doc="xslForm13F_X02/primary_doc.xml"):
    return {"cik": cik, "form": form, "accession": accession, "filing_date": filed,
            "period": period, "primary_doc": primary_doc}


def _holding(cusip, value_thousands, shares):
    return {"cusip": cusip, "name": cusip, "ticker": cusip, "shares": shares,
            "value_thousands": value_thousands, "value_millions": round(value_thousands / 1000, 1)}


def _cover(total=None, kind=None):
    """A parsed cover page (_parse_cover): tableValueTotal as filed, and amendmentType."""
    return {"table_value_total": total, "entries": None, "amendment_type": kind,
            "is_amendment": kind is not None, "report_type": "13F HOLDINGS REPORT"}


_NS = "http://www.sec.gov/edgar/document/thirteenf/informationtable"


def _infotable(rows) -> str:
    """A minimal information table: rows of (cusip, issuer, value, shares)."""
    body = "".join(
        f"<infoTable><nameOfIssuer>{name}</nameOfIssuer><titleOfClass>COM</titleOfClass>"
        f"<cusip>{cusip}</cusip><value>{value}</value><shrsOrPrnAmt><sshPrnamt>{shares}"
        f"</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>"
        f"<investmentDiscretion>SOLE</investmentDiscretion></infoTable>"
        for cusip, name, value, shares in rows
    )
    return f'<?xml version="1.0" encoding="UTF-8"?><informationTable xmlns="{_NS}">{body}</informationTable>'


def _url_for(cik, accession, name="infotable.xml"):
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{name}"


# ---------------------------------------------------------------------------
# Value units
# ---------------------------------------------------------------------------

class ValueUnitTests(unittest.TestCase):
    def test_berkshire_2026_aapl_is_dollars(self):
        rows = [_row(65_950_296_923, 227_917_808)]
        self.assertEqual(s.decide_value_unit(rows, "2026-08-14"), "dollars")
        holding = s._normalise_values(
            [dict(rows[0], cusip="037833100", name="APPLE INC", ticker="AAPL")], "dollars")[0]
        self.assertAlmostEqual(holding["value_thousands"], 65_950_296.923)
        self.assertEqual(holding["value_millions"], 65_950.3)  # $65.95 billion, not trillion

    def test_berkshire_2022_aapl_is_thousands(self):
        rows = [_row(123_661_679, 894_802_319)]
        self.assertEqual(s.decide_value_unit(rows, "2022-11-14"), "thousands")
        # Read as thousands it is $138.20 a share: AAPL's close on 2022-09-30.
        self.assertAlmostEqual(123_661_679 * 1000 / 894_802_319, 138.20, places=2)

    def test_t_rowe_price_2026_is_thousands_despite_the_date(self):
        rows = [_row(66_872_105, 334_210_126),   # NVIDIA, 0.2001 a share
                _row(39_993_190, 138_212_570),   # Apple, 0.2894
                _row(33_105_855, 92_637_476)]    # Alphabet, 0.3574
        self.assertEqual(s._default_value_unit("2026-08-14"), "dollars")
        self.assertEqual(s.decide_value_unit(rows, "2026-08-14"), "thousands")

    def test_baupost_2026_is_thousands_despite_the_date(self):
        rows = [_row(892_310, 3_743_854), _row(493_140, 1_275_154), _row(489_612, 6_753_112)]
        self.assertEqual(s.decide_value_unit(rows, "2026-08-13"), "thousands")

    def test_duquesne_2026_is_thousands_despite_the_date(self):
        rows = [_row(864_923, 3_186_306), _row(281_613, 589_680), _row(232_375, 3_102_880)]
        self.assertEqual(s.decide_value_unit(rows, "2026-08-14"), "thousands")

    def test_a_small_dollar_filer_is_dollars(self):
        # Dalal Street (Pabrai), 2026-06-30: $81.16, $4.89 and $164.94 a share.
        rows = [_row(141_547_098, 1_744_050), _row(99_749_443, 20_398_659), _row(85_305_978, 517_194)]
        self.assertEqual(s.decide_value_unit(rows, "2026-08-13"), "dollars")

    def test_an_ambiguous_median_falls_back_to_the_filing_date(self):
        rows = [_row(20_000, 1_000)]   # $20 a share as dollars, $20,000 as thousands
        self.assertEqual(s.decide_value_unit(rows, "2026-08-14"), "dollars")
        self.assertEqual(s.decide_value_unit(rows, "2021-08-14"), "thousands")

    def test_rows_that_say_nothing_about_the_unit_leave_the_filing_date(self):
        rows = [_row(5_000_000, 5_000_000, "PRN"), _row(0, 100), _row(100, 0)]
        self.assertEqual(s.decide_value_unit(rows, "2026-08-14"), "dollars")
        self.assertEqual(s.decide_value_unit(rows, "2022-12-30"), "thousands")
        self.assertEqual(s.decide_value_unit([], s.DOLLAR_VALUES_FROM), "dollars")


_COVER = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler"
                 xmlns:com="http://www.sec.gov/edgar/common">
  <formData><summaryPage>
    <otherIncludedManagersCount>14</otherIncludedManagersCount>
    <tableEntryTotal>89</tableEntryTotal>
    <tableValueTotal>{total}</tableValueTotal>
  </summaryPage></formData>
</edgarSubmission>"""


class CoverAumTests(unittest.TestCase):
    def _aum(self, total, filing_date, unit=None):
        seen = []

        def fake_maybe(url, **kwargs):
            seen.append(url)
            return _Resp(_COVER.format(total=total))

        with mock.patch.object(s, "_get_maybe", fake_maybe):
            out = s._get_aum_from_cover("0001067983", "0001193125-26-352200", filing_date,
                                        unit=unit, primary_doc="xslForm13F_X02/primary_doc.xml")
        return out, seen

    def test_berkshire_2026_cover_total_is_dollars(self):
        out, seen = self._aum(299_253_556_246, "2026-08-14")
        self.assertEqual(out, 299_253.6)   # $299.3B, in millions
        # The raw cover in the accession folder, not the rendered view, and no
        # data.sec.gov -index.json first.
        self.assertEqual(seen, [_url_for("0001067983", "0001193125-26-352200", "primary_doc.xml")])

    def test_berkshire_2022_cover_total_is_thousands(self):
        out, _ = self._aum(296_096_640, "2022-11-14")
        self.assertEqual(out, 296_096.6)

    def test_the_filings_decided_unit_beats_the_date(self):
        # T. Rowe Price Associates' 2026-06-30 cover says 999,124,702 -- thousands.
        out, _ = self._aum(999_124_702, "2026-08-14", unit="thousands")
        self.assertEqual(out, 999_124.7)

    def test_a_text_filing_has_no_cover_and_costs_no_request(self):
        with mock.patch.object(s, "_get_maybe", side_effect=AssertionError("no request expected")):
            self.assertIsNone(s._get_aum_from_cover("0001173334", "0001193125-12-060916",
                                                    "2012-02-14", primary_doc="d300863d13fhr.txt"))

    def test_an_html_cover_is_not_parsed(self):
        with mock.patch.object(s, "_get_maybe", return_value=_Resp("<!DOCTYPE html><html/>", "text/html")):
            self.assertIsNone(s._get_aum_from_cover("0001067983", "0001193125-26-352200", "2026-08-14"))


# ---------------------------------------------------------------------------
# Locating the information table
# ---------------------------------------------------------------------------

class InfotablePickerTests(unittest.TestCase):
    def test_x01_index_gives_the_raw_file_not_the_rendered_view(self):
        self.assertEqual(s._pick_infotable_from_index(_fixture("index_x01_barber_2022q3.html")),
                         _url_for("1624865", "0001624865-22-000003", "Form13F.xml"))

    def test_x01_index_with_an_infotable_file_name(self):
        self.assertEqual(s._pick_infotable_from_index(_fixture("index_x01_marcato_2019q4.html")),
                         _url_for("1541996", "0001172661-20-000286", "infotable.xml"))

    def test_x02_index(self):
        self.assertEqual(s._pick_infotable_from_index(_fixture("index_x02_barber_2023q1.html")),
                         _url_for("1624865", "0001624865-23-000002", "Form13F.xml"))

    def test_a_text_filing_index_has_no_information_table(self):
        html = _fixture("index_txt_pabrai_2011q4.html")
        self.assertIsNone(s._pick_infotable_from_index(html))
        self.assertIsNone(s._fallback_infotable_link(html, "1173334", "000119312512060916"))

    def test_the_fallback_heuristic_no_longer_returns_the_x01_view(self):
        # The bug itself: with the literal "xslForm13F_X02/" filter these
        # indexes yielded xslForm13F_X01/Form13F.xml and .../infotable.xml.
        self.assertEqual(
            s._fallback_infotable_link(_fixture("index_x01_barber_2022q3.html"), "1624865", "000162486522000003"),
            _url_for("1624865", "0001624865-22-000003", "Form13F.xml"))
        self.assertEqual(
            s._fallback_infotable_link(_fixture("index_x01_marcato_2019q4.html"), "1541996", "000117266120000286"),
            _url_for("1541996", "0001172661-20-000286", "infotable.xml"))

    def test_any_view_folder_is_excluded_whatever_its_case_or_version(self):
        acc = "/Archives/edgar/data/1/000000000126000001"
        html = ('<table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th>'
                '<th>Type</th><th>Size</th></tr>'
                f'<tr><td>2</td><td></td><td><a href="{acc}/XSLFORM13F_X03/file.XML">file.html</a></td>'
                '<td>INFORMATION TABLE</td><td>&nbsp;</td></tr>'
                f'<tr><td>2</td><td></td><td><a href="{acc}/file.XML">file.XML</a></td>'
                '<td>INFORMATION TABLE</td><td>2298607</td></tr></table>')
        expected = f"https://www.sec.gov{acc}/file.XML"
        self.assertEqual(s._pick_infotable_from_index(html), expected)
        self.assertEqual(s._fallback_infotable_link(html, "1", "000000000126000001"), expected)


class FindInfotableUrlTests(unittest.TestCase):
    def _serving(self, fixture):
        calls = []

        def fake_maybe(url, **kwargs):
            calls.append(url)
            if fixture and url.startswith("https://www.sec.gov/") and url.endswith("-index.htm"):
                return _Resp(_fixture(fixture), "text/html")
            return None

        return calls, fake_maybe

    def test_a_text_filing_is_skipped_without_a_request(self):
        with mock.patch.object(s, "_get_maybe", side_effect=AssertionError("no request expected")):
            self.assertIsNone(s._find_infotable_url("0001173334", "0001193125-12-060916", "d300863d13fhr.txt"))

    def test_an_x01_filing_resolves_in_one_request(self):
        calls, fake = self._serving("index_x01_barber_2022q3.html")
        with mock.patch.object(s, "_get_maybe", fake):
            url = s._find_infotable_url("0001624865", "0001624865-22-000003", "xslForm13F_X01/primary_doc.xml")
        self.assertEqual(url, _url_for("1624865", "0001624865-22-000003", "Form13F.xml"))
        self.assertEqual(calls, ["https://www.sec.gov/Archives/edgar/data/1624865/000162486522000003/"
                                 "0001624865-22-000003-index.htm"])

    def test_the_index_is_read_from_the_accession_folder(self):
        # Since 2026-10-07 SEC answers the short ".../<cik>/<accession>-index.htm"
        # 403 and only the folder path 200. Served only at the folder path, the
        # lookup must still land in one request.
        calls = []

        def fake_maybe(url, **kwargs):
            calls.append(url)
            if url == ("https://www.sec.gov/Archives/edgar/data/1624865/000162486523000002/"
                       "0001624865-23-000002-index.htm"):
                return _Resp(_fixture("index_x02_barber_2023q1.html"), "text/html")
            return None   # what _get_maybe makes of a 403

        with mock.patch.object(s, "_get_maybe", fake_maybe):
            url = s._find_infotable_url("0001624865", "0001624865-23-000002", "primary_doc.xml")
        self.assertEqual(url, _url_for("1624865", "0001624865-23-000002", "Form13F.xml"))
        self.assertEqual(len(calls), 1)

    def test_the_information_table_row_beats_the_link_heuristic(self):
        # An unrelated XML listed before the table is the heuristic's "first raw
        # XML"; the Type column is what names the information table.
        acc = "/Archives/edgar/data/1/000000000126000001"
        html = ('<table class="tableFile"><tr><th>Seq</th><th>Description</th><th>Document</th>'
                '<th>Type</th><th>Size</th></tr>'
                f'<tr><td>1</td><td></td><td><a href="{acc}/primary_doc.xml">primary_doc.xml</a></td>'
                '<td>13F-HR</td><td>2398</td></tr>'
                f'<tr><td>2</td><td></td><td><a href="{acc}/exhibit.xml">exhibit.xml</a></td>'
                '<td>EX-99</td><td>900</td></tr>'
                f'<tr><td>3</td><td></td><td><a href="{acc}/holdings.xml">holdings.xml</a></td>'
                '<td>INFORMATION TABLE</td><td>855</td></tr></table>')
        self.assertEqual(s._fallback_infotable_link(html, "1", "000000000126000001"),
                         f"https://www.sec.gov{acc}/exhibit.xml")
        with mock.patch.object(s, "_get_maybe", return_value=_Resp(html, "text/html")):
            url = s._find_infotable_url("0000000001", "0000000001-26-000001", "")
        self.assertEqual(url, f"https://www.sec.gov{acc}/holdings.xml")

    def test_an_index_that_lists_no_table_ends_the_search(self):
        # The index lists every document, so guessing names it omits is futile.
        calls, fake = self._serving("index_txt_pabrai_2011q4.html")
        with mock.patch.object(s, "_get_maybe", fake):
            self.assertIsNone(s._find_infotable_url("0001173334", "0001193125-12-060916", ""))
        self.assertEqual(len(calls), 1)

    def test_filename_guesses_only_when_no_index_page_could_be_read(self):
        calls, fake = self._serving(None)
        with mock.patch.object(s, "_get_maybe", fake):
            self.assertIsNone(s._find_infotable_url("0001624865", "0001624865-22-000003",
                                                    "xslForm13F_X01/primary_doc.xml"))
        self.assertEqual(sum(c.endswith("-index.htm") for c in calls), 4)
        self.assertGreater(len(calls), 4)
        self.assertFalse(any(c.endswith("-index.json") for c in calls))
        self.assertFalse(any("/xslForm13F_X01/" in c for c in calls))


class HtmlGuardTests(unittest.TestCase):
    URL = "https://www.sec.gov/Archives/edgar/data/1624865/000162486522000003/xslForm13F_X01/Form13F.xml"

    def test_the_rendered_view_is_rejected_by_content_type(self):
        with self.assertRaises(s.NotAnInfoTable) as ctx:
            s._check_not_html(_fixture("rendered_x01_barber_2022q3_head.html"), "text/html", self.URL)
        self.assertIn(self.URL, str(ctx.exception))

    def test_html_is_rejected_by_its_body_alone(self):
        with self.assertRaises(s.NotAnInfoTable):
            s._check_not_html(_fixture("rendered_x01_barber_2022q3_head.html"), "text/xml", self.URL)
        with self.assertRaises(s.NotAnInfoTable):
            s._check_not_html("\ufeff\n  <HTML><body>Request Rate Threshold Exceeded</body></HTML>", "", self.URL)

    def test_a_raw_infotable_passes(self):
        s._check_not_html(_fixture("infotable_marcato_2019q4.xml"), "text/xml", self.URL)
        s._check_not_html(_fixture("infotable_barber_2022q3.xml"), "", self.URL)

    def test_a_fetch_reports_html_instead_of_a_parse_error(self):
        filing = _filing("0001624865", "2022-09-30", "0001624865-22-000003", filed="2022-10-25",
                         primary_doc="xslForm13F_X01/primary_doc.xml")
        page = _Resp(_fixture("rendered_x01_barber_2022q3_head.html"), "text/html")
        with mock.patch.object(s, "_find_infotable_url", return_value=self.URL), \
             mock.patch.object(s, "_get", return_value=page), \
             mock.patch.object(s, "_parse_infotable_with_unit", side_effect=AssertionError("must not parse")):
            with self.assertRaises(s.NotAnInfoTable):
                s._fetch_filing_holdings(filing)


@needs_expat
class InfotableParseTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(s, "_resolve_cusip_to_ticker", return_value=None)  # no OpenFIGI
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_raw_barber_2022q3_table_parses(self):
        holdings, unit, options = s._parse_infotable_with_unit(_fixture("infotable_barber_2022q3.xml"), "2022-10-25")
        self.assertEqual(unit, "thousands")
        self.assertEqual(options, 0)                        # an equity-only book
        self.assertEqual(len(holdings), 127)
        self.assertEqual(sum(h["value_thousands"] for h in holdings), 344_213)   # $344.2M

    def test_the_raw_marcato_2019q4_table_parses(self):
        holdings = s._parse_infotable(_fixture("infotable_marcato_2019q4.xml"), "2020-01-31")
        self.assertEqual(len(holdings), 1)
        self.assertEqual(holdings[0]["value_thousands"], 1_201)

    def test_a_dollar_filing_is_normalised_to_thousands(self):
        xml = _infotable([("037833100", "APPLE INC", 65_950_296_923, 227_917_808)])
        holdings, unit, _options = s._parse_infotable_with_unit(xml, "2026-08-14")
        self.assertEqual(unit, "dollars")
        self.assertEqual(holdings[0]["ticker"], "AAPL")
        self.assertEqual(holdings[0]["value_millions"], 65_950.3)


# ---------------------------------------------------------------------------
# Which filer stands for which quarter
# ---------------------------------------------------------------------------

def _route(name, period):
    current = (s.FUNDS[name],) + tuple(s.COFILERS.get(name, ()))
    return s._filers_for_period(period, current, s.PREDECESSORS.get(name, ()))


class PeriodRoutingTests(unittest.TestCase):
    def test_pershing_squares_cutover(self):
        self.assertEqual(s.FUNDS["Pershing Square"], "0002026053")
        self.assertEqual(_route("Pershing Square", "2025-06-30"), ("0001336528",))
        self.assertEqual(_route("Pershing Square", "2026-03-31"), ("0001336528",))
        self.assertEqual(_route("Pershing Square", "2026-06-30"), ("0002026053",))

    def test_greenlights_cutover(self):
        self.assertEqual(_route("Greenlight Capital (Einhorn)", "2023-12-31"), ("0001079114",))
        self.assertEqual(_route("Greenlight Capital (Einhorn)", "2024-03-31"), ("0001489933",))

    def test_vanguard_is_its_predecessor_then_its_cofilers(self):
        self.assertEqual(_route("Vanguard Group", "2025-12-31"), ("0000102909",))
        later = _route("Vanguard Group", "2026-06-30")
        # The two index advisers, then the seven managers Vanguard Group Inc's
        # last combination report included.
        self.assertEqual(later[:2], ("0002100119", "0002100121"))
        self.assertEqual(len(later), 9)
        self.assertIn("0000933478", later)   # Vanguard Fiduciary Trust Co

    def test_a_fund_without_predecessors_is_always_its_own_filer(self):
        self.assertEqual(_route("Jane Street", "2019-03-31"), ("0001595888",))

    def test_chained_predecessors_route_by_cutover_in_any_order(self):
        chain = (("B", "2023-12-31"), ("A", "2019-12-31"))
        self.assertEqual(s._filers_for_period("2019-12-31", ("C",), chain), ("A",))
        self.assertEqual(s._filers_for_period("2020-03-31", ("C",), chain), ("B",))
        self.assertEqual(s._filers_for_period("2024-03-31", ("C",), chain), ("C",))

    def test_the_predecessor_wins_its_periods_even_where_the_successor_filed(self):
        filings = [
            _filing("0002026053", "2026-06-30", "A-06"),
            _filing("0002026053", "2026-03-31", "A-03"),   # the one-position book
            _filing("0001336528", "2026-03-31", "P-03"),
            _filing("0001336528", "2026-06-30", "P-NT", form="13F-NT"),
            _filing("0001336528", "2025-12-31", "P-12"),
        ]
        plan = s._plan_quarters(filings, ("0002026053",), s.PREDECESSORS["Pershing Square"])
        self.assertEqual([(p, [g["original"]["accession"] for g in entry]) for p, entry in plan],
                         [("2026-06-30", ["A-06"]), ("2026-03-31", ["P-03"]), ("2025-12-31", ["P-12"])])


class _StubbedFetch:
    """fetch_fund_holdings with the network stubbed and the XML parse stubbed."""

    def _run(self, name, lists, books, covers=None, full=None, options=None, units=None):
        """Run fetch_fund_holdings(name) against canned filings.

        *books*: accession -> holdings (None: the filing has no infotable).
        *covers*: accession -> cover dict (see _cover). *options*: accession ->
        options value in thousands. *units*: accession -> unit filed, else the
        filing date's default.
        """
        infotable_calls, cover_calls = [], []

        def find(cik, accession, primary_doc=""):
            infotable_calls.append((cik, accession))
            if books.get(accession) is None:
                return None
            return _url_for(cik, accession)

        def parse(text, filing_date="", label=""):
            accession = next(a for a in books if f"/{a.replace('-', '')}/" in text)
            unit = (units or {}).get(accession, s._default_value_unit(filing_date))
            return s._ParsedTable([dict(h) for h in books[accession]], unit,
                                  (options or {}).get(accession, 0.0))

        def read_cover(cik, accession, primary_doc=""):
            cover_calls.append((cik, accession))
            return (covers or {}).get(accession)

        patches = [
            mock.patch.object(s, "_get_filings_list", side_effect=lambda c: [dict(f) for f in lists.get(c, [])]),
            mock.patch.object(s, "_find_infotable_url", side_effect=find),
            mock.patch.object(s, "_get", side_effect=lambda url, **kw: _Resp(url)),
            mock.patch.object(s, "_parse_infotable_with_unit", side_effect=parse),
            mock.patch.object(s, "_read_cover", side_effect=read_cover),
        ]
        if full is not None:
            patches.append(mock.patch.object(s, "FULL_HOLDINGS_QUARTERS", full))
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return s.fetch_fund_holdings(name), infotable_calls, cover_calls


class FundRoutingEndToEndTests(_StubbedFetch, unittest.TestCase):
    def test_pershing_quarters_come_from_their_owners_under_their_own_ciks(self):
        lists = {
            "0002026053": [_filing("0002026053", "2026-06-30", "0002026053-26-000003"),
                           _filing("0002026053", "2026-03-31", "0002026053-26-000002")],
            "0001336528": [_filing("0001336528", "2026-03-31", "0001336528-26-000002"),
                           _filing("0001336528", "2025-12-31", "0001336528-26-000001")],
        }
        books = {
            "0002026053-26-000003": [_holding("HHH", 5_000, 100), _holding("UBER", 3_000, 60)],
            "0002026053-26-000002": [_holding("ONE", 1, 1)],
            "0001336528-26-000002": [_holding("HHH", 4_000, 100), _holding("UBER", 2_000, 50)],
            "0001336528-26-000001": [_holding("HHH", 3_500, 100)],
        }
        out, calls, _ = self._run("Pershing Square", lists, books)
        self.assertIsNone(out["error"])
        self.assertEqual(out["ciks"], ["0002026053", "0001336528"])
        self.assertEqual([q["period"] for q in out["quarters"]], ["2026-06-30", "2026-03-31", "2025-12-31"])
        self.assertEqual([q["filers"] for q in out["quarters"]],
                         [["0002026053"], ["0001336528"], ["0001336528"]])
        # Each document fetched under the CIK that filed it; the successor's
        # one-position 2026-03-31 book never fetched at all.
        self.assertEqual(sorted(calls), [("0001336528", "0001336528-26-000001"),
                                         ("0001336528", "0001336528-26-000002"),
                                         ("0002026053", "0002026053-26-000003")])
        latest = {h["cusip"]: h for h in out["holdings"]}
        self.assertEqual(latest["HHH"]["change"], "unchanged")
        self.assertEqual(latest["UBER"]["change"], "increased")

    def test_cover_only_quarters_inherit_the_filers_parsed_unit(self):
        lists = {"0000080255": [
            _filing("0000080255", "2026-06-30", "TR-26Q2", filed="2026-08-14"),
            _filing("0000080255", "2024-06-30", "TR-24Q2", filed="2024-08-14"),
            _filing("0000080255", "2022-09-30", "TR-22Q3", filed="2022-11-14"),
        ]}
        books = {"TR-26Q2": [_holding("NVDA", 66_872_105, 334_210_126)]}
        # Cover totals as filed: thousands, like the 2026 infotable.
        covers = {"TR-24Q2": _cover(900_000_000), "TR-22Q3": _cover(700_000_000)}
        out, _, cover_calls = self._run("T. Rowe Price", lists, books, covers=covers, full=1,
                                        units={"TR-26Q2": "thousands"})
        self.assertIsNone(out["error"])
        # A thousands filer in 2026 filed thousands in 2024, though the date
        # says dollars; before 2023 the form itself said thousands.
        self.assertEqual([(q["period"], q["total_value_millions"]) for q in out["quarters"][1:]],
                         [("2024-06-30", 900_000.0), ("2022-09-30", 700_000.0)])
        self.assertEqual(cover_calls, [("0000080255", "TR-24Q2"), ("0000080255", "TR-22Q3")])


class CoFilerMergeTests(_StubbedFetch, unittest.TestCase):
    """Vanguard Group = Vanguard Capital Management + Vanguard Portfolio Management."""

    LISTS = {
        "0002100119": [_filing("0002100119", "2026-06-30", "VCM-06"), _filing("0002100119", "2026-03-31", "VCM-03")],
        "0002100121": [_filing("0002100121", "2026-06-30", "VPM-06"), _filing("0002100121", "2026-03-31", "VPM-03")],
        "0000102909": [_filing("0000102909", "2026-03-31", "VGI-NT", form="13F-NT"),
                       _filing("0000102909", "2025-12-31", "VGI-12", filed="2026-01-29")],
    }

    def test_two_cofilers_are_summed_by_cusip(self):
        books = {
            "VCM-06": [_holding("037833100", 28.936, 100), _holding("594918104", 5.0, 10)],
            "VPM-06": [_holding("037833100", 14.468, 50), _holding("67066G104", 3.0, 20)],
        }
        covers = {"VCM-03": _cover(4_000_000_000_000), "VPM-03": _cover(2_000_000_000_000),
                  "VGI-12": _cover(6_500_000_000_000)}   # dollars, as filed in 2026
        out, calls, cover_calls = self._run("Vanguard Group", self.LISTS, books, covers=covers, full=1)
        self.assertIsNone(out["error"])
        latest = out["quarters"][0]
        self.assertEqual(latest["filers"], ["0002100119", "0002100121"])
        by_cusip = {h["cusip"]: h for h in latest["holdings"]}
        self.assertEqual(by_cusip["037833100"]["shares"], 150)
        self.assertAlmostEqual(by_cusip["037833100"]["value_thousands"], 43.404)
        self.assertEqual(latest["total_holdings"], 3)
        self.assertEqual(sorted(calls), [("0002100119", "VCM-06"), ("0002100121", "VPM-06")])
        # Cover-only quarters sum the co-filers' totals; the predecessor's
        # quarter stands alone.
        self.assertEqual([(q["period"], q["total_value_millions"], q["filers"], q.get("aum_only"))
                          for q in out["quarters"][1:]],
                         [("2026-03-31", 6_000_000.0, ["0002100119", "0002100121"], True),
                          ("2025-12-31", 6_500_000.0, ["0000102909"], True)])
        self.assertEqual([c[:2] for c in cover_calls],
                         [("0002100119", "VCM-03"), ("0002100121", "VPM-03"), ("0000102909", "VGI-12")])
        self.assertEqual([q["reported_value_millions"] for q in out["quarters"][1:]],
                         [6_000_000.0, 6_500_000.0])

    def test_half_a_cofiled_quarter_is_dropped_not_shown_as_the_whole(self):
        books = {
            "VCM-06": [_holding("037833100", 28.936, 100)],
            "VPM-06": None,                                   # no infotable found
            "VCM-03": [_holding("037833100", 20.0, 90)],
            "VPM-03": [_holding("037833100", 10.0, 45)],
            "VGI-12": [_holding("037833100", 25.0, 120)],
        }
        with self.assertLogs("ystocker.sec13f", "WARNING") as logs:
            out, _, _ = self._run("Vanguard Group", self.LISTS, books)
        self.assertEqual(out["period_of_report"], "2026-03-31")
        self.assertEqual([q["period"] for q in out["quarters"]], ["2026-03-31", "2025-12-31"])
        self.assertIn("skipped", "\n".join(logs.output))

    def test_a_cofiler_that_did_not_file_leaves_the_other_alone_and_says_so(self):
        lists = {cik: [f for f in filings if f["accession"] != "VPM-03"]
                 for cik, filings in self.LISTS.items()}
        books = {
            "VCM-06": [_holding("037833100", 28.936, 100)],
            "VPM-06": [_holding("037833100", 14.468, 50)],
            "VCM-03": [_holding("037833100", 20.0, 90)],
            "VGI-12": [_holding("037833100", 25.0, 120)],
        }
        with self.assertLogs("ystocker.sec13f", "WARNING") as logs:
            out, _, _ = self._run("Vanguard Group", lists, books)
        filers = {q["period"]: q["filers"] for q in out["quarters"]}
        self.assertEqual(filers, {"2026-06-30": ["0002100119", "0002100121"],
                                  "2026-03-31": ["0002100119"],
                                  "2025-12-31": ["0000102909"]})
        self.assertIn("no 13F-HR from co-filer CIK 0002100121", "\n".join(logs.output))

    @needs_expat
    def test_two_real_infotables_are_summed_by_cusip(self):
        tables = {
            "VCM-06": _infotable([("037833100", "APPLE INC", 28_936, 100),
                                  ("594918104", "MICROSOFT CORP", 5_000, 10)]),
            "VPM-06": _infotable([("037833100", "APPLE INC", 14_468, 50),
                                  ("67066G104", "NVIDIA CORP", 3_000, 20)]),
        }

        def get(url, **kwargs):
            return _Resp(next(t for acc, t in tables.items() if f"/{acc.replace('-', '')}/" in url))

        lists = {k: [f for f in v if f["period"] == "2026-06-30"] for k, v in self.LISTS.items()}
        with mock.patch.object(s, "_get_filings_list", side_effect=lambda c: [dict(f) for f in lists.get(c, [])]), \
             mock.patch.object(s, "_find_infotable_url", side_effect=lambda c, a, p="": _url_for(c, a)), \
             mock.patch.object(s, "_get", side_effect=get), \
             mock.patch.object(s, "_resolve_cusip_to_ticker", return_value=None):
            out = s.fetch_fund_holdings("Vanguard Group")
        self.assertIsNone(out["error"])
        q = out["quarters"][0]
        self.assertEqual(q["value_units"], {"0002100119": "dollars", "0002100121": "dollars"})
        aapl = next(h for h in q["holdings"] if h["ticker"] == "AAPL")
        self.assertEqual(aapl["shares"], 150)
        self.assertAlmostEqual(aapl["value_thousands"], 43.404)   # $43,404 filed in dollars


# ---------------------------------------------------------------------------
# Amendments: a restatement replaces the report, NEW HOLDINGS add to it
# ---------------------------------------------------------------------------

_VCM_COVER = """<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/thirteenffiler" xmlns:com="http://www.sec.gov/edgar/common">
  <formData>
    <coverPage>
      <reportCalendarOrQuarter>03-31-2026</reportCalendarOrQuarter>
      <isAmendment>true</isAmendment>
      <amendmentNo>2</amendmentNo>
      <amendmentInfo><amendmentType>NEW HOLDINGS</amendmentType></amendmentInfo>
      <reportType>13F HOLDINGS REPORT</reportType>
    </coverPage>
    <summaryPage>
      <otherIncludedManagersCount>0</otherIncludedManagersCount>
      <tableEntryTotal>80</tableEntryTotal>
      <tableValueTotal>46987828353</tableValueTotal>
    </summaryPage>
  </formData>
</edgarSubmission>"""


class CoverParseTests(unittest.TestCase):
    def test_vanguard_capital_managements_new_holdings_cover(self):
        cover = s._parse_cover(_VCM_COVER)
        self.assertEqual(cover["amendment_type"], s.NEW_HOLDINGS)
        self.assertTrue(cover["is_amendment"])
        self.assertEqual(cover["entries"], 80)
        self.assertEqual(cover["table_value_total"], 46_987_828_353)
        self.assertEqual(cover["report_type"], "13F HOLDINGS REPORT")

    def test_prefixed_elements_and_loose_spelling(self):
        xml = ("<ns1:edgarSubmission xmlns:ns1='x'><ns1:isAmendment>TRUE</ns1:isAmendment>"
               "<ns1:amendmentType> Restatement </ns1:amendmentType>"
               "<ns1:tableValueTotal>3,995,910,438,125</ns1:tableValueTotal></ns1:edgarSubmission>")
        cover = s._parse_cover(xml)
        self.assertEqual(cover["amendment_type"], s.RESTATEMENT)
        self.assertTrue(cover["is_amendment"])
        self.assertEqual(cover["table_value_total"], 3_995_910_438_125)

    def test_an_original_has_no_amendment_type(self):
        cover = s._parse_cover(_COVER.format(total=258_701_144_516))
        self.assertIsNone(cover["amendment_type"])
        self.assertFalse(cover["is_amendment"])
        self.assertEqual(cover["table_value_total"], 258_701_144_516)


def _amended(cik, period, accession, filed, form="13F-HR"):
    return _filing(cik, period, accession, form=form, filed=filed)


class RealCoverTests(unittest.TestCase):
    """_parse_cover on amendment cover pages exactly as EDGAR serves them."""

    CASES = {   # file: (amendmentType, tableEntryTotal, tableValueTotal)
        "cover_vcm_2026q1_restatement.xml":        (s.RESTATEMENT, 3_982, 3_995_910_438_125),
        "cover_vcm_2026q1_new_holdings.xml":       (s.NEW_HOLDINGS, 80, 46_987_828_353),
        "cover_berkshire_2025q1_new_holdings.xml": (s.NEW_HOLDINGS, 4, 1_106_550_356),
        "cover_janestreet_2025q1_restatement.xml": (s.RESTATEMENT, 13_402, 396_972_524_215),
    }

    def test_real_amendment_covers(self):
        for name, (kind, entries, total) in self.CASES.items():
            with self.subTest(name):
                cover = s._parse_cover(_fixture(name))
                self.assertEqual(cover["amendment_type"], kind)
                self.assertEqual(cover["entries"], entries)
                self.assertEqual(cover["table_value_total"], total)
                self.assertTrue(cover["is_amendment"])
                self.assertEqual(cover["report_type"], "13F HOLDINGS REPORT")

    def test_read_cover_fetches_the_filers_raw_cover(self):
        seen = []

        def fake_maybe(url, **kwargs):
            seen.append(url)
            return _Resp(_fixture("cover_berkshire_2025q1_new_holdings.xml"))

        with mock.patch.object(s, "_get_maybe", fake_maybe):
            cover = s._read_cover("0001067983", "0000950123-25-008361", "xslForm13F_X02/primary_doc.xml")
        self.assertEqual(cover["amendment_type"], s.NEW_HOLDINGS)
        self.assertEqual(seen, [_url_for("0001067983", "0000950123-25-008361", "primary_doc.xml")])


class AmendmentResolutionTests(unittest.TestCase):
    """_resolve_amendments on the shapes EDGAR actually holds (filings checked 2026-10-04)."""

    def _resolve(self, filings, types):
        group = s._group_period_filings(filings[0]["cik"], filings)
        base, adds = s._resolve_amendments(group, types)
        return (base or {}).get("accession"), [a["accession"] for a in adds]

    def test_berkshire_2025q1_new_holdings_are_added_to_the_original(self):
        # Original: 110 rows, $258.7B. NEW HOLDINGS, three months later: 4 rows, $1.1B.
        filings = [_amended("0001067983", "2025-03-31", "0000950123-25-005701", "2025-05-15"),
                   _amended("0001067983", "2025-03-31", "0000950123-25-008361", "2025-08-14", "13F-HR/A")]
        self.assertEqual(self._resolve(filings, {"0000950123-25-008361": s.NEW_HOLDINGS}),
                         ("0000950123-25-005701", ["0000950123-25-008361"]))

    def test_berkshire_2023q3_restated_then_new_holdings(self):
        filings = [_amended("0001067983", "2023-09-30", "0000950123-23-010898", "2023-11-14"),
                   _amended("0001067983", "2023-09-30", "0000950123-23-011029", "2023-11-16", "13F-HR/A"),
                   _amended("0001067983", "2023-09-30", "0000950123-24-005653", "2024-05-15", "13F-HR/A")]
        types = {"0000950123-23-011029": s.RESTATEMENT, "0000950123-24-005653": s.NEW_HOLDINGS}
        self.assertEqual(self._resolve(filings, types),
                         ("0000950123-23-011029", ["0000950123-24-005653"]))

    def test_vanguard_capital_management_2026q1_same_day_restatement_and_new_holdings(self):
        filings = [_amended("0002100119", "2026-03-31", "0002100119-26-001306", "2026-05-08"),
                   _amended("0002100119", "2026-03-31", "0002100119-26-001313", "2026-05-15", "13F-HR/A"),
                   _amended("0002100119", "2026-03-31", "0002100119-26-001311", "2026-05-15", "13F-HR/A")]
        types = {"0002100119-26-001311": s.RESTATEMENT, "0002100119-26-001313": s.NEW_HOLDINGS}
        self.assertEqual(self._resolve(filings, types),
                         ("0002100119-26-001311", ["0002100119-26-001313"]))

    def test_jane_street_2025q1_restatement_replaces_the_original(self):
        filings = [_amended("0001595888", "2025-03-31", "0001595888-25-000099", "2025-05-14"),
                   _amended("0001595888", "2025-03-31", "0001595888-25-000100", "2025-05-19", "13F-HR/A")]
        self.assertEqual(self._resolve(filings, {"0001595888-25-000100": s.RESTATEMENT}),
                         ("0001595888-25-000100", []))

    def test_new_holdings_before_a_later_restatement_are_inside_it(self):
        filings = [_amended("1", "2024-03-31", "O", "2024-05-15"),
                   _amended("1", "2024-03-31", "N", "2024-08-14", "13F-HR/A"),
                   _amended("1", "2024-03-31", "R", "2024-11-14", "13F-HR/A")]
        self.assertEqual(self._resolve(filings, {"N": s.NEW_HOLDINGS, "R": s.RESTATEMENT}), ("R", []))

    def test_the_latest_of_several_restatements_wins(self):
        filings = [_amended("1", "2024-03-31", "O", "2024-05-15"),
                   _amended("1", "2024-03-31", "R1", "2024-05-20", "13F-HR/A"),
                   _amended("1", "2024-03-31", "R2", "2024-06-20", "13F-HR/A")]
        self.assertEqual(self._resolve(filings, {"R1": s.RESTATEMENT, "R2": s.RESTATEMENT}), ("R2", []))

    def test_an_amendment_of_unknown_type_is_ignored(self):
        filings = [_amended("1", "2024-03-31", "O", "2024-05-15"),
                   _amended("1", "2024-03-31", "A", "2024-06-20", "13F-HR/A")]
        self.assertEqual(self._resolve(filings, {"A": None}), ("O", []))

    def test_new_holdings_alone_are_not_a_portfolio(self):
        filings = [_amended("1", "2024-03-31", "N", "2024-08-14", "13F-HR/A")]
        self.assertEqual(self._resolve(filings, {"N": s.NEW_HOLDINGS}), (None, []))


class AmendmentFetchTests(_StubbedFetch, unittest.TestCase):
    BRK = "0001067983"
    LISTS = {BRK: [
        _amended(BRK, "2025-06-30", "BRK-Q2", "2025-08-14"),
        _amended(BRK, "2025-03-31", "BRK-Q1", "2025-05-15"),
        _amended(BRK, "2025-03-31", "BRK-Q1-NH", "2025-08-14", "13F-HR/A"),
    ]}
    COVERS = {
        "BRK-Q1":    _cover(258_701_144_516),
        "BRK-Q1-NH": _cover(1_106_550_356, s.NEW_HOLDINGS),
    }

    def test_berkshires_new_holdings_are_merged_into_the_quarter_not_shown_as_it(self):
        books = {
            "BRK-Q2":    [_holding("037833100", 60_000_000.0, 280_000_000)],
            "BRK-Q1":    [_holding("037833100", 55_000_000.0, 300_000_000),
                          _holding("H1467J104", 7_000_000.0, 27_000_000)],
            "BRK-Q1-NH": [_holding("H1467J104", 800_000.0, 3_000_000),
                          _holding("NEWCO0001", 306_550.4, 1_000_000)],
        }
        out, calls, cover_calls = self._run("Berkshire Hathaway", self.LISTS, books, covers=self.COVERS)
        self.assertIsNone(out["error"])
        q1 = out["quarters"][1]
        self.assertEqual(q1["period"], "2025-03-31")
        self.assertEqual(q1["accessions"], ["BRK-Q1", "BRK-Q1-NH"])
        by_cusip = {h["cusip"]: h for h in q1["holdings"]}
        self.assertEqual(by_cusip["H1467J104"]["shares"], 30_000_000)   # summed by CUSIP
        self.assertEqual(q1["total_holdings"], 3)
        self.assertAlmostEqual(q1["total_value_millions"], 63_106.6)
        # One cover read, for the amendment's type; the original needs none.
        self.assertEqual(cover_calls, [(self.BRK, "BRK-Q1-NH")])
        self.assertEqual(sorted(calls), [(self.BRK, "BRK-Q1"), (self.BRK, "BRK-Q1-NH"), (self.BRK, "BRK-Q2")])

    def test_berkshires_cover_only_quarter_follows_the_same_rule(self):
        books = {"BRK-Q2": [_holding("037833100", 60_000_000.0, 280_000_000)]}
        out, _, _ = self._run("Berkshire Hathaway", self.LISTS, books, covers=self.COVERS, full=1)
        q1 = out["quarters"][1]
        self.assertTrue(q1["aum_only"])
        # $258.70B original + $1.11B NEW HOLDINGS -- not the $1.1B the amendment alone said.
        self.assertEqual(q1["total_value_millions"], 259_807.7)
        self.assertEqual(q1["reported_value_millions"], 259_807.7)
        self.assertEqual(q1["accessions"], ["BRK-Q1", "BRK-Q1-NH"])

    VANGUARD_Q1 = {
        "0002100119": [_amended("0002100119", "2026-03-31", "0002100119-26-001306", "2026-05-08"),
                       _amended("0002100119", "2026-03-31", "0002100119-26-001313", "2026-05-15", "13F-HR/A"),
                       _amended("0002100119", "2026-03-31", "0002100119-26-001311", "2026-05-15", "13F-HR/A")],
        "0002100121": [_amended("0002100121", "2026-03-31", "0002100121-26-000861", "2026-05-08"),
                       _amended("0002100121", "2026-03-31", "0002100121-26-000865", "2026-05-15", "13F-HR/A")],
        "0000102909": [_amended("0000102909", "2026-03-31", "0000102909-26-002707", "2026-05-08", "13F-NT")],
    }
    #: The real cover totals, in dollars.
    VANGUARD_Q1_COVERS = {
        "0002100119-26-001306": _cover(3_995_749_438_125),
        "0002100119-26-001311": _cover(3_995_910_438_125, s.RESTATEMENT),
        "0002100119-26-001313": _cover(46_987_828_353, s.NEW_HOLDINGS),
        "0002100121-26-000861": _cover(1_886_798_423_444),
        "0002100121-26-000865": _cover(27_292_489_098, s.NEW_HOLDINGS),
    }

    def test_vanguards_2026q1_is_both_advisers_reports_not_their_new_holdings(self):
        # Cover-only, so the totals are the filings' own. The old reading,
        # each adviser's NEW HOLDINGS amendment alone, was $74.3B.
        lists = {cik: list(f) for cik, f in self.VANGUARD_Q1.items()}
        lists["0002100119"].insert(0, _amended("0002100119", "2026-06-30", "VCM-06", "2026-08-13"))
        books = {"VCM-06": [_holding("037833100", 1_000.0, 10)]}
        out, _, _ = self._run("Vanguard Group", lists, books, covers=self.VANGUARD_Q1_COVERS, full=1)
        q1 = next(q for q in out["quarters"] if q["period"] == "2026-03-31")
        self.assertEqual(q1["reported_value_millions"], 5_956_989.2)
        self.assertEqual(q1["filers"], ["0002100119", "0002100121"])
        self.assertEqual(q1["accessions"], ["0002100119-26-001311", "0002100119-26-001313",
                                            "0002100121-26-000861", "0002100121-26-000865"])

    def test_vanguards_2026q1_parsed_uses_the_restatement_not_the_original(self):
        books = {
            "0002100119-26-001306": [_holding("ORIGINAL1", 1.0, 1)],     # superseded; never read
            "0002100119-26-001311": [_holding("037833100", 900.0, 10)],
            "0002100119-26-001313": [_holding("NEWVCM001", 50.0, 1)],
            "0002100121-26-000861": [_holding("037833100", 400.0, 5)],
            "0002100121-26-000865": [_holding("NEWVPM001", 30.0, 1)],
        }
        out, calls, _ = self._run("Vanguard Group", self.VANGUARD_Q1, books, covers=self.VANGUARD_Q1_COVERS)
        q1 = out["quarters"][0]
        self.assertEqual(q1["period"], "2026-03-31")
        self.assertNotIn(("0002100119", "0002100119-26-001306"), calls)
        by_cusip = {h["cusip"]: h for h in q1["holdings"]}
        self.assertEqual(sorted(by_cusip), ["037833100", "NEWVCM001", "NEWVPM001"])
        self.assertEqual(by_cusip["037833100"]["shares"], 15)

    def test_a_period_with_only_new_holdings_is_left_out(self):
        lists = {self.BRK: [_amended(self.BRK, "2025-06-30", "BRK-Q2", "2025-08-14"),
                            _amended(self.BRK, "2025-03-31", "BRK-Q1-NH", "2025-08-14", "13F-HR/A")]}
        books = {"BRK-Q2": [_holding("037833100", 60_000_000.0, 280_000_000)],
                 "BRK-Q1-NH": [_holding("NEWCO0001", 306_550.4, 1_000_000)]}
        with self.assertLogs("ystocker.sec13f", "WARNING") as logs:
            out, _, _ = self._run("Berkshire Hathaway", lists, books, covers=self.COVERS)
        self.assertEqual([q["period"] for q in out["quarters"]], ["2025-06-30"])
        self.assertIn("NEW HOLDINGS amendments but no report", "\n".join(logs.output))

    def test_jane_streets_restated_quarter_read_from_its_real_cover(self):
        # Nothing above _get_maybe is stubbed on the cover path: the real
        # restatement cover decides the base, and supplies the total.
        js = "0001595888"
        lists = {js: [_amended(js, "2025-06-30", "0001595888-25-000117", "2025-08-14"),
                      _amended(js, "2025-03-31", "0001595888-25-000099", "2025-05-14"),
                      _amended(js, "2025-03-31", "0001595888-25-000100", "2025-05-19", "13F-HR/A")]}
        requested = []

        def fake_maybe(url, **kwargs):
            requested.append(url)
            if url.endswith("/000159588825000100/primary_doc.xml"):
                return _Resp(_fixture("cover_janestreet_2025q1_restatement.xml"))
            return None

        parsed = s._ParsedTable([_holding("037833100", 62_000_000.0, 1)], "dollars", 443_600_000.0)
        with mock.patch.object(s, "_get_filings_list", side_effect=lambda c: [dict(f) for f in lists.get(c, [])]), \
             mock.patch.object(s, "_fetch_filing_holdings", return_value=parsed), \
             mock.patch.object(s, "_get_maybe", side_effect=fake_maybe), \
             mock.patch.object(s, "FULL_HOLDINGS_QUARTERS", 1):
            out = s.fetch_fund_holdings("Jane Street")
        q1 = out["quarters"][1]
        self.assertEqual(q1["accessions"], ["0001595888-25-000100"])
        self.assertEqual(q1["reported_value_millions"], 396_972.5)   # the coordinator's 397.0
        # The superseded original's cover is never read.
        self.assertEqual(requested, [_url_for(js, "0001595888-25-000100", "primary_doc.xml")])


# ---------------------------------------------------------------------------
# Options: reported value is positions plus options, like the cover total
# ---------------------------------------------------------------------------

class ReportedValueTests(_StubbedFetch, unittest.TestCase):
    JS = "0001595888"

    def test_reported_value_includes_options_and_total_value_does_not(self):
        # Jane Street Group, 2025-06-30: $62.0B of positions, $443.7B of options,
        # $505.6B reported. The 2025-03-31 quarter is cover-only here.
        lists = {self.JS: [_amended(self.JS, "2025-06-30", "JS-Q2", "2025-08-14"),
                           _amended(self.JS, "2025-03-31", "JS-Q1", "2025-05-14")]}
        books = {"JS-Q2": [_holding("037833100", 40_000_000.0, 200_000),
                           _holding("67066G104", 22_000_000.0, 150_000)]}
        out, _, _ = self._run("Jane Street", lists, books, full=1,
                              options={"JS-Q2": 443_600_000.0},
                              covers={"JS-Q1": _cover(399_300_000_000)})
        q2, q1 = out["quarters"]
        self.assertEqual(q2["total_value_millions"], 62_000.0)
        self.assertEqual(q2["reported_value_millions"], 505_600.0)
        self.assertEqual(q2["holdings"][0]["pct_portfolio"], 64.52)    # of positions, not of options
        self.assertEqual((q1["total_value_millions"], q1["reported_value_millions"]), (399_300.0, 399_300.0))
        self.assertEqual(out["reported_value_millions"], 505_600.0)

    @needs_expat
    def test_the_parse_totals_options_without_making_them_holdings(self):
        put = ("<infoTable><nameOfIssuer>SOME ETF</nameOfIssuer><titleOfClass>PUT</titleOfClass>"
               "<cusip>99999X999</cusip><value>66000000</value><shrsOrPrnAmt><sshPrnamt>100000"
               "</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt><putCall>Put</putCall>"
               "<investmentDiscretion>SOLE</investmentDiscretion></infoTable>")
        xml = _infotable([("037833100", "APPLE INC", 28_936_000, 100_000)]).replace(
            "</informationTable>", put + "</informationTable>")
        # The put's CUSIP is in no map: resolving it would be an OpenFIGI call.
        with mock.patch.object(s, "_resolve_cusip_to_ticker", side_effect=AssertionError("no lookup")):
            table = s._parse_infotable_with_unit(xml, "2025-08-14")
        self.assertEqual(table.unit, "dollars")
        self.assertEqual([h["ticker"] for h in table.holdings], ["AAPL"])
        self.assertEqual(table.options_thousands, 66_000.0)
        self.assertEqual(table.reported_thousands, 28_936.0 + 66_000.0)


# ---------------------------------------------------------------------------
# Registry and aliases
# ---------------------------------------------------------------------------

class RegistryTests(unittest.TestCase):
    #: Checked against EDGAR's entity names and latest 13F-HR on 2026-10-04.
    CORRECTED = {
        "Vanguard Group":               "0002100119",  # Vanguard Capital Management LLC
        "T. Rowe Price":                "0000080255",  # PRICE T ROWE ASSOCIATES INC /MD/
        "Capital Group":                "0001422848",  # Capital Research Global Investors
        "Geode Capital":                "0001214717",  # GEODE CAPITAL MANAGEMENT, LLC
        "Balyasny Asset Mgmt":          "0001218710",  # BALYASNY ASSET MANAGEMENT L.P.
        "Brevan Howard":                "0001512857",  # Brevan Howard Capital Management LP
        "Marshall Wace":                "0001318757",  # MARSHALL WACE, LLP
        "Coatue Management":            "0001135730",  # COATUE MANAGEMENT LLC
        "D1 Capital":                   "0001747057",  # D1 Capital Partners L.P.
        "Light Street Capital":         "0001569049",  # LIGHT STREET CAPITAL MANAGEMENT, LLC
        "Tiger Cub Hill House":         "0001762304",  # HHLR ADVISORS, LTD.
        "Greenlight Capital (Einhorn)": "0001489933",  # DME Capital Management, LP
        "Pabrai Investment Funds":      "0001549575",  # Dalal Street, LLC
        "Altimeter Capital":            "0001541617",  # Altimeter Capital Management, LP
        "Tudor Investment":             "0000923093",  # TUDOR INVESTMENT CORP ET AL
        "Susquehanna":                  "0001446194",  # SUSQUEHANNA INTERNATIONAL GROUP, LLP
        "Jane Street":                  "0001595888",  # JANE STREET GROUP, LLC
        "Hudson Bay Capital":           "0001393825",  # Hudson Bay Capital Management LP
    }
    #: What those entries pointed at before -- other people's portfolios.
    WRONG = {"0001113169", "0001000275", "0001364742", "0001244466", "0001577528", "0001417718",
             "0001336528", "0001752538", "0001517413", "0001645010", "0001173334", "0001541996",
             "0000928063", "0001505123", "0001624865", "0001528885"}

    def test_the_corrected_ciks(self):
        for name, cik in self.CORRECTED.items():
            self.assertEqual(s.FUNDS[name], cik, name)

    def test_no_fund_points_at_a_cik_that_was_somebody_else(self):
        self.assertFalse(self.WRONG & set(s.FUNDS.values()))

    def test_no_filer_is_listed_twice(self):
        ciks = (list(s.FUNDS.values())
                + [c for extra in s.COFILERS.values() for c in extra]
                + [c for chain in s.PREDECESSORS.values() for c, _ in chain])
        self.assertEqual(len(ciks), len(set(ciks)))
        for cik in ciks:
            self.assertRegex(cik, r"^\d{10}$")

    def test_predecessors_and_cofilers_belong_to_funds(self):
        for name, chain in s.PREDECESSORS.items():
            self.assertIn(name, s.FUNDS)
            for _, last_period in chain:
                date.fromisoformat(last_period)
        for name in s.COFILERS:
            self.assertIn(name, s.FUNDS)


class AliasTests(unittest.TestCase):
    def test_alias_slugs_resolve_to_the_canonical_fund(self):
        self.assertEqual(s.resolve_fund_name("buffett-family-office"), "Berkshire Hathaway")
        self.assertEqual(s.resolve_fund_name("carl-icahn"), "Icahn Capital")
        self.assertEqual(s.resolve_fund_name("druckenmiller-family-office"), "Duquesne Family Office")
        self.assertEqual(s.resolve_fund_name("Carl Icahn"), "Icahn Capital")

    def test_fund_slugs_and_display_names_resolve(self):
        self.assertEqual(s.resolve_fund_name("berkshire-hathaway"), "Berkshire Hathaway")
        self.assertEqual(s.resolve_fund_name("greenlight-capital-(einhorn)"), "Greenlight Capital (Einhorn)")
        self.assertEqual(s.resolve_fund_name("Greenlight Capital (Einhorn)"), "Greenlight Capital (Einhorn)")
        self.assertEqual(s.resolve_fund_name("  JANE-STREET "), "Jane Street")
        for name in s.FUNDS:
            self.assertEqual(s.resolve_fund_name(s.fund_slug(name)), name)

    def test_unknown_slugs_resolve_to_none(self):
        for slug in ("", "   ", "nope", "berkshire", None):
            self.assertIsNone(s.resolve_fund_name(slug))

    def test_aliases_are_not_funds_and_name_funds(self):
        for alias, canonical in s.ALIASES.items():
            self.assertNotIn(alias, s.FUNDS)
            self.assertIn(canonical, s.FUNDS)


# ---------------------------------------------------------------------------
# refresh_cache and the cache file
# ---------------------------------------------------------------------------

class _StateMixin:
    def _keep_state(self):
        saved = (s._sec13f_data, s._sec13f_ts, s._sec13f_warming)

        def restore():
            s._sec13f_data, s._sec13f_ts, s._sec13f_warming = saved

        self.addCleanup(restore)


class RefreshCacheTests(_StateMixin, unittest.TestCase):
    def setUp(self):
        self._keep_state()
        self.saved = []
        patcher = mock.patch.object(s, "_save_cache", side_effect=lambda data, ts: self.saved.append(data))
        patcher.start()
        self.addCleanup(patcher.stop)
        # With nothing in memory a refresh reads the disk copy, so point that
        # somewhere empty rather than at this checkout's cache/.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(s, "_CACHE_FILE", Path(tmp.name) / "sec13f_cache.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _write_disk(self, data, version=None):
        s._CACHE_FILE.write_text(json.dumps({
            "version": s._CACHE_VER if version is None else version,
            "timestamp": time.time() - 3 * 86400,     # past the TTL: still worth carrying
            "data": data,
        }))

    def _refresh_with_one_slow_fund(self, previous):
        release = threading.Event()
        self.addCleanup(release.set)
        funds = {"Fast A": "0000000001", "Slow B": "0000000002", "Fast C": "0000000003"}

        def fetch(name, cik=None):
            if name == "Slow B":
                release.wait(10)
                return {"error": None, "holdings": ["too late"]}
            return {"error": None, "holdings": [name]}

        s._sec13f_data = previous
        with mock.patch.object(s, "FUNDS", funds), \
             mock.patch.object(s, "fetch_fund_holdings", side_effect=fetch), \
             mock.patch.object(s, "_REFRESH_TIMEOUT_SECONDS", 0.5), \
             self.assertLogs("ystocker.sec13f", "WARNING") as logs:
            started = time.monotonic()
            s.refresh_cache()
            elapsed = time.monotonic() - started
        return elapsed, "\n".join(logs.output)

    def test_finished_results_are_kept_and_the_slow_fund_carried_forward(self):
        elapsed, log_text = self._refresh_with_one_slow_fund(
            {"Slow B": {"error": None, "holdings": ["yesterday"]}})
        self.assertLess(elapsed, 5)   # did not wait for the straggler
        data = s._sec13f_data
        self.assertEqual(data["Fast A"]["holdings"], ["Fast A"])
        self.assertEqual(data["Fast C"]["holdings"], ["Fast C"])
        self.assertEqual(data["Slow B"]["holdings"], ["yesterday"])
        self.assertTrue(data["Slow B"]["carried_forward"])
        self.assertEqual(self.saved, [data])
        self.assertIn("carried forward: Slow B", log_text)
        self.assertFalse(s.is_warming())

    def test_a_slow_fund_with_no_previous_entry_says_it_timed_out(self):
        self._refresh_with_one_slow_fund(None)
        data = s._sec13f_data
        self.assertEqual(data["Fast A"]["holdings"], ["Fast A"])
        self.assertIn("Timed out", data["Slow B"]["error"])

    def test_a_fund_that_fails_to_refetch_keeps_its_previous_book(self):
        funds = {"Good A": "0000000001", "Broken B": "0000000002", "New C": "0000000003"}

        def fetch(name, cik=None):
            if name == "Good A":
                return {"error": None, "quarters": [{"period": "2026-06-30"}], "holdings": ["today"]}
            return {"error": "Could not fetch any holdings", "quarters": []}

        s._sec13f_data = {
            "Good A": {"error": None, "quarters": [{"period": "2026-03-31"}], "holdings": ["yesterday"]},
            "Broken B": {"error": None, "quarters": [{"period": "2026-06-30"}], "holdings": ["kept"]},
        }
        with mock.patch.object(s, "FUNDS", funds), \
             mock.patch.object(s, "fetch_fund_holdings", side_effect=fetch), \
             self.assertLogs("ystocker.sec13f", "WARNING") as logs:
            s.refresh_cache()
        data = s._sec13f_data
        self.assertEqual(data["Good A"]["holdings"], ["today"])
        self.assertNotIn("carried_forward", data["Good A"])
        self.assertEqual(data["Broken B"]["holdings"], ["kept"])
        self.assertTrue(data["Broken B"]["carried_forward"])
        self.assertEqual(data["Broken B"]["refresh_error"], "Could not fetch any holdings")
        self.assertIsNone(data["Broken B"]["error"])
        # Nothing to fall back on: the error stands.
        self.assertEqual(data["New C"]["error"], "Could not fetch any holdings")
        self.assertIn("kept their previous entry: Broken B", "\n".join(logs.output))

    def test_a_previous_error_is_not_carried_over_a_new_error(self):
        funds = {"Broken B": "0000000002"}
        s._sec13f_data = {"Broken B": {"error": "old failure", "quarters": []}}
        with mock.patch.object(s, "FUNDS", funds), \
             mock.patch.object(s, "fetch_fund_holdings",
                               return_value={"error": "new failure", "quarters": []}):
            s.refresh_cache()
        self.assertEqual(s._sec13f_data["Broken B"]["error"], "new failure")
        self.assertNotIn("carried_forward", s._sec13f_data["Broken B"])

    def test_with_nothing_in_memory_the_disk_copy_is_carried_forward(self):
        # 2026-10-08: a worker forked during the master's first fetch held no
        # copy, every fetch hung, and 48 time-outs were saved over the file.
        self._write_disk({"Slow B": {"error": None, "holdings": ["on disk"]}})
        self._refresh_with_one_slow_fund(None)
        data = s._sec13f_data
        self.assertEqual(data["Fast A"]["holdings"], ["Fast A"])
        self.assertEqual(data["Slow B"]["holdings"], ["on disk"])
        self.assertTrue(data["Slow B"]["carried_forward"])
        self.assertEqual(self.saved, [data])

    def test_with_nothing_in_memory_a_failed_refetch_keeps_the_disk_book(self):
        self._write_disk({"Broken B": {"error": None, "quarters": [{"period": "2026-06-30"}],
                                       "holdings": ["on disk"]}})
        s._sec13f_data = None
        with mock.patch.object(s, "FUNDS", {"Broken B": "0000000002"}), \
             mock.patch.object(s, "fetch_fund_holdings",
                               return_value={"error": "Could not fetch any holdings", "quarters": []}), \
             self.assertLogs("ystocker.sec13f", "WARNING"):
            s.refresh_cache()
        self.assertEqual(s._sec13f_data["Broken B"]["holdings"], ["on disk"])
        self.assertTrue(s._sec13f_data["Broken B"]["carried_forward"])

    def test_a_disk_copy_from_older_code_is_not_carried_forward(self):
        self._write_disk({"Slow B": {"error": None, "holdings": ["old shape"]}},
                         version=s._CACHE_VER - 1)
        self._refresh_with_one_slow_fund(None)
        self.assertIn("Timed out", s._sec13f_data["Slow B"]["error"])

    def test_memory_wins_over_the_disk_copy(self):
        self._write_disk({"Slow B": {"error": None, "holdings": ["on disk"]}})
        self._refresh_with_one_slow_fund({"Slow B": {"error": None, "holdings": ["in memory"]}})
        self.assertEqual(s._sec13f_data["Slow B"]["holdings"], ["in memory"])

    def test_each_fund_is_fetched_once_and_no_alias_is_fetched(self):
        fetched = []
        lock = threading.Lock()

        def fetch(name, cik=None):
            with lock:
                fetched.append((name, cik))
            return {"error": None, "holdings": []}

        with mock.patch.object(s, "fetch_fund_holdings", side_effect=fetch):
            s.refresh_cache()
        self.assertEqual(sorted(fetched), sorted(s.FUNDS.items()))
        self.assertFalse(set(s.ALIASES) & set(s._sec13f_data))


class CacheVersionTests(_StateMixin, unittest.TestCase):
    def setUp(self):
        self._keep_state()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        patcher = mock.patch.object(s, "_CACHE_FILE", Path(tmp.name) / "sec13f_cache.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_saved_payload_carries_the_version_and_loads(self):
        s._save_cache({"Jane Street": {"error": None}}, time.time())
        self.assertEqual(json.loads(s._CACHE_FILE.read_text())["version"], s._CACHE_VER)
        s._sec13f_data = None
        self.assertTrue(s._load_cache())
        self.assertEqual(s._sec13f_data, {"Jane Street": {"error": None}})

    def test_a_cache_written_by_older_code_is_refused(self):
        for payload in ({"timestamp": time.time(), "data": {"Jane Street": {}}},
                        {"version": s._CACHE_VER - 1, "timestamp": time.time(), "data": {"Jane Street": {}}}):
            s._CACHE_FILE.write_text(json.dumps(payload))
            s._sec13f_data = None
            self.assertFalse(s._load_cache())
            self.assertIsNone(s._sec13f_data)


class ManualRefreshTests(_StateMixin, unittest.TestCase):
    """/13f/refresh is an anonymous GET; the browser's cooldown binds no bot."""

    def setUp(self):
        self._keep_state()
        s._sec13f_warming = False

    def test_not_while_one_runs_in_this_process(self):
        s._sec13f_ts, s._sec13f_warming = None, True
        self.assertFalse(s.manual_refresh_allowed())

    def test_not_within_ten_minutes_of_the_last(self):
        now = 1_000_000.0
        s._sec13f_ts = now - 60
        self.assertFalse(s.manual_refresh_allowed(now))
        s._sec13f_ts = now - s.MANUAL_REFRESH_MIN_AGE_SECONDS
        self.assertTrue(s.manual_refresh_allowed(now))

    def test_a_process_with_no_copy_may_refresh(self):
        s._sec13f_ts = None
        self.assertTrue(s.manual_refresh_allowed())


if __name__ == "__main__":
    unittest.main()
