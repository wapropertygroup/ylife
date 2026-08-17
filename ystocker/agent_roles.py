"""
ystocker.agent_roles
~~~~~~~~~~~~~~~~~~~~
The TradingAgents cast, and how to split a report into their turns.

One source of truth for both renderers: the /agents page draws the report as a
conversation between these roles, and report_pdf badges each section with the
same name and colour. The list is injected into the page as JSON rather than
duplicated in JavaScript, so adding a role cannot leave the two disagreeing.

Roles are matched by *exact heading text*, never by heading level. The section
bodies are LLM-authored markdown and contain their own ``###`` and even ``#``
headings -- a real report has ``# 台积电 (TSM) 基本面综合分析报告`` inside the
Fundamentals section -- so "every h3 is a speaker" silently turns the model's
own subheadings into imaginary agents.

Names come from the headings ``tradingagents.reporting.write_report_tree``
actually emits, read off a completed run rather than guessed: note it is
"Sentiment Analyst" and not "Social Analyst", which the package's own module
name would suggest.
"""
from __future__ import annotations

import re
from typing import Any, Optional

# group: which team section the role belongs to, used only for ordering/labels.
# color: the accent for the avatar, the bubble border and the PDF badge.
# icon: shown on the web page only. The PDF's fonts are Helvetica and a CJK
#       TrueType face, neither of which has emoji glyphs, so the PDF uses the
#       coloured badge instead of trying to draw these.
ROLES: list[dict[str, str]] = [
    {"key": "market",       "name": "Market Analyst",       "zh": "市场分析师",
     "short": "Market", "icon": "📈", "color": "#3b82f6", "group": "analysts"},
    {"key": "sentiment",    "name": "Sentiment Analyst",    "zh": "情绪分析师",
     "short": "Sentiment", "icon": "💬", "color": "#a855f7", "group": "analysts"},
    {"key": "news",         "name": "News Analyst",         "zh": "新闻分析师",
     "short": "News", "icon": "📰", "color": "#06b6d4", "group": "analysts"},
    {"key": "fundamentals", "name": "Fundamentals Analyst", "zh": "基本面分析师",
     "short": "Fundamentals", "icon": "🏦", "color": "#14b8a6", "group": "analysts"},

    {"key": "bull",         "name": "Bull Researcher",      "zh": "看涨研究员",
     "short": "Bull", "icon": "🐂", "color": "#22c55e", "group": "research"},
    {"key": "bear",         "name": "Bear Researcher",      "zh": "看跌研究员",
     "short": "Bear", "icon": "🐻", "color": "#ef4444", "group": "research"},
    {"key": "research_mgr", "name": "Research Manager",     "zh": "研究主管",
     "short": "Research Mgr", "icon": "⚖️", "color": "#f59e0b", "group": "research"},

    {"key": "trader",       "name": "Trader",               "zh": "交易员",
     "short": "Trader", "icon": "🎯", "color": "#6366f1", "group": "trading"},

    {"key": "aggressive",   "name": "Aggressive Analyst",   "zh": "激进派",
     "short": "Aggressive", "icon": "🔥", "color": "#f97316", "group": "risk"},
    {"key": "conservative", "name": "Conservative Analyst", "zh": "保守派",
     "short": "Conservative", "icon": "🛡️", "color": "#0ea5e9", "group": "risk"},
    {"key": "neutral",      "name": "Neutral Analyst",      "zh": "中立派",
     "short": "Neutral", "icon": "☯️", "color": "#94a3b8", "group": "risk"},

    {"key": "portfolio",    "name": "Portfolio Manager",    "zh": "投资组合经理",
     "short": "Portfolio", "icon": "🏛️", "color": "#8b5cf6", "group": "decision"},
]

# Team headings (``## II. Research Team Decision``). Kept so the phase can be
# labelled, and so a team heading is not mistaken for prose.
TEAM_RE = re.compile(r"^##\s+(?:[IVX]+\.\s*)?(.+?)\s*$")

# Team headings come from the report text, which write_report_tree emits in
# English regardless of output_language, so they need translating for the
# Chinese page. Matched on a substring because the numeral is already stripped
# and upstream wording varies slightly ("Decision" vs "Plan").
TEAM_LABELS_ZH: list[tuple[str, str]] = [
    ("analyst team",   "分析师团队报告"),
    ("research team",  "研究团队决策"),
    ("trading team",   "交易团队计划"),
    ("risk management", "风险管理团队决策"),
    ("portfolio manager", "投资组合经理决策"),
]


def team_label_zh(team: str) -> str:
    """Chinese label for a team heading, or the original if unrecognised."""
    low = (team or "").casefold()
    for needle, zh in TEAM_LABELS_ZH:
        if needle in low:
            return zh
    return team


_BY_NAME = {r["name"].casefold(): r for r in ROLES}

# Historical/alternate spellings, so a rename upstream degrades to the right
# role instead of silently falling out of the conversation.
_ALIASES = {
    "social analyst": "sentiment",
    "social media analyst": "sentiment",
    "risky analyst": "aggressive",
    "safe analyst": "conservative",
    "risk manager": "portfolio",
}
_BY_KEY = {r["key"]: r for r in ROLES}


def role_for_heading(text: str) -> Optional[dict[str, str]]:
    """Return the role whose name is exactly this heading, else None."""
    key = (text or "").strip().casefold()
    if key in _BY_NAME:
        return _BY_NAME[key]
    alias = _ALIASES.get(key)
    return _BY_KEY.get(alias) if alias else None


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*$")


def split_sections(markdown: str) -> list[dict[str, Any]]:
    """Split a report into ordered turns.

    Returns dicts of ``{role, team, body}``. ``role`` is None for the preamble
    (the report title and generation stamp) so a caller can render it as a
    header rather than as somebody speaking.

    Only a heading that exactly names a known role starts a new turn; every
    other heading, at any level, stays inside the current body. That is what
    keeps the model's own ``## 1. 公司概况`` subheadings attached to the analyst
    who wrote them.
    """
    out: list[dict[str, Any]] = []
    cur: dict[str, Any] = {"role": None, "team": None, "body": []}
    team: Optional[str] = None

    for line in (markdown or "").splitlines():
        m = _HEADING_RE.match(line)
        if m:
            hashes, text = m.group(1), m.group(2)
            role = role_for_heading(text)
            if role is not None:
                if cur["body"] or cur["role"] is not None:
                    out.append(cur)
                cur = {"role": role, "team": team, "body": []}
                continue
            # A team heading only relabels the phase; it is not a speaker, and
            # it must not swallow the preamble into a nameless section.
            if len(hashes) == 2:
                tm = TEAM_RE.match(line)
                if tm and _looks_like_team(tm.group(1)):
                    team = tm.group(1)
                    continue
        cur["body"].append(line)

    if cur["body"] or cur["role"] is not None:
        out.append(cur)

    for sec in out:
        sec["body"] = "\n".join(sec["body"]).strip()
        sec["team_zh"] = team_label_zh(sec["team"]) if sec["team"] else None
    return [s for s in out if s["body"] or s["role"]]


_TEAM_WORDS = ("team", "decision", "plan", "manager", "reports")


def _looks_like_team(text: str) -> bool:
    """Whether an h2 is one of the report's own team dividers.

    Guarded because section bodies contain their own h2s: a real report has
    ``## 2. 核心财务与估值指标`` inside the Fundamentals body, and treating that
    as a phase divider would drop the model's heading from the output.
    """
    low = text.casefold()
    return any(w in low for w in _TEAM_WORDS)


def roles_json() -> list[dict[str, str]]:
    """The cast, for injection into a template."""
    return ROLES
