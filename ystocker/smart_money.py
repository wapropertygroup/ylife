"""
ystocker.smart_money
~~~~~~~~~~~~~~~~~~~~
/smart-money (聪明钱): what the people whose trades are public have been
doing, by person and by stock, from three disclosures that are each already
on this site somewhere:

* **Fund managers** -- 13F (sec13f.py): this quarter's changes in each fund's
  50 largest positions, for the managers named in :data:`PEOPLE`.
* **Insiders** -- Form 4 (insiders.py): open-market buys and sells by
  officers, directors and 10% owners of the followed companies.
* **House members** -- PTRs (congress.py): trades by members, their spouses
  and their children.

Modelled on openbit.trade/people and its stock dossiers (asked for 2026-10-07).
Two of its ideas are the point of the page. A stock is read across all three
at once, and called the same way (同向) only when two or more agree with none
against. And every figure travels with its own date and lag, because the three
run on different clocks: a 13F is a quarter-end snapshot filed up to 45 days
later, a Form 4 is due in two business days, a PTR within 45 days of the trade.
"Two sources buying" can mean two moves four months apart, so the gap is shown.

Who is a person here
--------------------
:data:`PEOPLE` names discretionary managers whose book reflects one view:
Buffett's Berkshire, Ackman's Pershing Square, Tepper's Appaloosa. Left out,
though /13f tracks them: index giants (Vanguard, BlackRock, State Street),
quants (Renaissance, Two Sigma, AQR), market makers (Jane Street, Susquehanna)
and multi-manager pods (Citadel, Millennium, Point72, Balyasny), whose quarter
is thousands of unrelated decisions. "Ken Griffin bought X" would be a story
the filing does not tell. They still appear in a stock's dossier as funds that
hold it.

What the filings cannot say
---------------------------
* A 13F lists positions, so a fund's exit cannot be told from a position
  falling out of its 50 largest. Exits are not claimed.
* A routine 10b5-1 sale was scheduled months ahead and says little about the
  insider's view today, so plan trades are listed but do not set the insiders'
  direction. Neither does a purchase in an offering.
* A House trade's amount is a range; sums are of range midpoints and say so.
* None of it is real time, and none of it is advice.

Pure: data in, payload out. The route gathers the three inputs from their
modules' caches; nothing here fetches.
"""
from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any, Iterable, Optional

#: Fund name in sec13f.FUNDS -> (person, Chinese name, "runs" or "founded").
#: "founded" where the person no longer runs it (Dalio, Soros): the filing is
#: the firm's, and the page says whose firm it is rather than whose trade.
PEOPLE: dict[str, tuple[str, str, str]] = {
    "Berkshire Hathaway": ("Warren Buffett", "沃伦·巴菲特", "runs"),
    "Pershing Square": ("Bill Ackman", "比尔·阿克曼", "runs"),
    "Bridgewater Associates": ("Ray Dalio", "瑞·达利欧", "founded"),
    "Duquesne Family Office": ("Stanley Druckenmiller", "斯坦利·德鲁肯米勒", "runs"),
    "Soros Fund Management": ("George Soros", "乔治·索罗斯", "founded"),
    "Appaloosa (Tepper)": ("David Tepper", "大卫·泰珀", "runs"),
    "Icahn Capital": ("Carl Icahn", "卡尔·伊坎", "runs"),
    "Tiger Global": ("Chase Coleman", "蔡斯·科尔曼", "runs"),
    "Baupost Group": ("Seth Klarman", "塞思·卡拉曼", "runs"),
    "ARK Investment": ("Cathie Wood", "凯茜·伍德", "runs"),
    "Greenlight Capital (Einhorn)": ("David Einhorn", "大卫·艾因霍恩", "runs"),
    "Pabrai Investment Funds": ("Mohnish Pabrai", "莫尼什·帕伯莱", "runs"),
    "Third Point": ("Dan Loeb", "丹·勒布", "runs"),
    "Elliott Management": ("Paul Singer", "保罗·辛格", "founded"),
    "Trian Partners": ("Nelson Peltz", "纳尔逊·佩尔茨", "runs"),
    "Tudor Investment": ("Paul Tudor Jones", "保罗·都铎·琼斯", "runs"),
    "Viking Global": ("Andreas Halvorsen", "安德烈亚斯·哈尔沃森", "runs"),
    "Lone Pine Capital": ("Stephen Mandel", "斯蒂芬·曼德尔", "founded"),
    "Coatue Management": ("Philippe Laffont", "菲利普·拉丰", "runs"),
    "D1 Capital": ("Dan Sundheim", "丹·桑德海姆", "runs"),
    "Altimeter Capital": ("Brad Gerstner", "布拉德·格斯特纳", "runs"),
    "Tiger Cub Hill House": ("Zhang Lei", "张磊", "runs"),
    "Starboard Value": ("Jeff Smith", "杰夫·史密斯", "runs"),
    "Maverick Capital": ("Lee Ainslie", "李·安斯利", "runs"),
    "Whale Rock Capital": ("Alex Sacerdote", "亚历克斯·萨塞多特", "runs"),
    "Light Street Capital": ("Glen Kacher", "格伦·卡彻", "runs"),
}

