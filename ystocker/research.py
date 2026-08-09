"""
ystocker.research
~~~~~~~~~~~~~~~~~
Prompt builders for the yStocker AI chart-analysis agent.

Two entry points, both driven by the same 17-section stock research template
(see ``TEMPLATE_OUTLINE``):

``build_chart_prompt()``
    Per-chart "✦ Explain" prompt.  Each chart is mapped to the template
    section(s) it actually informs (price → §10 technical structure,
    PE → §7 valuation, relative strength → §4 industry relative strength …),
    so the short per-chart notes accumulate into the same framework as the
    full report instead of being free-form commentary.

``build_research_prompt()``
    Full deep-research prompt: renders every metric the app already has into
    a verified-data block, then asks the model to fill in the complete
    template and emit the mandatory 8-line Investment Memo.

Design rules baked into the prompts:
  * Never claim a stock is cheap merely because it fell, nor that a great
    company is worth buying at any price.
  * Support / resistance are always written as *zones* ("98–101"), never as a
    single number.
  * Distinguish long-term company quality from short-term trading position.
  * Separate GAAP profit / non-GAAP (扣非) profit / free cash flow so earnings
    quality is visible.
  * Missing data must be labelled as missing — never invented.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Iterable, Sequence

log = logging.getLogger(__name__)

# Bump when the template text changes so cached reports are invalidated.
TEMPLATE_VERSION = "v1"

# Rows/items fed to the model per collection — keeps the prompt inside a sane
# token budget while still covering "8 quarters + 3 years" as the template asks.
_MAX_QUARTERS = 8
_MAX_ANNUAL = 5
_MAX_PEERS = 8
_MAX_NEWS = 15
_MAX_HOLDERS = 10
_MAX_INSIDER = 8
_MAX_EARNINGS = 8
_MAX_LOOKTHROUGH = 20


# ---------------------------------------------------------------------------
# Template outline — the shared contract between both prompt builders.
# ---------------------------------------------------------------------------

TEMPLATE_OUTLINE: tuple[tuple[str, str], ...] = (
    ("1",  ("Basic Info",                 "基本信息")),
    ("2",  ("One-Sentence Thesis",        "一句话投资逻辑")),
    ("3",  ("Company Quality",            "公司质量")),
    ("4",  ("Industry & Competition",     "行业与竞争格局")),
    ("5",  ("Financial Trends",           "财务趋势")),
    ("6",  ("Earnings Quality",           "盈利质量")),
    ("7",  ("Valuation",                  "估值")),
    ("8",  ("Latest Earnings Report",     "最新财报")),
    ("9",  ("News & Catalysts",           "新闻与催化剂")),
    ("10", ("Technical Structure",        "技术结构")),
    ("11", ("Drawdown Diagnosis",         "下跌原因诊断")),
    ("12", ("Portfolio Fit",              "组合适配度")),
    ("13", ("Position Tier",              "仓位等级")),
    ("14", ("Three-Tranche Buy Plan",     "三档买入计划")),
    ("15", ("No-Add Conditions",          "明确禁止加仓条件")),
    ("16", ("Trim Rules",                 "减仓规则")),
    ("17", ("Scorecard",                  "最终评分卡")),
)


# ---------------------------------------------------------------------------
# Chart → template section mapping (per-chart "✦ Explain")
# ---------------------------------------------------------------------------

CHART_SECTIONS: dict[str, dict[str, str]] = {
    "pe": {
        "label_en": "PE Ratio (TTM)",
        "label_zh": "市盈率 PE (TTM)",
        "section_en": "§7 Valuation — vs its own history",
        "section_zh": "§7 估值 — 与自身历史比较",
        "focus_en": (
            "Place the current multiple inside its own historical range: is it near the 5-year "
            "high, the low, or the middle? Say explicitly whether multiple expansion/compression "
            "is driving the move rather than earnings. A falling PE is only cheap if earnings hold."
        ),
        "focus_zh": (
            "把当前 PE 放进它自己的历史区间：靠近 5 年高位、低位还是中位？明确指出这段行情是"
            "估值扩张/压缩驱动，还是盈利驱动。PE 下降只有在盈利站得住时才叫便宜。"
        ),
    },
    "fwdpe": {
        "label_en": "Forward PE Ratio",
        "label_zh": "前瞻市盈率 Forward PE",
        "section_en": "§7 Valuation — forward / dynamic multiple",
        "section_zh": "§7 估值 — Forward / 动态估值",
        "focus_en": (
            "Focus on how the market has repriced forward earnings expectations. A forward PE "
            "falling faster than price means estimates are being revised up; the reverse means "
            "the multiple is being paid for hope."
        ),
        "focus_zh": (
            "重点讲市场对前瞻盈利预期的重定价。Forward PE 比股价跌得更快 = 盈利预期在上调；"
            "反过来 = 在为想象力付估值。"
        ),
    },
    "peg": {
        "label_en": "PEG Ratio",
        "label_zh": "PEG 比率",
        "section_en": "§7 Valuation — growth-adjusted",
        "section_zh": "§7 估值 — 增长调整后",
        "focus_en": (
            "Judge the growth-adjusted valuation: below 1 is attractive, above 2 is stretched. "
            "State whether the PEG moved because of the multiple or because the growth rate "
            "assumption changed — the latter is far more dangerous."
        ),
        "focus_zh": (
            "判断增长调整后的估值：低于 1 偏便宜，高于 2 偏贵。说清 PEG 变化是估值动的还是"
            "增长率假设动的——后者危险得多。"
        ),
    },
    "price": {
        "label_en": "Stock Price (USD)",
        "label_zh": "股价 (USD)",
        "section_en": "§10 Technical Structure (+ §11 Drawdown Diagnosis if down >10%)",
        "section_zh": "§10 技术结构（若回撤 >10%，同时触发 §11 下跌原因诊断）",
        "focus_en": (
            "Classify the trend using one of: strong uptrend / pullback within uptrend / "
            "high-level consolidation / breakout retest / oversold bounce / basing / downtrend. "
            "Give support and resistance as ZONES (e.g. \"98–101 support\"), never a single number. "
            "If the drawdown from the period high exceeds 10%, also answer the key question: "
            "the price fell — did the VALUE fall?"
        ),
        "focus_zh": (
            "先给趋势定性，只能选其一：强势上升 / 上升趋势回调 / 高位横盘 / 突破回踩 / 超跌反弹 / "
            "底部构筑 / 下降趋势。支撑与压力必须写成区间（例：「98–101 支撑区」），不要写单一数字。"
            "若从区间高点回撤超过 10%，还要回答核心问题：价格跌了，但价值有没有跌？"
        ),
    },
    "volume": {
        "label_en": "Volume",
        "label_zh": "成交量",
        "section_en": "§10 Technical Structure — volume confirmation",
        "section_zh": "§10 技术结构 — 成交量确认",
        "focus_en": (
            "Does volume confirm the price move? Rising price on shrinking volume is a weakening "
            "trend; capitulation volume at lows is different from distribution volume at highs."
        ),
        "focus_zh": (
            "成交量是否确认了价格？价涨量缩 = 趋势走弱；低位恐慌放量和高位派发放量是两件完全"
            "不同的事，要分清。"
        ),
    },
    "rsi": {
        "label_en": "RSI (14)",
        "label_zh": "RSI (14)",
        "section_en": "§10 Technical Structure — momentum",
        "section_zh": "§10 技术结构 — Momentum",
        "focus_en": (
            "Read momentum, not a trade signal. Note that RSI can stay above 70 for months in a "
            "strong trend — overbought is not a sell trigger by itself. Flag divergence between "
            "RSI and price if present."
        ),
        "focus_zh": (
            "读的是动量，不是买卖信号。强趋势里 RSI 可以在 70 上方待几个月——超买本身不构成卖出"
            "理由。如有 RSI 与价格背离，要点出来。"
        ),
    },
    "macd": {
        "label_en": "MACD",
        "label_zh": "MACD",
        "section_en": "§10 Technical Structure — momentum",
        "section_zh": "§10 技术结构 — Momentum",
        "focus_en": (
            "Describe momentum direction and whether it is strengthening or fading (histogram "
            "widening vs shrinking), plus any zero-line or signal-line cross."
        ),
        "focus_zh": (
            "讲动量方向以及在增强还是衰减（柱状体放大 vs 收缩），并指出零轴/信号线交叉。"
        ),
    },
    "stoch": {
        "label_en": "Stochastic Oscillator",
        "label_zh": "随机指标 Stochastic",
        "section_en": "§10 Technical Structure — momentum",
        "section_zh": "§10 技术结构 — Momentum",
        "focus_en": (
            "Where does price sit inside its recent range, and is %K crossing %D? Treat >80 / <20 "
            "as position-in-range information, not as an automatic reversal call."
        ),
        "focus_zh": (
            "价格处在近期区间的什么位置，%K 是否穿越 %D？>80 / <20 只说明「在区间的什么位置」，"
            "不等于自动反转。"
        ),
    },
    "hv": {
        "label_en": "Historical Volatility",
        "label_zh": "历史波动率 HV",
        "section_en": "§10 Technical Structure + position sizing risk",
        "section_zh": "§10 技术结构 + 仓位风险",
        "focus_en": (
            "Rising volatility means each 1% of account allocated carries more risk — tie the "
            "reading to position sizing. A volatility squeeze often precedes a large move in "
            "either direction."
        ),
        "focus_zh": (
            "波动率上升意味着同样 1% 的账户仓位承担更大风险——把结论落到仓位大小上。波动率"
            "收敛（squeeze）常常是大幅波动的前奏，但方向不定。"
        ),
    },
    "rsstrength": {
        "label_en": "Relative Strength vs SPY",
        "label_zh": "相对标普 500 强弱",
        "section_en": "§4 Industry & Competition — relative strength",
        "section_zh": "§4 行业与竞争格局 — 行业相对强弱",
        "focus_en": (
            "Is the stock leading or lagging the index, and did that change recently? Relative "
            "strength turning up before absolute price is a meaningful early signal; a stock "
            "underperforming a rising market is a warning regardless of the story."
        ),
        "focus_zh": (
            "个股相对指数是领先还是落后，最近是否发生转变？相对强度先于绝对价格转强是有意义的"
            "早期信号；在上涨市里跑输大盘则是警告，无论故事讲得多好。"
        ),
    },
    "forecast": {
        "label_en": "Price Forecast",
        "label_zh": "价格预测",
        "section_en": "§7 Valuation — scenario framing (Bear / Base / Bull)",
        "section_zh": "§7 估值 — 情景框架（Bear / Base / Bull）",
        "focus_en": (
            "These are statistical extrapolations of past prices — they contain no knowledge of "
            "fundamentals, guidance or catalysts. Say so plainly. Use the projection band only as "
            "a rough Bear/Base/Bull range, and note that the real driver of the target is "
            "EPS × a justified multiple."
        ),
        "focus_zh": (
            "这是对历史价格的统计外推，不包含任何基本面、guidance 或催化剂信息——必须直说这一点。"
            "预测带只能当作粗略的 Bear/Base/Bull 区间，真正决定目标价的是 EPS × 合理估值倍数。"
        ),
    },
}


# ---------------------------------------------------------------------------
# Small formatting helpers
# ---------------------------------------------------------------------------

def _num(v: Any, digits: int = 2, suffix: str = "", prefix: str = "") -> str:
    """Format a number, returning ``n/a`` for None/NaN/non-numeric input."""
    if v is None or isinstance(v, bool):
        return "n/a"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v) if v else "n/a"
    if f != f:  # NaN
        return "n/a"
    return f"{prefix}{f:,.{digits}f}{suffix}"


def _pct(v: Any, digits: int = 1) -> str:
    return _num(v, digits, suffix="%")


def _signed_pct(v: Any, digits: int = 1) -> str:
    if v is None:
        return "n/a"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "n/a"
    return f"{f:+,.{digits}f}%"


def _usd(v: Any, digits: int = 2) -> str:
    return _num(v, digits, prefix="$")


def _txt(v: Any) -> str:
    """Stringify a free-text field, collapsing empties to ``n/a``."""
    if v is None:
        return "n/a"
    s = str(v).strip()
    return s if s else "n/a"


def _lines(pairs: Iterable[tuple[str, str]]) -> str:
    """Render ``(label, value)`` pairs as ``- label: value`` lines, dropping empties."""
    out = [f"- {k}: {v}" for k, v in pairs if v not in (None, "", "n/a")]
    return "\n".join(out) if out else "- (no data available)"


def _block(title: str, body: str) -> str:
    """Wrap a body in a titled block; return "" when the body is empty."""
    if not body or not body.strip():
        return ""
    return f"### {title}\n{body.rstrip()}\n"


def _table(rows: Sequence[dict], cols: Sequence[tuple[str, str]], limit: int) -> str:
    """Render dict rows as a compact pipe table. ``cols`` is ``(key, header)``."""
    rows = [r for r in (rows or []) if isinstance(r, dict)][:limit]
    if not rows:
        return ""
    head = " | ".join(h for _k, h in cols)
    sep = " | ".join("---" for _ in cols)
    body = []
    for r in rows:
        cells = []
        for key, _h in cols:
            v = r.get(key)
            if v is None:
                cells.append("n/a")
            elif isinstance(v, float):
                cells.append(f"{v:,.2f}")
            else:
                cells.append(str(v))
        body.append(" | ".join(cells))
    return "\n".join([head, sep, *body])


def _zones(zs: Any) -> str:
    """Render support/resistance zones as ``lo–hi (n touches)`` strings."""
    if not isinstance(zs, list) or not zs:
        return "n/a"
    parts = []
    for z in zs[:4]:
        if isinstance(z, dict):
            lo, hi = z.get("lo"), z.get("hi")
            if lo is None or hi is None:
                continue
            touch = z.get("touches")
            label = f"{_num(lo)}–{_num(hi)}"
            if touch:
                label += f" ({touch} touch{'es' if touch != 1 else ''})"
            parts.append(label)
        elif isinstance(z, (int, float)):
            parts.append(_num(z))
    return "; ".join(parts) if parts else "n/a"


def _get(d: Any, *path: str, default: Any = None) -> Any:
    """Safe nested lookup: ``_get(bundle, "technicals", "rsi14")``."""
    cur = d
    for p in path:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(p)
    return cur if cur is not None else default


# ---------------------------------------------------------------------------
# Per-chart prompt  (the "✦ Explain" button next to each chart)
# ---------------------------------------------------------------------------

def build_chart_prompt(
    ticker: str,
    chart: str,
    period: str,
    lang: str,
    pairs: Sequence[tuple[str, Any]],
    context: dict | None = None,
) -> str:
    """Build the per-chart explanation prompt, anchored to a template section.

    Args:
        ticker: Uppercase symbol.
        chart: Chart key — one of ``CHART_SECTIONS`` (falls back gracefully).
        period: Display period, e.g. ``1y``.
        lang: ``en`` or ``zh``.
        pairs: ``(date, value)`` points, oldest first, ``None`` values removed.
        context: Optional snapshot of headline metrics so the note can tie the
            chart to price/valuation reality instead of reading the line alone.
    """
    zh = lang == "zh"
    meta = CHART_SECTIONS.get(chart)
    if meta is None:  # unknown chart key — degrade to a generic series read
        meta = {
            "label_en": chart, "label_zh": chart,
            "section_en": "§10 Technical Structure", "section_zh": "§10 技术结构",
            "focus_en": "Describe the trend, the level and what changed.",
            "focus_zh": "描述趋势、当前位置和发生了什么变化。",
        }

    label = meta["label_zh"] if zh else meta["label_en"]
    section = meta["section_zh"] if zh else meta["section_en"]
    focus = meta["focus_zh"] if zh else meta["focus_en"]

    # Formatter per chart family
    if chart in ("pe", "fwdpe"):
        fmt = lambda v: f"{float(v):.1f}x"          # noqa: E731
    elif chart == "peg":
        fmt = lambda v: f"{float(v):.2f}"           # noqa: E731
    elif chart == "price":
        fmt = lambda v: f"${float(v):.2f}"          # noqa: E731
    elif chart == "volume":
        fmt = lambda v: f"{float(v):,.0f}"          # noqa: E731
    else:
        fmt = lambda v: f"{float(v):.2f}"           # noqa: E731

    first_date, first_val = pairs[0]
    last_date, last_val = pairs[-1]
    values = [float(v) for _d, v in pairs]
    hi, lo = max(values), min(values)
    change = last_val - first_val
    pct = (change / first_val * 100) if first_val else 0.0

    recent = "\n".join(f"  {d}: {fmt(v)}" for d, v in pairs[-12:])

    # Position within the period range — the single most useful anchor.
    span = hi - lo
    pos_in_range = ((last_val - lo) / span * 100) if span else 50.0
    drawdown = ((last_val - hi) / hi * 100) if hi else 0.0

    stats = _lines([
        ("Period" if not zh else "区间", f"{period}  ({first_date} → {last_date})"),
        ("Start → Now" if not zh else "起点 → 当前", f"{fmt(first_val)} → {fmt(last_val)}"),
        ("Change" if not zh else "区间变化", f"{'+' if change >= 0 else ''}{fmt(change)} ({pct:+.1f}%)"),
        ("Period high / low" if not zh else "区间高 / 低", f"{fmt(hi)} / {fmt(lo)}"),
        ("Position in range" if not zh else "在区间中的位置", f"{pos_in_range:.0f}% (0=low, 100=high)"),
        ("Drawdown from period high" if not zh else "距区间高点回撤", f"{drawdown:+.1f}%"),
        ("Data points" if not zh else "数据点数", str(len(pairs))),
    ])

    ctx = context or {}
    ctx_body = _lines([
        ("Price" if not zh else "当前价格", _usd(ctx.get("price"))),
        ("Market cap ($B)" if not zh else "市值（十亿美元）", _num(ctx.get("market_cap"), 1)),
        ("PE (TTM)" if not zh else "PE (TTM)", _num(ctx.get("pe_ttm"), 1, "x")),
        ("Forward PE", _num(ctx.get("forward_pe"), 1, "x")),
        ("PEG", _num(ctx.get("peg"), 2)),
        ("Revenue growth YoY" if not zh else "收入增速 YoY", _pct(ctx.get("revenue_growth"))),
        ("EPS growth TTM" if not zh else "EPS 增速 TTM", _pct(ctx.get("eps_growth_ttm"))),
        ("Gross margin" if not zh else "毛利率", _pct(ctx.get("gross_margin"))),
        ("52w high / low" if not zh else "52周 高 / 低",
         f"{_usd(ctx.get('week52_high'))} / {_usd(ctx.get('week52_low'))}"
         if ctx.get("week52_high") else "n/a"),
        ("Analyst target" if not zh else "分析师目标价", _usd(ctx.get("target_price"))),
        ("Next earnings" if not zh else "下次财报", _txt(ctx.get("earnings_date"))),
        ("Sector" if not zh else "行业", _txt(ctx.get("sector"))),
    ])

    if zh:
        head = (
            f"你是一位机构股票研究分析师，正在为 {ticker} 做逐图分析。\n"
            f"这张图对应股票研究模板的：**{section}**。\n\n"
            f"请用 2–3 段中文（简体）解读下面这张「{label}」图。"
        )
        rules = (
            "写作要求：\n"
            "1. 只用给到的数字说话，要具体到数值；缺数据就说「数据缺失」，绝对不要编。\n"
            "2. 不要用标题、不要用 bullet、不要用表格——只写连贯段落。\n"
            "3. 区分「公司长期质量」和「短期交易位置」，这张图属于哪一类要说清。\n"
            "4. 任何支撑/压力位都必须写成区间（例：「98–101 支撑区」），不要写单一数字。\n"
            "5. 不要因为跌得多就默认便宜，也不要因为公司优秀就默认现价值得买。\n"
            "6. 最后单独一行，用这个格式给出落到模板里的结论：\n"
            f"   `→ {section}：<一句话结论>`"
        )
        ctx_title = "该股当前基本面/估值快照（站内已核实数据，优先采信）"
        chart_title = f"图表数据：{label}"
        recent_title = "最近 12 个数据点"
        focus_title = "本图分析重点"
    else:
        head = (
            f"You are an institutional equity research analyst working through {ticker} "
            f"chart by chart.\n"
            f"This chart maps to the stock research template section: **{section}**.\n\n"
            f"Explain the following \"{label}\" chart in 2–3 concise paragraphs."
        )
        rules = (
            "Writing rules:\n"
            "1. Be specific about the numbers given. If something is missing, say it is missing — "
            "never invent a figure.\n"
            "2. No headers, no bullet points, no tables — flowing paragraphs only.\n"
            "3. Distinguish long-term company quality from short-term trading position, and say "
            "which one this chart speaks to.\n"
            "4. Any support/resistance must be written as a ZONE (e.g. \"98–101 support\"), never "
            "a single number.\n"
            "5. Do not assume cheap just because it fell, nor that a great company is worth "
            "buying at today's price.\n"
            "6. End with one separate final line in exactly this format:\n"
            f"   `→ {section}: <one-sentence takeaway>`"
        )
        ctx_title = "Current fundamentals snapshot (verified in-app data — prefer these numbers)"
        chart_title = f"Chart data: {label}"
        recent_title = "Most recent 12 data points"
        focus_title = "Focus for this chart"

    parts = [
        head,
        "",
        _block(chart_title, stats),
        _block(recent_title, recent),
    ]
    if ctx_body != "- (no data available)":
        parts.append(_block(ctx_title, ctx_body))
    parts.extend([
        _block(focus_title, focus),
        rules,
    ])
    return "\n".join(p for p in parts if p)


# ---------------------------------------------------------------------------
# Full research report prompt
# ---------------------------------------------------------------------------

def _render_data_pack(bundle: dict, lang: str) -> str:
    """Render every verified metric we hold into labelled blocks for the prompt."""
    zh = lang == "zh"
    L = (lambda en, cn: cn if zh else en)  # noqa: E731  label picker

    ident = bundle.get("identity") or {}
    val = bundle.get("valuation") or {}
    growth = bundle.get("growth") or {}
    marg = bundle.get("margins") or {}
    bal = bundle.get("balance") or {}
    ana = bundle.get("analyst") or {}
    tech = bundle.get("technicals") or {}
    rel = bundle.get("relative") or {}
    port = bundle.get("portfolio") or {}
    opt = bundle.get("options") or {}
    zs = bundle.get("zscore") or {}
    fc = bundle.get("forecast") or {}

    blocks: list[str] = []

    # ── §1 identity ───────────────────────────────────────────────────────
    blocks.append(_block(L("Identity", "标的信息"), _lines([
        ("Ticker", _txt(ident.get("ticker"))),
        (L("Name", "名称"), _txt(ident.get("name"))),
        (L("Type", "类型"), _txt(ident.get("quote_type"))),
        (L("Sector", "板块"), _txt(ident.get("sector"))),
        (L("Industry", "细分行业"), _txt(ident.get("industry"))),
        (L("Fund family / category", "基金公司 / 分类"),
         f"{_txt(ident.get('fund_family'))} / {_txt(ident.get('category'))}"
         if ident.get("fund_family") or ident.get("category") else "n/a"),
        (L("Expense ratio", "费用率"), _pct(ident.get("expense_ratio"), 3)),
        (L("AUM ($B)", "规模（十亿美元）"), _num(ident.get("total_assets"), 1)),
        (L("Price", "当前价格"), _usd(ident.get("price"))),
        (L("Market cap ($B)", "当前市值（十亿美元）"), _num(ident.get("market_cap"), 1)),
        (L("Day change", "当日涨跌"), _signed_pct(ident.get("day_change"))),
    ])))

    # ── §5/§7 valuation ──────────────────────────────────────────────────
    blocks.append(_block(L("Valuation (current)", "当前估值"), _lines([
        ("PE (TTM)", _num(val.get("pe_ttm"), 1, "x")),
        ("Forward PE", _num(val.get("forward_pe"), 1, "x")),
        ("PEG", _num(val.get("peg"), 2)),
        ("EV/EBITDA", _num(val.get("ev_ebitda"), 1, "x")),
        ("P/S", _num(val.get("ps_ratio"), 2)),
        ("P/B", _num(val.get("pb_ratio"), 2)),
        (L("EPS (TTM)", "EPS (TTM)"), _usd(val.get("eps"))),
        (L("FCF ($B)", "自由现金流（十亿美元）"), _num(val.get("fcf"), 2)),
        (L("FCF yield", "FCF Yield"), _pct(val.get("fcf_yield"), 2)),
        (L("Dividend yield", "股息率"), _pct(val.get("dividend_yield"), 2)),
        (L("Payout ratio", "分红率"), _pct(val.get("payout_ratio"))),
        (L("EV ($B)", "企业价值（十亿美元）"), _num(val.get("ev"), 1)),
        (L("EBITDA ($B)", "EBITDA（十亿美元）"), _num(val.get("ebitda"), 1)),
    ])))

    # PE range vs its own history (derived client-side from the PE series)
    hist_val = bundle.get("valuation_history") or {}
    blocks.append(_block(L("Valuation vs its own history", "估值与自身历史比较"), _lines([
        (L("Current PE", "当前 PE"), _num(hist_val.get("pe_now"), 1, "x")),
        (L("PE mean (window)", "区间平均 PE"), _num(hist_val.get("pe_avg"), 1, "x")),
        (L("PE low (window)", "区间最低 PE"), _num(hist_val.get("pe_low"), 1, "x")),
        (L("PE high (window)", "区间最高 PE"), _num(hist_val.get("pe_high"), 1, "x")),
        (L("Percentile in window", "在区间中的百分位"), _pct(hist_val.get("pe_percentile"), 0)),
        (L("Window length", "统计窗口"), _txt(hist_val.get("window"))),
        (L("Price z-score", "价格 z-score"), _num(zs.get("z"), 2)),
        (L("Z-score zone", "z-score 区域"), _txt(zs.get("zone"))),
    ])))

    # ── §5 growth / margins / balance sheet ──────────────────────────────
    blocks.append(_block(L("Growth", "增长"), _lines([
        (L("Revenue growth YoY", "收入增速 YoY"), _pct(growth.get("revenue_growth"))),
        (L("Revenue 3y CAGR", "收入 3 年 CAGR"), _pct(growth.get("revenue_cagr_3y"))),
        (L("EPS growth TTM", "EPS 增速 TTM"), _pct(growth.get("eps_growth_ttm"))),
        (L("EPS growth latest quarter", "EPS 增速（最新季度）"), _pct(growth.get("eps_growth_q"))),
        (L("Revenue QoQ (latest)", "收入 QoQ（最新季度）"), _pct(growth.get("revenue_qoq"))),
        (L("Revenue YoY (latest quarter)", "收入 YoY（最新季度）"), _pct(growth.get("revenue_yoy_q"))),
        (L("Accelerating or decelerating?", "加速还是减速"), _txt(growth.get("trend"))),
    ])))

    blocks.append(_block(L("Margins & returns", "利润率与回报"), _lines([
        (L("Gross margin", "毛利率"), _pct(marg.get("gross_margin"))),
        (L("Operating margin", "营业利润率"), _pct(marg.get("operating_margin"))),
        (L("Net margin", "净利率"), _pct(marg.get("net_margin"))),
        (L("FCF margin", "FCF Margin"), _pct(marg.get("fcf_margin"))),
        ("ROE", _pct(marg.get("roe"))),
        ("ROA", _pct(marg.get("roa"))),
    ])))

    blocks.append(_block(L("Balance sheet", "资产负债表"), _lines([
        (L("Cash ($B)", "现金（十亿美元）"), _num(bal.get("total_cash"), 1)),
        (L("Total debt ($B)", "总债务（十亿美元）"), _num(bal.get("total_debt"), 1)),
        (L("Net cash / (net debt) ($B)", "净现金 /（净负债）（十亿美元）"), _num(bal.get("net_cash"), 1)),
        (L("Debt / equity", "债务/权益"), _num(bal.get("debt_equity"), 2)),
        (L("Current ratio", "流动比率"), _num(bal.get("current_ratio"), 2)),
        (L("Interest coverage", "利息覆盖"), _num(bal.get("interest_coverage"), 1, "x")),
        (L("Shares outstanding (B)", "总股本（十亿股）"), _num(bal.get("shares_outstanding"), 2)),
        (L("Share count change (annual rows)", "股本变化（按年度报表）"), _txt(bal.get("share_trend"))),
        ("Beta", _num(bal.get("beta"), 2)),
    ])))

    # ── §5 statements ─────────────────────────────────────────────────────
    blocks.append(_block(
        L("Annual financials (newest first, $B; est = consensus estimate)",
          "年度财务（新到旧，单位十亿美元；est = 一致预期）"),
        _table(bundle.get("annual") or [], [
            ("year", L("Year", "年度")), ("is_estimate", "est"),
            ("revenue", L("Revenue", "收入")), ("gross_profit", L("Gross profit", "毛利")),
            ("ebitda_is", "EBITDA"), ("net_income", L("Net income", "净利润")),
            ("eps_diluted", L("EPS dil.", "摊薄EPS")),
            ("gross_margin_pct", L("GM%", "毛利率%")), ("net_margin_pct", L("NM%", "净利率%")),
        ], _MAX_ANNUAL)))

    blocks.append(_block(
        L("Quarterly financials (last 8 quarters, newest first, $B)",
          "季度财务（最近 8 个季度，新到旧，单位十亿美元）"),
        _table(bundle.get("quarterly") or [], [
            ("quarter", L("Quarter", "季度")), ("revenue", L("Revenue", "收入")),
            ("gross_profit", L("Gross profit", "毛利")),
            ("net_income", L("Net income", "净利润")), ("eps_basic", "EPS"),
        ], _MAX_QUARTERS)))

    # ── §8 earnings surprises ─────────────────────────────────────────────
    blocks.append(_block(
        L("Earnings surprise history (beat/miss vs consensus EPS)",
          "财报 EPS 超预期历史（Beat/Miss）"),
        _table(bundle.get("earnings_markers") or [], [
            ("date", L("Date", "日期")), ("reported_eps", L("Reported", "实际")),
            ("estimated_eps", L("Estimate", "预期")), ("surprise_pct", L("Surprise %", "超预期%")),
        ], _MAX_EARNINGS)))

    blocks.append(_block(L("Key dates", "关键日期"), _lines([
        (L("Next earnings", "下次财报"), _txt(bundle.get("earnings_date"))),
        (L("Ex-dividend", "除息日"), _txt(bundle.get("ex_dividend_date"))),
    ])))

    # ── §7 analyst consensus ──────────────────────────────────────────────
    blocks.append(_block(L("Analyst consensus", "分析师一致预期"), _lines([
        (L("Rating", "评级"), _txt(ana.get("recommendation"))),
        (L("Mean score (1=buy,5=sell)", "评级均值（1买 5卖）"), _num(ana.get("recommendation_mean"), 2)),
        (L("Analyst count", "覆盖分析师数"), _txt(ana.get("analyst_count"))),
        (L("Target (mean)", "目标价（均值）"), _usd(ana.get("target_price"))),
        (L("Target median", "目标价中位数"), _usd(ana.get("target_median"))),
        (L("Target high / low", "目标价 高 / 低"),
         f"{_usd(ana.get('target_high'))} / {_usd(ana.get('target_low'))}"
         if ana.get("target_high") else "n/a"),
        (L("Implied upside", "隐含上涨空间"), _signed_pct(ana.get("upside"))),
    ])))

    # ── §10 technical structure ───────────────────────────────────────────
    blocks.append(_block(L("Technical structure", "技术结构"), _lines([
        (L("Last close", "最新收盘"), _usd(tech.get("last"))),
        ("MA5", _usd(tech.get("ma5"))),
        ("MA20", _usd(tech.get("ma20"))),
        ("MA60", _usd(tech.get("ma60"))),
        (L("MA120 (weekly-derived ≈MA24W)", "MA120（周线折算 ≈MA24W）"), _usd(tech.get("ma120"))),
        (L("MA240 (weekly-derived ≈MA48W)", "MA240（周线折算 ≈MA48W）"), _usd(tech.get("ma240"))),
        (L("Price vs MA20 / MA60", "价格相对 MA20 / MA60"),
         f"{_signed_pct(tech.get('vs_ma20'))} / {_signed_pct(tech.get('vs_ma60'))}"),
        ("RSI(14)", _num(tech.get("rsi14"), 1)),
        (L("MACD line / signal / hist", "MACD / 信号线 / 柱"),
         f"{_num(tech.get('macd'), 3)} / {_num(tech.get('macd_signal'), 3)} / {_num(tech.get('macd_hist'), 3)}"),
        (L("Stochastic %K / %D", "Stochastic %K / %D"),
         f"{_num(tech.get('stoch_k'), 1)} / {_num(tech.get('stoch_d'), 1)}"),
        (L("Bollinger upper / mid / lower", "布林 上 / 中 / 下"),
         f"{_usd(tech.get('bb_upper'))} / {_usd(tech.get('bb_mid'))} / {_usd(tech.get('bb_lower'))}"),
        (L("HV20 / HV60 (annualised)", "HV20 / HV60（年化）"),
         f"{_pct(tech.get('hv20'))} / {_pct(tech.get('hv60'))}"),
        (L("Volume (latest vs 20d avg)", "成交量（最新 vs 20日均）"),
         f"{_num(tech.get('volume_last'), 0)} vs {_num(tech.get('volume_avg20'), 0)}"
         f" ({_num(tech.get('volume_rel'), 2, 'x')})" if tech.get("volume_last") else "n/a"),
        (L("52w high / low", "52周 高 / 低"),
         f"{_usd(tech.get('week52_high'))} / {_usd(tech.get('week52_low'))}"),
        (L("Distance from 52w high", "距 52 周高点"), _signed_pct(tech.get("from_52w_high"))),
        (L("Distance from 52w low", "距 52 周低点"), _signed_pct(tech.get("from_52w_low"))),
        (L("Return 20d / 60d / YTD", "20日 / 60日 / 年初至今 收益"),
         f"{_signed_pct(tech.get('ret_20d'))} / {_signed_pct(tech.get('ret_60d'))} / {_signed_pct(tech.get('ytd_return'))}"),
        (L("Support zones (nearest first)", "支撑区（由近到远）"), _zones(tech.get("support_zones"))),
        (L("Resistance zones (nearest first)", "压力区（由近到远）"), _zones(tech.get("resistance_zones"))),
    ])))

    # ── §4 relative strength ──────────────────────────────────────────────
    blocks.append(_block(L("Relative strength", "相对强弱"), _lines([
        (L("Sector ETF used", "对标行业 ETF"), _txt(rel.get("sector_etf"))),
        (L("vs SPY — 1M / 3M / 1Y", "vs 标普500 — 1个月 / 3个月 / 1年"),
         f"{_signed_pct(rel.get('vs_spy_1m'))} / {_signed_pct(rel.get('vs_spy_3m'))} / {_signed_pct(rel.get('vs_spy_1y'))}"),
        (L("vs sector ETF — 1M / 3M / 1Y", "vs 行业ETF — 1个月 / 3个月 / 1年"),
         f"{_signed_pct(rel.get('vs_sector_1m'))} / {_signed_pct(rel.get('vs_sector_3m'))} / {_signed_pct(rel.get('vs_sector_1y'))}"),
        (L("Own return — 1M / 3M / 1Y", "自身收益 — 1个月 / 3个月 / 1年"),
         f"{_signed_pct(rel.get('self_1m'))} / {_signed_pct(rel.get('self_3m'))} / {_signed_pct(rel.get('self_1y'))}"),
    ])))

    # ── §7 peer comparison ────────────────────────────────────────────────
    peers = bundle.get("peers") or {}
    blocks.append(_block(
        L(f"Peer comparison (group: {_txt(peers.get('group'))})",
          f"同行对比（分组：{_txt(peers.get('group'))}）"),
        _table(peers.get("rows") or [], [
            ("ticker", L("Ticker", "代码")), ("pe", "PE"), ("fwd_pe", "Fwd PE"),
            ("peg", "PEG"), ("ps", "P/S"),
            ("rev_growth", L("Rev growth %", "收入增速%")),
            ("week52_change", L("52w %", "52周%")), ("upside", L("Upside %", "上涨空间%")),
        ], _MAX_PEERS)))

    # ── §9 news ───────────────────────────────────────────────────────────
    news = [n for n in (bundle.get("news") or []) if isinstance(n, dict)][:_MAX_NEWS]
    if news:
        news_body = "\n".join(
            f"{i}. [{'!' if n.get('important') else ' '}] {_txt(n.get('date'))} — "
            f"{_txt(n.get('title'))}  ({_txt(n.get('publisher'))})"
            for i, n in enumerate(news, 1)
        )
        blocks.append(_block(
            L("Recent news headlines (! = flagged important)", "近期新闻标题（! = 重要）"),
            news_body))

    # ── §9/§10 options positioning ────────────────────────────────────────
    blocks.append(_block(L("Options positioning", "期权持仓结构"), _lines([
        (L("Call wall (max call OI strike)", "Call Wall（最大未平仓行权价）"), _usd(opt.get("call_wall"))),
        (L("Put wall", "Put Wall"), _usd(opt.get("put_wall"))),
        (L("Put/call OI ratio", "Put/Call 未平仓比"), _num(opt.get("put_call_ratio"), 2)),
    ])))

    # ── ownership ─────────────────────────────────────────────────────────
    blocks.append(_block(L("Ownership & short interest", "持股结构与做空"), _lines([
        (L("Held by institutions", "机构持股比例"), _pct(bundle.get("held_institutions"))),
        (L("Held by insiders", "内部人持股比例"), _pct(bundle.get("held_insiders"))),
        (L("Short % of float", "空头占流通股"), _pct(bundle.get("short_float"))),
        (L("Short ratio (days to cover)", "空头回补天数"), _num(bundle.get("short_ratio"), 1)),
    ])))

    blocks.append(_block(
        L("13F institutional holders (tracked funds)", "13F 机构持仓（跟踪基金）"),
        _table(bundle.get("institutional") or [], [
            ("fund", L("Fund", "基金")),
            ("value_millions", L("Value $M", "市值 百万")),
            ("pct_portfolio", L("% of fund", "占其组合%")),
            ("change_pct", L("QoQ change %", "环比变化%")),
        ], _MAX_HOLDERS)))

    blocks.append(_block(
        L("Recent insider transactions", "近期内部人交易"),
        _table(bundle.get("insider") or [], [
            ("date", L("Date", "日期")), ("insider", L("Insider", "内部人")),
            ("transaction", L("Type", "类型")), ("shares", L("Shares", "股数")),
            ("value", L("Value $", "金额 $")),
        ], _MAX_INSIDER)))

    # ── forecast models ───────────────────────────────────────────────────
    if fc:
        blocks.append(_block(
            L("Statistical price projections (past-price extrapolation only — "
              "no fundamentals, guidance or catalysts)",
              "统计价格外推（仅基于历史价格，不含基本面/guidance/催化剂）"),
            _lines([
                (L("Prophet end-of-horizon", "Prophet 期末值"), _usd(fc.get("prophet"))),
                (L("ARIMA end-of-horizon", "ARIMA 期末值"), _usd(fc.get("arima"))),
                (L("Linear end-of-horizon", "线性 期末值"), _usd(fc.get("linear"))),
                (L("Horizon", "预测期"), _txt(fc.get("horizon"))),
            ])))

    # ── §12 portfolio, incl. ETF look-through ─────────────────────────────
    lt_rows = [r for r in (port.get("lookthrough") or []) if isinstance(r, dict)]
    port_body = _lines([
        (L("Total account value", "账户总值"), _usd(port.get("total_account_value"), 0)),
        (L("Direct position value", "本股直接持仓市值"), _usd(port.get("direct_value"), 0)),
        (L("Direct position", "直接仓位"), _pct(port.get("direct_pct"), 2)),
        (L("Shares held", "持仓股数"), _num(port.get("shares"), 0)),
        (L("Average cost", "平均成本"), _usd(port.get("avg_cost"))),
        (L("Unrealised P/L", "浮动盈亏"), _signed_pct(port.get("unrealized_pct"))),
        (L("Planned max position", "计划最大仓位"), _pct(port.get("max_pct"), 1)),
        (L("Stated role in portfolio", "投资定位（用户填写）"), _txt(port.get("role"))),
        (L("ETF look-through exposure", "ETF 穿透暴露"), _pct(port.get("lookthrough_pct"), 2)),
        (L("Same-sector single-stock exposure", "同板块个股暴露"), _pct(port.get("same_sector_pct"), 2)),
        (L("TRUE TOTAL EXPOSURE (direct + look-through)", "穿透后真实总暴露"),
         _pct(port.get("true_total_pct"), 2)),
        (L("Headroom to planned max", "距计划上限还剩"), _pct(port.get("headroom_pct"), 2)),
    ])
    if lt_rows:
        port_body += "\n\n" + L("Look-through detail:", "穿透明细：") + "\n" + _table(lt_rows, [
            ("holding", L("Holding", "持仓")),
            ("value", L("Value $", "市值 $")),
            ("pct_account", L("% of account", "占账户%")),
            ("weight_in_holding", L("Weight of subject inside it %", "标的在其中权重%")),
            ("exposure_pct", L("Look-through % of account", "穿透后占账户%")),
            ("sector_weight", L("Same-sector weight inside it %", "其中同板块权重%")),
            ("note", L("Data caveat", "数据说明")),
        ], _MAX_LOOKTHROUGH)
        if any(r.get("note") for r in lt_rows):
            port_body += "\n" + L(
                "NOTE: some look-through weights are unavailable upstream, so the computed true "
                "exposure is a FLOOR, not an exact figure. Say so in §12 and use search to "
                "estimate the missing weights.",
                "注意：部分穿透权重上游数据缺失，因此计算出的真实暴露是**下限**而非精确值。"
                "§12 必须说明这一点，并可用搜索补估缺失的权重。")
    blocks.append(_block(
        L("Portfolio context (user-supplied + computed look-through)",
          "组合上下文（用户填写 + 计算得到的穿透暴露）"),
        port_body))

    other = [r for r in (port.get("holdings") or []) if isinstance(r, dict)]
    if other:
        blocks.append(_block(
            L("All current holdings (for correlation / overlap judgement)",
              "当前全部持仓（用于相关性/重叠判断）"),
            _table(other, [
                ("ticker", L("Ticker", "代码")), ("value", L("Value $", "市值 $")),
                ("pct_account", L("% of account", "占账户%")),
                ("kind", L("Type", "类型")),
            ], _MAX_LOOKTHROUGH)))

    return "\n".join(b for b in blocks if b)


def _template_instructions(lang: str, has_portfolio: bool) -> str:
    """The 17-section template the model must fill in, plus the memo contract."""
    if lang == "zh":
        missing_note = (
            "用户已提供持仓数据，§1/§12/§13/§14 必须使用上面「组合上下文」里的真实数字，"
            "包括穿透后的真实暴露。"
            if has_portfolio else
            "用户未提供持仓数据。§1 的持仓字段、§12 的穿透暴露、§13/§14 的账户占比一律写"
            "「待填写」，但仍要给出以百分比表示的**建议**目标仓位和分档买入的价格区间。"
        )
        return f"""
