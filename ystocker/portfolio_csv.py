"""
ystocker.portfolio_csv
~~~~~~~~~~~~~~~~~~~~~~
Turning a brokerage position export into positions, without a parser per broker.

Why alias sniffing rather than seven parsers
--------------------------------------------
Fidelity, Schwab, Vanguard, IBKR, Robinhood, E*TRADE and Futu all export
positions as CSV and no two agree on anything: the symbol column is ``Symbol``,
``Instrument``, ``Investment Name`` or ``代码``; quantity is ``Quantity``,
``Shares`` or ``持仓数量``; and three of them put non-data rows above and below
the real ones. Seven bespoke parsers would be seven things to notice when a broker
changes a heading, and would leave a hand-kept spreadsheet with no path in at all.

So this finds the header row by *scoring* candidate rows against alias sets, then
maps columns by alias. One code path serves every broker above and the documented
minimal template (``symbol,quantity``) alike, and an unrecognised export degrades
to a partial mapping the user can see rather than a hard failure.

Which is the point of returning :attr:`ParseResult.mapping`: the caller shows
"we read column 4 as quantity" *before* anything is saved. A silent mis-map is
the failure mode that matters here, because it produces a portfolio that looks
plausible and is wrong — and 穿透 percentages computed from it would be wrong
in a way no reader could detect.

Traps that are all real, all observed in actual exports
-------------------------------------------------------
* **BOM.** Fidelity and Schwab emit UTF-8 with a BOM, so the first header becomes
  ``\\ufeffSymbol`` and an exact-match mapping misses it. Decoded ``utf-8-sig``.
* **Chinese exports are often GB18030**, not UTF-8, and a wrong codec turns every
  heading into mojibake — which scores zero and looks like "no header found"
  rather than "wrong encoding".
* **Money formatting**: ``$1,234.56``, ``(123.45)`` for negative, ``--`` and
  ``n/a`` for blank. ``float("$1,234.56")`` raises, so an unguarded parse drops
  the row.
* **Preamble and footer rows.** Schwab opens with ``"Positions for account ..."``
  and closes with ``"Account Total"``; Fidelity appends several disclaimer
  paragraphs. A footer total row parsed as a position would double the portfolio.
* **Share classes.** Yahoo spells Berkshire ``BRK-B`` and brokers spell it
  ``BRK.B``. But a dot is also an exchange suffix — ``ASML.AS``, ``0700.HK``,
  ``2330.TW`` are all real Yahoo symbols reached by look-through — so the
  rewrite is narrowed to a *single trailing letter*, which is a share class and
  never an exchange. See :func:`normalise_symbol`.
* **Fidelity marks money-market symbols with asterisks** (``SPAXX**``), which is
  not part of the symbol.
* **Cash rows often have no symbol at all**, just ``Cash & Cash Investments`` in
  the description. Dropping them understates the portfolio and inflates every
  percentage; they become :data:`CASH_SYMBOL`.

Pure: no Flask, no network, no disk. ``tests/test_portfolio_csv.py`` feeds it
real export shapes as strings.
"""

from __future__ import annotations

import csv
import io
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

log = logging.getLogger(__name__)

#: Pseudo-symbol for a cash line that carries no ticker. Not a real Yahoo symbol,
#: and ``funddata`` resolves it locally so it never becomes a failed lookup.
CASH_SYMBOL = "$CASH"

#: Hard caps. A position export is a few hundred rows; anything past this is a
#: mistake or an attack, and both are better refused than streamed into DynamoDB.
MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 2_000
#: How far to hunt for the header row before giving up. Fidelity's preamble is
#: three lines; IBKR sectioned statements can push the positions header further.
MAX_HEADER_SCAN = 40

# ---------------------------------------------------------------------------
# Column aliases. Compared case-insensitively against a squashed form of the
# heading (punctuation and whitespace removed), so "Cost Basis Total",
# "cost_basis_total" and "COST BASIS TOTAL" all match one entry.
# ---------------------------------------------------------------------------