#: Form 4s and PTRs filed within this many days count.
WINDOW_DAYS = 90
SOURCES = ("funds", "insiders", "house")
#: A person's action list on the page, and the stocks the table carries.
MAX_ACTIONS = 30
MAX_TICKERS = 400
MAX_INSIDERS = 60

_CHANGE_DIR = {"new": "buy", "increased": "buy", "reduced": "sell", "unchanged": "hold"}


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text).lower()).strip("-")


def _days(a: Optional[str], b: Optional[str]) -> Optional[int]:
    try:
        return (date.fromisoformat(b) - date.fromisoformat(a)).days
    except (TypeError, ValueError):
        return None


def _latest_parsed_quarter(fund: dict[str, Any]) -> Optional[dict[str, Any]]:
    for q in fund.get("quarters") or ():
        if q.get("holdings"):
            return q
    return None


def fund_people(holdings: dict[str, dict[str, Any]], people: Optional[dict] = None) -> list[dict[str, Any]]:
    """Each named manager's latest quarter: what they opened, added, trimmed
    and held among the fund's 50 largest positions. A fund whose fetch failed
    is listed with its error rather than dropped, so a gap reads as a gap."""
    people = PEOPLE if people is None else people
    out = []
    for order, (fund, (person, person_zh, role)) in enumerate(people.items()):
        fd = holdings.get(fund)
        base = {"kind": "fund", "id": "fund-" + slug(fund), "name": person, "name_zh": person_zh,
                "role": role, "org": fund, "order": order}
        q = _latest_parsed_quarter(fd or {}) if fd and not fd.get("error") else None
        if q is None:
            out.append({**base, "error": (fd or {}).get("error") or "no 13F data", "actions": []})
            continue
        actions = []
        for h in q.get("holdings") or ():
            ch = h.get("change")
            actions.append({"t": h.get("ticker"), "n": h.get("name"), "ch": ch, "dir": _CHANGE_DIR.get(ch),
                            "pct": h.get("change_pct"), "w": h.get("pct_portfolio"), "rank": h.get("rank")})
        buys = sum(1 for a in actions if a["dir"] == "buy")
        sells = sum(1 for a in actions if a["dir"] == "sell")
        moved = [a for a in actions if a["dir"] in ("buy", "sell")]
        moved.sort(key=lambda a: (a["ch"] != "new", -(a["w"] or 0)))
        out.append({
            **base, "as_of": q.get("period"), "filed": q.get("filing_date"),
            "lag": _days(q.get("period"), q.get("filing_date")),
            "positions": q.get("total_holdings"), "value_m": q.get("total_value_millions"),
            "buys": buys, "sells": sells, "holds": sum(1 for a in actions if a["dir"] == "hold"),
            "n_tickers": len({a["t"] or a["n"] for a in moved}),
            "top": [a["t"] or a["n"] for a in actions[:3]],
            "opened": [a["t"] or a["n"] for a in actions if a["ch"] == "new"][:8],
            "carried": bool(fd.get("carried_forward")),
            "actions": (moved + [a for a in actions if a["dir"] == "hold"])[:MAX_ACTIONS],
        })
    return out