请严格按下面的 17 段模板输出完整研究报告，用 Markdown（标题、表格、清单都可以用）。
每一段都要写，不能跳过；某项确实没有数据就写「数据缺失」，绝对不要编造数字。

**输出格式硬性要求：**
* 不要复述模板里的提问、说明或要求文字——每一节只输出你填好的分析结论。
* 小节标题保持简洁，例如 `## 6. 盈利质量`；不要把括号里的要求抄进标题。
* 直接给答案，不要写「根据模板要求…」这类过场话。

{missing_note}

## 1. 基本信息
股票代码 / 名称 / 当前价格 / 当前市值 / 当前持仓 / 平均成本 / 占总账户比例 /
计划最大仓位 / 投资定位（长期核心仓、成长仓、周期仓、反转仓、战术交易仓——选一个并说明理由）

## 2. 一句话投资逻辑
**压缩到 3 句话以内。** 必须包含：主要增长来源、市场目前可能低估了什么、
未来 1–3 年的核心驱动力、以及「什么事情发生后这个逻辑会被证明错误」。

## 3. 公司质量
- 商业模式：靠什么赚钱、收入组成、利润最高的业务、是否有经常性收入、客户是否集中
- 护城河逐项打分 1–5：品牌 / 技术 / 网络效应 / 转换成本 / 成本优势 / 规模优势 / 监管壁垒
- **综合护城河：__/5**（要给出加权理由）

