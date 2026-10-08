"""
ystocker.sector_map
~~~~~~~~~~~~~~~~~~~
/sectors: every level of the S&P 500's GICS tree -- 11 sectors, 25 industry
groups and the ~126 sub-industries the index holds today -- placed in four
quadrants by how it has done against the index over a month and over a week,
with the companies behind each one.

Modelled on openbit.trade/market/sectors (asked for 2026-10-07), which sorts
232 sectors and themes into 新启动 / 持续领涨 / 冲高回落 / 持续弱势 by their
5- and 20-day moves. /markets already draws that map for the 25 industry
groups (its rotation map). What it cannot show is the level below, where most
weeks' rotation happens -- independent power producers against the rest of
Utilities, gold miners against the rest of Materials -- or which companies did
the moving.

Method
------
Arithmetic on the frame breadth.py downloads once a day, so it makes no
request of its own. :func:`gics.prepare` supplies the sector table's constant
share counts, end session and base dates, and this groups the same members by
sub-industry too. A group's return over a window is the ratio of its members'
values at the two ends, counting only companies already in the index when the
window starts (gics.py says why: backcasting today's members flatters every
window). So a sector's figure here is exactly the sector table's.

The quadrant comes from two of those figures, both against the index: across,
the month (:data:`TREND`); up, the week (:data:`MOMENTUM`).

* lead -- ahead over the month and the week
* weak -- ahead over the month, behind over the week
* lag -- behind over both
* improve -- behind over the month, ahead over the week

These are the rotation map's quadrants under its names, so a group reads the
same on both pages. The coordinates are the returns themselves. openbit places
each dot by its rank inside its cell, which spreads them evenly and loses the
distance: a group 0.1 points ahead and one 15 points ahead would sit side by
side.

Two figures here have no column in the sector table:

* **Breadth** -- how many members rose and fell on the last session, so a
  group carried by one company reads differently from one that moved together.
* **Dollar volume against its own 20-session average** -- the closest this
  data comes to openbit's 成交/20日均, from the volume column of the same
  download. Close times volume per member, summed per group; a session with
  missing volume leaves that member out of both sums.

A member whose series breaks by more than half, or more than doubles, in one
session (gics.CLIFF_DOWN / CLIFF_UP) is read as a split or spin-off the price
history was not re-based for, and has no figure for any window spanning that
session: CTVA's -84% on 2026-10-01, Corteva's separation, had put its
three-company sub-industry at -56% for the week. Its row says why.

A one-company sub-industry is a stock, not an industry: 26 of the 126 hold a
single company. They are kept, with their count shown, but the page never
names one in its headline sentence (:data:`MIN_HEADLINE_MEMBERS`), or a single
stock's spike would read as money moving into an industry.

The payload is a cache, not an observed series: every figure is recomputable
from prices. It is written to its own file rather than into breadth's, which
/api/breadth serves whole to /markets, and which would otherwise carry 500
companies' returns it never draws.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

from ystocker import gics

log = logging.getLogger(__name__)

CACHE_FILE = Path(__file__).parent.parent / "cache" / "sector_map.json"
#: Bumped when the payload's shape changes; a file of another schema is ignored
#: until the next build rewrites it.
SCHEMA = 1

#: The windows the page offers, in gics.py's labels. 1D, 1W and 1M are
#: openbit's 昨日 / 5日 / 20日; 3M and YTD fill the detail panel.
PERIODS: tuple[str, ...] = ("1D", "1W", "1M", "3M", "YTD")
TREND, MOMENTUM = "1M", "1W"
LEVELS: tuple[str, ...] = ("sector", "group", "sub")

VOLUME_SESSIONS = 20
#: A member needs this many of the 20 sessions with a volume to be averaged.
MIN_VOLUME_SESSIONS = 15
#: Smallest sub-industry the headline sentence may name (see module docstring).
MIN_HEADLINE_MEMBERS = 3
#: A member's index join date is sent only when it falls inside the longest
#: window drawn, so the page can say a figure leaves it out.
NEW_MEMBER_PERIOD = "YTD"


def slug(name: str) -> str:
    """A sub-industry's id: ``"Hotels, Resorts & Cruise Lines"`` ->
    ``"hotels-resorts-cruise-lines"``. GICS gives sub-industries 8-digit codes,
    but the constituent list names them only, so the name is the key."""
    return re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")


def quadrant(trend: Optional[float], momentum: Optional[float]) -> Optional[str]:
    """The rotation map's quadrant for a group, or None when a window is missing."""
    if trend is None or momentum is None:
        return None
    if trend >= 0:
        return "lead" if momentum >= 0 else "weak"
    return "improve" if momentum >= 0 else "lag"


def _r(x: Any, digits: int = 2) -> Optional[float]:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, digits) if math.isfinite(v) else None


def _normalised(frame: Any) -> Any:
    """The same index treatment gics.prepare gives the closes."""
    import pandas as pd

    out = frame.sort_index()
    out = out[~out.index.duplicated(keep="last")]
    out.index = pd.DatetimeIndex(out.index).tz_localize(None).normalize()
    return out


