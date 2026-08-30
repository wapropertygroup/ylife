"""
End-to-end checks for the position-limit policy endpoints.

``check_`` prefixed so ``unittest discover`` skips it: this needs a Flask app and
stubs matplotlib, matching ``tests/check_assets_endpoints.py`` — whose stubbing
approach and rationale this reuses verbatim (a broken Homebrew Python on the dev
machine, not a statement about the app).

Run with::

    ASSETS_LOCAL_STORE=1 venv/bin/python tests/check_assets_policy_endpoints.py
"""
from __future__ import annotations

import os
import sys
import types


class _Any:
    """Accepts any attribute access, call, subscript or context-manager use."""

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


for name in ("matplotlib", "matplotlib.pyplot", "matplotlib.dates",
             "matplotlib.ticker", "matplotlib.colors", "matplotlib.patches",
             "matplotlib.font_manager", "seaborn"):
    # numpy is deliberately NOT stubbed: pandas parses numpy.__version__ at import
    # and a stub that answers every attribute makes that a TypeError.
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda _n: _Any()   # type: ignore[attr-defined]
    sys.modules.setdefault(name, mod)

os.environ.setdefault("ASSETS_LOCAL_STORE", "1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ystocker import create_app  # noqa: E402

_ok = 0
_fail = 0


def section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 58 - len(title)))


def check(label: str, cond: bool) -> None:
    global _ok, _fail
    print(("  ok   " if cond else "  FAIL ") + label)
    if cond:
        _ok += 1
    else:
        _fail += 1


def main() -> int:
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["user_email"] = "policy-check@example.com"

    section("auth")
    anon = app.test_client()
    check("anonymous GET is 401", anon.get("/api/assets/policy").status_code == 401)
    check("anonymous PUT is 401",
          anon.put("/api/assets/policy", json={"policy": {}}).status_code == 401)

    section("defaults")
    # Start from a known state: this store is shared with any earlier run.
    client.put("/api/assets/policy", json={"policy": {}})
    resp = client.get("/api/assets/policy")
    policy = resp.get_json()["policy"]
    check("200", resp.status_code == 200)
    check("single-name limit starts unset", policy["max_single_name_pct"] is None)
    check("issuer limit starts unset", policy["max_issuer_pct"] is None)
    check("cash starts at zero", policy["cash"] == 0.0)

    section("normalisation on write")
    resp = client.put("/api/assets/policy", json={"policy": {
        "max_single_name_pct": 8,
        "max_issuer_pct": 0,            # zero is not a limit anybody meant
        "cash": -5,                     # negative cash is not cash
        "risk_budget": 99,              # not a field this store knows
        "holding_types": {"aapl": "core", "nvda": "speculative"},
    }})
    stored = resp.get_json()["policy"]
    check("200", resp.status_code == 200)
    check("valid limit kept", stored["max_single_name_pct"] == 8.0)
    check("zero limit rejected to None", stored["max_issuer_pct"] is None)
    check("negative cash clamped to zero", stored["cash"] == 0.0)
    check("unknown key dropped", "risk_budget" not in stored)
    check("holding type normalised and allowlisted",
          stored["holding_types"] == {"AAPL": "core"})
    check("response echoes what was stored, not what was sent",
          client.get("/api/assets/policy").get_json()["policy"] == stored)

    section("the policy row survives a position write")
    # The whole reason the policy lives under p# rather than u#: save() is a
    # whole-item put_item, so a shared row would lose the limits here, silently.
    client.post("/api/assets/positions", json={"positions": [
        {"symbol": "AAPL", "value": 9000.0},
        {"symbol": "MSFT", "value": 1000.0},
    ]})
    after = client.get("/api/assets/policy").get_json()["policy"]
    check("limit still 8% after replacing every position",
          after["max_single_name_pct"] == 8.0)

    section("verdicts on /api/assets")
    payload = client.get("/api/assets").get_json()
    check("policy echoed on the analysis payload",
          (payload.get("policy") or {}).get("max_single_name_pct") == 8.0)
    con = payload.get("constraints")
    check("constraints block present", con is not None)
    if con:
        check("AAPL at 90% of the portfolio is a breach", con["verdict"] == "breach")
        check("at least one breach counted", con["breach_count"] >= 1)
        check("worst verdict sorts first", con["checks"][0]["verdict"] == "breach")
        check("direct holdings give a verified band",
              con["checks"][0]["after"]["verified"] is True)
        check("no headroom figure anywhere in the payload",
              "headroom" not in repr(con))
        floor = con["checks"][0]["after"]["floor_pct"]
        ceiling = con["checks"][0]["after"]["ceiling_pct"]
        check("floor equals ceiling for an all-direct portfolio",
              abs(floor - ceiling) < 1e-6)

    section("an unset limit is not a satisfied one")
    client.put("/api/assets/policy", json={"policy": {"cash": 5000}})
    payload = client.get("/api/assets").get_json()
    check("constraints is null when no limit is stated",
          payload.get("constraints") is None)
    check("cash alone does not manufacture a check",
          payload["policy"]["cash"] == 5000.0)

    section("malformed input")
    check("non-dict policy body is accepted and normalised",
          client.put("/api/assets/policy", json={"policy": "8"}).status_code == 200)
    check("bare body is read as the policy itself",
          client.put("/api/assets/policy",
                     json={"max_single_name_pct": 12}).get_json()
          ["policy"]["max_single_name_pct"] == 12.0)
    check("out-of-range limit is dropped, not stored",
          client.put("/api/assets/policy",
                     json={"policy": {"max_single_name_pct": 500}}).get_json()
          ["policy"]["max_single_name_pct"] is None)

    print("\n" + "=" * 62)
    print(f"  {_ok} passed, {_fail} failed")
    return 1 if _fail else 0


if __name__ == "__main__":
    raise SystemExit(main())
