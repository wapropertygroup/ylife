"""ystocker/directory.py -- SEC's list of every listed company, for /companies.

The rows below are cut from the real ``company_tickers_exchange.json`` as the
box fetched it on 2026-10-03 (10,434 tickers, 8,008 filers). What they pin:

* one card per company, not per ticker: GOOGL (row 3) is Alphabet's face and
  GOOG (row 7,471) rides along as ``also``, where search still finds it;
* SEC's order is kept, since it is the only "largest first" there is for the
  8,000 companies with no quote here;
* an exchange SEC does not name (170 rows have none) is shown as none, never
  guessed into NYSE or Nasdaq;
* a short or broken answer from SEC never replaces the last good list -- an
  empty directory would read as "no companies", which is the one wrong answer.

Pure apart from the cache tests, which use a temporary file and a stubbed
``sec13f.edgar_get``: no network, no app.
"""
from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from ystocker import directory

FIELDS = ["cik", "name", "ticker", "exchange"]
HEAD = [
    [1045810, "NVIDIA CORP", "NVDA", "Nasdaq"],
    [320193, "Apple Inc.", "AAPL", "Nasdaq"],
    [1652044, "Alphabet Inc.", "GOOGL", "Nasdaq"],
    [789019, "MICROSOFT CORP", "MSFT", "Nasdaq"],
    [1730168, "Broadcom Inc.", "AVGO", "Nasdaq"],
    [1067983, "BERKSHIRE HATHAWAY INC", "BRK-B", "NYSE"],
    [1046179, "TAIWAN SEMICONDUCTOR MANUFACTURING CO LTD", "TSM", "NYSE"],
    [1652044, "Alphabet Inc.", "GOOG", "Nasdaq"],
    [1067983, "BERKSHIRE HATHAWAY INC", "BRK-A", "NYSE"],
    [2150886, "Fresnillo Plc/ADR", "FNLPF", "OTC"],
    [9999901, "No Exchange Holdings", "NOEX", None],
    [9999902, "Odd Venue Corp", "ODDV", "IEX"],
]


def _raw(rows, fields=FIELDS):
    return {"fields": list(fields), "data": [list(r) for r in rows]}


def _filler(n, start=5_000_000):
    return [[start + i, f"Filler Co {i}", f"FIL{i}", "OTC"] for i in range(n)]


class ParseTests(unittest.TestCase):
    def test_one_row_per_company_in_secs_order(self):
        rows = directory.parse(_raw(HEAD))
        self.assertEqual([r["t"] for r in rows],
                         ["NVDA", "AAPL", "GOOGL", "MSFT", "AVGO", "BRK-B", "TSM", "FNLPF", "NOEX", "ODDV"])

    def test_a_companys_other_tickers_ride_along(self):
        rows = {r["t"]: r for r in directory.parse(_raw(HEAD))}
        self.assertEqual(rows["GOOGL"]["also"], ["GOOG"])
        self.assertEqual(rows["BRK-B"]["also"], ["BRK-A"])
        self.assertEqual(rows["NVDA"]["also"], [])
        self.assertEqual(rows["GOOGL"]["cik"], 1652044)

    def test_an_unnamed_exchange_is_none_not_a_guess(self):
        rows = {r["t"]: r for r in directory.parse(_raw(HEAD))}
        self.assertEqual(rows["NOEX"]["x"], "")
        self.assertEqual(rows["ODDV"]["x"], "")        # a label SEC does not use today
        self.assertEqual(rows["FNLPF"]["x"], "OTC")

    def test_columns_are_found_by_name(self):
        fields = ["ticker", "exchange", "cik", "name"]
        data = [[r[2], r[3], r[0], r[1]] for r in HEAD]
        self.assertEqual([r["t"] for r in directory.parse({"fields": fields, "data": data})][:3],
                         ["NVDA", "AAPL", "GOOGL"])

    def test_broken_rows_are_skipped_and_garbage_is_empty(self):
        rows = directory.parse(_raw(HEAD + [["x", "No CIK", "NOCIK", "NYSE"],
                                            [123, "No ticker", "", "NYSE"],
                                            "not a row"]))
        self.assertNotIn("NOCIK", [r["t"] for r in rows])
        self.assertEqual(len(rows), 10)
        self.assertEqual(directory.parse(None), [])
        self.assertEqual(directory.parse({"fields": ["cik"], "data": [[1]]}), [])
        self.assertEqual(directory.parse({"data": []}), [])

    def test_a_missing_name_falls_back_to_the_ticker(self):
        rows = directory.parse(_raw([[1, "", "ABC", "NYSE"]]))
        self.assertEqual(rows[0]["n"], "ABC")

    def test_the_wire_form_is_compact(self):
        wire = directory.wire(directory.parse(_raw(HEAD)))
        self.assertEqual(wire[2], ["GOOGL", "Alphabet Inc.", "Nasdaq", "GOOG"])
        self.assertEqual(wire[0], ["NVDA", "NVIDIA CORP", "Nasdaq", ""])


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "sec_company_tickers_exchange.json"
        self.patch = mock.patch.object(directory, "CACHE_FILE", self.file)
        self.patch.start()
        directory._mem, directory._mem_mtime = None, 0.0

    def tearDown(self):
        self.patch.stop()
        directory._mem, directory._mem_mtime = None, 0.0
        self.tmp.cleanup()

    def _stub(self, payload=None, exc=None):
        class Resp:
            def json(self_inner):
                return payload

        def get(url, *a, **k):
            self.assertEqual(url, directory.URL)
            if exc:
                raise exc
            return Resp()
        return mock.patch("ystocker.sec13f.edgar_get", get)

    def test_nothing_cached_is_none(self):
        self.assertIsNone(directory.peek())
        self.assertFalse(directory.is_fresh())

    def test_a_refresh_stores_the_list_and_peek_serves_it(self):
        with self._stub(_raw(HEAD + _filler(1200))):
            self.assertTrue(directory.refresh())
        got = directory.peek()
        self.assertEqual(len(got["rows"]), 1210)
        body = json.loads(got["json"])
        self.assertEqual(body["count"], 1210)
        self.assertEqual(body["companies"][2][0], "GOOGL")
        self.assertTrue(directory.is_fresh())
        self.assertTrue(self.file.exists())

    def test_another_worker_reads_the_masters_file(self):
        self.file.write_text(json.dumps(_raw(HEAD + _filler(1200))))
        got = directory.peek()
        self.assertEqual(got["rows"][0]["t"], "NVDA")

    def test_a_short_answer_keeps_the_last_copy(self):
        self.file.write_text(json.dumps(_raw(HEAD + _filler(1200))))
        before = self.file.read_text()
        with self._stub(_raw(HEAD)):                       # 12 rows: an error page
            self.assertFalse(directory.refresh())
        self.assertEqual(self.file.read_text(), before)
        self.assertEqual(len(directory.peek()["rows"]), 1210)

    def test_an_unreachable_sec_keeps_the_last_copy(self):
        self.file.write_text(json.dumps(_raw(HEAD + _filler(1200))))
        with self._stub(exc=RuntimeError("cooldown")):
            self.assertFalse(directory.refresh())
        self.assertEqual(len(directory.peek()["rows"]), 1210)

    def test_a_list_a_day_old_is_stale(self):
        self.file.write_text(json.dumps(_raw(HEAD + _filler(1200))))
        old = time.time() - directory.TTL_SECONDS - 60
        import os
        os.utime(self.file, (old, old))
        self.assertIsNotNone(directory.peek())
        self.assertFalse(directory.is_fresh())


if __name__ == "__main__":
    unittest.main()
