"""
ystocker.wiki
~~~~~~~~~~~~~
The TradeAgents wiki: the product docs at ``/docs`` and the Research Lab at
``/research``, plus the analyst roster the ``/agents`` landing page draws.

Why a registry and not a directory listing
------------------------------------------
Every page is a Jinja content file under ``templates/wiki/``, but the *list* of
pages lives here, with its titles, summaries and dates in both languages. The
sidebar, the prev/next links, the Research Lab index and the browser tab all read
this one table, so a page cannot appear in the nav under one title and render
under another. ``tests/test_wiki.py`` asserts the two halves agree in both
directions: every entry has a content file, and every content file has an entry
— an orphan file is a page nobody can reach, which is the quiet way docs rot.

Bilingual by construction
-------------------------
Each title and summary is a ``{"en": ..., "zh": ...}`` pair rather than an i18n
key, because long-form content is written as paired ``data-l="en"`` /
``data-l="zh"`` blocks (see base.html), not as hundreds of entries in
``static/i18n.js``. The test refuses an empty string in either language for the
reason ``test_i18n_completeness`` does: a blank translation does not render as
nothing, it renders as the *other* language's neighbour, or as a gap in the nav.

Research Lab posts are dated, and the date is the day the finding was measured,
not the day the page was written: a post quotes numbers from that day's data,
and a reader needs to know how old they are.

Pure: no Flask, no I/O. ``desk()`` takes the roster tuples as arguments rather
than importing ``agents`` so it can be tested without the runner.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional, Sequence

#: A slug is a path segment and a template filename, so it is held to the
#: narrowest shape that serves both.
SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

DOC_GROUPS: tuple[dict[str, Any], ...] = (
    {"key": "start",     "title": {"en": "Getting started", "zh": "入门"}},
    {"key": "concepts",  "title": {"en": "Core concepts",   "zh": "核心概念"}},
    {"key": "using",     "title": {"en": "Using reports",   "zh": "使用报告"}},
    {"key": "reference", "title": {"en": "Reference",       "zh": "参考"}},
)

#: In reading order. Prev/next follow this order, across group boundaries.
DOCS: tuple[dict[str, Any], ...] = (
    {"slug": "overview", "group": "start",
     "title": {"en": "Overview", "zh": "概览"},
     "summary": {"en": "What the desk is, what a run produces, and the boundary it keeps.",
                 "zh": "投研台是什么、一次分析产出什么，以及它恪守的边界。"}},
    {"slug": "quick-start", "group": "start",
     "title": {"en": "Quick start", "zh": "快速开始"},
     "summary": {"en": "Sign in, pick a ticker and a date, and read your first report.",
                 "zh": "登录，选择股票和日期，读完你的第一份报告。"}},
    {"slug": "pricing", "group": "start",
     "title": {"en": "Pricing and credits", "zh": "价格与次数"},
     "summary": {"en": "Three free runs a day, then run packs that never expire.",
                 "zh": "每天 3 次免费分析，之后可购买永不过期的次数包。"}},
    {"slug": "workflow", "group": "concepts",
     "title": {"en": "How a run works", "zh": "一次分析如何运作"},
     "summary": {"en": "From six analyst reports to one signed decision, stage by stage.",
                 "zh": "从六份分析师报告到一份签发的决策，逐阶段拆解。"}},
    {"slug": "desk", "group": "concepts",
     "title": {"en": "The desk", "zh": "投研团队"},
     "summary": {"en": "Every role on the team, what it reads and what it writes.",
                 "zh": "团队里的每个角色：读什么，写什么。"}},
    {"slug": "models", "group": "concepts",
     "title": {"en": "Models and thinking depth", "zh": "模型与思考深度"},
     "summary": {"en": "Five model choices, the thinking levels each accepts, and why a run records what ran.",
                 "zh": "五种模型选择、各自支持的思考深度，以及报告为何记录实际运行的模型。"}},
    {"slug": "markets", "group": "concepts",
     "title": {"en": "Markets and data", "zh": "市场与数据"},
     "summary": {"en": "US listings, ADRs and China A-shares, and where each analyst's evidence comes from.",
                 "zh": "美股、ADR 与 A 股，以及每位分析师的证据来自哪里。"}},
    {"slug": "reports", "group": "using",
     "title": {"en": "Reading a report", "zh": "阅读报告"},
     "summary": {"en": "The conversation view, the decision, PDFs, email and follow-up questions.",
                 "zh": "对话视图、最终决策、PDF、邮件通知与追问。"}},
    {"slug": "sharing", "group": "using",
     "title": {"en": "Sharing a report", "zh": "分享报告"},
     "summary": {"en": "Send a finished report by email, text or WeChat — and take it back.",
                 "zh": "通过邮件、短信或微信发送已完成的报告，并可随时撤回。"}},
    {"slug": "limits", "group": "reference",
     "title": {"en": "Limits and quotas", "zh": "限额与配额"},
     "summary": {"en": "Every daily ceiling, and the reason each one exists.",
                 "zh": "每一项每日上限，以及它存在的理由。"}},
    {"slug": "changelog", "group": "reference",
     "title": {"en": "What's new", "zh": "更新日志"},
     "summary": {"en": "Dated changes to the desk, newest first.",
                 "zh": "投研台的更新记录，按时间倒序。"}},
    {"slug": "faq", "group": "reference",
     "title": {"en": "FAQ", "zh": "常见问题"},
     "summary": {"en": "Short answers to the questions people actually ask.",
                 "zh": "大家真正会问的问题，简短作答。"}},
)

DEFAULT_DOC = "overview"

#: Newest first is enforced by the test, not by sorting here: the order in this
#: tuple is the order a reader sees, and silently re-sorting would hide a typo'd
#: date that put a post in the wrong place.
POSTS: tuple[dict[str, Any], ...] = (
    {"slug": "gics-industry-groups", "date": "2026-09-26", "minutes": 7,
     "kicker": {"en": "Markets · Method note", "zh": "市场 · 方法说明"},
     "title": {"en": "Rotation happens a level down",
               "zh": "轮动发生在更细的一层"},
     "summary": {"en": "The S&P 500's eleven sectors hide their 25 industry groups. We rebuilt "
                       "all 25 from the index's own constituents, checked them against the "
                       "official indices, and found this year's tech rally was two groups, "
                       "not one sector.",
                 "zh": "标普 500 的 11 个板块掩盖了其下的 25 个行业组。我们用指数自身的成分股"
                       "重建了全部 25 个行业组，与官方指数逐一核对，发现今年的科技行情其实"
                       "是两个行业组的行情，而不是整个板块。"},
     "tags": ({"en": "GICS", "zh": "GICS"},
              {"en": "Industry groups", "zh": "行业组"},
              {"en": "Survivorship bias", "zh": "幸存者偏差"},
              {"en": "S&P 500", "zh": "标普 500"})},
    {"slug": "forward-history", "date": "2026-09-20", "minutes": 8,
     "kicker": {"en": "Valuation · Out-of-sample study", "zh": "估值 · 样本外研究"},
     "title": {"en": "A forward multiple needs a forward history",
               "zh": "前瞻倍数，需要前瞻的历史"},
     "summary": {"en": "Ranking today's forward P/E against five years of trailing P/Es marks "
                       "every growing company cheap — for half of our 22-name test, as cheap as "
                       "a percentile can go. Rebuilding a forward history fixed the ranking. "
                       "Here is how, and what it still gets wrong.",
                 "zh": "把今天的前瞻市盈率放进过去五年的滚动市盈率里排名，会让每一家成长型"
                       "公司都显得便宜——在我们 22 只股票的测试中，一半直接被排到了百分位的"
                       "最底端。重建一段前瞻历史修正了排名。本文说明做法，以及它仍然存在的"
                       "偏差。"},
     "tags": ({"en": "Valuation", "zh": "估值"},
              {"en": "Percentiles", "zh": "百分位"},
              {"en": "Look-ahead bias", "zh": "前视偏差"},
              {"en": "22 tickers", "zh": "22 只股票"})},
    {"slug": "tsm-pe-1-01", "date": "2026-09-19", "minutes": 6,
     "kicker": {"en": "Data integrity · Post-mortem", "zh": "数据完整性 · 事后复盘"},
     "title": {"en": "TSM at a P/E of 1.01",
               "zh": "市盈率 1.01 倍的台积电"},
     "summary": {"en": "A reader spotted TSM trading at one times earnings. It was three "
                       "incompatible bases from one data feed — dollars per ADR, Taiwan dollars "
                       "per ADR, and ordinary shares — and every one of them pointed cheap.",
                 "zh": "一位读者发现台积电的市盈率只有 1 倍。原因是同一个数据源给出了三种互不"
                       "兼容的口径——每份 ADR 的美元价格、每份 ADR 的新台币收益、以及普通股"
                       "股数——而且每一个错误都指向“更便宜”。"},
     "tags": ({"en": "ADRs", "zh": "ADR"},
              {"en": "Currency", "zh": "币种"},
              {"en": "Share basis", "zh": "股本口径"},
              {"en": "Post-mortem", "zh": "复盘"})},
    {"slug": "decision-ledger", "date": "2026-08-30", "minutes": 6,
     "kicker": {"en": "Evaluation · Methodology", "zh": "评估 · 方法论"},
     "title": {"en": "Grading the desk without flattering it",
               "zh": "给投研台打分，而不美化它"},
     "summary": {"en": "Before any hit rate, a ledger: one row per decision, measured from a close "
                       "the call could actually have traded, counted in sessions rather than "
                       "days — and deliberately storing no score of its own.",
                 "zh": "在谈命中率之前，先有一本账：每个决策一行，从决策真正可以成交的那个收盘价"
                       "开始计算，以交易日而非自然日计数——并且刻意不保存任何自评分数。"},
     "tags": ({"en": "Evaluation", "zh": "评估"},
              {"en": "Look-ahead bias", "zh": "前视偏差"},
              {"en": "Methodology", "zh": "方法论"})},
    {"slug": "look-through-floors", "date": "2026-08-29", "minutes": 6,
     "kicker": {"en": "Portfolios · Look-through", "zh": "组合 · 穿透"},
     "title": {"en": "Every look-through number is a floor",
               "zh": "穿透后的每个数字都是下限"},
     "summary": {"en": "Yahoo discloses a fund's top ten holdings — 37.6% of VOO, 13.0% of VXUS. "
                       "Grossing the rest up would invent concentration at the moment you are "
                       "judging it. So every figure is a lower bound, with its coverage beside it.",
                 "zh": "Yahoo 只披露基金的前十大持仓——VOO 的 37.6%、VXUS 的 13.0%。把其余部分"
                       "按比例放大，就会在你判断集中度的那一刻凭空制造集中度。所以每个数字都是"
                       "下限，并标明覆盖率。"},
     "tags": ({"en": "ETFs", "zh": "ETF"},
              {"en": "Look-through", "zh": "穿透"},
              {"en": "Concentration", "zh": "集中度"})},
)

#: The analyst keys ``agents.py`` hands TradingAgents are the package's own names,
#: and one of them differs from the role the report headings produce: the package
#: calls its sentiment analyst ``social``, while the heading — and therefore
#: ``agent_roles`` — says "Sentiment Analyst".
_ANALYST_ROLE = {"social": "sentiment"}

#: Team order and labels for the roster, matching the order ``agent_roles.ROLES``
#: lays turns out in — the order both renderers read a report in.
DESK_TEAMS: tuple[dict[str, Any], ...] = (
    {"key": "analysts", "title": {"en": "Analyst team", "zh": "分析师团队"}},
    {"key": "research", "title": {"en": "Research team", "zh": "研究团队"}},
    {"key": "trading",  "title": {"en": "Trader",        "zh": "交易员"}},
    {"key": "risk",     "title": {"en": "Risk team",     "zh": "风险管理团队"}},
    {"key": "decision", "title": {"en": "Portfolio manager", "zh": "投资组合经理"}},
)

#: What each seat does, as the landing's run log (``short``) and /docs/desk
#: (``desc``) describe it. Written from the prompts in the TradingAgents fork
#: (tradingagents/agents/analysts/*.py and managers/*.py), not from what the role
#: names suggest: the sentiment analyst reads StockTwits and Reddit as well as
#: headlines, and the quality and valuation analysts only *narrate* numbers that
#: code has already computed. A role with no entry here renders without a note
#: rather than failing — the test is what insists every seated role has one.
ROLE_NOTES: dict[str, dict[str, dict[str, str]]] = {
    "market": {
        "short": {"en": "price action & indicators", "zh": "价格走势与技术指标"},
        "desc": {"en": "Reads the price history up to the run's date and picks up to eight "
                       "complementary indicators — moving averages, MACD, RSI, Bollinger bands, "
                       "ATR — to describe trend, momentum and volatility.",
                 "zh": "读取截至分析日期的价格历史，从均线、MACD、RSI、布林带、ATR 等指标中"
                       "挑选最多八个互补的指标，描述趋势、动量与波动。"}},
    "sentiment": {
        "short": {"en": "headlines, StockTwits & Reddit", "zh": "新闻标题、StockTwits 与 Reddit"},
        "desc": {"en": "Weighs a week of Yahoo Finance headlines against retail chatter on "
                       "StockTwits and Reddit (r/wallstreetbets, r/stocks, r/investing), and treats "
                       "a gap between the two as a signal in itself.",
                 "zh": "把一周的 Yahoo Finance 新闻标题，与 StockTwits 和 Reddit（r/wallstreetbets、"
                       "r/stocks、r/investing）上的散户讨论放在一起比较，并把两者之间的分歧本身"
                       "视为一种信号。"}},
    "news": {
        "short": {"en": "company & macro news", "zh": "公司与宏观新闻"},
        "desc": {"en": "Covers the past week of company and world news, grounded in FRED macro "
                       "series and prediction-market odds for the events ahead.",
                 "zh": "梳理过去一周的公司新闻与全球新闻，并以 FRED 宏观数据和预测市场对未来"
                       "事件的定价作为依据。"}},
    "earnings": {
        "short": {"en": "estimates & revisions", "zh": "盈利预期与修正"},
        "desc": {"en": "Tracks consensus estimates and how they have been revised, and the "
                       "company's record of surprises. The figures are computed by code; the "
                       "analyst only interprets them.",
                 "zh": "跟踪一致预期及其修正轨迹，以及公司过往的业绩超预期记录。数字由代码计算，"
                       "分析师只负责解读。"}},
    "quality": {
        "short": {"en": "is it a good business?", "zh": "这是一门好生意吗？"},
        "desc": {"en": "Answers only “is this a good business” — margins, returns, cash "
                       "conversion and balance sheet — then adds a moat assessment and red flags "
                       "to numbers code has already scored.",
                 "zh": "只回答“这是不是一门好生意”——利润率、回报率、现金转化与资产负债表——"
                       "在代码已经打好分的数字之上，补充护城河评估与风险警示。"}},
    "valuation": {
        "short": {"en": "is it a good price?", "zh": "这是一个好价格吗？"},
        "desc": {"en": "Answers only “is this a good price”, separately from whether it is a "
                       "good business: code prices it, and the analyst explains what the price "
                       "assumes.",
                 "zh": "只回答“这是不是一个好价格”，与“是不是好生意”分开讨论：由代码完成定价，"
                       "分析师解释这个价格隐含了什么假设。"}},
    "policy": {
        "short": {"en": "policy & regulation", "zh": "政策与监管"},
        "desc": {"en": "A-shares only. Traces monetary, fiscal, CSRC and industrial policy through "
                       "sector to company, separating announced policy from speculation.",
                 "zh": "仅限 A 股。沿“政策 → 行业 → 公司”的链条，追踪货币、财政、证监会与产业"
                       "政策的影响，并区分已发布的政策与市场猜测。"}},
    "hot_money": {
        "short": {"en": "Dragon-Tiger seats & flows", "zh": "龙虎榜席位与资金流"},
        "desc": {"en": "A-shares only. Follows Dragon-Tiger List seats, main-capital and northbound "
                       "flow, limit-up behaviour and theme rotation — keeping verified seat data "
                       "apart from inference.",
                 "zh": "仅限 A 股。跟踪龙虎榜席位、主力与北向资金、涨停行为和题材轮动，并把"
                       "已核实的席位数据与推断严格分开。"}},
    "lockup": {
        "short": {"en": "unlocks & reductions", "zh": "解禁与减持"},
        "desc": {"en": "A-shares only. Watches the next 90 days of restricted-share unlocks against "
                       "the float, plus announced reductions and pledges — an unlock is potential "
                       "supply, not proof of selling.",
                 "zh": "仅限 A 股。关注未来 90 天限售股解禁相对流通盘的规模，以及已公告的减持与"
                       "质押——解禁只是潜在供给，并不等于一定会卖出。"}},
    "bull": {
        "short": {"en": "argues the upside", "zh": "论证上行空间"},
        "desc": {"en": "Builds the strongest case for owning it from the analysts' reports, and "
                       "answers the bear point by point.",
                 "zh": "以分析师报告为依据，构建持有它的最强论据，并逐条回应看跌研究员。"}},
    "bear": {
        "short": {"en": "argues the downside", "zh": "论证下行风险"},
        "desc": {"en": "Builds the strongest case against, from the same reports, and answers the "
                       "bull point by point.",
                 "zh": "基于同样的报告，构建反对持有的最强论据，并逐条回应看涨研究员。"}},
    "research_mgr": {
        "short": {"en": "rules on the debate", "zh": "裁决多空辩论"},
        "desc": {"en": "Judges the debate and hands the trader an investment plan with a rating.",
                 "zh": "裁决多空辩论，并向交易员交付一份附带评级的投资计划。"}},
    "trader": {
        "short": {"en": "entry, stop & size", "zh": "入场、止损与仓位"},
        "desc": {"en": "Turns the plan into a concrete proposal — a direction, an entry and a stop "
                       "stated as prices, and a position size.",
                 "zh": "把投资计划变成具体方案——方向、以价格表示的入场位与止损位，以及仓位大小。"}},
    "aggressive": {
        "short": {"en": "argues for more risk", "zh": "主张承担更多风险"},
        "desc": {"en": "Argues the plan is too timid, and where taking more risk would be rewarded.",
                 "zh": "认为方案过于保守，并指出承担更多风险会在哪里得到回报。"}},
    "conservative": {
        "short": {"en": "argues for less", "zh": "主张降低风险"},
        "desc": {"en": "Argues the plan is too bold: what could go wrong, and how badly.",
                 "zh": "认为方案过于激进：可能出什么问题，以及会糟到什么程度。"}},
    "neutral": {
        "short": {"en": "weighs the two", "zh": "权衡双方"},
        "desc": {"en": "Weighs both against the evidence and pushes for the balanced version.",
                 "zh": "依据证据权衡双方观点，推动形成更平衡的方案。"}},
    "portfolio": {
        "short": {"en": "signs the rating", "zh": "签发最终评级"},
        "desc": {"en": "Reads everything and signs the final call — exactly one of Buy, "
                       "Overweight, Hold, Underweight or Sell — with the levels and sizing.",
                 "zh": "通读全部内容后签发最终结论——买入、增持、持有、减持、卖出五选一——"
                       "并给出价位与仓位。"}},
}


def doc(slug: str) -> Optional[dict[str, Any]]:
    """The docs entry for *slug*, or None."""
    for page in DOCS:
        if page["slug"] == slug:
            return page
    return None


def docs_nav() -> list[dict[str, Any]]:
    """The sidebar: each group with its pages, in reading order."""
    return [{**group, "pages": [p for p in DOCS if p["group"] == group["key"]]}
            for group in DOC_GROUPS]


def group_title(key: str) -> dict[str, str]:
    for group in DOC_GROUPS:
        if group["key"] == key:
            return group["title"]
    return {"en": key, "zh": key}


def neighbours(slug: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """``(previous, next)`` in reading order; either may be None at the ends."""
    slugs = [p["slug"] for p in DOCS]
    if slug not in slugs:
        return None, None
    i = slugs.index(slug)
    prev_page = DOCS[i - 1] if i > 0 else None
    next_page = DOCS[i + 1] if i + 1 < len(DOCS) else None
    return prev_page, next_page


def posts() -> list[dict[str, Any]]:
    """Research Lab posts, newest first (the order of ``POSTS``)."""
    return list(POSTS)


def post(slug: str) -> Optional[dict[str, Any]]:
    for entry in POSTS:
        if entry["slug"] == slug:
            return entry
    return None


def post_neighbours(slug: str) -> tuple[Optional[dict[str, Any]], Optional[dict[str, Any]]]:
    """``(newer, older)`` around *slug*."""
    slugs = [p["slug"] for p in POSTS]
    if slug not in slugs:
        return None, None
    i = slugs.index(slug)
    newer = POSTS[i - 1] if i > 0 else None
    older = POSTS[i + 1] if i + 1 < len(POSTS) else None
    return newer, older


def desk(roles: Sequence[Mapping[str, Any]],
         base_analysts: Iterable[str],
         astock_analysts: Iterable[str]) -> list[dict[str, Any]]:
    """The roster as the landing page and ``/docs/desk`` draw it.

    Built from the analyst tuples the runner actually uses, not from every role
    ``agent_roles`` knows: ``ROLES`` also carries the Fundamentals Analyst so an
    older report still renders its turn, but that analyst has not been in the
    default roster since quality and valuation replaced it — listing it here
    would describe a desk nobody runs. Each analyst is flagged ``a_share_only``
    when it joins only for 沪深京 codes.

    Returns the teams in report order, each ``{"key", "title", "roles"}``, with
    every role a copy of its ``agent_roles`` entry plus ``a_share_only`` and its
    ``ROLE_NOTES`` entry (``short`` / ``desc``, None when absent). A team that
    ends up empty is omitted rather than rendered as a bare heading.
    """
    base = [_ANALYST_ROLE.get(a, a) for a in base_analysts]
    astock = [_ANALYST_ROLE.get(a, a) for a in astock_analysts]
    by_key = {r["key"]: r for r in roles}

    def seat(role: Mapping[str, Any], a_share_only: bool) -> dict[str, Any]:
        notes = ROLE_NOTES.get(role["key"], {})
        return {**role, "a_share_only": a_share_only,
                "short": notes.get("short"), "desc": notes.get("desc")}

    teams: list[dict[str, Any]] = []
    for team in DESK_TEAMS:
        members: list[dict[str, Any]] = []
        if team["key"] == "analysts":
            for key in dict.fromkeys(base + astock):     # ordered, de-duplicated
                role = by_key.get(key)
                if role is None:
                    continue
                members.append(seat(role, key not in base))
        else:
            members = [seat(r, False) for r in roles if r.get("group") == team["key"]]
        if members:
            teams.append({**team, "roles": members})
    return teams
