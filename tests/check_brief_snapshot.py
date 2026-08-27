"""Exercise ystocker.brief formatters against the real on-disk caches.

Not a unit test — a shape check. The risk in brief.py is not logic, it is key
names and units: every payload here was written by a different module with its
own conventions (value_thousands vs value_millions, pct_dec vs pct, day_chg vs
day_chg_pct). This loads what the box actually cached and reports how many rows
each section produced, so a mis-keyed field shows up as an empty table rather
than being discovered in production.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ystocker import brief  # noqa: E402

CACHE = Path(__file__).resolve().parents[1] / "cache"


def _load(name: str):
    p = CACHE / name
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except json.JSONDecodeError as exc:
        print(f"  ! {name} is not valid JSON: {exc}")
        return None


def main() -> int:
    fed_c       = _load("fed_cache.json") or {}
    fedwatch_c  = _load("fedwatch_cache.json") or {}
    housing_c   = _load("housing_cache.json") or {}
    valuation_c = _load("valuation_cache.json") or {}
    sec13f_c    = _load("sec13f_cache.json") or {}
    breadth_c   = _load("breadth_cache.json") or {}
    aaii_c      = _load("aaii_cache.json") or {}
    yc_c        = _load("yield_curve_cache.json") or {}

    # The disk caches wrap their payload; unwrap the way each module does.
    def _unwrap(c, *keys):
        for k in keys:
            if isinstance(c, dict) and k in c:
                return c[k]
        return c

    from ystocker import fed as fed_mod

    holdings = _unwrap(sec13f_c, "data", "holdings")
    consensus = None
    if isinstance(holdings, dict):
        from collections import defaultdict
        tf, tv = defaultdict(list), defaultdict(float)
        for fname, fd in holdings.items():
            if not isinstance(fd, dict) or fd.get("error"):
                continue
            for h in fd.get("holdings", []):
                t = h.get("ticker")
                if t:
                    tf[t].append(fname)
                    tv[t] += h.get("value_millions", 0) or 0
        consensus = sorted(
            [{"ticker": t, "fund_count": len(n), "total_value_m": round(tv[t]),
              "fund_names": n} for t, n in tf.items() if len(n) >= 2],
            key=lambda x: -x["fund_count"])[:25]

    aaii_payload = _unwrap(aaii_c, "data")
    sources = {
        "markets":       None,   # no disk cache; warmed in-process only
        "commodities":   None,
        "movers":        None,
        "fg":            None,
        "pcr":           None,
        "skew":          None,
        "yield_curve":   _unwrap(yc_c, "data"),
        "yield_spread":  None,
        "credit_spread": None,
        "aaii":          (aaii_payload or {}).get("latest") if isinstance(aaii_payload, dict) else None,
        "events":        None,
        "breadth":       _unwrap(breadth_c, "data"),
        "fed":           _unwrap(fed_c, "data"),
        "fedwatch":      _unwrap(fedwatch_c, "data"),
        "housing":       _unwrap(housing_c, "data"),
        "valuation":     _unwrap(valuation_c, "data"),
        "fed_series_meta": fed_mod.SERIES,
        "evaluation":    None,   # needs the live ticker cache + pandas
        "holdings13f":   holdings,
        "consensus13f":  consensus,
    }

    snap = brief.build_snapshot(sources, "2026-08-27")

    print("=" * 72)
    print(f"SNAPSHOT: {len(snap):,} chars, {len(snap.splitlines()):,} lines")
    print("=" * 72)

    # Per-section row counts. A section that loaded its source but produced no
    # table row is the failure this script exists to catch.
    sections = re.split(r"^=== ", snap, flags=re.M)[1:]
    ok = True
    SEP = re.compile(r"^\|[\s\-|]+\|$")
    for sec in sections:
        title = sec.splitlines()[0].strip()
        body = "\n".join(sec.splitlines()[1:])
        pipe_lines = [l for l in body.splitlines() if l.startswith("|")]
        seps = [l for l in pipe_lines if SEP.match(l)]
        # Each table contributes one separator and one header; the rest are data.
        data_rows = len(pipe_lines) - 2 * len(seps)
        unavailable = "DATA UNAVAILABLE" in body
        key = title.split("(")[0].strip()
        if unavailable and not data_rows:
            flag = "COLD "
        elif data_rows:
            flag = "OK   "
        else:
            flag = "EMPTY"
            ok = False
        print(f"  [{flag}] {key:<46} tables: {len(seps)}  rows: {data_rows}"
              + ("  (some data unavailable)" if unavailable and data_rows else ""))

    print()
    nas = snap.count("n/a")
    print(f"'n/a' cells: {nas}")

    # The tile lists must stay in step with what the payloads actually ship. A
    # key listed but absent means a permanently blank row; a key shipped but not
    # listed means the brief silently drops a metric the dashboard shows.
    print()
    print("tile-list coverage:")
    for label, payload, tiles in (
        ("housing", sources["housing"], brief._HOUSING_TILES),
        ("valuation", sources["valuation"], brief._VALUATION_TILES),
    ):
        if not isinstance(payload, dict) or not payload.get("headline"):
            print(f"  --   {label}: payload cold, cannot check")
            continue
        have = set(payload["headline"])
        listed = {k for k, _, _ in tiles}
        absent, unused = sorted(listed - have), sorted(have - listed)
        for kind, keys in (("listed-but-absent", absent), ("shipped-but-unused", unused)):
            if keys:
                print(f"  FAIL {label} {kind}: {', '.join(keys)}")
                globals()["COVERAGE_FAIL"] = True
            else:
                print(f"  ok   {label} {kind}: none")

    # Any unit _tile() does not know prints a bare float and logs. Catch the log.
    import logging as _logging

    class _Catch(_logging.Handler):
        def __init__(self):
            super().__init__()
            self.hits: list[str] = []

        def emit(self, rec):
            if "unhandled tile unit" in rec.getMessage():
                self.hits.append(rec.getMessage())

    catcher = _Catch()
    _logging.getLogger("ystocker.brief").addHandler(catcher)
    _logging.getLogger("ystocker.brief").setLevel(_logging.INFO)
    brief.build_snapshot(sources, "2026-08-27")
    print()
    if catcher.hits:
        print(f"  FAIL unhandled units: {len(catcher.hits)}")
        for h in sorted(set(catcher.hits)):
            print(f"       {h}")
        globals()["COVERAGE_FAIL"] = True
    else:
        print("  ok   every tile unit handled explicitly")

    # Show two sections in full so units can be eyeballed.
    for want in ("6. FED POLICY", "9. FED BALANCE"):
        for sec in sections:
            if sec.startswith(want):
                print()
                print("-" * 72)
                print("=== " + sec[:2200].rstrip())
                break

    print()
    ok = ok and not globals().get("COVERAGE_FAIL")
    print("RESULT:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