def insider_people(rows: Iterable[dict[str, Any]], since: str) -> list[dict[str, Any]]:
    """Insiders with an open-market buy or sell filed on or after ``since``,
    largest trading first. Rows are insiders.feed_view's (compact keys)."""
    people: dict[tuple[str, str], dict[str, Any]] = {}
    for r in rows:
        if r.get("k") not in ("buy", "sell") or (r.get("f") or "") < since:
            continue
        key = (r.get("o") or "?", r.get("t") or "?")
        p = people.setdefault(key, {
            "kind": "insider", "id": "ins-" + slug(f"{key[0]}-{key[1]}"), "name": r.get("o"),
            "org": r.get("n") or r.get("t"), "ticker": r.get("t"), "title": r.get("ti"), "roles": r.get("ro"),
            "buys": 0, "sells": 0, "value": 0.0, "latest": None, "actions": []})
        p["buys" if r["k"] == "buy" else "sells"] += 1
        if isinstance(r.get("v"), (int, float)):
            p["value"] += float(r["v"])
        p["latest"] = max(p["latest"] or "", r.get("d") or "") or None
        if len(p["actions"]) < MAX_ACTIONS:
            p["actions"].append({"t": r.get("t"), "dir": r["k"], "d": r.get("d"), "f": r.get("f"),
                                 "v": r.get("v"), "plan": bool(r.get("pl")), "offering": bool(r.get("of")),
                                 "lag": _days(r.get("d"), r.get("f"))})
    out = sorted(people.values(), key=lambda p: -p["value"])[:MAX_INSIDERS]
    for p in out:
        p["n_tickers"] = 1
        p["value"] = round(p["value"])
    return out


def house_people(rows: Iterable[dict[str, Any]], since: str, names_zh: Optional[dict] = None) -> list[dict[str, Any]]:
    """House members with a trade filed on or after ``since``, newest filing
    first. Rows are congress.feed_rows'. Every trade is listed; only stock and
    options trades with a direction count towards buys and sells."""
    names_zh = names_zh or {}
    people: dict[str, dict[str, Any]] = {}
    for r in rows:
        if (r.get("f") or "") < since:
            continue
        m = r.get("m") or "?"
        p = people.setdefault(m, {"kind": "house", "id": "house-" + slug(m), "name": m,
                                  "name_zh": names_zh.get(m), "org": r.get("md"),
                                  "buys": 0, "sells": 0, "trades": 0, "tickers": set(), "mid": 0.0,
                                  "latest": None, "filed": None, "actions": []})
        p["trades"] += 1
        if r.get("dir") in ("buy", "sell") and r.get("t"):
            p["buys" if r["dir"] == "buy" else "sells"] += 1
            p["tickers"].add(r["t"])
        if r.get("lo") and r.get("hi"):
            p["mid"] += (r["lo"] + r["hi"]) / 2
        p["latest"] = max(p["latest"] or "", r.get("d") or "") or None
        p["filed"] = max(p["filed"] or "", r.get("f") or "") or None
        if len(p["actions"]) < MAX_ACTIONS:
            p["actions"].append({"t": r.get("t"), "n": r.get("n"), "dir": r.get("dir"), "k": r.get("k"),
                                 "c": r.get("c"), "op": r.get("op"), "o": r.get("o"), "d": r.get("d"),
                                 "f": r.get("f"), "lo": r.get("lo"), "hi": r.get("hi"),
                                 "lag": _days(r.get("d"), r.get("f")), "doc": r.get("doc"), "yr": r.get("yr")})
    out = []
    for p in people.values():
        p["n_tickers"] = len(p.pop("tickers"))
        p["mid"] = round(p["mid"])
        out.append(p)
    out.sort(key=lambda p: (p["filed"] or "", p["trades"]), reverse=True)
    return out


def _side(buy_n: int, sell_n: int) -> Optional[str]:
    return "buy" if buy_n > sell_n else "sell" if sell_n > buy_n else ("mixed" if buy_n else None)


