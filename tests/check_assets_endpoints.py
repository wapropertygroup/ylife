"""
End-to-end check of /assets and its API, driven through Flask's test client.

Named ``check_`` so ``unittest discover`` skips it: it builds a real app (which
starts background threads) and uses the local file store. It needs no network,
because the fund cache is pre-seeded with a synthetic universe.

matplotlib is stubbed before ``ystocker.routes`` is imported. That is a workaround
for a broken Homebrew Python on the dev machine (pyexpat cannot load against the
system libexpat), not a statement about the app -- /assets draws its charts in
Chart.js and imports nothing from matplotlib.
"""
from __future__ import annotations

import json
import os
import sys
import types

# ── stub matplotlib/seaborn before routes imports charts ────────────────────
class _Any:
    """Accepts any attribute access, call, subscript or context-manager use.

    charts.py runs ``sns.set_theme(...)`` and ``plt.rcParams[...]`` at import time,
    so the stub has to be callable and subscriptable rather than a bare namespace.
    """
    def __getattr__(self, _name):
        return _Any()

    def __call__(self, *_a, **_k):
        return _Any()

    def __getitem__(self, _k):
        return _Any()

    def __setitem__(self, _k, _v):
        return None

    def __enter__(self):
        return _Any()

    def __exit__(self, *_a):
        return False

    def update(self, *_a, **_k):
        return None


for name in ("matplotlib", "matplotlib.pyplot", "matplotlib.ticker",
             "matplotlib.dates", "matplotlib.patches", "matplotlib.colors",
             "matplotlib.figure", "matplotlib.cm", "matplotlib.font_manager",
             "seaborn"):
    if name not in sys.modules:
        mod = types.ModuleType(name)
        mod.__getattr__ = lambda _attr: _Any()          # type: ignore[attr-defined]
        sys.modules[name] = mod

os.environ["ASSETS_LOCAL_STORE"] = "1"
os.environ.setdefault("YSTOCKER_SECRET_KEY", "check-assets-secret")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ystocker import create_app                                  # noqa: E402
from ystocker import funddata, portfolio                         # noqa: E402

EMAIL = "check-assets@example.com"

# ── a synthetic fund universe, so this needs no network ─────────────────────
UNIVERSE = {
    "VOO": {"symbol": "VOO", "name": "Vanguard S&P 500 ETF", "kind": "fund",
            "price": 512.34, "quote_type": "ETF",
            "holdings": [{"symbol": "NVDA", "name": "NVIDIA Corp", "weight": 0.08},
                         {"symbol": "AAPL", "name": "Apple Inc", "weight": 0.07},
                         {"symbol": "MSFT", "name": "Microsoft Corp", "weight": 0.06}],
            "asset_classes": {"stock": 0.999, "bond": 0.0, "cash": 0.001},
            "sectors": {"technology": 0.35, "financial_services": 0.14}},
    "QQQ": {"symbol": "QQQ", "name": "Invesco QQQ Trust", "kind": "fund",
            "price": 488.20, "quote_type": "ETF",
            "holdings": [{"symbol": "NVDA", "name": "NVIDIA Corp", "weight": 0.10},
                         {"symbol": "AAPL", "name": "Apple Inc", "weight": 0.09}],
            "asset_classes": {"stock": 1.0, "bond": 0.0, "cash": 0.0},
            "sectors": {"technology": 0.60}},
    "BND": {"symbol": "BND", "name": "Vanguard Total Bond", "kind": "fund",
            "price": 73.40, "quote_type": "ETF", "holdings": [],
            "asset_classes": {"stock": 0.0, "bond": 0.9836, "cash": 0.0164},
            "sectors": {}},
    "AAPL": {"symbol": "AAPL", "name": "Apple Inc", "kind": "equity",
             "price": 232.10, "quote_type": "EQUITY", "holdings": [],
             "asset_classes": {}, "sectors": {}, "sector": "Technology"},
    "NVDA": {"symbol": "NVDA", "name": "NVIDIA Corp", "kind": "equity",
             "price": 178.50, "quote_type": "EQUITY", "holdings": [],
             "asset_classes": {}, "sectors": {}, "sector": "Technology"},
    "MSFT": {"symbol": "MSFT", "name": "Microsoft Corp", "kind": "equity",
             "price": 505.00, "quote_type": "EQUITY", "holdings": [],
             "asset_classes": {}, "sectors": {}, "sector": "Technology"},
}

FIDELITY_CSV = b"""Account Number,Account Name,Symbol,Description,Quantity,Last Price,Current Value,Cost Basis Total
X1,ROTH,VOO,VANGUARD 500 INDEX ETF,25.000,$512.34,$12808.50,$9500.00
X1,ROTH,BRK.B,BERKSHIRE HATHAWAY CL B,12.000,$465.00,$5580.00,$4500.00
X1,ROTH,SPAXX**,FIDELITY GOVT MONEY MARKET,1500.000,$1.00,$1500.00,$1500.00
"Brokerage services are provided by Fidelity Brokerage Services LLC.",,,,,,,
"""