## 4. 行业与竞争格局
行业阶段（高景气/复苏/成熟/下行/衰退，选一个）、TAM 是否扩大、公司市占率趋势、
最大竞争对手、相对竞争优势、是否存在价格战、是否存在技术替代风险。
**行业相对强弱**：公司 vs 行业ETF、公司 vs 标普500，分别给 1个月 / 3个月 / 1年。

## 5. 财务趋势
用最近 8 个季度 + 3 个年度的数据：
- 收入：YoY、QoQ、3年CAGR、**在加速还是减速**
- 盈利：毛利率、营业利润率、净利率、EPS增长、扣非利润增长
- 现金流：经营现金流、自由现金流、FCF Margin、**FCF 是否持续高于净利润**
- 资产负债表：现金、总债务、净现金/净负债、利息覆盖、**股本是否持续稀释**

## 6. 盈利质量
净利润增长是否来自主营业务？有多少来自投资收益 / 公允价值变化 / 政府补贴 /
资产出售 / 税务收益 / 一次性项目？
**必须把三个数字并排列出**：GAAP（归母）利润、扣非利润、自由现金流。
判定：利润上涨 + 扣非上涨 + FCF 上涨 = 高质量改善；只有 GAAP 上涨 = 存疑。

## 7. 估值
不要只看一个 PE。
- 当前估值：TTM PE、Forward PE、动态PE、PEG、EV/EBITDA、P/S、FCF Yield
- 与自身历史比较：当前PE、5年平均PE、5年低位、5年高位（说明数据来源和窗口）
- 与同行比较：**输出表格**（本公司 / 对手A / 对手B），列 PE、增速、PEG、毛利率、FCF Margin
- 情景估值三档，每档都要 **EPS × 合理PE = 目标价**，并给出涨跌幅：
  - Bear Case
  - Base Case
  - Bull Case