def tickers_view(funds: list[dict[str, Any]], insider_rows: Iterable[dict[str, Any]],
                 house_rows: Iterable[dict[str, Any]], since: str,
                 names_zh: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
    """Every stock any of the three moved, with each source's side and dates,
    and whether two or more agree (``agree``) with none against."""
    book: dict[str, dict[str, Any]] = {}

    def slot(t: str, src: str) -> dict[str, Any]:
        row = book.setdefault(t, {"t": t, "n": None, "src": {}})
        # ``date`` is the latest of the trades that set the side, and the one
        # the gap between sources is measured from; ``seen`` is the latest of
        # anything listed, a plan sale or a held position included.
        return row["src"].setdefault(src, {"buy": 0, "sell": 0, "hold": 0, "who": [], "date": None,
                                           "seen": None, "filed": None, "value": 0.0})

    for p in funds:
        for a in p.get("actions") or ():
            if not a.get("t") or a.get("dir") is None:
                continue
            s = slot(a["t"], "funds")
            s[a["dir"]] += 1
            if a["dir"] != "hold" and len(s["who"]) < 8:
                s["who"].append({"name": p["name"], "name_zh": p.get("name_zh"), "org": p["org"],
                                 "ch": a["ch"], "w": a.get("w")})
            if a["dir"] != "hold":
                s["date"] = max(s["date"] or "", p.get("as_of") or "") or None
            s["seen"] = max(s["seen"] or "", p.get("as_of") or "") or None
            s["filed"] = max(s["filed"] or "", p.get("filed") or "") or None
            book[a["t"]]["n"] = book[a["t"]]["n"] or a.get("n")
    for r in insider_rows:
        if r.get("k") not in ("buy", "sell") or not r.get("t") or (r.get("f") or "") < since:
            continue
        s = slot(r["t"], "insiders")
        # A scheduled plan sale, or a purchase in an offering, is listed but
        # does not set the side (module docstring).
        counts = not r.get("pl") and not (r["k"] == "buy" and r.get("of"))
        if counts:
            s[r["k"]] += 1
            if isinstance(r.get("v"), (int, float)):
                s["value"] += float(r["v"]) * (1 if r["k"] == "buy" else -1)
        s.setdefault("plan", 0)
        s["plan"] += 0 if counts else 1
        if len(s["who"]) < 8 and r.get("o") not in [w["name"] for w in s["who"]]:
            s["who"].append({"name": r.get("o"), "title": r.get("ti"), "dir": r["k"], "plan": bool(r.get("pl"))})
        if counts:
            s["date"] = max(s["date"] or "", r.get("d") or "") or None
        s["seen"] = max(s["seen"] or "", r.get("d") or "") or None
        s["filed"] = max(s["filed"] or "", r.get("f") or "") or None
        book[r["t"]]["n"] = book[r["t"]]["n"] or r.get("n")
    for r in house_rows:
        if not r.get("t") or r.get("dir") not in ("buy", "sell") or (r.get("f") or "") < since:
            continue
        s = slot(r["t"], "house")
        s[r["dir"]] += 1
        if r.get("lo") and r.get("hi"):
            s["value"] += (r["lo"] + r["hi"]) / 2 * (1 if r["dir"] == "buy" else -1)
        if len(s["who"]) < 8 and r.get("m") not in [w["name"] for w in s["who"]]:
            s["who"].append({"name": r.get("m"), "name_zh": (names_zh or {}).get(r.get("m")),
                             "org": r.get("md"), "dir": r["dir"]})
        s["date"] = max(s["date"] or "", r.get("d") or "") or None
        s["seen"] = max(s["seen"] or "", r.get("d") or "") or None
        s["filed"] = max(s["filed"] or "", r.get("f") or "") or None

    out = []
    for row in book.values():
        sides = {}
        for src, s in row["src"].items():
            s["side"] = _side(s["buy"], s["sell"])
            s["value"] = round(s["value"])
            if s["side"] in ("buy", "sell"):
                sides[src] = s["side"]
        buying = [k for k, v in sides.items() if v == "buy"]
        selling = [k for k, v in sides.items() if v == "sell"]
        agree = None
        if len(buying) >= 2 and not selling:
            agree = {"side": "buy", "sources": buying}
        elif len(selling) >= 2 and not buying:
            agree = {"side": "sell", "sources": selling}
        if agree:
            dates = [row["src"][k]["date"] for k in agree["sources"] if row["src"][k]["date"]]
            agree["span_days"] = _days(min(dates), max(dates)) if len(dates) >= 2 else None
        row["agree"] = agree
        row["split"] = bool(buying and selling)
        row["latest"] = max((s["seen"] or "" for s in row["src"].values()), default="") or None
        row["actors"] = sum(len(s["who"]) for s in row["src"].values())
        out.append(row)
    out.sort(key=lambda r: (len(r["agree"]["sources"]) if r["agree"] else 0, r["actors"], r["latest"] or ""),
             reverse=True)
    return out[:MAX_TICKERS]


def build(holdings: dict[str, dict[str, Any]], insider_view: Optional[dict[str, Any]],
          house_feed: Optional[dict[str, Any]], *, today: date,
          names_zh: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """The /api/smart-money payload. ``insider_view`` is insiders.feed_view()'s
    answer (or None), ``house_feed`` congress.peek()'s (or None)."""
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    funds = fund_people(holdings)
    insider_rows = (insider_view or {}).get("rows") or []
    house_rows = (house_feed or {}).get("rows") or []
    insiders = insider_people(insider_rows, since)
    house = house_people(house_rows, since, names_zh)
    fund_dates = [p["as_of"] for p in funds if p.get("as_of")]
    fund_filed = [p["filed"] for p in funds if p.get("filed")]
    cov = (insider_view or {}).get("coverage") or {}
    counts = (house_feed or {}).get("counts") or {}
    return {
        "as_of": today.isoformat(), "since": since, "window_days": WINDOW_DAYS,
        "sources": {
            "funds": {"named": len(PEOPLE), "with_data": sum(1 for p in funds if not p.get("error")),
                      "as_of": max(fund_dates) if fund_dates else None,
                      "filed": max(fund_filed) if fund_filed else None},
            "insiders": {"checked": cov.get("checked"), "universe": cov.get("universe"),
                         "updated": cov.get("newest_check"), "trades": sum(1 for r in insider_rows
                                                                         if r.get("k") in ("buy", "sell") and (r.get("f") or "") >= since)},
            "house": {"filings": counts.get("filings"), "paper": counts.get("paper"),
                      "pending": counts.get("pending"), "latest_filed": (house_feed or {}).get("latest_filed"),
                      "built": (house_feed or {}).get("built"), "members": len(house)},
        },
        "people": funds + house + insiders,
        "tickers": tickers_view(funds, insider_rows, house_rows, since, names_zh),
    }


def dossier(ticker: str, holdings: dict[str, dict[str, Any]], insider_rows: Iterable[dict[str, Any]],
            house_rows: Iterable[dict[str, Any]], *, today: date,
            names_zh: Optional[dict[str, str]] = None) -> dict[str, Any]:
    """One stock across all three: every tracked fund holding it among its 50
    largest (named managers first), its insiders' trades and the House's, and
    the same agreement the table reports. For /history's 聪明钱 tab."""
    names_zh = names_zh or {}
    since = (today - timedelta(days=WINDOW_DAYS)).isoformat()
    funds_out = []
    for fund, fd in holdings.items():
        if not isinstance(fd, dict) or fd.get("error"):
            continue
        q = _latest_parsed_quarter(fd)
        if q is None:
            continue
        h = next((x for x in q.get("holdings") or () if x.get("ticker") == ticker), None)
        if h is None:
            continue
        person = PEOPLE.get(fund)
        funds_out.append({"fund": fund, "person": person[0] if person else None,
                          "person_zh": person[1] if person else None, "role": person[2] if person else None,
                          "w": h.get("pct_portfolio"), "rank": h.get("rank"), "value_m": h.get("value_millions"),
                          "ch": h.get("change"), "pct": h.get("change_pct"), "as_of": q.get("period"),
                          "filed": q.get("filing_date")})
    funds_out.sort(key=lambda f: (f["person"] is None, -(f["w"] or 0)))
    ins = [r for r in insider_rows if r.get("t") == ticker and r.get("k") in ("buy", "sell") and (r.get("f") or "") >= since]
    hou = [r for r in house_rows if r.get("t") == ticker and (r.get("f") or "") >= since]
    named = [{"name": f["person"], "name_zh": f["person_zh"], "org": f["fund"], "as_of": f["as_of"],
              "filed": f["filed"], "actions": [{"t": ticker, "ch": f["ch"], "dir": _CHANGE_DIR.get(f["ch"]), "w": f["w"]}]}
             for f in funds_out if f["person"]]
    row = next(iter(tickers_view(named, ins, hou, since, names_zh)), None)
    return {
        "ticker": ticker, "as_of": today.isoformat(), "since": since, "window_days": WINDOW_DAYS,
        "funds": funds_out,
        "insiders": [{"o": r.get("o"), "ti": r.get("ti"), "ro": r.get("ro"), "k": r["k"], "d": r.get("d"),
                      "d2": r.get("d2"), "f": r.get("f"), "v": r.get("v"), "sh": r.get("sh"), "px": r.get("px"),
                      "pl": bool(r.get("pl")), "of": bool(r.get("of")), "c": r.get("c"), "a": r.get("a"),
                      "pd": r.get("pd"), "lag": _days(r.get("d"), r.get("f"))} for r in ins],
        "house": [{**{k: r.get(k) for k in ("m", "md", "o", "k", "dir", "c", "op", "d", "f", "lo", "hi", "doc", "yr")},
                   "m_zh": names_zh.get(r.get("m")), "lag": _days(r.get("d"), r.get("f"))} for r in hou],
        "agree": row["agree"] if row else None,
        "split": row["split"] if row else False,
        "sides": {src: s["side"] for src, s in (row["src"].items() if row else ())},
    }