XSS_CSV = ('symbol,description,quantity\n'
           'AAPL,"<img src=x onerror=alert(1)>",10\n').encode()


def seed_cache() -> None:
    """Pre-load funddata's in-memory cache so nothing hits the network."""
    import time
    now = time.time()
    with funddata._lock:                                  # noqa: SLF001
        funddata._loaded = True                           # noqa: SLF001
        for symbol, rec in UNIVERSE.items():
            full = dict(rec)
            full.update({"quote_at": now, "comp_at": now, "read_at": now,
                         "currency": "USD"})
            full.setdefault("sector", "")
            funddata._mem[symbol] = full                  # noqa: SLF001


PASS, FAIL = [], []


def check(label: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(label)
    print(("  ok   " if cond else "  FAIL ") + label + (f"  — {detail}" if detail and not cond else ""))


def main() -> int:
    app = create_app()
    app.config["TESTING"] = True
    seed_cache()
    portfolio.save(EMAIL, [])

    with app.test_client() as client:

        print("\n── anonymous access ─────────────────────────────────────────")
        r = client.get("/assets")
        check("GET /assets renders for anonymous (200, no redirect)", r.status_code == 200,
              f"got {r.status_code}")
        body = r.get_data(as_text=True)
        check("anonymous page shows the sign-in pitch", "assets.signin_title" in body)
        check("anonymous page does not ship the app script", "asAddForm" not in body)

        r = client.get("/api/assets")
        check("GET /api/assets is 401 for anonymous", r.status_code == 401,
              f"got {r.status_code}")
        check("401 carries reason=auth", r.get_json().get("reason") == "auth")

        for path, method in (("/api/assets/positions", "post"),
                             ("/api/assets/position", "post"),
                             ("/api/assets/import", "post"),
                             ("/api/assets/analyze", "post")):
            resp = getattr(client, method)(path, json={})
            check(f"{path} is gated for anonymous", resp.status_code == 401,
                  f"got {resp.status_code}")

        r = client.get("/api/assets/template.csv")
        check("template.csv is public", r.status_code == 200)
        check("template.csv is a CSV attachment",
              "text/csv" in r.headers.get("Content-Type", "")
              and "attachment" in r.headers.get("Content-Disposition", ""))

        print("\n── signed in ────────────────────────────────────────────────")
        with client.session_transaction() as sess:
            sess["user_email"] = EMAIL
            sess["user_name"] = "Check User"

        r = client.get("/assets")
        check("GET /assets renders the app when signed in",
              r.status_code == 200 and "asAddForm" in r.get_data(as_text=True))
        check("assets page includes the AI memo UI",
              "asAiRun" in r.get_data(as_text=True)
              and "/api/assets/analyze" in r.get_data(as_text=True))

        r = client.get("/api/assets")
        check("GET /api/assets is 200 when signed in", r.status_code == 200,
              f"got {r.status_code}")
        d = r.get_json()
        check("empty portfolio reports zero positions", d["position_count"] == 0)
        check("empty portfolio is not 'warming'", d["warming"] is False)

        print("\n── add positions ────────────────────────────────────────────")
        for pos in ({"symbol": "VOO", "quantity": 25, "cost_basis": 9500, "account": "Taxable"},
                    {"symbol": "QQQ", "quantity": 10, "cost_basis": 4200, "account": "Taxable"},
                    {"symbol": "AAPL", "quantity": 40, "cost_basis": 7200, "account": "Taxable"},
                    {"symbol": "BND", "quantity": 60, "cost_basis": 4400, "account": "IRA"}):
            r = client.post("/api/assets/position", json=pos)
            if r.status_code != 200:
                check(f"add {pos['symbol']}", False, r.get_data(as_text=True)[:120])
        r = client.get("/api/assets")
        d = r.get_json()
        check("four positions stored", d["position_count"] == 4, str(d["position_count"]))

        expected_total = 25 * 512.34 + 10 * 488.20 + 40 * 232.10 + 60 * 73.40
        check("total value is quantity x live price",
              abs(d["total_value"] - expected_total) < 0.5,
              f"{d['total_value']} vs {expected_total}")

        print("\n── 穿透 correctness ─────────────────────────────────────────")
        inv = d["seen_value"] + d["residual"]["total"]["value"]
        check("INVARIANT seen + residual == total", abs(inv - d["total_value"]) < 0.02,
              f"{inv} vs {d['total_value']}")

        exp = {e["symbol"]: e for e in d["exposures"]}
        check("AAPL exposure exceeds the direct holding",
              exp["AAPL"]["pct"] > exp["AAPL"]["direct_pct"] > 0)
        check("NVDA is exposure with zero direct holding",
              "NVDA" in exp and exp["NVDA"]["direct_pct"] == 0
              and exp["NVDA"]["pct"] > 0)
        check("NVDA is reached through 2 holdings",
              exp["NVDA"]["route_count"] == 2, str(exp["NVDA"].get("route_count")))
        overlaps = {o["symbol"] for o in d["overlaps"]}
        check("overlap table names AAPL and NVDA",
              {"AAPL", "NVDA"} <= overlaps, str(overlaps))
        check("bond sleeve is non_equity, not hidden equity",
              d["residual"]["non_equity"]["value"] > 4000
              and abs(d["residual"]["unclassified"]["value"]) < 0.01)
        check("coverage is a fraction, under 100%", 0 < d["coverage_pct"] < 100,
              str(d["coverage_pct"]))
        check("asset mix has stock and bond",
              {"stock", "bond"} <= {r_["key"] for r_ in d["asset_mix"]})
        check("asset mix sums to ~100%",
              abs(sum(r_["pct"] for r_ in d["asset_mix"]) - 100) < 1.0,
              str(sum(r_["pct"] for r_ in d["asset_mix"])))
        check("sector mix sums to ~100% of classified equity",
              abs(sum(r_["pct"] for r_ in d["sector_mix"]) - 100) < 1.0,
              str(sum(r_["pct"] for r_ in d["sector_mix"])))

        print("\n── remove ───────────────────────────────────────────────────")
        r = client.post("/api/assets/position/BND")
        check("remove BND succeeds", r.status_code == 200)
        check("three positions remain", r.get_json()["count"] == 3)

        print("\n── CSV import: preview then commit ──────────────────────────")
        r = client.post("/api/assets/import?commit=0", data={
            "file": (__import__("io").BytesIO(FIDELITY_CSV), "positions.csv")},
            content_type="multipart/form-data")
        check("preview returns 200", r.status_code == 200, r.get_data(as_text=True)[:200])
        p = r.get_json()
        check("preview does NOT commit", p.get("committed") is False)
        check("broker detected as Fidelity", p.get("broker") == "Fidelity", str(p.get("broker")))
        check("column mapping is reported for review",
              p["mapping"].get("symbol") == "Symbol"
              and p["mapping"].get("cost_basis") == "Cost Basis Total")
        syms = [x["symbol"] for x in p["rows"]]
        check("BRK.B normalised to BRK-B", "BRK-B" in syms, str(syms))
        check("SPAXX** asterisks stripped", "SPAXX" in syms, str(syms))
        check("disclaimer row skipped", len(p["skipped"]) >= 1)
        r2 = client.get("/api/assets")
        check("preview left the portfolio untouched",
              r2.get_json()["position_count"] == 3)

        r = client.post("/api/assets/import?commit=1", data={
            "file": (__import__("io").BytesIO(FIDELITY_CSV), "positions.csv"),
            "mode": "replace", "commit": "1"},
            content_type="multipart/form-data")
        check("commit returns 200", r.status_code == 200, r.get_data(as_text=True)[:200])
        c = r.get_json()
        check("commit reports committed=True", c.get("committed") is True)
        check("import replaced the portfolio with 3 rows", c["count"] == 3, str(c["count"]))
        d = client.get("/api/assets").get_json()
        check("stored portfolio matches the import", d["position_count"] == 3)
        check("imported-but-unpriced symbols are flagged pending",
              "BRK-B" in d["pending_symbols"], str(d["pending_symbols"]))

        print("\n── merge mode ───────────────────────────────────────────────")
        r = client.post("/api/assets/import?commit=1", data={
            "file": (__import__("io").BytesIO(b"symbol,quantity\nMSFT,5\n"), "m.csv"),
            "mode": "merge", "commit": "1"},
            content_type="multipart/form-data")
        check("merge added a row", r.get_json()["count"] == 4, str(r.get_json()["count"]))

        print("\n── malformed input ──────────────────────────────────────────")
        r = client.post("/api/assets/import?commit=0", data={
            "file": (__import__("io").BytesIO(b"alpha,beta\n1,2\n"), "junk.csv")},
            content_type="multipart/form-data")
        check("unrecognised CSV is 400 with an explanation",
              r.status_code == 400 and "header" in (r.get_json().get("error") or "").lower())

        r = client.post("/api/assets/import?commit=0", data={},
                        content_type="multipart/form-data")
        check("missing file is 400", r.status_code == 400)

        r = client.post("/api/assets/positions", json={"positions": "not a list"})
        check("bad positions body is 400", r.status_code == 400)

        r = client.post("/api/assets/position", json={"symbol": "", "quantity": 1})
        check("position with no symbol is 400", r.status_code == 400)

        print("\n── XSS: a CSV cell containing markup ────────────────────────")
        r = client.post("/api/assets/import?commit=1", data={
            "file": (__import__("io").BytesIO(XSS_CSV), "x.csv"),
            "mode": "replace", "commit": "1"},
            content_type="multipart/form-data")
        check("markup-bearing CSV imports without error", r.status_code == 200)
        d = client.get("/api/assets").get_json()
        name = next((p_["name"] for p_ in d["positions"] if p_["symbol"] == "AAPL"), "")
        check("the raw markup survives in JSON (escaping is the client's job)",
              "<img" in name, name)
        page = client.get("/assets").get_data(as_text=True)
        check("the page itself never inlines position data",
              "<img src=x onerror" not in page)

    print("\n" + "=" * 62)
    print(f"  {len(PASS)} passed, {len(FAIL)} failed")
    if FAIL:
        for f in FAIL:
            print("   FAILED:", f)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
