"""The saved deep-research reports behind /history's Research tab.

``research_store`` exists because a report used to live only in an 8-hour disk
cache: reopening the tab the next day showed nothing, and a box rebuild deleted
every report ever generated (reported 2026-10-01 on /history/NBIS). The rows now
outlive both -- and since a report can quote the reader's account value, shares
and cost, the one property worth testing hardest is that a reader can only ever
reach their own rows.

That is enforced by the key, not by a check: every read builds the sort key from
the session's address. So these tests run the real store code against an
in-memory table that *evaluates* the boto3 key conditions it is handed, rather
than a mock that would agree with whatever was asked of it -- the prefix test
below (``a@b.co`` must not see ``a@b.com``) only means something if the
``begins_with`` is actually applied.

No AWS, no app, no network.
"""
from __future__ import annotations

import gzip
import unittest
from datetime import datetime, timedelta, timezone

from boto3.dynamodb.conditions import And, BeginsWith, Equals

from ystocker import research_store as rs

NOW = datetime(2026, 10, 1, 14, 3, 11, tzinfo=timezone.utc)


class _Table:
    """DynamoDB's semantics for the four calls the store makes."""

    def __init__(self, page_size: int | None = None) -> None:
        self.items: dict[tuple[str, str], dict] = {}
        self.page_size = page_size
        self.fail = False
        self.queries = 0

    def _check(self):
        if self.fail:
            raise RuntimeError("stubbed outage")

    def put_item(self, Item):
        self._check()
        self.items[(Item["ticker"], Item["sk"])] = dict(Item)

    def get_item(self, Key):
        self._check()
        item = self.items.get((Key["ticker"], Key["sk"]))
        return {"Item": dict(item)} if item else {}

    def delete_item(self, Key, ReturnValues=None):
        self._check()
        old = self.items.pop((Key["ticker"], Key["sk"]), None)
        return {"Attributes": old} if old else {}

    @staticmethod
    def _match(cond, item) -> bool:
        vals = cond.get_expression()["values"]
        if isinstance(cond, And):
            return all(_Table._match(v, item) for v in vals)
        name, value = vals[0].name, vals[1]
        if isinstance(cond, Equals):
            return item.get(name) == value
        if isinstance(cond, BeginsWith):
            return str(item.get(name, "")).startswith(value)
        raise NotImplementedError(type(cond))

    def query(self, KeyConditionExpression, ScanIndexForward=True, Limit=None,
              ExclusiveStartKey=None):
        self._check()
        self.queries += 1
        hits = sorted((i for i in self.items.values()
                       if self._match(KeyConditionExpression, i)),
                      key=lambda i: i["sk"], reverse=not ScanIndexForward)
        if ExclusiveStartKey:
            keys = [i["sk"] for i in hits]
            hits = hits[keys.index(ExclusiveStartKey["sk"]) + 1:]
        cap = min(x for x in (Limit, self.page_size, len(hits)) if x is not None)
        page = hits[:cap]
        out = {"Items": [dict(i) for i in page]}
        if len(hits) > cap and page:
            out["LastEvaluatedKey"] = {"ticker": page[-1]["ticker"], "sk": page[-1]["sk"]}
        return out


def _save(owner="reader@example.com", ticker="NBIS", text="## 1. 基本信息\nok",
          lang="zh", when=NOW, **kw):
    return rs.save(ticker=ticker, owner=owner, text=text, lang=lang, now=when, **kw)


class WithTable(unittest.TestCase):
    def setUp(self):
        self._saved_table = rs._table
        self.table = _Table()
        rs._table = self.table

    def tearDown(self):
        rs._table = self._saved_table


