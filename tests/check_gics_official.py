"""Check ystocker.gics against S&P's own GICS indices. Needs Yahoo — run on the box.

Yahoo resolves 35 of S&P's 36 official GICS index symbols but carries history
for only 8 of them (the rest return today's quote alone), which is why gics.py
builds every group from the constituents. Those 8, plus the S&P 500 itself, are
therefore an independent oracle for the method: this builds them with
``gics.performance()`` on **price-only** closes — the official indices exclude
dividends, where the page uses total return — and prints both side by side.

Run it after regenerating the snapshot. It exits non-zero when a group of ten
or more names misses by more than the tolerance below; smaller groups are
printed but not judged, because a single disputed classification is a large
share of them (Telecommunication Services is four names, and EchoStar alone
accounts for its gap).

Run: python tests/check_gics_official.py
"""
from __future__ import annotations

import sys
import time
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ystocker import gics  # noqa: E402

# Code -> official symbol, for the ones with history. "60" and "5510" are the
# Real Estate sector and the Utilities group (= sector); the rest are groups.
OFFICIAL = {"4530": "^SP500-4530", "4010": "^SP500-4010", "4030": "^SP500-4030",
            "5010": "^SP500-5010", "5020": "^SP500-5020", "2550": "^SP500-2550",
            "5510": "^SP500-5510", "60": "^SP500-60", "index": "^GSPC"}

# Percentage points. Short horizons are where a broken method shows — a wrong
# base date or share count moves 1D/1W immediately — and they reproduce the
# official figures to the hundredth, so they are held tight. Long horizons are
# dominated by membership the snapshot cannot see (names removed during the
# window), which is drift rather than error: measured on 2026-09-25, Media &
# Entertainment's 1Y read +12.70 against an official +14.21 with every shorter
# horizon inside 0.4. A tolerance that fails on that case every run would make
# this check permanently red and therefore useless.
TOLERANCE = {"1D": 0.25, "1W": 0.5, "1M": 0.5, "3M": 0.75,
             "6M": 2.0, "YTD": 2.0, "1Y": 2.0}
JUDGED_MIN_NAMES = 10


def main() -> int:
    warnings.filterwarnings("ignore")
    import yfinance as yf

    snap = gics.load_snapshot()
    if not snap:
        print("no snapshot")
        return 2
    syms = gics.tickers(snap) + list(OFFICIAL.values())
    df = yf.download(syms, period="2y", interval="1d", auto_adjust=False,
                     progress=False, threads=True)
    closes = df["Close"]
    res = gics.performance(closes, snap, now=time.time())

    rows: dict[str, dict] = {"index": {"returns": res["index"]["returns"], "n": res["coverage"]["priced"]}}
    for s in res["sectors"]:
        rows[s["code"]] = s
        for g in s["groups"]:
            rows[g["code"]] = g

    print(f"as of {res['asof']}  bases {res['base_dates']}")
    print(f"coverage {res['coverage']['priced']}/{res['coverage']['members']} "
          f"({res['coverage']['weight_pct']}% of weight), missing {res['coverage']['missing']}")
    worst = 0
    for code, sym in OFFICIAL.items():
        off = closes[sym].dropna()
        row = rows[code]
        cells = []
        for p in res["periods"]:
            d0 = res["base_dates"][p]
            try:
                o = (off.loc[res["asof"]] / off.loc[d0] - 1) * 100
            except KeyError:
                cells.append(f"{p}: n/a")
                continue
            ours = row["returns"].get(p)
            diff = None if ours is None else ours - o
            flag = ""
            if diff is not None and row["n"] >= JUDGED_MIN_NAMES and abs(diff) > TOLERANCE[p]:
                flag, worst = " !", 1
            cells.append(f"{p}:{ours:+7.2f}/{o:+7.2f}{flag}" if ours is not None else f"{p}: —")
        print(f"{sym:12s} n={row['n']:3d}  " + "  ".join(cells))
    print("OK" if not worst else "MISS beyond tolerance (marked !)")
    return worst


if __name__ == "__main__":
    sys.exit(main())