- **最后必须计算：预期收益 / 潜在损失（赔率）**，而不是只说「贵」或「便宜」。

## 8. 最新财报
Revenue：Beat / Meet / Miss；EPS：Beat / Meet / Miss；毛利率；FCF；Guidance。
**最重要的三个变化**（编号 1. 2. 3.）。
Management Commentary 重点找：需求、定价、利润率、库存、Capex、AI/新产品、
客户需求、下半年 Guidance。

## 9. 新闻与催化剂
未来 1–12 个月，分「正面催化剂」和「负面催化剂」两栏。
**每一条都必须回答：它到底改变了收入、利润、估值，还是只是情绪？**
格式：`- <事件> → 改变的是【收入/利润/估值/情绪】：<一句话说明>`

## 10. 技术结构
- 当前趋势（强势上升 / 上升趋势回调 / 高位横盘 / 突破回踩 / 超跌反弹 / 底部构筑 /
  下降趋势——**只能选一个**）
- 均线：MA5、MA20、MA60、MA120、MA240（周线折算的要标注）
- Momentum：RSI、MACD、成交量、相对强度
- 关键价格：强支撑 / 第一支撑 / 当前价格 / 第一压力 / 强压力
  **全部写成区间**，例如「98–101 支撑区」，不允许写「支撑是 100」。

