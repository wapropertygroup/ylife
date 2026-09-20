"""Render /evaluation's template offline into a file:// page, for a visual check.

Scratch tool. Builds the same context routes.evaluation() passes, rewrites
/static/ to absolute file paths, and stubs /api/evaluation-extras so the two
deferred panels render.

Run from anywhere: ``python tools/render_eval.py [en|zh]``. Both paths below are
anchored on the repo root rather than on this file's directory, which is what
broke when the script moved out of the root — `sys.path` picked up ``tools/``
and the import failed, and `STATIC` resolved to ``tools/ystocker/static`` so
every asset 404'd and the page died on ``I18n is not defined``. A file:// page
whose scripts do not load still renders its server-side HTML, so the failure
looked like a layout bug rather than a missing stylesheet.
"""
from __future__ import annotations

import json
import os
import random
import re
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class _Any:
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

os.environ.setdefault("YSTOCKER_SECRET_KEY", "render-eval-secret")

from ystocker import PEER_GROUPS, create_app  # noqa: E402

_STATIC_DIR = ROOT / "ystocker" / "static"
if not _STATIC_DIR.is_dir():                       # pragma: no cover - guard
    raise SystemExit(
        f"static directory not found at {_STATIC_DIR}. Without it every asset "
        "404s and the rendered page dies on `I18n is not defined` while still "
        "looking almost right — so fail loudly here instead.")
STATIC = _STATIC_DIR.as_uri()

NAMES = {
    "NVDA": "NVIDIA Corporation", "AAPL": "Apple Inc.", "MSFT": "Microsoft Corporation",
    "GOOGL": "Alphabet Inc.", "AMZN": "Amazon.com, Inc.", "META": "Meta",
    "TSLA": "Tesla, Inc.", "TSM": "Taiwan Semiconductor", "AVGO": "Broadcom Inc.",
}

rng = random.Random(7)


def row(ticker: str, sector: str) -> dict:
    return {
        "ticker": ticker,
        "name": NAMES.get(ticker, f"{ticker} Holdings Corporation"),
        "sector": sector,
        "upside": rng.uniform(-15, 60),
        "pe_fwd": rng.uniform(8, 70),
        "pe_ttm": rng.uniform(8, 90),
        "peg": rng.uniform(0.2, 4.0),
        "ps_ratio": rng.uniform(0.5, 20),
        "short_float": rng.uniform(0.5, 12),
        "eps_growth_ttm": rng.uniform(-30, 200),
        "eps_growth_q": rng.uniform(-30, 200),
        "ev_ebitda": rng.uniform(4, 40),
        "market_cap": rng.uniform(5, 4000),
        "day_change_pct": rng.uniform(-3, 3),
        "cash": {"pfcf": rng.uniform(8, 90), "forward_pfcf": rng.uniform(8, 90),
                 "growth": "1.2", "forward_source": "eps"},
    }


SPARK = ("ticker", "upside", "pe_fwd", "pe_ttm", "ps_ratio", "short_float")

sector_cards, all_rows, seen = {}, [], set()
for group, tickers in PEER_GROUPS.items():
    cd = [row(t, group) for t in tickers]
    sector_cards[group] = {
        "tickers": list(tickers),
        "chartdata": [{k: r[k] for k in SPARK} for r in cd],
    }
    for r in cd:
        if r["ticker"] in seen:
            continue
        seen.add(r["ticker"])
        all_rows.append(r)