class KeyShapeTests(unittest.TestCase):
    def test_the_sort_key_is_owner_then_a_time_ordered_ref(self):
        item = rs.build_item(ticker="nbis", owner=" Reader@Example.com ", text="x",
                             lang="zh", now=NOW, rid="0123456789abcdef")
        self.assertEqual(item["ticker"], "NBIS")
        self.assertEqual(item["owner"], "reader@example.com")
        self.assertEqual(item["sk"], "reader@example.com#20261001T140311000000Z-0123456789abcdef")
        self.assertEqual(item["created_at"], "2026-10-01T14:03:11+00:00")

    def test_refs_sort_as_time(self):
        early = rs.make_ref(NOW, "f" * 16)
        late = rs.make_ref(NOW + timedelta(seconds=1), "0" * 16)
        self.assertLess(early, late, "a later report must sort after an earlier one "
                        "whatever its random id, or 'newest first' is a lie")

    def test_an_address_holding_the_delimiter_is_refused(self):
        """No provider issues one, and a '#' inside the owner half is exactly
        how one reader's prefix would come to match another's."""
        self.assertIsNone(rs.normalise_owner("a#b@example.com"))
        with self.assertRaises(ValueError):
            rs.build_item(ticker="NBIS", owner="a#b@example.com", text="x", lang="en")

    def test_input_that_must_not_be_stored_is_refused_before_any_write(self):
        for kw in ({"text": "   "}, {"ticker": "../etc"}, {"owner": ""},
                   {"text": "x" * (rs.MAX_TEXT + 1)}):
            args = {"ticker": "NBIS", "owner": "r@example.com", "text": "x", "lang": "en"}
            args.update(kw)
            with self.subTest(kw=list(kw)), self.assertRaises(ValueError):
                rs.build_item(**args)

    def test_refs_are_validated_before_they_reach_a_key(self):
        good = rs.make_ref(NOW, "0123456789abcdef")
        self.assertEqual(rs.valid_ref(good), good)
        for bad in ("", "../x", good + "#", "other@example.com#" + good,
                    "20261001T140311000000Z-XYZ", "20261001T140311Z-0123456789abcdef", good.replace("Z-", "-")):
            with self.subTest(bad=bad):
                self.assertIsNone(rs.valid_ref(bad))

    def test_indices_and_futures_are_valid_tickers_here(self):
        """Research runs on whatever /history opens -- wider than the agents'
        ticker rule on purpose."""
        for t in ("^GSPC", "GC=F", "BRK-B", "600519.SS"):
            self.assertEqual(rs.normalise_ticker(t), t)