## 11. 下跌原因诊断
如果从 52 周高点或近期高点回撤超过 10%，必须逐项判断原因：
大盘风险 / 行业风险 / 公司基本面 / 估值压缩 / 获利回吐 / 政策 / 财报 / 技术破位。
然后回答核心问题：**价格跌了，但价值有没有跌？** 这决定能不能抄底。
若回撤不到 10%，写明「回撤未达 10%，本节不触发」并给出实际回撤幅度。

## 12. 组合适配度（最重要的一节）
- 当前直接仓位 __%
- **穿透后真实暴露**：把 ETF 内部持仓、同行业个股、相同风险因子加总
  （例：MSFT + QQQM里的MSFT + XLK里的MSFT + IGV的软件暴露）
- 与现有资产相关性：高 / 中 / 低，逐个点名
- 加仓后测算：当前 __% + 加仓 __% = 最终 __%，**是否超过最大仓位？**

## 13. 仓位等级
用统一分级定位这只票，并说明理由：
0–1% 观察仓；1–3% 试探/战术仓；3–5% 正常仓；5–8% 重要仓位；
8–12% 高确信度核心仓；12–15% 重仓核心；>15% 必须明确知道为什么承担集中风险。
（行业 ETF 可以比单股适度更高。）

## 14. 三档买入计划
不要回答「现在能买吗」，改成三档，每档给出价格区间、金额或账户占比、触发条件：
- **第一笔：低吸仓** — 条件：支撑区止跌
- **第二笔：深跌仓** — 条件：基本面没变化，但市场继续杀估值
- **第三笔：确认仓** — 条件：收复关键均线 / 突破平台

