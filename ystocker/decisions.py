"""
ystocker.decisions
~~~~~~~~~~~~~~~~~~
The decision ledger: one row per finished ``/agents`` run, and what happened next.

Why this is a store and not a report
------------------------------------
Every other claim about the agent pipeline in this repo is recomputable. This one
is not. A row records what the system decided *on a particular day, from a
particular set of inputs, with a particular model* — and none of that can be
reconstructed later, because the model changes, the news moves, and the prompt is
not kept. So this is an **observed series**, on the same terms as
``ystocker-valuation-history`` and the four others: lose the row and it is gone.

It exists because it is the only thing that can settle whether any of the pipeline
work was worth doing. The portfolio context, the look-through bands, the risk gate,
the numeric levels — each is defensible on its own reasoning and none of them has
been shown to improve a decision. A ledger cannot prove causation either, but it is
the difference between "we believe this helped" and a measurement.

What is deliberately NOT recorded
---------------------------------
**The user.** A row is joinable back to ``ystocker-agent-jobs`` by ``job_id``, so
an operator who needs the owner can get it; keeping the address out of here means
the ledger is not itself user data and can be scanned, aggregated and exported for
analysis without a privacy question attached to every query. ``assets.build_ai_prompt``
makes the same call for the same reason.

**The report.** It is up to ~57 KB and already stored on the job. A ledger row is a
few hundred bytes and is meant to be read a thousand at a time.

**Any derived score.** No Sharpe, no hit rate, no alpha. Those are one query away
from the raw rows and they bake in choices — which benchmark, which horizon, how to
treat an unsettled row — that belong to whoever is asking, not to the writer. What
*is* stored is the benchmark's return alongside the instrument's, so an excess
return is derivable without this module having picked a definition.

Key schema
----------
``date`` (HASH, S) + ``job_id`` (RANGE, S).

The brief for this said "keyed by ``{date}#{job_id}``", and a single composite hash
key would satisfy that literally. It is not what is used, for one reason: the
settlement job below has to ask "which rows are old enough to settle", which under
a composite key is a full table Scan, and ``CLAUDE.md`` already records that on
``PAY_PER_REQUEST`` a scan is billed by volume scanned — the note attached to
``/api/fedwatch/history``. With ``date`` as the partition key that question is a
Query per day. It also keeps the shape of the other five observed-series tables,
all of which have a queryable ``date``.

Numbers are stored as **strings**, mirroring ``fedwatch._item_from_row``: DynamoDB
rejects ``float``, and the alternatives in this repo are ``Decimal(str(...))``,
which forces a rounding decision at the write, or a JSON blob, which
``portfolio.py`` uses because a position list has no fixed columns. A ledger does
have fixed columns and wants them queryable, so strings it is. DynamoDB and the
disk mirror hold the *same* shape and one parser reads both — the trap
``fedwatch._item_from_row`` documents, where an asymmetric pair silently lost a
field on every fallback read.

Create the table by hand, matching the other five (not in
``deploy/cloudformation.yaml``: CloudFormation cannot adopt a live table without an
import operation, so adding it would break the next ``--full`` deploy rather than
converge it; IAM already grants ``table/ystocker-*``)::

    aws dynamodb create-table --table-name ystocker-agent-decisions \\
      --region us-west-2 --billing-mode PAY_PER_REQUEST \\
      --attribute-definitions AttributeName=date,AttributeType=S \\
                              AttributeName=job_id,AttributeType=S \\
      --key-schema AttributeName=date,KeyType=HASH \\
                   AttributeName=job_id,KeyType=RANGE

No TTL. The whole value of the series is that it gets longer.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)

TABLE_NAME = (os.environ.get("AGENT_DECISIONS_TABLE",
                             "ystocker-agent-decisions").strip()
              or "ystocker-agent-decisions")
REGION = (os.environ.get("AWS_REGION") or "us-west-2").strip()

#: Disk mirror. Unlike ``portfolio.py`` this *is* allowed to be the only copy for a
#: while: a lost ledger row costs a data point in an analysis, not somebody's
#: portfolio, so the cache-module convention (degrade to disk, keep going) is the
#: right one here and the fail-closed convention is not.
LOCAL_PATH = Path(__file__).parent.parent / "cache" / "agent_decisions.json"

#: Forward-return horizons, in **trading** days. Calendar days would put a
#: Friday decision's "1 day" on a Saturday, and the 60-day window across a month
#: of holidays would be a different length for every row.
HORIZONS = (1, 5, 20, 60)

#: What an unsettled horizon looks like. Absent, never zero — a horizon that has
#: not matured and one that returned exactly nothing are different facts, and the
#: second is vanishingly rare while the first is most of the table on any given day.
_RET_PREFIX = "ret_"
_BENCH_PREFIX = "bench_"

#: The benchmark whose return travels with every row so an excess return is
#: derivable. Stored per row rather than looked up at analysis time because a row
#: has to stay interpretable after the benchmark's own history is restated.
BENCHMARK = os.environ.get("AGENT_DECISIONS_BENCHMARK", "SPY").strip() or "SPY"

_ET = ZoneInfo("America/New_York")

#: US equity close, in exchange-local time. Compared against in ET rather than
#: against a fixed UTC hour, which is wrong for the several weeks a year when the
#: US and the reader's assumptions disagree about daylight saving.
_CLOSE_HOUR_ET = 16

_table = None
_table_lock = threading.Lock()
_local_lock = threading.Lock()


# ---------------------------------------------------------------------------
# The row
# ---------------------------------------------------------------------------

#: Numeric columns, stored as strings. Named explicitly rather than inferred from
#: the value's type: a field that happened to be integral on the first run would
#: otherwise be stored as a number and then as a string on the next, and the
#: parser would have to handle both.
_NUMERIC = (
    "elapsed_sec",
    "pm_size_pct", "pm_entry", "pm_stop", "pm_target",
    "trader_size_pct", "trader_entry", "trader_stop", "trader_target",
    "trader_reward_risk",
    "gate_proposed_pct", "gate_approved_pct",
    "ref_close",
) + tuple(f"{_RET_PREFIX}{h}d" for h in HORIZONS) \
  + tuple(f"{_BENCH_PREFIX}{h}d" for h in HORIZONS)

_STRINGS = (
    "date", "job_id", "ticker", "trade_date", "finished_at",
    "rating", "decision", "trader_action",
    "gate_verdict", "compliance_status",
    "ref_date", "benchmark",
    "models", "analysts", "settled_at",
)

_BOOLS = ("degraded", "recovered", "portfolio_context", "gate_violation")


def build_row(job: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """One ledger row from a finished job, or None if the job cannot be recorded.

    Pure: no I/O, no clock beyond what the job already carries. That is what lets
    the shape be pinned by tests without a table.

    Returns None for a run that is not ``done`` or has no id — there is nothing to
    settle a return against, and a half row would be worse than an absent one
    because an analysis counting rows would include it.
    """
    job_id = str(job.get("id") or "").strip()
    if not job_id or job.get("status") != "done":
        return None

    finished = str(job.get("finished_at") or "").strip()
    # The ledger's own date is the day the decision *existed*, not the trade date
    # the analysis was written for. They differ whenever a run is queued overnight,
    # and the one a forward return must be measured from is the former.
    row_date = finished[:10] if finished else str(job.get("date") or "")[:10]
    if not row_date:
        return None

    pm = dict(job.get("pm_levels") or {})
    tr = dict(job.get("trader_levels") or {})
    gate = dict(job.get("risk_gate") or {})
    comp = dict(job.get("gate_compliance") or {})

    row: dict[str, Any] = {
        "date": row_date,
        "job_id": job_id,
        "ticker": str(job.get("ticker") or "").upper(),
        "trade_date": str(job.get("date") or ""),
        "finished_at": finished,
        "decision": str(job.get("decision") or ""),
        "benchmark": BENCHMARK,

        "rating": str(pm.get("rating") or ""),
        "pm_size_pct": pm.get("position_size_pct"),
        "pm_entry": pm.get("entry_price"),
        "pm_stop": pm.get("stop_loss"),
        "pm_target": pm.get("price_target"),

        "trader_action": str(tr.get("action") or ""),
        "trader_size_pct": tr.get("position_size_pct"),
        "trader_entry": tr.get("entry_price"),
        "trader_stop": tr.get("stop_loss"),
        "trader_target": tr.get("target_price"),
        "trader_reward_risk": tr.get("reward_risk"),

        "gate_verdict": str(gate.get("verdict") or ""),
        "gate_proposed_pct": gate.get("proposed_size_pct"),
        "gate_approved_pct": gate.get("approved_size_pct"),
        "compliance_status": str(comp.get("status") or ""),
        "gate_violation": bool(comp.get("violated")),

        # Provenance. Which model actually answered matters more than which was
        # configured: a run that fell back to a weaker model mid-debate is not
        # evidence about the configured one, and ``agents._fallback_models_used``
        # already scrapes that from stderr for exactly this reason.
        "models": ",".join(job.get("fallback_models") or []),
        "degraded": bool(job.get("degraded")),
        "recovered": bool(job.get("recovered")),
        "portfolio_context": bool(job.get("portfolio_context")),
        "elapsed_sec": job.get("elapsed_sec"),
    }
    return {k: v for k, v in row.items() if v is not None and v != ""}


def _item_from_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """Canonical stored form — the exact inverse of :func:`_row_from_item`.

    Symmetric by construction, and the disk mirror holds the same shape. The
    asymmetric version of this in an earlier module silently dropped a field on
    every fallback read; see ``fedwatch._item_from_row``.
    """
    item: dict[str, Any] = {}
    for key in _STRINGS:
        value = row.get(key)
        if value not in (None, ""):
            item[key] = str(value)
    for key in _NUMERIC:
        value = row.get(key)
        if value is not None:
            item[key] = str(value)          # DynamoDB rejects float
    for key in _BOOLS:
        if key in row:
            item[key] = bool(row[key])
    return item


def _row_from_item(item: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _STRINGS:
        if item.get(key) is not None:
            out[key] = str(item[key])
    for key in _NUMERIC:
        if item.get(key) is None:
            continue
        try:
            out[key] = float(item[key])
        except (TypeError, ValueError):
            # A single unparseable cell must not void the row: the rest of it is
            # still a real observation and an analysis can skip one column.
            log.debug("decisions: unparseable %s on %s/%s", key,
                      item.get("date"), item.get("job_id"))
    for key in _BOOLS:
        if key in item:
            out[key] = bool(item[key])
    return out


# ---------------------------------------------------------------------------
# Forward returns — pure, so the arithmetic is testable with no network
# ---------------------------------------------------------------------------

def reference_date(finished_at: str, sessions: Sequence[str]) -> Optional[str]:
    """The first session whose close was still ahead when the decision was made.

    This is the price a decision could actually have been acted on, and getting it
    wrong in the obvious direction is lookahead: measuring from the close of the
    day the analysis was *for* credits the system with a move that had already
    printed before it spoke.

    A run that finishes before 16:00 in New York can trade that session's close; one
    that finishes after it cannot, and the first available close is the next
    session. Compared in exchange-local time on purpose — a fixed UTC hour is wrong
    for the several weeks a year when US daylight saving has shifted and the
    reader's mental model has not.

    ``sessions`` is the instrument's own trading dates, ascending, as ``YYYY-MM-DD``.
    Using the instrument's sessions rather than a calendar means a holiday, a
    half-day or a suspension is handled without a market-calendar dependency.
    """
    if not finished_at or not sessions:
        return None
    try:
        stamp = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    local = stamp.astimezone(_ET)
    day = local.date().isoformat()
    after_close = local.hour >= _CLOSE_HOUR_ET

    for session in sessions:
        if session > day or (session == day and not after_close):
            return session
    return None


def forward_returns(closes: Mapping[str, float], ref_date: str,
                    horizons: Iterable[int] = HORIZONS,
                    ) -> dict[int, Optional[float]]:
    """Return from *ref_date*'s close to the close *n* sessions later, per horizon.

    ``None`` for a horizon that has not matured — the sessions simply do not exist
    yet. Absent, never zero: a horizon that has not happened and one that moved
    nothing are different facts, and the second is rare while the first is most of
    a live table.

    Horizons count **sessions**, not calendar days, and the sessions come from the
    instrument's own price history. A calendar-day horizon would put a Friday
    decision's "1 day" on a Saturday and would make the 60-day window a different
    length for every row depending on which holidays it spanned.
    """
    sessions = sorted(closes)
    try:
        start = sessions.index(ref_date)
    except ValueError:
        return {int(h): None for h in horizons}
    base = closes.get(ref_date)
    out: dict[int, Optional[float]] = {}
    for h in horizons:
        h = int(h)
        idx = start + h
        if not base or idx >= len(sessions):
            out[h] = None
            continue
        end = closes.get(sessions[idx])
        out[h] = round(end / base - 1.0, 6) if end and base else None
    return out


def settle_row(row: Mapping[str, Any],
               closes: Mapping[str, float],
               bench_closes: Optional[Mapping[str, float]] = None,
               ) -> dict[str, Any]:
    """A copy of *row* with whatever forward returns are now available filled in.

    Idempotent, and it never overwrites a settled horizon. A price series can be
    restated — a split, a dividend adjustment, a vendor correction — and a ledger
    that re-derived every return on every pass would silently rewrite history. The
    first settlement of a horizon is the one kept, and the row records when.

    The benchmark's return over the same sessions travels alongside rather than
    being subtracted here, so an excess return is derivable without this module
    having chosen a definition of one.
    """
    out = dict(row)
    ref = out.get("ref_date") or reference_date(str(out.get("finished_at") or ""),
                                               sorted(closes))
    if not ref:
        return out
    out["ref_date"] = ref
    if out.get("ref_close") is None and closes.get(ref) is not None:
        out["ref_close"] = float(closes[ref])

    filled = False
    for horizon, value in forward_returns(closes, ref).items():
        key = f"{_RET_PREFIX}{horizon}d"
        if value is not None and out.get(key) is None:
            out[key] = value
            filled = True

    if bench_closes:
        # The benchmark is measured over the *instrument's* sessions, from the same
        # reference date, so the two are comparable even when the instrument has a
        # session the benchmark does not (a foreign listing, a half day).
        for horizon, value in forward_returns(bench_closes, ref).items():
            key = f"{_BENCH_PREFIX}{horizon}d"
            if value is not None and out.get(key) is None:
                out[key] = value
                filled = True

    if filled:
        out["settled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return out


def is_fully_settled(row: Mapping[str, Any]) -> bool:
    """Whether every horizon has a return, so the row need never be revisited."""
    return all(row.get(f"{_RET_PREFIX}{h}d") is not None for h in HORIZONS)


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def _get_table():
    """The ledger table, or None. Absence degrades to disk — see the docstring."""
    global _table
    if _table is not None:
        return _table
    with _table_lock:
        if _table is not None:
            return _table
        try:
            import boto3

            table = boto3.resource("dynamodb", region_name=REGION).Table(TABLE_NAME)
            table.load()
            _table = table
            log.info("decisions: DynamoDB connected: %s", TABLE_NAME)
        except Exception as exc:  # noqa: BLE001 - a cache-style degrade is correct here
            log.warning("decisions: DynamoDB unavailable (%s)", exc)
            return None
    return _table


def _local_read() -> dict[str, Any]:
    try:
        data = json.loads(LOCAL_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:  # noqa: BLE001
        log.warning("decisions: unreadable local ledger (%s)", exc)
        return {}


def _local_write(data: Mapping[str, Any]) -> None:
    LOCAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(LOCAL_PATH.parent), suffix=".tmp")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(dict(data), fh, indent=2, sort_keys=True)
    os.replace(tmp, LOCAL_PATH)


def _local_key(row: Mapping[str, Any]) -> str:
    return f"{row.get('date')}#{row.get('job_id')}"


def record(job: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    """Write one finished run to the ledger. Returns the row, or None.

    Never raises. A ledger write failing must not turn a finished, paid-for analysis
    into an error — the report is the deliverable and this is bookkeeping about it.
    That is the opposite of ``portfolio.save``'s fail-closed posture, and the
    difference is what the caller loses: a portfolio, or a row in a table used for
    analysis later.

    Same-key writes overwrite, so a re-``_reap`` of the same job converges rather
    than accumulating — the reason ``fedwatch.record_snapshot`` and
    ``cta.record_observation`` do the same, and the reason this needs no
    deduplication of its own.
    """
    try:
        row = build_row(job)
    except Exception as exc:  # noqa: BLE001
        log.warning("decisions: could not build a row for %s: %s",
                    (job or {}).get("id"), exc)
        return None
    if row is None:
        return None

    item = _item_from_row(row)
    table = _get_table()
    if table is not None:
        try:
            table.put_item(Item=item)
        except Exception as exc:  # noqa: BLE001
            log.warning("decisions: DynamoDB write failed for %s: %s",
                        row["job_id"], exc)
    with _local_lock:
        data = _local_read()
        data[_local_key(row)] = item
        try:
            _local_write(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("decisions: local ledger write failed: %s", exc)
    log.info("decisions: recorded %s %s (%s)", row["date"], row["ticker"],
             row.get("rating") or row.get("decision") or "?")
    return row


def unsettled(before: Optional[str] = None,
              lookback_days: int = 120) -> list[dict[str, Any]]:
    """Rows still missing at least one forward return, oldest first.

    Queried day by day rather than scanned. ``CLAUDE.md`` records what an unbounded
    scan costs on ``PAY_PER_REQUEST`` — it is the note attached to
    ``/api/fedwatch/history`` — and the settlement job runs daily forever, so a
    scan here would be the most expensive query in the app by a wide margin.

    ``lookback_days`` bounds the walk. The longest horizon is 60 sessions, roughly
    84 calendar days, so 120 covers it with room for a run that sat unsettled while
    the table was unreachable. Anything older is left alone deliberately and the
    count is logged rather than silently dropped.
    """
    end = _date.fromisoformat(before) if before else datetime.now(timezone.utc).date()
    days = [(end - timedelta(days=n)).isoformat() for n in range(lookback_days)]

    table = _get_table()
    out: list[dict[str, Any]] = []
    if table is not None:
        from boto3.dynamodb.conditions import Key

        for day in days:
            try:
                resp = table.query(KeyConditionExpression=Key("date").eq(day))
            except Exception as exc:  # noqa: BLE001
                log.warning("decisions: query failed for %s: %s", day, exc)
                continue
            for item in resp.get("Items") or []:
                row = _row_from_item(item)
                if not is_fully_settled(row):
                    out.append(row)
    else:
        with _local_lock:
            data = _local_read()
        wanted = set(days)
        for item in data.values():
            row = _row_from_item(item)
            if row.get("date") in wanted and not is_fully_settled(row):
                out.append(row)

    out.sort(key=lambda r: (r.get("date") or "", r.get("job_id") or ""))
    return out


def save_settled(row: Mapping[str, Any]) -> None:
    """Persist a row after settlement. Overwrites its own key."""
    item = _item_from_row(row)
    table = _get_table()
    if table is not None:
        try:
            table.put_item(Item=item)
        except Exception as exc:  # noqa: BLE001
            log.warning("decisions: settle write failed for %s: %s",
                        row.get("job_id"), exc)
    with _local_lock:
        data = _local_read()
        data[_local_key(row)] = item
        try:
            _local_write(data)
        except Exception as exc:  # noqa: BLE001
            log.warning("decisions: local settle write failed: %s", exc)


def history(limit: Optional[int] = None,
            lookback_days: int = 400) -> list[dict[str, Any]]:
    """The ledger, newest first. For analysis, not for a request handler.

    Deliberately has no ``history_cached`` sibling: nothing public reads this, and
    the moment something does it will need the caching note
    ``fedwatch.history_cached`` carries.
    """
    end = datetime.now(timezone.utc).date()
    days = [(end - timedelta(days=n)).isoformat() for n in range(lookback_days)]
    table = _get_table()
    rows: list[dict[str, Any]] = []
    if table is not None:
        from boto3.dynamodb.conditions import Key

        for day in days:
            try:
                resp = table.query(KeyConditionExpression=Key("date").eq(day))
            except Exception as exc:  # noqa: BLE001
                log.warning("decisions: query failed for %s: %s", day, exc)
                continue
            rows.extend(_row_from_item(i) for i in (resp.get("Items") or []))
            if limit and len(rows) >= limit:
                break
    else:
        with _local_lock:
            data = _local_read()
        rows = [_row_from_item(i) for i in data.values()]

    rows.sort(key=lambda r: (r.get("date") or "", r.get("job_id") or ""),
              reverse=True)
    return rows[:limit] if limit else rows
