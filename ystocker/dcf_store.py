"""
ystocker.dcf_store
~~~~~~~~~~~~~~~~~~
Hand-entered DCF inputs, per ticker — the override half of the DCF branch.

:mod:`ystocker.dcf` derives Bear/Base/Bull fair values from the statements
:mod:`ystocker.dca_history` already fetches, which is what makes the absolute
anchor available for the whole ranked universe on day one. A derived DCF is
still a machine extrapolating a cash-flow series, though, and the framework's
§2 describes an analyst choosing a WACC, a steady-state margin and a
reinvestment path. This is where that judgement goes when somebody has actually
done the work on a name.

Two override modes, and they are deliberately different things
--------------------------------------------------------------
**Fair values** (``bear`` / ``base`` / ``bull``) replace the model's output. The
stored numbers go straight to :func:`ystocker.dcf.score`, the same function the
derived path ends at, so an override and a derivation cannot disagree about how
an upside becomes a 0-100 score — only about what the fair value is.

**Assumptions** (``wacc`` / ``terminal_growth``) replace the model's *inputs* and
let it run. This is the cheaper and usually better override: the cash-flow
history is not in dispute, the discount rate is.

Supplying both takes the fair values, because they are the more specific claim.
That precedence is recorded on the row as ``mode`` rather than inferred at read
time, so a reader can see which one is in force.

``w_dcf`` is stored alongside and is the only implementation of §4's dynamic
weight rule — *"将 w_DCF 下调 5-15 个百分点，并按比例重分配给原 relative
factors"*. Nothing can derive that automatically: it is a judgement that this
particular DCF deserves less influence, which is exactly the kind of thing a
person supplies and a model cannot.

Why this degrades instead of failing closed
-------------------------------------------
:mod:`ystocker.portfolio` raises when its table is unreachable, because
returning ``[]`` renders as "you have no positions" on the page whose job is to
show them. The opposite is true here. A missing override means the derived DCF
runs, which is a complete and correct answer rather than a misleading empty one
— and the payload carries ``source`` so the page states which of the two
produced the number. So this falls back to the disk mirror and then to nothing,
matching :mod:`ystocker.dca_universe`.

Writes are gated
----------------
``/dca`` is **public**. An override changes the score every visitor sees, so the
route behind this checks :func:`ystocker.quota.is_vip` before calling
:func:`put`. That gate lives in ``routes.py`` rather than here for the reason
every other authorization check in this codebase does: a store that consults the
session is a store that cannot be tested without one.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Optional

log = logging.getLogger(__name__)

__all__ = [
    "TABLE_NAME", "MIRROR_PATH", "FIELDS", "MODE_VALUES", "MODE_ASSUMPTIONS",
    "ValidationError", "validate", "get", "put", "delete", "all_rows", "stats",
]

TABLE_NAME = os.environ.get("DCA_DCF_TABLE", "ystocker-dca-dcf").strip()

#: On-disk mirror, so local dev works with no credentials and a briefly
#: unreachable table does not lose a row somebody just typed.
MIRROR_PATH = Path(__file__).parent.parent / "cache" / "dca_dcf.json"

#: Numeric fields a caller may set, and the band each must fall in. Anything
#: outside is refused rather than clamped: a clamped fair value is a number the
#: reader did not enter, sitting on a page that says they did.
FIELDS: dict[str, tuple[float, float]] = {
    "bear":            (0.0, 1e7),
    "base":            (0.0, 1e7),
    "bull":            (0.0, 1e7),
    "wacc":            (0.01, 0.40),
    "terminal_growth": (-0.02, 0.06),
    "confidence":      (0.5, 1.0),
    "w_dcf":           (0.0, 0.30),
}

MODE_VALUES = "fair_values"
MODE_ASSUMPTIONS = "assumptions"

#: Longest a note may be. Stored verbatim and rendered escaped; the cap is about
#: row size, not safety.
MAX_NOTE = 500


class ValidationError(ValueError):
    """A rejected override. The message is shown to the person who typed it."""


_lock = threading.Lock()
_table = None
_table_unavail_until = 0.0


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def _num(value: Any) -> Optional[float]:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and abs(out) != float("inf") else None


def _factor_values(value: Any) -> dict[str, float]:
    """Validate a ``{factor: current multiple}`` map, or raise.

    **This is the override to reach for.** It replaces the *"Now"* column — the
    multiple itself, 31.23 rather than "the 11th percentile" — and the engine
    then ranks it against that factor's own reconstructed history exactly as it
    ranks a measured one. So the rank stays *measured*: only the input is
    hand-set, and the distribution it is judged against is untouched.

    That is a materially weaker claim than :func:`_factor_percentiles`, which
    asserts the rank outright. A person knows what a P/E is; almost nobody knows
    where it sits in five years of weekly history, and the engine does. Prefer
    this wherever a series exists.

    Values must be positive: every factor here is a price-to-something multiple
    or a yield, and a negative one is the absence of a measurement rather than a
    cheap reading — which is the same rule :func:`ystocker.dca_history.reconstruct`
    applies when it skips rather than clamps a negative denominator.
    """
    if value in (None, "", {}):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValidationError("Factor values must be a JSON object.") from exc
    if not isinstance(value, Mapping):
        raise ValidationError("Factor values must be a JSON object.")

    from ystocker.dca import DIRECTION

    out: dict[str, float] = {}
    for key, raw in value.items():
        factor = str(key).strip()
        if factor not in DIRECTION:
            raise ValidationError(
                f"Unknown factor {factor!r}. Known factors: "
                f"{', '.join(sorted(DIRECTION))}.")
        num = _num(raw)
        if num is None:
            continue
        if num <= 0:
            raise ValidationError(
                f"{factor} must be a positive multiple (got {num:g}). "
                "A negative reading is an absent measurement, not a cheap one.")
        out[factor] = round(num, 4)
    return out


def _factor_percentiles(value: Any) -> dict[str, float]:
    """Validate a ``{factor: percentile}`` map, or raise.

    These are **relative-branch** overrides and they exist for a failure the DCF
    override cannot reach. A factor goes unmeasurable when its *series* could not
    be reconstructed at all — negative EPS leaves no P/E history, a young listing
    leaves too few vintages — and when enough of them go, the surviving weight
    drops under :data:`ystocker.dca.MIN_SURVIVING_WEIGHT` and the whole ticker
    refuses to score. Observed on a semiconductor template with only P/FCF and
    EV/EBITDA left: 30% surviving against a 50% floor, so five factors produced
    no number at all.

    A **percentile** is the unit, not the multiple, and that is forced rather
    than chosen: when there is no reconstructed history there is nothing to rank
    a hand-typed multiple against, so a "current P/E" would have nowhere to go.
    The percentile is the rank itself — the thing the engine actually consumes.

    Keys are checked against :data:`ystocker.dca.DIRECTION` so a typo cannot
    become a silently ignored field, and values must be a real 0-100: a
    percentile outside that range is not a strong opinion, it is a mistake.
    """
    if value in (None, "", {}):
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError as exc:
            raise ValidationError("Factor overrides must be a JSON object.") from exc
    if not isinstance(value, Mapping):
        raise ValidationError("Factor overrides must be a JSON object.")

    from ystocker.dca import DIRECTION

    out: dict[str, float] = {}
    for key, raw in value.items():
        factor = str(key).strip()
        if factor not in DIRECTION:
            raise ValidationError(
                f"Unknown factor {factor!r}. Known factors: "
                f"{', '.join(sorted(DIRECTION))}.")
        pct = _num(raw)
        if pct is None:
            continue          # blanking a field clears it, same as the numerics
        if not 0.0 <= pct <= 100.0:
            raise ValidationError(
                f"{factor} percentile must be between 0 and 100 (got {pct:g}).")
        out[factor] = round(pct, 2)
    return out


def validate(ticker: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Turn a submitted form into a storable row, or raise :class:`ValidationError`.

    Pure, so the rules are testable without AWS or a request.

    The ordering check is the one worth having. ``bear > bull`` is almost always
    two fields typed into the wrong boxes, and it is not caught downstream by
    anything: :func:`ystocker.dcf.combine_scenarios` would happily weight them
    25/50/25 and return a score that is merely wrong rather than obviously so.

    A base case is required for the fair-value mode — §3 maps ``V_Base`` and
    shrinks toward it, so there is no scenario set without one. Wings are
    optional and a lone wing is dropped rather than half-used, matching
    ``combine_scenarios``.
    """
    symbol = (ticker or "").strip().upper()
    if not symbol or len(symbol) > 24:
        raise ValidationError("A ticker is required.")

    row: dict[str, Any] = {"ticker": symbol}
    for field, (low, high) in FIELDS.items():
        value = _num(payload.get(field))
        if value is None:
            continue
        if not low <= value <= high:
            raise ValidationError(
                f"{field} must be between {low:g} and {high:g} (got {value:g}).")
        row[field] = value

    has_values = "base" in row
    if ("bear" in row or "bull" in row) and not has_values:
        raise ValidationError(
            "A base case is required whenever a bear or bull case is given.")

    if has_values:
        bear, base, bull = row.get("bear"), row["base"], row.get("bull")
        if bear is not None and bull is not None and bear > bull:
            raise ValidationError("The bear case cannot exceed the bull case.")
        if bear is not None and bear > base:
            raise ValidationError("The bear case cannot exceed the base case.")
        if bull is not None and bull < base:
            raise ValidationError("The bull case cannot be below the base case.")

    has_assumptions = "wacc" in row or "terminal_growth" in row
    values = _factor_values(payload.get("values"))
    if values:
        row["values"] = values
    factors = _factor_percentiles(payload.get("factors"))
    if factors:
        row["factors"] = factors
    # A factor set both ways is a contradiction rather than a belt-and-braces:
    # the value would be re-ranked to one percentile and the percentile would
    # assert another, and whichever the code happened to apply second would win
    # silently. Refused while the person who typed both is still looking.
    both = sorted(set(values) & set(factors))
    if both:
        raise ValidationError(
            f"Give a value or a percentile for {', '.join(both)}, not both. "
            "A value is re-ranked against the real history; a percentile "
            "replaces the rank outright.")
    if (not has_values and not has_assumptions and "w_dcf" not in row
            and not factors and not values):
        raise ValidationError(
            "Nothing to store: give fair values, assumptions, a DCF weight, "
            "a factor value, or a factor percentile.")

    # §13 refuses a valuation whose WACC is not meaningfully above g. Caught
    # here as well as in `dcf`, because an override that can never score is
    # better rejected while the person who typed it is still looking at it.
    if row.get("wacc") is not None:
        from ystocker import dcf as dcf_mod

        g = row.get("terminal_growth", dcf_mod.TERMINAL_GROWTH)
        if row["wacc"] - g < dcf_mod.MIN_WACC_SPREAD:
            raise ValidationError(
                f"WACC must exceed terminal growth by at least "
                f"{dcf_mod.MIN_WACC_SPREAD:.1%}.")

    stamp = payload.get("valuation_date")
    row["valuation_date"] = _as_iso(stamp) or date.today().isoformat()
    row["mode"] = MODE_VALUES if has_values else MODE_ASSUMPTIONS

    note = str(payload.get("note") or "").strip()
    if note:
        row["note"] = note[:MAX_NOTE]
    author = str(payload.get("author") or "").strip().lower()
    if author:
        row["author"] = author[:200]
    row["updated_at"] = time.time()
    return row