## 15. 明确禁止加仓条件
列出具体的、可观测的条件（EPS预期连续下调、Guidance下调、毛利率明显恶化、
FCF恶化、Thesis失效、行业景气反转、仓位已超上限……）。
必须包含这一句：**跌得更多，本身不是加仓理由。**

## 16. 减仓规则
区分三种情况并分别给出动作：
- A. 正常波动 → 不操作
- B. 估值过热 → 减交易仓，保留核心
- C. Thesis 破坏 → 主动退出
每种都要回答：减多少账户百分点？什么价格/事件触发？是否保留核心仓？

## 17. 最终评分卡
输出表格并**算出加权总分**：公司质量20 / 增长15 / 盈利质量10 / 估值15 /
行业景气10 / 催化剂10 / 技术结构10 / 组合适配10 = 100。
给出档位：85+ 核心候选；75–84 可持有/分批布局；65–74 观察或战术仓；
55–64 赔率一般；<55 通常回避。

---

# 最终 Investment Memo（强制输出这 8 项，一项一行）

> **股票：**
> **当前价格：**
> **投资定位：** 核心 / 成长 / 周期 / 反转 / 战术
> **核心逻辑：**
> **最大风险：**
> **合理价值区间：**
> **当前动作：** 买入 / 小买 / 等待 / 持有 / 减仓 / 卖出
> **目标仓位：__%**