class RoundTripTests(unittest.TestCase):
    def test_the_body_is_gzipped_and_comes_back_whole(self):
        text = "## 1. 基本信息\n" + "数据缺失。" * 2000
        item = rs.build_item(ticker="NBIS", owner="r@example.com", text=text, lang="zh",
                             now=NOW, sources=["Reuters", "", "  "], price=98.4,
                             degraded=True, has_portfolio=True, fingerprint="abc",
                             template="v1", model="gemini-2.5-flash")
        self.assertLess(len(item["body"]), len(text.encode()) // 3)
        row = rs.row_from_item(item)
        self.assertEqual(row["text"], text)
        self.assertEqual(row["sources"], ["Reuters"])
        self.assertEqual(row["price"], 98.4)
        self.assertTrue(row["degraded"] and row["has_portfolio"])
        self.assertFalse(row["truncated"] or row["unreadable"])
        self.assertEqual(row["ref"], item["sk"].split("#", 1)[1])

    def test_a_price_is_stored_as_a_string_and_junk_is_dropped(self):
        """DynamoDB rejects floats outright."""
        ok = rs.build_item(ticker="NBIS", owner="r@example.com", text="x", lang="en",
                           price=98.40)
        self.assertEqual(ok["price"], "98.4")
        for junk in (None, "n/a", float("nan"), -1, True):
            item = rs.build_item(ticker="NBIS", owner="r@example.com", text="x",
                                 lang="en", price=junk)
            self.assertNotIn("price", item, junk)

    def test_an_unreadable_body_is_flagged_not_dropped(self):
        """A row that silently vanished from the picker would read as a report
        that was never saved."""
        item = rs.build_item(ticker="NBIS", owner="r@example.com", text="x", lang="en")
        item["body"] = b"not gzip"
        row = rs.row_from_item(item)
        self.assertTrue(row["unreadable"])
        self.assertEqual(row["text"], "")
        self.assertTrue(row["ref"])

    def test_the_wire_shape_is_an_allowlist(self):
        item = rs.build_item(ticker="NBIS", owner="r@example.com", text="secret body",
                             lang="en", fingerprint="fp", sources=["A"])
        row = rs.row_from_item(item)
        meta = rs.meta(row)
        for private in ("owner", "text", "fingerprint", "digest", "sources", "ticker"):
            self.assertNotIn(private, meta)
        full = rs.with_text(row)
        self.assertEqual(full["text"], "secret body")
        self.assertEqual(full["sources"], ["A"])
        self.assertNotIn("owner", full)


class CacheHitTests(unittest.TestCase):
    def _row(self, **kw):
        base = {"lang": "zh", "fingerprint": "fp", "template": "v1", "text": "t",
                "truncated": False, "unreadable": False,
                "created_at": (NOW - timedelta(hours=1)).isoformat()}
        base.update(kw)
        return base

    def hit(self, rows):
        return rs.cache_hit(rows, lang="zh", fingerprint="fp", template="v1",
                            max_age=8 * 3600, now=NOW)

    def test_same_inputs_inside_the_ttl_is_a_hit(self):
        self.assertIsNotNone(self.hit([self._row()]))

    def test_anything_that_changes_the_answer_misses(self):
        for kw in ({"lang": "en"}, {"fingerprint": "other"}, {"template": "v0"},
                   {"created_at": (NOW - timedelta(hours=9)).isoformat()}):
            with self.subTest(kw=kw):
                self.assertIsNone(self.hit([self._row(**kw)]))

    def test_a_truncated_or_unreadable_report_is_kept_but_not_served_as_an_answer(self):
        self.assertIsNone(self.hit([self._row(truncated=True)]))
        self.assertIsNone(self.hit([self._row(unreadable=True, text="")]))

    def test_the_newest_matching_row_wins(self):
        rows = [self._row(text="new"), self._row(text="old",
                created_at=(NOW - timedelta(hours=2)).isoformat())]
        self.assertEqual(self.hit(rows)["text"], "new")

    def test_the_tab_opens_on_the_page_language_else_the_newest(self):
        rows = [{"ref": "a", "lang": "en"}, {"ref": "b", "lang": "zh"}]
        self.assertEqual(rs.pick_latest(rows, "zh")["ref"], "b")
        self.assertEqual(rs.pick_latest(rows, "en")["ref"], "a")
        self.assertEqual(rs.pick_latest(rows[:1], "zh")["ref"], "a")
        self.assertIsNone(rs.pick_latest([], "zh"))


class StoreTests(WithTable):
    def test_a_saved_report_lists_newest_first(self):
        _save(text="first", when=NOW)
        _save(text="second", when=NOW + timedelta(minutes=5))
        rows = rs.list_for("NBIS", "reader@example.com")
        self.assertEqual([r["text"] for r in rows], ["second", "first"])

    def test_a_reader_never_sees_another_readers_rows(self):
        _save(owner="alice@example.com", text="alice's position")
        _save(owner="bob@example.com", text="bob's position")
        rows = rs.list_for("NBIS", "alice@example.com")
        self.assertEqual([r["text"] for r in rows], ["alice's position"])

    def test_an_address_that_prefixes_another_does_not_match_it(self):
        """The delimiter is what keeps a@b.co out of a@b.com's rows."""
        _save(owner="a@b.com", text="the longer address")
        self.assertEqual(rs.list_for("NBIS", "a@b.co"), [])

    def test_another_ticker_is_another_partition(self):
        _save(ticker="NVDA", text="nvda")
        self.assertEqual(rs.list_for("NBIS", "reader@example.com"), [])

    def test_another_readers_ref_is_simply_a_miss(self):
        mine = _save(owner="alice@example.com")
        self.assertIsNotNone(rs.get("NBIS", "alice@example.com", mine["ref"]))
        self.assertIsNone(rs.get("NBIS", "bob@example.com", mine["ref"]))
        self.assertFalse(rs.delete("NBIS", "bob@example.com", mine["ref"]))
        self.assertEqual(len(self.table.items), 1, "bob deleted alice's report")

    def test_delete_removes_the_row_and_says_whether_it_existed(self):
        row = _save()
        self.assertTrue(rs.delete("NBIS", "reader@example.com", row["ref"]))
        self.assertFalse(rs.delete("NBIS", "reader@example.com", row["ref"]))
        self.assertIsNone(rs.get("NBIS", "reader@example.com", row["ref"]))

    def test_the_list_follows_pages_up_to_its_limit(self):
        self.table.page_size = 2
        for i in range(5):
            _save(text=f"r{i}", when=NOW + timedelta(minutes=i))
        rows = rs.list_for("NBIS", "reader@example.com", limit=4)
        self.assertEqual([r["text"] for r in rows], ["r4", "r3", "r2", "r1"])
        self.assertGreater(self.table.queries, 1)

    def test_an_outage_raises_rather_than_answering_none_saved(self):
        """'You have no saved reports' is the one wrong answer on the panel whose
        job is to show them."""
        self.table.fail = True
        for call in (lambda: rs.list_for("NBIS", "reader@example.com"),
                     lambda: rs.get("NBIS", "reader@example.com", rs.make_ref(NOW, "0" * 16)),
                     lambda: _save()):
            with self.assertRaises(rs.StoreUnavailable):
                call()

    def test_no_table_at_all_is_an_outage_too(self):
        rs._table = None
        rs._table_unavail_until = 9e18      # as after a failed connect
        try:
            with self.assertRaises(rs.StoreUnavailable):
                rs.list_for("NBIS", "reader@example.com")
        finally:
            rs._table_unavail_until = 0.0

    def test_a_saved_row_is_returned_in_the_shape_the_route_sends(self):
        row = _save(price="98.4", sources=["Reuters"])
        self.assertEqual(rs.meta(row)["price"], 98.4)
        stored = next(iter(self.table.items.values()))
        self.assertIsInstance(stored["body"], bytes)
        self.assertEqual(gzip.decompress(stored["body"]).decode(), row["text"])


if __name__ == "__main__":
    unittest.main()