_ALIASES: dict[str, tuple[str, ...]] = {
    "symbol": (
        "symbol", "ticker", "tickersymbol", "securitysymbol", "sym", "instrument",
        "security", "securityid", "symbolcusip", "stocksymbol", "symbolticker",
        # Chinese (Futu / moomoo / tiger / 富途 / 老虎)
        "代码", "代號", "股票代码", "股票代號", "证券代码", "證券代號", "标的", "標的",
    ),
    "name": (
        "description", "name", "securityname", "securitydescription", "companyname",
        "investmentname", "instrumentname", "holdingname", "fundname", "productname",
        "名称", "名稱", "股票名称", "股票名稱", "证券名称", "證券名稱", "产品名称",
    ),
    "quantity": (
        "quantity", "qty", "shares", "share", "units", "unit", "sharesheld",
        "quantityheld", "position", "positionquantity", "numberofshares", "sharequantity",
        "持仓数量", "持倉數量", "持股数量", "持股數量", "数量", "數量", "股数", "股數",
        "持仓", "持倉",
    ),
    "price": (
        "lastprice", "price", "currentprice", "marketprice", "closingprice", "close",
        "lastpricedollar", "lastpriceusd", "shareprice", "priceusd", "last",
        "现价", "現價", "最新价", "最新價", "市价", "市價", "价格", "價格", "当前价格",
    ),
    "market_value": (
        "currentvalue", "marketvalue", "value", "mktvalue", "marketvalueusd",
        "positionvalue", "totalvalue", "valueusd", "currentvalueusd", "mktval",
        "marketvaluedollar", "valuedollar",
        # Robinhood heads its market-value column "Equity". Safe as an alias
        # because this matches a *heading*: Schwab's "Security Type" column has
        # "Equity" as a cell value, which is never consulted here.
        "equity",
        "市值", "市價值", "市场价值", "市場價值", "持仓市值", "持倉市值", "总市值",
    ),
    "cost_basis": (
        "costbasis", "costbasistotal", "totalcost", "cost", "bookcost", "costvalue",
        "totalcostbasis", "costbasisusd", "amountinvested",
        "成本", "总成本", "總成本", "成本金额", "成本金額", "投入成本", "持仓成本",
    ),
    "cost_per_share": (
        "averagecost", "avgcost", "costprice", "averageprice", "avgprice",
        "pricepaid", "unitcost", "costpershare", "averagecostbasis", "avgcostpershare",
        "成本价", "成本價", "均价", "均價", "平均成本", "摊薄成本", "攤薄成本",
    ),
    "account": (
        "account", "accountnumber", "accountname", "accountnum", "acct", "acctnum",
        "accountid", "accountalias",
        "账户", "帳戶", "账号", "帳號", "资金账号",
    ),
}

#: Rows whose symbol *or* name matches one of these are totals, subtotals or
#: disclaimers rather than positions. Matched on the squashed form.
_NOT_A_POSITION = re.compile(
    r"^(account|grand|sub|portfolio|position)?total"
    r"|^total"
    r"|^cash(and)?cashinvestments?$"
    r"|^pendingactivity"
    r"|^accountvalue"
    r"|^dateofdata"
    r"|^brokerageservices"
    r"|^thevalueof"
    r"|^positionsfor"
    r"|^合计$|^總計$|^总计$|^小计$|^小計$",
    re.IGNORECASE,
)