EXTRAS = {
    "analyst": {
        "asof": "2026-09-18", "covered": 222, "universe": 271, "stale": False,
        "lead_period": "+1y",
        "tickers": {
            t: {
                "ticker": t,
                "eps_trend": {"+1y": {"current": rng.uniform(-5, 1200),
                                      "chg30_pct": rng.uniform(-20, 55),
                                      "chg30_abs": rng.uniform(-5, 300)}},
                "eps_revisions": {"+1y": {"up30": rng.randint(0, 42),
                                          "down30": rng.randint(0, 4),
                                          "net30": rng.randint(-2, 40)}},
                "recommendations": [{"strong_buy": rng.randint(0, 30), "buy": rng.randint(0, 30),
                                     "hold": rng.randint(0, 15), "sell": rng.randint(0, 3),
                                     "strong_sell": 0}],
                "price_target": {"upside_pct": rng.uniform(-13, 140)},
            }
            for t in ["PARA", "MPC", "DELL", "NVDA", "WOLF", "PSX", "SHEN", "6857.T",
                      "CCOI", "SNOW", "6098.T", "PSA", "6273.T", "ADI", "MRVL"]
        },
    },
    "sectors": {
        "asof": "2026-09-18", "stale": False,
        "sectors": [
            {"key": "technology", "name": "Technology", "market_weight": 32.6,
             "market_cap": 28.5e12, "companies": 861,
             "top_companies": [{"ticker": x} for x in ("NVDA", "AAPL", "MSFT")]},
            {"key": "financial-services", "name": "Financial Services", "market_weight": 13.6,
             "market_cap": 11.8e12, "companies": 1520,
             "top_companies": [{"ticker": x} for x in ("BRK-B", "JPM", "V")]},
            {"key": "industrials", "name": "Industrials", "market_weight": 10.5,
             "market_cap": 9.1e12, "companies": 800,
             "top_companies": [{"ticker": x} for x in ("SPCX", "CAT", "GE")]},
            {"key": "healthcare", "name": "Healthcare", "market_weight": 9.4,
             "market_cap": 8.2e12, "companies": 1128,
             "top_companies": [{"ticker": x} for x in ("LLY", "JNJ", "ABBV")]},
            {"key": "communication-services", "name": "Communication Services",
             "market_weight": 9.3, "market_cap": 8.1e12, "companies": 249,
             "top_companies": [{"ticker": x} for x in ("GOOG", "META", "NFLX")]},
            {"key": "consumer-cyclical", "name": "Consumer Cyclical", "market_weight": 9.0,
             "market_cap": 7.9e12, "companies": 573,
             "top_companies": [{"ticker": x} for x in ("AMZN", "TSLA", "HD")]},
            {"key": "energy", "name": "Energy", "market_weight": 5.0,
             "market_cap": 4.3e12, "companies": 249,
             "top_companies": [{"ticker": x} for x in ("XOM", "CVX", "COP")]},
            {"key": "consumer-defensive", "name": "Consumer Defensive", "market_weight": 4.3,
             "market_cap": 3.7e12, "companies": 260,
             "top_companies": [{"ticker": x} for x in ("WMT", "COST", "KO")]},
            {"key": "basic-materials", "name": "Basic Materials", "market_weight": 2.6,
             "market_cap": 2.3e12, "companies": 291,
             "top_companies": [{"ticker": x} for x in ("LIN", "SCCO", "NEM")]},
            {"key": "real-estate", "name": "Real Estate", "market_weight": 2.0,
             "market_cap": 1.7e12, "companies": 257,
             "top_companies": [{"ticker": x} for x in ("WELL", "PLD", "EQIX")]},
            {"key": "utilities", "name": "Utilities", "market_weight": 1.9,
             "market_cap": 1.6e12, "companies": 109,
             "top_companies": [{"ticker": x} for x in ("NEE", "SO", "DUK")]},
        ],
    },
}

STUB = """<script>
(function () {
  const extras = %s;
  const real = window.fetch;
  window.fetch = function (url, opts) {
    if (typeof url === 'string' && url.indexOf('/api/evaluation-extras') === 0) {
      return Promise.resolve({ ok: true, json: () => Promise.resolve(extras) });
    }
    return real(url, opts);
  };
})();
</script>
""" % json.dumps(EXTRAS)

app = create_app()
with app.test_request_context("/evaluation?lang=" + (sys.argv[1] if len(sys.argv) > 1 else "zh")):
    from flask import render_template
    html = render_template(
        "index.html",
        peer_groups=list(PEER_GROUPS.keys()),
        sector_cards=sector_cards,
        all_chartdata=json.dumps(all_rows),
        fetch_errors=[],
        cache_last_updated=1789000000,
        warming=False,
    )

html = html.replace('href="/static/', f'href="{STATIC}/').replace('src="/static/', f'src="{STATIC}/')
html = re.sub(r"<head([^>]*)>", lambda m: f"<head{m.group(1)}>" + STUB, html, count=1)
out = Path("/tmp/eval_render.html")
out.write_text(html)
print(out)