def build(closes: Any, snap: dict[str, Any], volumes: Any = None, *,
          now: Optional[float] = None) -> dict[str, Any]:
    """The /sectors payload from the daily frame. Pure; raises ValueError when
    gics.prepare finds no session to end on.

    ``closes`` and ``volumes`` are breadth.py's adjusted closes and volumes, one
    column per Yahoo symbol; ``volumes`` may be None, which leaves every volume
    figure None. ``now`` decides whether the newest bar is a finished session.
    """
    import pandas as pd

    prep = gics.prepare(closes, snap, now=now)
    members: dict[str, dict[str, Any]] = snap["members"]
    px, q, end, p_end = prep.px, prep.q, prep.end, prep.p_end

    sub_name = pd.Series({t: members[t]["sub"] for t in q.index}, dtype=object)
    group = sub_name.map(gics.group_of)
    keep = group.notna()
    q = q[keep]
    idx = q.index
    sub_name, group = sub_name[idx], group[idx]
    keys = {"sector": group.str[:2], "group": group, "sub": sub_name.map(slug)}

    live = idx[p_end[idx].notna()]
    v_now = q[live] * p_end[live]
    tot_now = float(v_now.sum())
    if tot_now <= 0:
        raise ValueError("no priced member on the end session")

    # Per window: the members counted (in the index at its start, priced at
    # both ends), their values at each end, and every priced member's own move.
    per: dict[str, dict[str, Any]] = {}
    own: dict[str, Any] = {}
    for p in PERIODS:
        d0 = prep.bases.get(p)
        if d0 is None:
            continue
        p0 = px.loc[d0, idx]
        # A member whose series has a cliff in the window (gics.CLIFF_DOWN /
        # CLIFF_UP: a split or spin-off the history was not re-based for) has
        # no figure for it, of its own or towards its group.
        clean = ~prep.spans_cliff(idx, d0)
        priced = p0.notna() & p_end[idx].notna() & clean
        own[p] = (p_end[idx][priced] / p0[priced] - 1) * 100
        was_member = prep.added[idx].isna() | (prep.added[idx] <= d0)
        counted = idx[priced & was_member]
        v0, v1 = q[counted] * p0[counted], q[counted] * p_end[counted]
        t0, t1 = float(v0.sum()), float(v1.sum())
        if t0 <= 0:
            continue
        per[p] = {"t0": t0, "t1": t1, "v0": v0, "v1": v1, "counted": counted}

    # Dollar volume on the end session against the 20 sessions before it.
    vol_now = vol_avg = None
    if volumes is not None:
        try:
            vol = _normalised(volumes.reindex(columns=idx))
            dv = px[idx].reindex(vol.index) * vol
            window = prep.days[prep.days < end][-VOLUME_SESSIONS:]
            if end in dv.index and len(window) >= MIN_VOLUME_SESSIONS:
                hist = dv.loc[window]
                enough = hist.notna().sum() >= MIN_VOLUME_SESSIONS
                avg = hist.mean()
                now_dv = dv.loc[end]
                ok = now_dv.notna() & enough & (avg > 0)
                vol_now, vol_avg = now_dv[ok], avg[ok]
        except (KeyError, ValueError, TypeError) as exc:
            log.warning("sector map: volume figures skipped: %s", exc)
            vol_now = vol_avg = None

    day = own.get("1D")
    day_counted = per["1D"]["counted"] if "1D" in per else idx[:0]
    day_moves = day.reindex(day_counted).dropna() if day is not None else None

    def figures(level: str, key: str, ids: Any) -> dict[str, Any]:
        ret: dict[str, Optional[float]] = {}
        rel: dict[str, Optional[float]] = {}
        for p, blk in per.items():
            mine = blk["counted"][keys[level][blk["counted"]] == key]
            a0 = float(blk["v0"][mine].sum())
            a1 = float(blk["v1"][mine].sum())
            if a0 <= 0:
                ret[p] = rel[p] = None
                continue
            r = a1 / a0 - 1
            ret[p] = _r(r * 100)
            rel[p] = _r((r - (blk["t1"] / blk["t0"] - 1)) * 100)
        out: dict[str, Any] = {"returns": ret, "rel": rel,
                               "quad": quadrant(rel.get(TREND), rel.get(MOMENTUM))}
        moves = day_moves.reindex(ids).dropna() if day_moves is not None else None
        if moves is not None and len(moves):
            out["adv"] = int((moves > 0).sum())
            out["dec"] = int((moves < 0).sum())
            out["flat"] = int((moves == 0).sum())
            best, worst = moves.idxmax(), moves.idxmin()
            out["leader"] = {"t": best, "r": _r(moves[best])}
            out["laggard"] = {"t": worst, "r": _r(moves[worst])}
        else:
            out["adv"] = out["dec"] = out["flat"] = None
            out["leader"] = out["laggard"] = None
        out["vr"] = None
        if vol_now is not None:
            have = [t for t in ids if t in vol_now.index]
            den = float(vol_avg[have].sum()) if have else 0.0
            if den > 0:
                out["vr"] = _r(float(vol_now[have].sum()) / den)
        return out

    level_rows: dict[str, list[dict[str, Any]]] = {}
    for level in LEVELS:
        key_live = keys[level][live]
        value = v_now.groupby(key_live).sum()
        rows = []
        for key in sorted(value.index, key=lambda k: -value[k]):
            ids = list(live[(key_live == key).to_numpy()])
            mine = v_now[ids].sort_values(ascending=False)
            row: dict[str, Any] = {"id": key, "weight": _r(value[key] / tot_now * 100, 3),
                                   "n": len(ids)}
            if level == "sector":
                row["name"] = gics.SECTORS.get(key, key)
            elif level == "group":
                row["name"] = gics.INDUSTRY_GROUPS.get(key, key)
                row["sector"] = key[:2]
            else:
                name = str(sub_name[ids[0]])
                row["name"] = name
                row["name_zh"] = gics.SUB_INDUSTRY_ZH.get(name)
                row["group"] = str(group[ids[0]])
                row["sector"] = row["group"][:2]
            row["top"] = [{"t": t, "w": _r(v / value[key] * 100, 1)} for t, v in mine.head(3).items()]
            row.update(figures(level, key, ids))
            rows.append(row)
        level_rows[level] = rows

    new_since = prep.bases.get(NEW_MEMBER_PERIOD)
    earliest = min((d for d in prep.bases.values() if d is not None), default=None)
    member_rows = []
    for t, v in v_now.sort_values(ascending=False).items():
        row = {"t": t, "s": keys["sub"][t], "w": _r(v / tot_now * 100, 3),
               "r": {p: _r(own[p][t]) for p in own if t in own[p].index}}
        if vol_now is not None and t in vol_now.index:
            row["vr"] = _r(float(vol_now[t]) / float(vol_avg[t]))
        added = prep.added.get(t)
        if new_since is not None and added is not None and not pd.isna(added) and added > new_since:
            row["added"] = added.date().isoformat()
        cliff = [c for c in prep.cliff_list(earliest) if c["t"] == t]
        if cliff:
            row["cliff"] = cliff[-1]
        member_rows.append(row)

    index_row: dict[str, Any] = {
        "returns": {p: _r((blk["t1"] / blk["t0"] - 1) * 100) for p, blk in per.items()},
    }
    if day_moves is not None and len(day_moves):
        index_row.update(adv=int((day_moves > 0).sum()), dec=int((day_moves < 0).sum()),
                         flat=int((day_moves == 0).sum()))
    if vol_now is not None and float(vol_avg.sum()) > 0:
        index_row["vr"] = _r(float(vol_now.sum()) / float(vol_avg.sum()))

    return {
        "schema": SCHEMA,
        "asof": end.date().isoformat(),
        "partial_dropped": prep.partial_dropped,
        "weights_asof": snap.get("weights_asof"),
        "members_asof": snap.get("members_asof"),
        "periods": [p for p in PERIODS if p in per],
        "base_dates": {p: prep.bases[p].date().isoformat() for p in per},
        "trend": TREND,
        "momentum": MOMENTUM,
        "min_headline_members": MIN_HEADLINE_MEMBERS,
        "index": index_row,
        "levels": level_rows,
        "members": member_rows,
        "coverage": {
            "members": len(members),
            "priced": int(len(live)),
            "missing": sorted(set(members) - set(live)),
            "volume": int(len(vol_now)) if vol_now is not None else 0,
            "cliffs": prep.cliff_list(earliest),
        },
    }