#: Fields that ARE a cash label, rather than fields that merely mention cash.
#: Fully anchored on purpose: an unanchored ``moneymarket`` would swallow
#: Fidelity's "FIDELITY GOVERNMENT MONEY MARKET" description and collapse SPAXX
#: — a real symbol with a real value that ``funddata`` can resolve — into an
#: anonymous cash blob. Checked before _NOT_A_POSITION, which would otherwise
#: discard "Cash & Cash Investments" as a subtotal.
#:
#: Note ``cash(and)?cash``: _squash strips the ampersand, so Schwab's
#: "Cash & Cash Investments" arrives as "cashcashinvestments" with no "and".
_IS_CASH = re.compile(
    r"^(cash(and)?cashinvestments?|cash|cashbalance|cashsweep|"
    r"cashandequivalents?|现金|現金|现金余额|現金餘額)$",
    re.IGNORECASE,
)

_BLANKS = frozenset({"", "-", "--", "---", "n/a", "na", "nan", "none", "null",
                     "not applicable", "—", "–"})

#: Single letters that follow a dot as a **share class**, and are therefore
#: rewritten to Yahoo's hyphen form. An allowlist rather than "any single letter",
#: because single-letter *exchange* suffixes exist and collide head-on:
#: ``.L`` (London), ``.T`` (Tokyo), ``.F`` (Frankfurt), ``.V`` (TSX Venture).
#: ``HSBA.L`` and ``7203.T`` are both real Yahoo symbols — and ``7203.T`` is in
#: this repo's own Nikkei peer group — so a blanket rule silently converted every
#: London and Tokyo listing into an unresolvable symbol.
_SHARE_CLASS_LETTERS = frozenset("ABCUW")

#: A share class only ever hangs off an alphabetic root. Requiring letters here
#: is what keeps numeric foreign roots (``0700.HK``, ``005930.KQ``, ``7203.T``)
#: out of the rewrite entirely, independently of the letter allowlist above.
_SHARE_CLASS_RE = re.compile(r"^([A-Z]{1,5})\.([A-Z])$")

#: Distinctive header fingerprints, for display only. A wrong guess costs a
#: cosmetic label, so this is deliberately loose -- the mapping above is what
#: actually parses the file.
_BROKER_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Interactive Brokers", ("datadiscriminator", "assetcategory")),
    ("Fidelity",            ("lastpricechange", "costbasistotal")),
    ("Fidelity",            ("accountnumber", "currentvalue", "lastprice")),
    ("Charles Schwab",      ("securitytype", "daychange")),
    # Schwab without the day-change column: "Security Type" beside a market value
    # is still distinctive, and matching only the richer export left a plain Schwab
    # positions file labelled generically.
    ("Charles Schwab",      ("securitytype", "marketvalue")),
    ("Vanguard",            ("investmentname", "shares", "shareprice")),
    ("Robinhood",           ("instrument", "averagecost")),
    ("E*TRADE",             ("symbol", "lastpricedollar")),
    ("Futu / moomoo",       ("代码", "持仓数量")),
    ("Futu / moomoo",       ("代號", "持倉數量")),
)


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------

def _squash(text: Any) -> str:
    """Lowercase a heading with punctuation and whitespace removed.

    ``"Cost Basis Total"`` and ``"cost_basis_total"`` both become
    ``"costbasistotal"``. CJK is preserved: the class is explicitly
    "not alphanumeric" rather than "not ASCII", so ``代码`` survives.
    """
    if text is None:
        return ""
    out = str(text).replace("﻿", "").strip().lower()
    # Drop a trailing currency/unit marker so "Last Price $" matches "lastprice"
    # only via its own alias, while "Value ($)" still squashes cleanly.
    return re.sub(r"[^0-9a-z一-鿿]+", "", out)