def _as_iso(value: Any) -> Optional[str]:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10]).isoformat()
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# DynamoDB, with a disk mirror behind it
# ---------------------------------------------------------------------------

def _get_table():
    """The table, or ``None``. Degrades rather than raising — see the module
    docstring on why this is the opposite choice from ``portfolio``."""
    global _table, _table_unavail_until
    if _table is not None:
        return _table
    if time.time() < _table_unavail_until:
        return None
    with _lock:
        if _table is not None:
            return _table
        if time.time() < _table_unavail_until:
            return None
        try:
            import boto3

            ddb = boto3.resource(
                "dynamodb", region_name=os.environ.get("AWS_REGION", "us-west-2"))
            tbl = ddb.Table(TABLE_NAME)
            tbl.load()
            _table = tbl
            log.info("dcf_store: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001
            log.info("dcf_store: DynamoDB unavailable: %s", exc)
            _table = None
            _table_unavail_until = time.time() + 300
        return _table


def _read_mirror() -> dict[str, dict[str, Any]]:
    try:
        if not MIRROR_PATH.exists():
            return {}
        data = json.loads(MIRROR_PATH.read_text())
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.debug("dcf_store: unreadable mirror: %s", exc)
        return {}


def _write_mirror(rows: Mapping[str, Any]) -> None:
    """Atomic temp-file + replace, matching every other writer here."""
    try:
        MIRROR_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock:
            fd, tmp = tempfile.mkstemp(dir=str(MIRROR_PATH.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(rows, handle)
                os.replace(tmp, MIRROR_PATH)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    except Exception as exc:  # noqa: BLE001 - a mirror write must not fail a page
        log.warning("dcf_store: could not persist mirror: %s", exc)


def _ddb_rows() -> dict[str, dict[str, Any]]:
    """Every override from DynamoDB. A Scan, bounded by how few of these exist —
    one per name somebody has actually modelled by hand."""
    table = _get_table()
    if table is None:
        return {}
    try:
        out: dict[str, dict[str, Any]] = {}
        kwargs: dict[str, Any] = {}
        while True:
            resp = table.scan(**kwargs)
            for item in resp.get("Items", []):
                symbol = str(item.get("ticker") or "").upper()
                if not symbol:
                    continue
                row: dict[str, Any] = {"ticker": symbol}
                for field in FIELDS:
                    value = _num(item.get(field))
                    if value is not None:
                        row[field] = value
                for field in ("valuation_date", "mode", "note", "author"):
                    if item.get(field):
                        row[field] = str(item[field])
                # `factors` is a map, and everything else here goes to DynamoDB
                # as a string — so it round-trips as JSON text rather than as a
                # second storage convention in the same row.
                for mapfield in ("factors", "values"):
                    if not item.get(mapfield):
                        continue
                    try:
                        parsed = json.loads(str(item[mapfield]))
                        if isinstance(parsed, dict):
                            row[mapfield] = {
                                k: v for k, v in parsed.items()
                                if isinstance(v, (int, float))}
                    except ValueError:
                        log.warning("dcf_store: unreadable %s for %s",
                                    mapfield, symbol)
                row["updated_at"] = _num(item.get("updated_at")) or 0.0
                out[symbol] = row
            if "LastEvaluatedKey" not in resp:
                break
            kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("dcf_store: scan failed: %s", exc)
        return {}


def all_rows() -> dict[str, dict[str, Any]]:
    """Every override, DynamoDB unioned over the disk mirror.

    Unioned rather than one preferred, matching ``valuation._previous_snapshots``
    and ``dca_universe``: the mirror can hold a row written while DynamoDB was
    briefly unreachable, and DynamoDB holds everything that predates this box.
    The **newer** ``updated_at`` wins a collision, so a row edited on the live
    table is not shadowed by a stale local copy.
    """
    merged = dict(_read_mirror())
    for symbol, row in _ddb_rows().items():
        existing = merged.get(symbol)
        if existing is None or (row.get("updated_at") or 0) >= (existing.get("updated_at") or 0):
            merged[symbol] = row
    return merged


def get(ticker: str) -> Optional[dict[str, Any]]:
    """One ticker's override, or ``None``. Never raises."""
    symbol = (ticker or "").strip().upper()
    if not symbol:
        return None
    try:
        return all_rows().get(symbol)
    except Exception as exc:  # noqa: BLE001 - an override is a convenience
        log.info("dcf_store: lookup failed for %s: %s", symbol, exc)
        return None


def put(row: Mapping[str, Any]) -> dict[str, Any]:
    """Store a validated row. Writes the mirror even when DynamoDB is absent.

    The mirror is written **first** and unconditionally, which is the opposite
    of ``share.create()``'s ordering and for the opposite reason: nothing here
    hands out a link that has to resolve, so the failure that matters is losing
    input somebody typed rather than publishing a dead reference.
    """
    symbol = str(row["ticker"]).upper()
    rows = dict(_read_mirror())
    rows[symbol] = dict(row)
    _write_mirror(rows)

    table = _get_table()
    if table is not None:
        try:
            item: dict[str, Any] = {"ticker": symbol}
            for key, value in row.items():
                if key == "ticker" or value is None:
                    continue
                if isinstance(value, Mapping):
                    # `factors`. json.dumps rather than str(): str() on a dict
                    # emits single quotes, which json.loads then refuses on the
                    # way back — the row would store and silently fail to read.
                    item[key] = json.dumps(dict(value), sort_keys=True)
                    continue
                # DynamoDB rejects float; every numeric here goes as a string
                # and comes back through _num, matching dca_history.save_row.
                item[key] = str(value) if isinstance(value, (int, float)) else str(value)
            table.put_item(Item=item)
        except Exception as exc:  # noqa: BLE001
            log.warning("dcf_store: save failed for %s: %s", symbol, exc)
    return dict(row)


def delete(ticker: str) -> bool:
    """Remove an override. Returns whether anything was there."""
    symbol = (ticker or "").strip().upper()
    rows = dict(_read_mirror())
    existed = rows.pop(symbol, None) is not None
    _write_mirror(rows)

    table = _get_table()
    if table is not None:
        try:
            table.delete_item(Key={"ticker": symbol})
            existed = True
        except Exception as exc:  # noqa: BLE001
            log.warning("dcf_store: delete failed for %s: %s", symbol, exc)
    return existed


def stats() -> dict[str, Any]:
    """What is stored, for the page to report. Never raises."""
    try:
        rows = all_rows()
    except Exception:  # noqa: BLE001
        return {"count": 0, "tickers": [], "available": False}
    return {
        "count": len(rows),
        "tickers": sorted(rows),
        "available": _get_table() is not None,
    }