# ---------------------------------------------------------------------------
# The cache file
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_mem: Optional[dict[str, Any]] = None
_mem_mtime: Optional[float] = None


def save(payload: dict[str, Any], path: Optional[Path] = None) -> None:
    """Write the payload atomically. Built in gunicorn's master (breadth's
    thread); workers notice the new mtime in :func:`peek`."""
    target = path or CACHE_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(payload, fh, separators=(",", ":"))
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    log.info("sector map: saved %s (%d sub-industries, as of %s)", target.name,
             len(payload.get("levels", {}).get("sub", [])), payload.get("asof"))


def exists(path: Optional[Path] = None) -> bool:
    return (path or CACHE_FILE).exists()


def peek(path: Optional[Path] = None) -> Optional[dict[str, Any]]:
    """The last saved payload, any age, or None. Never builds. Re-read only when
    the file's mtime moves, so a worker forked before today's build still
    serves it."""
    global _mem, _mem_mtime
    target = path or CACHE_FILE
    try:
        mtime = target.stat().st_mtime
    except OSError:
        return None
    use_memory = path is None
    if use_memory:
        with _lock:
            if _mem is not None and _mem_mtime == mtime:
                return _mem
    try:
        payload = json.loads(target.read_text())
    except (OSError, ValueError) as exc:
        log.warning("sector map: unreadable cache %s: %s", target, exc)
        return None
    if not isinstance(payload, dict) or payload.get("schema") != SCHEMA:
        return None
    if use_memory:
        with _lock:
            _mem, _mem_mtime = payload, mtime
    return payload