def parse_number(raw: Any) -> Optional[float]:
    """A CSV money/quantity cell as a float, or None when it is blank.

    Handles ``$1,234.56``, ``(123.45)`` (accounting negative), ``1 234,00`` is
    *not* handled deliberately — European decimal commas are ambiguous against
    thousands separators and guessing wrong is a 1000x error, which is exactly
    the class of mistake ``data.py`` documents for currency.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    text = str(raw).strip()
    if text.lower() in _BLANKS:
        return None
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    # Strip currency symbols, thousands separators, percent signs, spaces.
    text = re.sub(r"[$€£¥₩,%\s ]", "", text)
    if text in ("", "-", "+", "."):
        return None
    if text.startswith("+"):
        text = text[1:]
    try:
        value = float(text)
    except ValueError:
        return None
    return -value if negative else value


def normalise_symbol(raw: Any) -> str:
    """A broker's symbol as Yahoo spells it.

    Two rewrites, both narrow on purpose:

    * ``**`` and ``*`` suffixes are Fidelity footnote markers (``SPAXX**``), not
      part of the symbol.
    * A trailing dot-plus-**share-class-letter** becomes Yahoo's hyphen form:
      ``BRK.B`` -> ``BRK-B``, ``BF.B`` -> ``BF-B``, ``LGF.A`` -> ``LGF-A``.

    The second rule is guarded twice, because "any single letter after a dot" is
    wrong and fails silently. Single-letter *exchange* suffixes collide with share
    classes — ``HSBA.L`` is London, ``7203.T`` is Tokyo, ``ADS.F`` is Frankfurt,
    ``ABC.V`` is TSX Venture — and ``7203.T`` is in this repo's own Nikkei peer
    group. So the letter must be in :data:`_SHARE_CLASS_LETTERS` *and* the root
    must be alphabetic, which additionally excludes every numeric foreign root
    (``0700.HK``, ``005930.KQ``). Longer suffixes (``.AS``, ``.HK``, ``.SW``) were
    never candidates. Getting this wrong turns a real holding into an
    unresolvable symbol, which look-through then reports as an unnamed residual.
    """
    if raw is None:
        return ""
    text = str(raw).replace("﻿", "").strip().upper()
    text = text.strip("\"'").strip()
    text = re.sub(r"\*+$", "", text)          # Fidelity footnote markers
    text = text.split()[0] if text.split() else ""
    m = _SHARE_CLASS_RE.match(text)
    if m and m.group(2) in _SHARE_CLASS_LETTERS:
        text = f"{m.group(1)}-{m.group(2)}"
    return text


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------

@dataclass
class ParsedRow:
    symbol: str
    name: str = ""
    quantity: Optional[float] = None
    price: Optional[float] = None
    market_value: Optional[float] = None
    cost_basis: Optional[float] = None
    account: str = ""
    line: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "name": self.name,
                "quantity": self.quantity, "price": self.price,
                "market_value": self.market_value, "cost_basis": self.cost_basis,
                "account": self.account, "line": self.line,
                "warnings": list(self.warnings)}


@dataclass
class SkippedRow:
    line: int
    reason: str
    excerpt: str

    def as_dict(self) -> dict[str, Any]:
        return {"line": self.line, "reason": self.reason, "excerpt": self.excerpt}


@dataclass
class ParseResult:
    rows: list[ParsedRow] = field(default_factory=list)
    skipped: list[SkippedRow] = field(default_factory=list)
    #: field name -> the heading it was read from. Shown to the user before save.
    mapping: dict[str, str] = field(default_factory=dict)
    broker: str = ""
    encoding: str = ""
    delimiter: str = ","
    header_line: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and bool(self.rows)

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "error": self.error,
            "rows": [r.as_dict() for r in self.rows],
            "skipped": [s.as_dict() for s in self.skipped],
            "mapping": dict(self.mapping),
            "broker": self.broker,
            "encoding": self.encoding,
            "delimiter": self.delimiter,
            "header_line": self.header_line,
            "warnings": list(self.warnings),
            "row_count": len(self.rows),
            "skipped_count": len(self.skipped),
        }


# ---------------------------------------------------------------------------
# Decoding and header discovery
# ---------------------------------------------------------------------------

def _decode(raw: bytes) -> tuple[str, str]:
    """Text plus the codec that produced it.

    ``utf-8-sig`` first because it strips a BOM when present and behaves as plain
    UTF-8 when not. ``gb18030`` before any latin fallback: it is a superset of
    GBK/GB2312 and is what Chinese brokers emit, and mojibake here does not raise
    — it produces headings that match no alias, which surfaces as "could not find
    a header row" and sends the user hunting for the wrong problem.
    """
    for codec in ("utf-8-sig", "utf-8", "gb18030", "big5", "cp1252", "latin-1"):
        try:
            return raw.decode(codec), codec
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _sniff_delimiter(sample: str) -> str:
    try:
        return csv.Sniffer().sniff(sample, delimiters=",;\t|").delimiter
    except csv.Error:
        # Sniffer fails on single-column files and on quoted preambles. Count.
        counts = {d: sample.count(d) for d in (",", "\t", ";", "|")}
        best = max(counts, key=lambda d: counts[d])
        return best if counts[best] else ","


def _score_header(cells: Iterable[Any]) -> tuple[int, dict[str, str]]:
    """How well *cells* works as a header, plus the mapping it would give.

    Score is the number of distinct fields matched, so a row matching symbol +
    quantity + value beats a stray text line that happens to contain "Total".
    First match wins per field: brokers repeat headings (Fidelity has both
    ``Last Price`` and ``Last Price Change``) and the leftmost is the plain one.
    """
    mapping: dict[str, str] = {}
    squashed = [(_squash(c), str(c).strip()) for c in cells]
    for field_name, aliases in _ALIASES.items():
        for sq, original in squashed:
            if sq and sq in aliases and field_name not in mapping:
                mapping[field_name] = original
                break
    return len(mapping), mapping


def _find_header(rows: list[list[str]]) -> tuple[int, dict[str, str], dict[str, int]]:
    """Index of the best header row, its field->heading map, and field->column.

    A header must yield a symbol *or* name column and at least one quantitative
    column; without both it is prose that happens to contain a keyword. Requiring
    a *symbol* specifically would reject Vanguard's ``Investment Name`` exports.
    """
    best: tuple[int, int, dict[str, str]] = (-1, 0, {})
    for idx, cells in enumerate(rows[:MAX_HEADER_SCAN]):
        if not cells or all(not str(c).strip() for c in cells):
            continue
        score, mapping = _score_header(cells)
        has_id = "symbol" in mapping or "name" in mapping
        has_qty = any(k in mapping for k in
                      ("quantity", "market_value", "price", "cost_basis"))
        if not (has_id and has_qty):
            continue
        if score > best[1]:
            best = (idx, score, mapping)
    if best[0] < 0:
        return -1, {}, {}

    idx, _score, mapping = best
    heading_to_col: dict[str, int] = {}
    for col, cell in enumerate(rows[idx]):
        key = str(cell).strip()
        if key and key not in heading_to_col:
            heading_to_col[key] = col
    columns = {f: heading_to_col[h] for f, h in mapping.items() if h in heading_to_col}
    return idx, mapping, columns


def _guess_broker(header_cells: Iterable[Any]) -> str:
    """A display-only label for the export's origin.

    Substring matching throughout, deliberately loose: a wrong guess costs a
    cosmetic line of text, whereas the column mapping above is what actually
    parses the file and is reported separately. Exact matching was worse than
    useless here — Schwab's "Day Change %" squashes to ``daychange`` because
    _squash drops the percent sign, so an exact ``daychangepct`` never matched.
    """
    squashed = {_squash(c) for c in header_cells if _squash(c)}
    for name, needles in _BROKER_HINTS:
        if all(any(needle in s for s in squashed) for needle in needles):
            return name
    return ""


# ---------------------------------------------------------------------------
# The parse
# ---------------------------------------------------------------------------

def parse(raw: bytes | str, *, max_rows: int = MAX_ROWS) -> ParseResult:
    """Parse a brokerage position export into :class:`ParsedRow` objects.

    Never raises on malformed input: an unusable file comes back with
    :attr:`ParseResult.error` set, and an individual unusable row lands in
    :attr:`ParseResult.skipped` with a reason. The caller shows both, because a
    row silently missing from an imported portfolio makes every 穿透 percentage
    that follows quietly wrong.
    """
    result = ParseResult()

    if isinstance(raw, str):
        text, encoding = raw, "str"
    else:
        if len(raw) > MAX_BYTES:
            result.error = (f"File is {len(raw) // 1024} KB; the limit is "
                            f"{MAX_BYTES // 1024} KB")
            return result
        text, encoding = _decode(raw)
    result.encoding = encoding

    if not text.strip():
        result.error = "File is empty"
        return result

    result.delimiter = _sniff_delimiter(text[:8192])
    try:
        rows = list(csv.reader(io.StringIO(text), delimiter=result.delimiter))
    except csv.Error as exc:
        result.error = f"Could not read as CSV: {exc}"
        return result

    header_idx, mapping, columns = _find_header(rows)
    if header_idx < 0:
        result.error = ("Could not find a header row naming a symbol and a "
                        "quantity or value column. Expected something like "
                        "'symbol,quantity' — see the template.")
        return result

    result.mapping = mapping
    result.header_line = header_idx + 1
    result.broker = _guess_broker(rows[header_idx])
    width = len(rows[header_idx])

    def cell(row: list[str], fname: str) -> str:
        col = columns.get(fname)
        if col is None or col >= len(row):
            return ""
        return str(row[col]).strip()

    seen_lines = 0
    for offset, row in enumerate(rows[header_idx + 1:], start=header_idx + 2):
        if not row or all(not str(c).strip() for c in row):
            continue
        if seen_lines >= max_rows:
            result.warnings.append(
                f"Stopped at {max_rows} rows; the rest of the file was not read")
            break

        excerpt = result.delimiter.join(str(c) for c in row)[:120]
        raw_symbol = cell(row, "symbol")
        name = cell(row, "name")
        # A blank marker is not a name. Schwab writes "--" in the description of its
        # cash line, which otherwise rendered literally as the position's name.
        if name.lower() in _BLANKS:
            name = ""

        # A cash line usually has no symbol, only a description. It is a real
        # balance and must not be dropped as a subtotal.
        #
        # The symbol field only becomes CASH_SYMBOL when it *is* a cash label:
        # Schwab writes "Cash & Cash Investments" in the symbol column, and
        # normalise_symbol would take its first word and invent a ticker "CASH".
        # A named sweep fund (Vanguard's VMFXX with "Cash" in the description)
        # keeps its own symbol, because that one is resolvable.
        symbol_is_cash = bool(_IS_CASH.match(_squash(raw_symbol)))
        name_is_cash = bool(_IS_CASH.match(_squash(name)))
        if symbol_is_cash or (name_is_cash and not raw_symbol):
            value = (parse_number(cell(row, "market_value"))
                     or parse_number(cell(row, "quantity")))
            if value:
                result.rows.append(ParsedRow(
                    symbol=CASH_SYMBOL, name=name or "Cash", market_value=value,
                    account=cell(row, "account"), line=offset))
                seen_lines += 1
            else:
                result.skipped.append(SkippedRow(offset, "cash row with no value",
                                                 excerpt))
            continue

        # Totals, disclaimers and section headings.
        if (_NOT_A_POSITION.match(_squash(raw_symbol))
                or (not raw_symbol and _NOT_A_POSITION.match(_squash(name)))):
            result.skipped.append(SkippedRow(offset, "total or non-position row",
                                             excerpt))
            continue

        symbol = normalise_symbol(raw_symbol)
        if not symbol:
            result.skipped.append(SkippedRow(offset, "no symbol", excerpt))
            continue
        # A symbol column that caught prose -- Fidelity's disclaimer paragraphs
        # land here. Real tickers are short and have no spaces.
        if len(symbol) > 12 or not re.fullmatch(r"[A-Z0-9][A-Z0-9.\-=^]*", symbol):
            result.skipped.append(SkippedRow(offset, "not a symbol", excerpt))
            continue

        quantity = parse_number(cell(row, "quantity"))
        price = parse_number(cell(row, "price"))
        value = parse_number(cell(row, "market_value"))
        cost = parse_number(cell(row, "cost_basis"))
        if cost is None:
            per_share = parse_number(cell(row, "cost_per_share"))
            if per_share is not None and quantity:
                cost = round(per_share * quantity, 2)

        parsed = ParsedRow(symbol=symbol, name=name, quantity=quantity,
                           price=price, market_value=value, cost_basis=cost,
                           account=cell(row, "account"), line=offset)

        if value is None and quantity is not None and price is not None:
            parsed.market_value = round(quantity * price, 2)
            parsed.warnings.append("value computed as quantity x price")

        if parsed.quantity is None and parsed.market_value is None:
            result.skipped.append(SkippedRow(offset, "no quantity or value",
                                             excerpt))
            continue
        if parsed.market_value is not None and parsed.market_value < 0:
            result.skipped.append(SkippedRow(offset, "negative value (short?)",
                                             excerpt))
            continue
        if len(row) != width:
            parsed.warnings.append(
                f"row has {len(row)} columns, header has {width}")

        result.rows.append(parsed)
        seen_lines += 1

    if not result.rows and not result.error:
        result.error = ("Found a header row but no positions under it. "
                        f"{len(result.skipped)} row(s) were skipped — check the "
                        "column mapping.")

    # Same symbol in several accounts is normal and must not silently collapse:
    # merging is the caller's choice, so it is reported rather than performed.
    counts: dict[str, int] = {}
    for r in result.rows:
        counts[r.symbol] = counts.get(r.symbol, 0) + 1
    dupes = sorted(s for s, n in counts.items() if n > 1)
    if dupes:
        result.warnings.append(
            "appears more than once (separate accounts?): " + ", ".join(dupes[:10]))

    log.info("portfolio_csv: %s rows, %s skipped, broker=%s, encoding=%s",
             len(result.rows), len(result.skipped), result.broker or "unknown",
             result.encoding)
    return result


def merge_duplicates(rows: Iterable[ParsedRow]) -> list[ParsedRow]:
    """Combine rows sharing a symbol, summing quantity, value and cost.

    Offered separately from :func:`parse` so the import preview can show the file
    as it actually is, and the user decides. Accounts are concatenated so the
    merged line still says where it came from.
    """
    out: dict[str, ParsedRow] = {}
    for row in rows:
        cur = out.get(row.symbol)
        if cur is None:
            out[row.symbol] = ParsedRow(**{**row.as_dict(),
                                           "warnings": list(row.warnings)})
            continue

        def add(a: Optional[float], b: Optional[float]) -> Optional[float]:
            if a is None:
                return b
            if b is None:
                return a
            return a + b

        cur.quantity = add(cur.quantity, row.quantity)
        cur.market_value = add(cur.market_value, row.market_value)
        cur.cost_basis = add(cur.cost_basis, row.cost_basis)
        cur.price = cur.price or row.price
        cur.name = cur.name or row.name
        accounts = [a for a in (cur.account, row.account) if a]
        cur.account = " + ".join(dict.fromkeys(accounts))
        if "merged" not in " ".join(cur.warnings):
            cur.warnings.append("merged from several rows")
    return list(out.values())


#: The documented minimal format, offered as a download so a user with a broker
#: this does not recognise has a shape to reformat into. Kept in sync with the
#: alias table above by tests/test_portfolio_csv.py.
TEMPLATE_CSV = (
    "symbol,quantity,cost_basis,account\n"
    "VOO,25,9500,Taxable\n"
    "QQQ,10,4200,Taxable\n"
    "VTTSX,150,3100,401k\n"
    "AAPL,40,7200,Taxable\n"
    "BND,60,4400,IRA\n"
)