然后再给：
**低吸区：** / **确认买入区：** / **停止加仓线：** / **Thesis失效条件：** /
**下一次复盘日期或事件：**
""".strip()

    missing_note_en = (
        "The user supplied portfolio data. §1/§12/§13/§14 must use the real numbers from the "
        "Portfolio context block above, including the computed look-through exposure."
        if has_portfolio else
        "The user supplied no portfolio data. Write \"not provided\" for the holdings fields in "
        "§1, the look-through exposure in §12, and the account percentages in §13/§14 — but still "
        "give a RECOMMENDED target position as a % of account and concrete buy-tranche price zones."
    )
    return f"""
Produce the full research report following the 17-section template below, in Markdown (headers,
tables and lists are fine). Every section must appear — do not skip any. Where a figure genuinely
is not available, write "no data" rather than inventing it.

**Output format requirements:**
* Do not echo the template's questions, instructions or requirement text — under each heading,
  output only your filled-in analysis.
* Keep headings clean, e.g. `## 6. Earnings Quality`; do not copy the parenthetical requirements
  into the heading.
* Answer directly — no "as requested by the template" preamble.

{missing_note_en}

## 1. Basic Info
Ticker / name / current price / market cap / current position / average cost / % of total account /
planned max position / role in portfolio (long-term core, growth, cyclical, turnaround, or
tactical trade — pick one and justify it).

## 2. One-Sentence Thesis
**Compress to 3 sentences maximum.** Must cover: the main source of growth, what the market may
be underestimating today, the core driver over the next 1–3 years, and what event would prove the
thesis wrong.

## 3. Company Quality
- Business model: how it makes money, revenue mix, most profitable segment, recurring revenue,
  customer concentration.
- Moat scored 1–5 each: brand / technology / network effects / switching costs / cost advantage /
  scale / regulatory barriers.
- **Overall moat: __/5** with the weighting rationale.

## 4. Industry & Competition
Industry phase (booming / recovering / mature / declining / in recession — pick one), whether TAM
is expanding, market-share trend, largest competitor, relative advantage, price-war risk,
technology-substitution risk.
**Relative strength**: stock vs sector ETF and stock vs S&P 500, each over 1M / 3M / 1Y.

## 5. Financial Trends
Use the last 8 quarters + 3 fiscal years:
- Revenue: YoY, QoQ, 3-year CAGR, and **is growth accelerating or decelerating**
- Profit: gross / operating / net margin, EPS growth, core (ex-items) profit growth
- Cash flow: operating CF, FCF, FCF margin, **is FCF persistently above net income**
- Balance sheet: cash, total debt, net cash/(net debt), interest coverage,
  **is the share count being persistently diluted**

## 6. Earnings Quality
Is net-income growth coming from the core business? How much comes from investment gains,
fair-value changes, government subsidies, asset sales, tax benefits or one-off items?
**Show three numbers side by side**: GAAP net income, core (ex-items) profit, free cash flow.
Verdict: profit up + core up + FCF up = high-quality improvement. GAAP up alone = suspect.

## 7. Valuation
Never rely on a single PE.
- Current: TTM PE, forward PE, dynamic PE, PEG, EV/EBITDA, P/S, FCF yield
- Vs its own history: current PE, 5-year average, 5-year low, 5-year high (state the data window)
- Vs peers: **output a table** (this company / peer A / peer B) with PE, growth, PEG, gross
  margin, FCF margin
- Three scenarios, each as **EPS × justified PE = target price** plus the % move:
  - Bear Case
  - Base Case
  - Bull Case
- **Finish by computing expected gain / potential loss (the payoff ratio)** — not just a verdict
  of "expensive" or "cheap".

## 8. Latest Earnings Report
Revenue: Beat / Meet / Miss. EPS: Beat / Meet / Miss. Gross margin. FCF. Guidance.
**The three most important changes** (numbered 1. 2. 3.).
From management commentary, extract: demand, pricing, margin, inventory, capex, AI / new products,
customer demand, second-half guidance.

## 9. News & Catalysts
Next 1–12 months, split into positive and negative catalysts.
**Every single item must answer: does it change revenue, profit, valuation — or is it just
sentiment?** Use the format:
`- <event> → changes [REVENUE / PROFIT / VALUATION / SENTIMENT]: <one line>`

## 10. Technical Structure
- Current trend (strong uptrend / pullback within uptrend / high-level consolidation / breakout
  retest / oversold bounce / basing / downtrend — **pick exactly one**)
- Moving averages: MA5, MA20, MA60, MA120, MA240 (flag weekly-derived values)
- Momentum: RSI, MACD, volume, relative strength
- Key levels: strong support / first support / current price / first resistance / strong resistance
  — **all written as zones**, e.g. "98–101 support". Never "support is 100".

## 11. Drawdown Diagnosis
If the drawdown from the 52-week or recent high exceeds 10%, work through the cause:
market risk / sector risk / company fundamentals / multiple compression / profit-taking /
policy / earnings / technical breakdown.
Then answer the core question: **the price fell — did the value fall?** That decides whether this
is a dip worth buying. If the drawdown is under 10%, say the section is not triggered and give the
actual drawdown figure.

## 12. Portfolio Fit (the most important section)
- Current direct position __%
- **True look-through exposure**: add ETF internal holdings, same-sector single stocks and shared
  risk factors (e.g. MSFT direct + MSFT inside QQQM + MSFT inside XLK + software beta via IGV)
- Correlation with existing assets: high / medium / low, naming each one
- If we add: current __% + add __% = final __%. **Does that breach the max position?**

## 13. Position Tier
Place the name on the standard ladder and justify it:
0–1% watch; 1–3% probe/tactical; 3–5% normal; 5–8% significant; 8–12% high-conviction core;
12–15% heavy core; >15% requires an explicit reason to accept concentration risk.
(Sector ETFs may sit moderately higher than single stocks.)

## 14. Three-Tranche Buy Plan
Do not answer "can I buy now". Give three tranches, each with a price zone, amount or % of
account, and a trigger condition:
- **Tranche 1: dip buy** — trigger: price stabilises in the support zone
- **Tranche 2: deep-drop buy** — trigger: fundamentals unchanged but the market keeps compressing
  the multiple
- **Tranche 3: confirmation buy** — trigger: reclaims the key moving average / breaks out of the
  base

## 15. Explicit No-Add Conditions
List concrete, observable conditions (consecutive EPS estimate cuts, guidance cut, clear gross
margin deterioration, FCF deterioration, thesis broken, industry cycle rolling over, position
already at the cap …).
Must include this line verbatim: **A bigger decline is not by itself a reason to add.**

## 16. Trim Rules
Distinguish three cases with a specific action for each:
- A. Normal volatility → do nothing
- B. Valuation overheated → trim the trading sleeve, keep the core
- C. Thesis broken → exit deliberately
For each: how many account percentage points to cut, what price or event triggers it, and whether
a core position is retained.

## 17. Scorecard
Output a table and **compute the weighted total**: company quality 20 / growth 15 / earnings
quality 10 / valuation 15 / industry cycle 10 / catalysts 10 / technical structure 10 /
portfolio fit 10 = 100.
Then give the band: 85+ core candidate; 75–84 hold or scale in; 65–74 watch or tactical;
55–64 mediocre odds; <55 usually avoid.

---

# Final Investment Memo (these 8 lines are mandatory, one per line)

> **Stock:**
> **Current price:**
> **Role:** core / growth / cyclical / turnaround / tactical
> **Core thesis:**
> **Biggest risk:**
> **Fair value range:**
> **Action now:** buy / small buy / wait / hold / trim / sell
> **Target position: __%**

Then add:
**Dip-buy zone:** / **Confirmation-buy zone:** / **Stop-adding level:** /
**Thesis-invalidation conditions:** / **Next review date or event:**
""".strip()


def build_research_prompt(
    ticker: str,
    bundle: dict,
    lang: str = "en",
    today: str | None = None,
) -> tuple[str, str]:
    """Build the full deep-research prompt.

    Returns:
        ``(system_instruction, user_prompt)``.  The system instruction carries
        the analyst persona and the anti-bias rules; the user prompt carries the
        verified data pack plus the 17-section template.
    """
    zh = lang == "zh"
    ident = bundle.get("identity") or {}
    name = _txt(ident.get("name"))
    is_etf = str(ident.get("quote_type") or "").upper() == "ETF"
    port = bundle.get("portfolio") or {}
    has_portfolio = bool(port.get("total_account_value") or port.get("direct_value")
                         or port.get("holdings"))

    if zh:
        system = (
            "你是一位纪律极强的机构股票研究分析师，服务的是一个「核心ETF + 大型科技个股 + "
            "行业ETF + 战术仓」的真实账户。\n\n"
            "铁律：\n"
            "1. 严格区分「公司长期质量」和「短期交易位置」——优秀的公司可以在错误的价格上，"
            "平庸的公司也可以在正确的价格上。\n"
            "2. 不要因为跌得多就默认便宜；也不要因为公司优秀就默认现价值得买。\n"
            "3. 估值必须多角度：TTM 与 Forward/动态估值并看，PEG、EV/EBITDA、P/S、FCF Yield "
            "都要考虑，并和自身历史、同行横向对比。\n"
            "4. 分析盈利质量，把一次性/非经常性收益（投资收益、公允价值变动、政府补贴、"
            "资产出售、税务收益）从主营业务利润里剥离出来。\n"
            "5. 支撑/压力永远写成区间，不写单一数字。\n"
            "6. 结论要给赔率（预期收益 / 潜在损失），不能只给「贵/便宜」。\n"
            "7. 站内提供的「已核实数据」优先于你搜索到的数字；两者冲突时以站内数据为准并"
            "指出差异。搜索用来补充 guidance、管理层表述、行业新闻、竞争格局和分析师预期。\n"
            "8. 任何你不确定或查不到的数字，写「数据缺失」——绝对不要编造。\n"
            "9. 最后必须输出完整的 Investment Memo 8 项。\n"
            "10. 这是研究分析，不是投资建议；不要给出保证性的收益承诺。"
        )
        header = (
            f"# 研究对象：{ticker}（{name}）{'　类型：ETF' if is_etf else ''}\n"
            f"{'今天日期：' + today if today else ''}\n\n"
            "请先用 Google 搜索核实/补充：最新股价与市值、最近一期财报（收入、EPS、毛利率、"
            "FCF、Guidance 是 Beat/Meet/Miss）、公司公告、行业新闻与竞争格局、分析师最新预期"
            "与目标价、以及未来 1–12 个月的已知催化剂（财报日、新产品、政策、降息路径等）。\n"
            "搜索完成后，结合下面的站内已核实数据，按模板输出。\n"
        )
        data_title = "# 站内已核实数据（优先采信）"
    else:
        system = (
            "You are a highly disciplined institutional equity research analyst serving a real "
            "account built from core ETFs + large-cap tech single stocks + sector ETFs + a "
            "tactical sleeve.\n\n"
            "Hard rules:\n"
            "1. Strictly separate long-term company quality from short-term trading position — a "
            "great company can be at a bad price, and a mediocre one at a good price.\n"
            "2. Never assume something is cheap just because it fell, nor that a great company is "
            "worth buying at today's price.\n"
            "3. Value from multiple angles: TTM and forward/dynamic multiples together, plus PEG, "
            "EV/EBITDA, P/S and FCF yield, compared both to its own history and to peers.\n"
            "4. Analyse earnings quality — strip one-off and non-recurring gains (investment "
            "income, fair-value changes, subsidies, asset sales, tax benefits) out of core "
            "operating profit.\n"
            "5. Support and resistance are always zones, never single numbers.\n"
            "6. Conclude with a payoff ratio (expected gain / potential loss), not just "
            "\"expensive\" or \"cheap\".\n"
            "7. The verified in-app data below outranks anything you find via search. If they "
            "conflict, use the in-app figure and flag the discrepancy. Use search to add "
            "guidance, management commentary, industry news, competitive dynamics and analyst "
            "estimates.\n"
            "8. Any figure you cannot verify: write \"no data\" — never fabricate.\n"
            "9. Always end with the complete 8-line Investment Memo.\n"
            "10. This is research analysis, not investment advice. Make no guaranteed-return "
            "claims."
        )
        header = (
            f"# Subject: {ticker} ({name}){'  — type: ETF' if is_etf else ''}\n"
            f"{'Today: ' + today if today else ''}\n\n"
            "First use Google Search to verify and fill in: latest price and market cap; the most "
            "recent earnings report (revenue, EPS, gross margin, FCF, and whether guidance was a "
            "Beat/Meet/Miss); company announcements; industry news and competitive dynamics; the "
            "latest analyst estimates and price targets; and known catalysts over the next 1–12 "
            "months (earnings date, new products, policy, rate path).\n"
            "Then combine that with the verified in-app data below and fill in the template.\n"
        )
        data_title = "# Verified in-app data (prefer these figures)"

    etf_note = ""
    if is_etf:
        etf_note = (
            "\n**这是一只 ETF。** §3「公司质量」改为评估：跟踪指数与编制规则、成分股集中度、"
            "费用率、规模与流动性、跟踪误差、分红处理。§6「盈利质量」改为评估底层成分股的"
            "整体盈利质量与指数成分变化。§12 的穿透暴露对 ETF 尤其关键。\n"
            if zh else
            "\n**This is an ETF.** For §3 Company Quality, assess instead: the index tracked and "
            "its construction rules, holdings concentration, expense ratio, AUM and liquidity, "
            "tracking error and distribution treatment. For §6 Earnings Quality, assess the "
            "aggregate earnings quality of the underlying holdings and index-composition drift. "
            "§12 look-through exposure matters most of all for an ETF.\n"
        )

    prompt = "\n".join([
        header,
        etf_note,
        data_title,
        "",
        _render_data_pack(bundle, lang),
        "",
        "---",
        "",
        _template_instructions(lang, has_portfolio),
    ])
    log.debug("Research prompt built: ticker=%s lang=%s chars=%d portfolio=%s",
              ticker, lang, len(prompt), has_portfolio)
    return system, prompt


def bundle_fingerprint(bundle: dict) -> str:
    """Short stable hash of the inputs that should invalidate a cached report.

    Only the fields that change the *answer* are hashed — price/technicals move
    constantly, so including them would defeat caching; the portfolio inputs and
    the reporting fundamentals are what matter.
    """
    import hashlib

    port = bundle.get("portfolio") or {}
    key = {
        "v": TEMPLATE_VERSION,
        "acct": port.get("total_account_value"),
        "shares": port.get("shares"),
        "cost": port.get("avg_cost"),
        "max": port.get("max_pct"),
        "role": port.get("role"),
        "hold": sorted(
            f"{h.get('ticker')}:{h.get('value')}"
            for h in (port.get("holdings") or []) if isinstance(h, dict)
        ),
        "eps": _get(bundle, "valuation", "eps"),
        "q0": (bundle.get("quarterly") or [{}])[0].get("quarter")
              if bundle.get("quarterly") else None,
    }
    raw = json.dumps(key, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:12]
