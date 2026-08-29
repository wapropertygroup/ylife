"""
ystocker.report_email
~~~~~~~~~~~~~~~~~~~~~
Email a finished agent report to the address that ran it.

Why this exists: a deep run takes tens of minutes, and ``/agents`` only learns it
finished by polling (``pollJob``, 5s backing off to 30s). Close the tab or let the
phone sleep and nothing tells you -- the report is there when you come back, but
you have to think to look. Browser notifications would need a push subscription,
VAPID keys, an installed PWA on iOS, and a server-side completion hook that can
outlive the gunicorn worker supervising the run. Email needs none of that: SES is
already wired up for the daily broadcast, and the run already knows the address
because it was charged to it.

Two rules shape the whole module:

- **The report is untrusted input.** It is LLM output, and the models sometimes
  answer in HTML rather than Markdown. Text is escaped before any tag is added,
  and where HTML *is* honoured it is rebuilt against an allowlist by
  :class:`_Rebuild` rather than passed through -- the same design as
  ``static/markdown.js``, using ``html.parser`` in place of ``DOMParser``.

- **Gmail clips a message over ~102 KB**, silently, behind a "[Message clipped]"
  link. A full multi-agent report is comfortably past that, so the body is
  rendered against :data:`_HTML_BUDGET` and stops at a section boundary with an
  explicit link to the rest, rather than being cut mid-table by the client. A
  short report still arrives whole.

Styling is inline on every element. Email clients are inconsistent about a
``<style>`` block in the head and Gmail drops it outright, so a stylesheet would
render the report as unstyled text in the one client most subscribers use.
"""
from __future__ import annotations

import logging
import os
import re
import threading
from html.parser import HTMLParser
from typing import Any, Optional

log = logging.getLogger(__name__)

# Gmail's clip threshold is ~102 KB of HTML. Budgeting the body to 78 KB leaves
# room for the shell, the header and the footer while staying clear of it.
_HTML_BUDGET = 78_000


# ---------------------------------------------------------------------------
# Palette. Matches _wrap_email_html in routes.py so the two mails look related.
# ---------------------------------------------------------------------------
_BG      = "#0f172a"
_CARD    = "#1e293b"
_LINE    = "#334155"
_TEXT    = "#e2e8f0"
_BRIGHT  = "#f8fafc"
_DIM     = "#94a3b8"
_ACCENT  = "#93c5fd"
_MONO    = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"
_SANS    = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"

# Inline style per rendered element. Anything absent gets no style attribute,
# which is correct for the purely structural wrappers (thead, tbody, tr).
_STYLE: dict[str, str] = {
    "p":  f"margin:0 0 10px;color:{_TEXT};font-size:14px;line-height:1.65",
    "h1": f"margin:18px 0 8px;color:{_BRIGHT};font-size:19px;font-weight:700;line-height:1.35",
    "h2": f"margin:16px 0 8px;color:{_BRIGHT};font-size:17px;font-weight:700;line-height:1.35",
    "h3": f"margin:14px 0 6px;color:{_BRIGHT};font-size:15px;font-weight:600;line-height:1.4",
    "h4": f"margin:12px 0 6px;color:{_ACCENT};font-size:14px;font-weight:600;line-height:1.4",
    "h5": f"margin:12px 0 6px;color:{_ACCENT};font-size:13px;font-weight:600",
    "h6": f"margin:12px 0 6px;color:{_DIM};font-size:13px;font-weight:600",
    "ul": f"margin:0 0 10px;padding-left:20px;color:{_TEXT};font-size:14px;line-height:1.65",
    "ol": f"margin:0 0 10px;padding-left:20px;color:{_TEXT};font-size:14px;line-height:1.65",
    "li": "margin:0 0 4px",
    "dl": f"margin:0 0 10px;color:{_TEXT};font-size:14px",
    "dt": f"margin:6px 0 0;color:{_BRIGHT};font-weight:600",
    "dd": "margin:0 0 0 16px",
    "table": "border-collapse:collapse;width:100%;margin:0 0 14px;font-size:13px",
    "th": (f"border:1px solid {_LINE};padding:6px 9px;background:{_BG};"
           f"color:{_ACCENT};text-align:left;font-weight:600"),
    "td": f"border:1px solid {_LINE};padding:6px 9px;color:{_TEXT};vertical-align:top",
    "caption": f"padding:0 0 6px;color:{_DIM};font-size:12px;text-align:left",
    "blockquote": (f"margin:0 0 12px;padding:8px 12px;border-left:3px solid #475569;"
                   f"background:{_BG};color:#cbd5e1;font-size:13px;line-height:1.6"),
    "code": (f"background:{_BG};padding:1px 5px;border-radius:3px;"
             f"font-family:{_MONO};font-size:12px;color:#fbbf24"),
    "pre": (f"margin:0 0 12px;padding:10px;background:{_BG};border-radius:6px;"
            f"color:{_TEXT};font-size:12px;font-family:{_MONO};"
            "white-space:pre-wrap;word-break:break-word"),
    "hr": f"border:0;border-top:1px solid {_LINE};margin:16px 0",
    "a": f"color:{_ACCENT};text-decoration:underline",
    "strong": f"color:{_BRIGHT};font-weight:600",
    "b": f"color:{_BRIGHT};font-weight:600",
    "mark": "background:#78350f;color:#fde68a;padding:0 3px",
    "small": f"font-size:12px;color:{_DIM}",
    "kbd": f"font-family:{_MONO};font-size:12px;color:{_TEXT}",
}


def _style_attr(tag: str) -> str:
    css = _STYLE.get(tag)
    return f' style="{css}"' if css else ""


# ---------------------------------------------------------------------------
# Sanitising HTML answers
# ---------------------------------------------------------------------------

# Elements kept when honouring HTML, mapped to the attributes each may keep.
# Everything absent is *unwrapped* -- its text survives, the element does not --
# so a <font color> degrades to plain text instead of the content vanishing.
_ALLOWED: dict[str, tuple[str, ...]] = {
    "p": (), "br": (), "hr": (), "div": (), "section": (), "span": (),
    "b": (), "strong": (), "i": (), "em": (), "u": (), "s": (), "del": (),
    "ins": (), "mark": (), "code": (), "pre": (), "kbd": (), "sub": (),
    "sup": (), "small": (),
    "ul": (), "ol": ("start",), "li": (), "dl": (), "dt": (), "dd": (),
    "table": (), "thead": (), "tbody": (), "tfoot": (), "caption": (), "tr": (),
    "th": ("colspan", "rowspan"), "td": ("colspan", "rowspan"),
    "h1": (), "h2": (), "h3": (), "h4": (), "h5": (), "h6": (), "blockquote": (),
    "a": ("href",),
}

# Dropped with their contents. Their text is markup or code, not prose, so
# unwrapping them would paste a stylesheet into the middle of a report.
_DROP = frozenset({
    "script", "style", "iframe", "object", "embed", "template", "noscript",
    "svg", "math", "form", "input", "button", "select", "textarea", "link",
    "meta", "base", "title", "head",
})

_VOID = frozenset({"br", "hr"})

_NUM_RE = re.compile(r"^\d{1,4}$")


def _esc(text: Any) -> str:
    """Escape for text content and double-quoted attribute values alike."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _safe_href(value: str) -> Optional[str]:
    """A link we are willing to emit, or None.

    Scheme allowlist: a relative or fragment link is fine, anything carrying a
    scheme must be http, https or mailto -- which excludes ``javascript:`` and
    ``data:``.
    """
    v = (value or "").strip()
    if not v:
        return None
    if re.match(r"^[a-z][a-z0-9+.-]*:", v, re.I):
        return v if re.match(r"^(https?|mailto):", v, re.I) else None
    return v


class _Rebuild(HTMLParser):
    """Rebuild untrusted HTML from an allowlist, styled for email.

    Nothing from the input is carried over: every tag and attribute in the
    output is constructed here, so the result is safe to hand to an email client
    as a string. Unbalanced input is tolerated because model HTML frequently is
    -- the tag stack is closed out at the end rather than trusted to match.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._stack: list[str] = []
        self._drop = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        tag = tag.lower()
        if self._drop:
            # Track nesting so </div> inside a dropped <script> does not end the
            # suppression early.
            if tag in _DROP:
                self._drop += 1
            return
        if tag in _DROP:
            self._drop = 1
            return
        if tag not in _ALLOWED:
            return                      # unwrap
        if tag in _VOID:
            self._parts.append(f"<{tag}{_style_attr(tag)}>")
            return
        self._parts.append(f"<{tag}{_style_attr(tag)}{self._attrs(tag, attrs)}>")
        self._stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self._drop:
            if tag in _DROP:
                self._drop -= 1
            return
        if tag in _VOID or tag not in _ALLOWED or tag not in self._stack:
            return
        # Close down to the matching tag. A stray unclosed <b> inside a <td>
        # would otherwise leak its styling across the rest of the table.
        while self._stack:
            top = self._stack.pop()
            self._parts.append(f"</{top}>")
            if top == tag:
                break

    def handle_data(self, data: str) -> None:
        if not self._drop:
            self._parts.append(_esc(data))

    def _attrs(self, tag: str, attrs: list) -> str:
        out = ""
        supplied = {k.lower(): (v or "") for k, v in attrs}
        for name in _ALLOWED[tag]:
            if name not in supplied:
                continue
            value = supplied[name]
            if name == "href":
                safe = _safe_href(value)
                if safe is None:
                    continue
                # No target/rel: an email client opens links in a browser
                # regardless, and Outlook shows rel/target as stray attributes.
                out += f' href="{_esc(safe)}"'
            elif name in ("colspan", "rowspan", "start"):
                if _NUM_RE.match(value.strip()):
                    out += f' {name}="{_esc(value.strip())}"'
        return out

    def result(self) -> str:
        while self._stack:
            self._parts.append(f"</{self._stack.pop()}>")
        return "".join(self._parts)


def sanitize(html: str) -> str:
    """Allowlist-rebuild a fragment of model-authored HTML."""
    parser = _Rebuild()
    try:
        parser.feed(str(html))
        parser.close()
    except Exception as exc:  # noqa: BLE001 - never fail a report over markup
        log.warning("report_email: HTML sanitise failed (%s); escaping instead", exc)
        return f'<p{_style_attr("p")}>{_esc(html)}</p>'
    return parser.result()


# ---------------------------------------------------------------------------
# Markdown -> email HTML
# ---------------------------------------------------------------------------

# Inline tags restored after escaping, so a <b> or <br> inside otherwise
# Markdown prose is honoured. Only the attribute-free form is matched, so there
# is no way to smuggle an event handler through: <b onclick=..> stays escaped
# and is shown as text.
_INLINE_HTML = re.compile(
    r"&lt;(/?)(b|strong|i|em|u|s|del|ins|mark|code|sub|sup|small|br)\s*/?&gt;", re.I)

_CODE_RE   = re.compile(r"`([^`]+)`")
_BOLD_RE   = re.compile(r"\*\*([^*]+)\*\*")
_ITAL_RE   = re.compile(r"(^|[^*])\*([^*\n]+)\*")
# Markdown links, which static/markdown.js deliberately does not handle. Worth
# the divergence here: a citation left as raw [1](https://...) is noise in a
# mail client, where there is no page around it to explain the convention.
_LINK_RE   = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)\)")

_BLOCK_HTML = re.compile(
    r"^<(p|div|section|table|ul|ol|dl|pre|blockquote|h[1-6]|figure)\b", re.I)
_HEAD_RE  = re.compile(r"^(#{1,6})\s+(.*)$")
_RULE_RE  = re.compile(r"^(---+|\*\*\*+|___+)$")
_UL_RE    = re.compile(r"^[-*+]\s+(.*)$")
_OL_RE    = re.compile(r"^\d+[.)]\s+(.*)$")
_SEP_RE   = re.compile(r"^\|[\s:|-]+\|$")
_TASK_RE  = re.compile(r"^\[( |x|X)\]\s*")
_NEXT_BLOCK_RE = re.compile(r"^(#{1,6}\s|>|\||[-*+]\s|\d+[.)]\s|---+$)")


def _inline(text: str) -> str:
    """Escape, then restore the inline formatting the reports actually use."""
    out = _esc(text)
    out = _LINK_RE.sub(_link_sub, out)
    out = _CODE_RE.sub(lambda m: f'<code{_style_attr("code")}>{m.group(1)}</code>', out)
    out = _BOLD_RE.sub(lambda m: f'<strong{_style_attr("strong")}>{m.group(1)}</strong>', out)
    out = _ITAL_RE.sub(lambda m: f"{m.group(1)}<em>{m.group(2)}</em>", out)
    out = _INLINE_HTML.sub(_inline_html_sub, out)
    return out


def _inline_html_sub(m: re.Match) -> str:
    """Restore one attribute-free inline tag that survived escaping.

    The style belongs on the opening tag only: ``</b style=...>`` is not a
    closing tag at all, and an email client that repairs it does so by guessing.
    """
    slash, tag = m.group(1), m.group(2).lower()
    if slash:
        return f"</{tag}>"
    return f"<{tag}{_style_attr(tag)}>"


def _link_sub(m: re.Match) -> str:
    # The href arrives already escaped by _esc, so &amp; in a query string is
    # correct as-is; _safe_href only needs to vet the scheme.
    label, raw = m.group(1), m.group(2)
    href = _safe_href(raw.replace("&amp;", "&"))
    if href is None:
        return label
    return f'<a{_style_attr("a")} href="{_esc(href)}">{label}</a>'


def _cells(row: str) -> list[str]:
    return [c.strip() for c in row.strip().strip("|").split("|")]


def render(markdown: str) -> str:
    """Convert a report body to inline-styled HTML for email.

    A body that *opens* with a block-level tag is treated as an HTML answer and
    handed to the allowlist whole. Deciding on the first tag rather than
    "contains HTML anywhere" keeps a Markdown report that happens to include one
    stray <div> on the Markdown path instead of silently losing its formatting.
    """
    text = str(markdown or "")
    if _BLOCK_HTML.match(text.strip()):
        return sanitize(text)

    lines = text.replace("\r", "").split("\n")
    out: list[str] = []
    i, list_type = 0, None

    def close_list() -> None:
        nonlocal list_type
        if list_type:
            out.append(f"</{list_type}>")
            list_type = None

    while i < len(lines):
        t = lines[i].strip()

        # An HTML block: consume to its matching close tag and sanitise the lot.
        # Counted rather than stopping at the first close, so a <table> holding
        # <tr>s nests correctly.
        m = _BLOCK_HTML.match(t)
        if m:
            close_list()
            name = m.group(1).lower()
            op = re.compile(r"<" + name + r"(?=[\s/>])", re.I)
            cl = re.compile(r"</" + name + r"\s*>", re.I)
            chunk, depth = [], 0
            while i < len(lines):
                raw = lines[i]
                chunk.append(raw)
                i += 1
                depth += len(op.findall(raw))
                depth -= len(cl.findall(raw))
                if depth <= 0:
                    break
            out.append(sanitize("\n".join(chunk)))
            continue

        # Table: a header row followed by a |---|---| separator.
        if t.startswith("|") and i + 1 < len(lines) and _SEP_RE.match(lines[i + 1].strip()):
            close_list()
            head = _cells(t)
            i += 2
            body = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                body.append(_cells(lines[i]))
                i += 1
            th = "".join(f'<th{_style_attr("th")}>{_inline(h)}</th>' for h in head)
            rows = "".join(
                "<tr>" + "".join(f'<td{_style_attr("td")}>{_inline(c)}</td>' for c in r) + "</tr>"
                for r in body)
            out.append(f'<table{_style_attr("table")}><thead><tr>{th}</tr></thead>'
                       f"<tbody>{rows}</tbody></table>")
            continue

        h = _HEAD_RE.match(t)
        if h:
            close_list()
            lvl = min(len(h.group(1)), 4)
            out.append(f'<h{lvl}{_style_attr(f"h{lvl}")}>{_inline(h.group(2))}</h{lvl}>')
            i += 1
            continue

        if _RULE_RE.match(t):
            close_list()
            out.append(f'<hr{_style_attr("hr")}>')
            i += 1
            continue

        if t.startswith(">"):
            close_list()
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(re.sub(r"^>\s?", "", lines[i].strip()))
                i += 1
            joined = "<br>".join(_inline(q) for q in quote)
            out.append(f'<blockquote{_style_attr("blockquote")}>{joined}</blockquote>')
            continue

        ul, ol = _UL_RE.match(t), _OL_RE.match(t)
        if ul or ol:
            want = "ul" if ul else "ol"
            if list_type != want:
                close_list()
                out.append(f"<{want}{_style_attr(want)}>")
                list_type = want
            body = (ul or ol).group(1)
            # Checkboxes as glyphs: no email client renders a disabled <input>
            # consistently, and several drop it outright.
            body = _TASK_RE.sub(lambda mm: "☑ " if mm.group(1).lower() == "x" else "☐ ", body)
            out.append(f'<li{_style_attr("li")}>{_inline(body)}</li>')
            i += 1
            continue

        if not t:
            close_list()
            i += 1
            continue

        close_list()
        # Always consume the current line first, so a line that matches the
        # block-start guard below cannot stall the loop.
        para = [t]
        i += 1
        while (i < len(lines) and lines[i].strip()
               and not _BLOCK_HTML.match(lines[i].strip())
               and not _NEXT_BLOCK_RE.match(lines[i].strip())):
            para.append(lines[i].strip())
            i += 1
        joined = "<br>".join(_inline(p) for p in para)
        out.append(f'<p{_style_attr("p")}>{joined}</p>')

    close_list()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Assembling the message
# ---------------------------------------------------------------------------

_STR: dict[str, dict[str, str]] = {
    "en": {
        "html_lang":  "en",
        "subject":    "{ticker} analysis ready{decision} — {date}",
        "header":     "AI research report",
        "decision":   "Decision",
        "open":       "Open the full report",
        "elapsed":    "Completed in {mins}",
        "secs":       "{n}s",
        "mins":       "{n} min",
        # Mirrors i18n.js agents.degraded_why, with the models named. Keeping the
        # wording identical to the page matters: a reader who saw the badge there
        # should not have to work out that this is the same condition.
        "degraded":   ("The configured model hit its daily quota, so part of this "
                       "report was written by a weaker model ({models})."),
        "recovered":  ("This run failed before it could file its report; the "
                       "analysis below was recovered from its stream."),
        # Mirrors i18n.js agents.pm_truncated, restated for a mail rather than a
        # section.
        "clipped":    ("This email shows the report up to here — open the report "
                       "for the full text."),
        # Sharing. `share_why` replaces `why` rather than joining it: the
        # unshared footer claims "you ran this analysis", which is false for a
        # recipient and is exactly the sentence a reader checks when deciding
        # whether mail they did not expect is legitimate.
        "share_subject": "{sharer} shared a {ticker} analysis with you — {date}",
        "share_by":      "{sharer} shared this report with you",
        "share_note":    "Their note",
        "share_open":    "Open the shared report",
        "share_why":     ("You are receiving this because {sharer} entered your "
                          "address on {brand}. You are not subscribed to "
                          "anything, and this link stops working in {days} days."),
        "why":        ("You are receiving this because you ran this analysis on "
                       "{brand}. It is sent once, when the run finishes."),
        "foot":        ("Generated by {brand} from the TradingAgents "
                       "multi-agent framework. For research only — not "
                       "investment advice."),
    },
    "zh": {
        "html_lang":  "zh-CN",
        "subject":    "{ticker} 分析完成{decision} — {date}",
        "header":     "AI 研究报告",
        "decision":   "决策",
        "open":       "打开完整报告",
        "elapsed":    "耗时 {mins}",
        "secs":       "{n} 秒",
        "mins":       "{n} 分钟",
        "degraded":   "配置的模型已用完当日额度，本报告部分内容由能力较弱的模型（{models}）生成。",
        "recovered":  "本次运行在生成报告前失败，以下分析由实时流恢复。",
        "clipped":    "邮件仅显示到此处，打开报告查看全文。",
        "share_subject": "{sharer} 与您分享了 {ticker} 分析 — {date}",
        "share_by":      "{sharer} 与您分享了这份报告",
        "share_note":    "对方留言",
        "share_open":    "打开分享的报告",
        "share_why":     ("您收到此邮件是因为 {sharer} 在 {brand} 上填写了您的邮箱地址。"
                          "您并未订阅任何内容，此链接将在 {days} 天后失效。"),
        "why":        "您收到此邮件是因为您在 {brand} 上运行了本次分析。仅在运行完成时发送一次。",
        "foot":       "由 {brand} 基于 TradingAgents 多智能体框架生成。仅供研究参考，不构成投资建议。",
    },
}

# Month names for the header date. Hardcoded rather than taken from strftime,
# which emits whatever the *server's* locale is -- an EC2 box in us-west-2 is
# LC_ALL=C, so %B is always English regardless of the report's language.
_MONTHS_EN = ("January", "February", "March", "April", "May", "June", "July",
              "August", "September", "October", "November", "December")


def _t(lang: str, key: str) -> str:
    table = _STR.get(lang) or _STR["en"]
    return table.get(key) or _STR["en"][key]


def _fmt_day(day: str, lang: str) -> str:
    """The analysis date, written the way each language writes dates.

    Falls back to the string as stored. ``submit`` validates the field as
    YYYY-MM-DD, but a record written by an older build is not worth an exception
    on the notification path.
    """
    parts = (day or "").split("-")
    if len(parts) != 3:
        return _plain(day, 40)
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return _plain(day, 40)
    if not 1 <= m <= 12:
        return _plain(day, 40)
    if lang == "zh":
        return f"{y}年{m}月{d}日"
    return f"{_MONTHS_EN[m - 1]} {d}, {y}"


def _lang_of(job: dict[str, Any]) -> str:
    """The language the report was written in, as a two-letter code."""
    code = (job.get("lang") or "").strip().lower()
    return "zh" if code == "zh" else "en"


def _minutes(job: dict[str, Any], lang: str) -> str:
    secs = job.get("elapsed_sec")
    try:
        secs = float(secs)
    except (TypeError, ValueError):
        return ""
    if secs < 90:
        return _t(lang, "secs").format(n=f"{secs:.0f}")
    return _t(lang, "mins").format(n=f"{secs / 60.0:.0f}")


def _decision_chip(job: dict[str, Any], lang: str) -> str:
    """The headline verdict, colour-coded, or "" when the run produced none."""
    raw = (job.get("decision") or "").strip()
    if not raw:
        return ""
    first = raw.splitlines()[0].strip()[:120]
    if not first:
        return ""
    low = first.casefold()
    if "buy" in low or "买入" in first or "增持" in first:
        fg, bg = "#4ade80", "#052e16"
    elif "sell" in low or "卖出" in first or "减持" in first:
        fg, bg = "#f87171", "#450a0a"
    else:
        fg, bg = "#fbbf24", "#451a03"
    return (f'<span style="display:inline-block;padding:4px 12px;border-radius:999px;'
            f'background:{bg};color:{fg};font-size:14px;font-weight:700;'
            f'letter-spacing:.02em">{_esc(first)}</span>')


def _advisory(text: str) -> str:
    return (f'<tr><td style="padding:0 0 10px"><div style="padding:9px 12px;'
            f'border-left:3px solid #f59e0b;background:#1c1917;color:#fcd34d;'
            f'font-size:12px;line-height:1.6">{text}</div></td></tr>')


def _role_header(role: Optional[dict[str, str]], lang: str) -> str:
    """The speaker's name bar, matching the conversation view's accent colour."""
    if not role:
        return ""
    name = role.get("zh") if lang == "zh" else role.get("name")
    colour = role.get("color") or _ACCENT
    return (f'<div style="margin:0 0 8px;padding:6px 10px;border-left:3px solid {colour};'
            f'background:{_BG}">'
            f'<span style="color:{colour};font-size:13px;font-weight:700">'
            f'{_esc(name or "")}</span></div>')


def _team_divider(team: str, lang: str) -> str:
    from ystocker.agent_roles import team_label_zh

    label = team_label_zh(team) if lang == "zh" else team
    return (f'<tr><td style="padding:16px 0 8px">'
            f'<div style="border-top:1px solid {_LINE};padding-top:10px;color:{_DIM};'
            f'font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase">'
            f'{_esc(label)}</div></td></tr>')


def _section_html(sec: dict[str, Any], lang: str) -> str:
    """One turn, as the inner HTML of its row."""
    body = (sec.get("body") or "").strip()
    role = sec.get("role")
    if role is None:
        # The preamble is the report's own title and generation stamp. The header
        # above already states both, so it is shown small and dim rather than
        # competing with them.
        return (f'<div style="color:{_DIM};font-size:12px;line-height:1.6">'
                f"{render(body)}</div>")
    return _role_header(role, lang) + render(body)


def _body_rows(job: dict[str, Any], lang: str, link: str,
               reserved: int = 0) -> str:
    """The report as table rows, stopping cleanly at the size budget.

    Sections are emitted whole: cutting one in half would leave a dangling table
    or list for the client to repair, and the reader cannot tell a truncated
    analysis from a short one. Whatever does not fit is represented by a single
    explicit notice pointing at the site.

    The Portfolio Manager's turn is *reserved* out of the budget before anything
    else is placed. It is the last section a report emits, so a naive in-order
    walk drops the decision and its rationale first and keeps seven analysts
    instead -- which inverts the value of the mail. The header states the verdict
    either way, but the reasoning behind it is the part worth reading.

    ``reserved`` is further budget already spent by the caller on chrome outside
    these rows -- today, a share banner. Passing it matters because the budget
    exists to stay under Gmail's ~102 KB clip, and a banner that pushes the mail
    over it would be clipped *by Gmail*, mid-element, which is the exact outcome
    all of the above is arranged to avoid.
    """
    from ystocker.agent_roles import split_sections

    report = (job.get("report") or "").strip()
    if not report:
        return ""

    sections = [s for s in split_sections(report)
                if (s.get("body") or "").strip() or s.get("role")]
    if not sections:
        return ""

    budget = max(0, _HTML_BUDGET - max(0, reserved))

    def is_decision(sec: dict[str, Any]) -> bool:
        role = sec.get("role") or {}
        return role.get("key") == "portfolio"

    decision = next((s for s in sections if is_decision(s)), None)
    decision_html = _section_html(decision, lang) if decision else ""
    # Capped at the whole budget: a decision section larger than the mail can
    # hold should spend all of it rather than be dropped in favour of analysts.
    reserve = min(len(decision_html), budget) if decision_html else 0

    rows: list[str] = []
    used = 0
    seen_team: Optional[str] = None
    dropped = 0

    for sec in sections:
        if decision is not None and sec is decision:
            continue                    # placed last, out of the reserve
        team = sec.get("team")
        chunk = ""
        if team and team != seen_team:
            chunk += _team_divider(team, lang)
        chunk += f'<tr><td style="padding:0 0 14px">{_section_html(sec, lang)}</td></tr>'

        # `and rows` keeps the first section unconditionally: a mail with nothing
        # but a "see the site" notice is worse than one slightly over budget.
        if dropped or (used + len(chunk) + reserve > budget and rows):
            dropped += 1
            continue
        rows.append(chunk)
        used += len(chunk)
        if team:
            seen_team = team

    if dropped:
        rows.append(
            f'<tr><td style="padding:6px 0 14px">'
            f'<div style="padding:10px 12px;border:1px dashed {_LINE};border-radius:6px;'
            f'color:{_DIM};font-size:12px;line-height:1.6">{_esc(_t(lang, "clipped"))} '
            f'<a href="{_esc(link)}" style="color:{_ACCENT};text-decoration:underline">'
            f'{_esc(_t(lang, "open"))}</a></div></td></tr>')
        log.info("report_email: %s clipped %d of %d section(s) at %d bytes",
                 job.get("id"), dropped, len(sections), used + reserve)

    if decision is not None:
        team = decision.get("team")
        if team and team != seen_team:
            rows.append(_team_divider(team, lang))
        rows.append(f'<tr><td style="padding:0 0 14px">{decision_html}</td></tr>')
    return "".join(rows)


def base_url() -> str:
    """Where to point the "open the full report" link.

    Defaults to the trade-agents brand rather than stock.li-family.us: /agents is
    the page that domain exists to serve, and a reader who ran the analysis there
    should not be sent to a hostname they do not recognise.
    """
    return (os.environ.get("AGENTS_BASE_URL")
            or os.environ.get("APP_BASE_URL")
            or "https://trade-agents.com").rstrip("/")


def brand_for(url: str) -> str:
    """The product name to sign this mail with, from the host it links to.

    The page brands itself per hostname (``brand_name`` in the app's context
    processor: TradeAgents on trade-agents.com, yStocker elsewhere), but that
    reads ``request.host`` and there is no request here -- this runs in a
    background thread, or in ``_reap`` on behalf of a *different* reader's poll.
    The link host is the honest substitute, since it is the site this reader will
    actually land on.

    ``TA_HOSTS`` is imported rather than restated so the two cannot disagree; a
    mail signed "yStocker" that links to trade-agents.com is the failure mode.
    """
    try:
        from ystocker import TA_HOSTS
    except Exception:  # noqa: BLE001 - never fail a mail over branding
        TA_HOSTS = {"trade-agents.com", "www.trade-agents.com"}
    host = re.sub(r"^[a-z]+://", "", (url or "").strip().lower())
    host = host.split("/")[0].split(":")[0]
    return "TradeAgents" if host in TA_HOSTS else "yStocker"


# The favicon's mark (static/favicon.svg): four rounded bars rising out of a dark
# plate. Scaled from its 32px viewBox. Kept as data rather than markup so the
# widths and heights stay legible next to each other.
_LOGO_BARS = ((6, 12, "#6366f1"), (6, 20, "#38bdf8"),
              (6, 26, "#34d399"), (4, 17, "#4338ca"))


def _logo() -> str:
    """The brand mark, drawn in table cells rather than fetched as an image.

    Deliberately not an ``<img>`` pointing at ``/static/img/pwa-icon-192.png``.
    Outlook and Apple Mail block remote images by default, so the one decorative
    element in the mail would be an empty box for a large share of readers, and a
    blocked-image placeholder looks like a broken email rather than a plain one.
    Table cells with ``bgcolor`` always paint.

    Each bar is a nested single-cell table inside a bottom-aligned cell: putting
    the colour on the outer cell instead makes every bar full height, because a
    cell's background fills the whole row regardless of its own height.
    """
    bars = ""
    for i, (w, h, colour) in enumerate(_LOGO_BARS):
        pad = "0 0 0 3px" if i else "0"
        bars += (
            f'<td valign="bottom" style="padding:{pad}">'
            f'<table cellpadding="0" cellspacing="0" border="0" width="{w}">'
            f'<tr><td width="{w}" height="{h}" bgcolor="{colour}" '
            f'style="border-radius:2px;font-size:0;line-height:0">&nbsp;</td>'
            f"</tr></table></td>")
    return (
        f'<table cellpadding="0" cellspacing="0" border="0" width="44" '
        f'style="width:44px;border-radius:11px;background-color:{_BG}">'
        f'<tr><td width="44" height="44" align="center" valign="bottom" '
        f'style="padding:0 0 9px">'
        f'<table cellpadding="0" cellspacing="0" border="0">'
        f"<tr>{bars}</tr></table>"
        f"</td></tr></table>")


def _share_banner(sharer: str, note: str, lang: str) -> str:
    """"So-and-so sent you this", plus their note, as a table row.

    Placed above the report and below the masthead, so the first thing a reader
    who did not ask for this mail sees is who caused it. That ordering is the
    anti-phishing property: an unexplained AI stock report from a domain you do
    not know reads as spam, and the explanation has to arrive before the content
    rather than in the footer.

    The note is escaped and emitted as a ``blockquote``-styled cell rather than
    run through ``render()``. A recipient's mail client must not be asked to
    interpret Markdown -- let alone the inline HTML ``render`` allows -- from a
    string one user typed and another user receives; the report itself comes from
    our own model, which is a different trust level entirely.
    """
    sharer = _plain(sharer, 96)
    line = _esc(_t(lang, "share_by").format(sharer=sharer))
    body = (f'<div style="color:{_BRIGHT};font-size:14px;font-weight:600;'
            f'line-height:1.5">{line}</div>')
    if note:
        # Newlines survive as <br>: a two-line note otherwise arrives as one run
        # of text, and this is the one field in the mail a human wrote by hand.
        safe = "<br>".join(_esc(ln) for ln in note.splitlines() if ln.strip())
        if safe:
            body += (
                f'<div style="margin:8px 0 0;color:{_DIM};font-size:11px;'
                f'font-weight:700;letter-spacing:.08em;text-transform:uppercase">'
                f'{_esc(_t(lang, "share_note"))}</div>'
                f'<div style="margin:4px 0 0;padding:8px 12px;'
                f'border-left:3px solid {_ACCENT};background:{_BG};'
                f'color:#cbd5e1;font-size:13px;line-height:1.6">{safe}</div>')
    return (f'<tr><td style="padding:18px 28px 0">'
            f'<div style="padding:12px 14px;border:1px solid {_LINE};'
            f'border-radius:8px;background:rgba(147,197,253,.06)">{body}</div>'
            f"</td></tr>")


def _header(brand: str, ticker: str, lang: str, meta: str, link: str) -> str:
    """The masthead: mark, wordmark, then what this report is and when.

    Laid out as a two-cell table rather than with flex or float, neither of which
    Outlook's Word rendering engine supports.
    """
    return (
        f'<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>'
        f'<td width="44" valign="top" style="width:44px;padding:0 14px 0 0">'
        f"{_logo()}</td>"
        f'<td valign="middle">'
        f'<div style="margin:0 0 2px;font-size:21px;font-weight:700;'
        f'letter-spacing:-.01em;line-height:1.2">'
        f'<a href="{_esc(link)}" style="color:#ffffff;text-decoration:none">'
        f"{_esc(brand)}</a></div>"
        f'<div style="color:#bfdbfe;font-size:14px;line-height:1.4">'
        f"{_esc(ticker)} — {_esc(_t(lang, 'header'))}</div>"
        f'<div style="color:#93c5fd;font-size:12px;line-height:1.5;'
        f'margin:2px 0 0">{_esc(meta)}</div>'
        f"</td></tr></table>")


_TAGS_RE = re.compile(r"<[^>]*>")
_WS_RE = re.compile(r"\s+")


def _plain(text: str, limit: int = 0) -> str:
    """Flatten model text for a mail header.

    Subjects are transported as a header value, so markup in one is not a
    scripting risk -- but it reads as garbage in an inbox, and the models do
    occasionally answer ``<b>BUY</b>``. Newlines are collapsed rather than
    merely stripped: a bare CR or LF reaching a header is header injection, and
    the only reason the decision is safe today is that ``build`` happens to take
    ``splitlines()[0]``. Not worth depending on from two places.
    """
    out = _WS_RE.sub(" ", _TAGS_RE.sub("", str(text or ""))).strip()
    return out[:limit] if limit else out


def build(job: dict[str, Any], link_base: str = "", *,
          shared_by: str = "", note: str = "",
          link_override: str = "") -> Optional[tuple[str, str, str]]:
    """Render one finished job as ``(subject, html, text)``, or None.

    None means "nothing worth sending": no report body. Every caller must handle
    it, because a run can reach ``done`` with an empty report.

    Passing ``shared_by`` turns the same report into a *shared* one: it gains a
    banner naming the sender and their note, and its subject and footer change to
    say why the mail arrived. One builder rather than two, because the alternative
    is a second renderer that drifts -- which is the mistake this module's own
    docstring records about ``_build_email_sections``.

    ``link_override`` exists because a shared report must not link to
    ``/agents?job=<id>``. That route is owner-or-VIP and answers 404 to everyone
    else, so the recipient of a share would find the one button in the mail dead.
    Callers sharing a report pass the capability URL from ``share.share_url()``.
    """
    report = (job.get("report") or "").strip()
    if not report:
        return None

    lang = _lang_of(job)
    ticker = _plain(job.get("ticker") or "?", 12)
    day = _fmt_day(_plain(job.get("date") or "", 20), lang)
    root = (link_base or base_url()).rstrip("/")
    link = link_override or f"{root}/agents?job={job.get('id')}"
    brand = brand_for(root)
    sharer = _plain(shared_by, 96)
    # Every share branch below tests this, not ``sharer``. _plain strips markup,
    # so a sharer of "<b>x</b>" reduces to "" -- and keying off the stripped name
    # would then quietly build a *non-share* mail: no banner, and the "you ran
    # this analysis on {brand}" footer, sent to somebody who did not. An unnamed
    # sharer is visibly odd; a misattributed provenance line is not, and it is the
    # sentence a recipient reads to decide whether unexpected mail is legitimate.
    sharing = bool(str(shared_by or "").strip())

    # The verdict's first line only. A decision can run to a paragraph, and
    # _plain would otherwise fold the whole thing into the subject.
    verdict_lines = (job.get("decision") or "").strip().splitlines()
    short = _plain(verdict_lines[0], 40) if verdict_lines else ""
    if sharing:
        # The decision is left out of a shared subject on purpose. "BUY" in the
        # subject line of mail somebody did not ask for reads as a tip being
        # pushed at them; who sent it is the part that makes it openable.
        subject = _plain(_t(lang, "share_subject").format(
            sharer=sharer, ticker=ticker, date=day), 180)
    else:
        subject = _plain(_t(lang, "subject").format(
            ticker=ticker, decision=f" — {short}" if short else "", date=day), 180)

    banner = _share_banner(sharer, note, lang) if sharing else ""

    rows = _body_rows(job, lang, link, reserved=len(banner))
    if not rows:
        return None

    advisories = ""
    if job.get("degraded") and job.get("fallback_models"):
        advisories += _advisory(_esc(
            _t(lang, "degraded").format(models=", ".join(job["fallback_models"]))))
    if job.get("recovered"):
        advisories += _advisory(_esc(_t(lang, "recovered")))

    chip = _decision_chip(job, lang)
    elapsed = _minutes(job, lang)
    meta_bits = [day]
    if elapsed:
        meta_bits.append(_t(lang, "elapsed").format(mins=elapsed))
    meta = " · ".join(b for b in meta_bits if b)

    chip_row = ""
    if chip:
        chip_row = (
            f'<tr><td style="padding:18px 28px 0">'
            f'<div style="color:{_DIM};font-size:11px;font-weight:700;'
            f'letter-spacing:.08em;text-transform:uppercase;margin:0 0 6px">'
            f'{_esc(_t(lang, "decision"))}</div>{chip}</td></tr>')

    cta = _t(lang, "share_open") if sharing else _t(lang, "open")
    if sharing:
        # Imported here rather than at module scope: share.py reaches back into
        # this module for base_url(), so a top-level import either way would be a
        # cycle. Taking the number from its owner is worth the lazy import --
        # a second copy of "30" would drift from the expiry actually enforced,
        # and the footer would then promise a window the link does not honour.
        from ystocker.share import TTL_DAYS

        why = _t(lang, "share_why").format(sharer=sharer, brand=brand,
                                           days=TTL_DAYS)
    else:
        why = _t(lang, "why").format(brand=brand)

    html = f"""<!DOCTYPE html>
<html lang="{_esc(_t(lang, 'html_lang'))}"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{_esc(subject)}</title></head>
<body style="margin:0;padding:0;background:{_BG};font-family:{_SANS}">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:{_BG};padding:28px 12px">
    <tr><td align="center">
      <table width="760" cellpadding="0" cellspacing="0"
             style="max-width:760px;width:100%;background:{_CARD};border-radius:14px;overflow:hidden">
        <tr><td style="background:linear-gradient(135deg,#1d4ed8,#7c3aed);padding:22px 28px">
          {_header(brand, ticker, lang, meta, link)}
        </td></tr>
        {banner}
        {chip_row}
        <tr><td style="padding:18px 28px 0">
          <a href="{_esc(link)}" style="display:inline-block;padding:9px 18px;border-radius:8px;
             background:#1d4ed8;color:#fff;font-size:13px;font-weight:600;text-decoration:none">
             {_esc(cta)}</a>
        </td></tr>
        <tr><td style="padding:20px 28px 4px">
          <table width="100%" cellpadding="0" cellspacing="0">{advisories}{rows}</table>
        </td></tr>
        <tr><td style="padding:14px 28px 24px;border-top:1px solid {_LINE}">
          <p style="margin:0;color:#64748b;font-size:12px;line-height:1.6">
            {_esc(_t(lang, 'foot').format(brand=brand))}</p>
          <p style="margin:8px 0 0;color:#475569;font-size:11px;line-height:1.6">
            {_esc(why)}</p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>"""

    # The plain-text alternative is the report's own Markdown. It is already the
    # best plain rendering there is -- headings, tables and lists all survive as
    # the model wrote them -- so flattening it further would only lose structure.
    # Note this part is *not* budgeted: the clip limit is Gmail's rendering of
    # the HTML alternative, and no client shows both.
    head_lines = [f"{brand} — {ticker} — {_t(lang, 'header')}", meta]
    if sharing:
        head_lines.append(_t(lang, "share_by").format(sharer=sharer))
        if note:
            head_lines.append(f"{_t(lang, 'share_note')}: {note}")
    if short:
        head_lines.append(f"{_t(lang, 'decision')}: {short}")
    head_lines.append(f"{cta}: {link}")
    text = "\n".join([
        *[h for h in head_lines if h],
        "", "-" * 60, "",
        report,
        "", "-" * 60,
        _t(lang, "foot").format(brand=brand),
        why,
    ])
    return subject, html, text


# ---------------------------------------------------------------------------
# Sending, exactly once
# ---------------------------------------------------------------------------

def enabled() -> bool:
    """Whether finished runs should be emailed at all.

    On by default once SES has a From address, since the address is the whole
    reason the feature is wanted. ``AGENTS_EMAIL_REPORT=0`` turns it off without
    a deploy, which matters because the alternative to a kill switch here is
    every finished run mailing a stranger during a misconfiguration.
    """
    flag = (os.environ.get("AGENTS_EMAIL_REPORT") or "").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return bool((os.environ.get("SES_FROM_EMAIL") or "").strip())


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+\.[^@\s]+$")


def _recipient(job: dict[str, Any]) -> Optional[str]:
    """The address to mail, or None if this job must not generate one."""
    if job.get("selftest"):
        return None                      # synthetic run, no human waiting on it
    addr = (job.get("user") or "").strip()
    return addr if _EMAIL_RE.match(addr) else None


def _marker(job_id: str):
    """Path of the send-once claim for ``job_id``.

    Both the directory and the suffix come from agents.py, which owns the job
    layout and prunes it. Hard-coding either here would leak a file per run the
    day that layout changes.
    """
    from ystocker.agents import EMAIL_MARKER_SUFFIX, JOB_DIR

    return JOB_DIR / f"{job_id}{EMAIL_MARKER_SUFFIX}"


def _claim(job_id: str) -> bool:
    """Take the exclusive right to email this job, atomically.

    ``O_CREAT|O_EXCL`` on the shared cache directory is the guard rather than a
    field on the job record. Completion is detected in two places -- the
    supervising thread in ``_run`` and ``_reap`` on any read -- and ``_reap``
    runs in *every* worker, so two requests can settle the same orphaned job
    within milliseconds of each other. Read-then-write on the record would let
    both through; the open() cannot.
    """
    from ystocker.agents import JOB_DIR

    try:
        JOB_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_marker(job_id)),
                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    except OSError as exc:
        # Fail closed. An unwritable cache directory means the claim cannot be
        # recorded, and sending without one risks mailing the report on every
        # single poll for as long as the condition lasts.
        log.warning("report_email: cannot claim %s (%s); not sending", job_id, exc)
        return False
    os.close(fd)
    return True


def _release(job_id: str) -> None:
    """Give the claim back so a later reap can retry a failed send."""
    try:
        _marker(job_id).unlink(missing_ok=True)
    except OSError as exc:
        log.warning("report_email: could not release claim for %s: %s", job_id, exc)


def _ses_send(to_addr: str, subject: str, html: str, text: str,
              what: str = "") -> bool:
    """Hand one message to SES. False on any failure, never raises.

    The single place this process talks to SES about a report, so that the region
    and the multipart shape cannot differ between the completion mail and a share.

    us-east-1 to match the daily broadcast: the sending identity and the
    production-access grant are both per region, and a verified domain in one is
    not verified in another.
    """
    ses_from = (os.environ.get("SES_FROM_EMAIL") or "").strip()
    if not to_addr or not ses_from:
        return False
    try:
        import boto3

        ses = boto3.client("ses", region_name="us-east-1")
        ses.send_email(
            Source=ses_from,
            Destination={"ToAddresses": [to_addr]},
            Message={
                "Subject": {"Data": subject, "Charset": "UTF-8"},
                "Body": {
                    "Html": {"Data": html, "Charset": "UTF-8"},
                    "Text": {"Data": text, "Charset": "UTF-8"},
                },
            },
        )
    except Exception as exc:  # noqa: BLE001 - one address failing is not fatal
        log.warning("report_email: SES send failed for %s -> %s: %s",
                    what or "?", to_addr, exc)
        return False
    log.info("report_email: sent %s (%d bytes html) to %s",
             what or "?", len(html), to_addr)
    return True


def send(job: dict[str, Any]) -> bool:
    """Mail the report for ``job`` to its owner. Assumes the claim is held."""
    addr = _recipient(job)
    if not addr:
        return False

    built = build(job)
    if built is None:
        log.info("report_email: %s has no report body; nothing to send", job.get("id"))
        return False
    subject, html, text = built
    return _ses_send(addr, subject, html, text,
                     what=f"{job.get('id')} ({job.get('ticker')})")


def send_share(job: dict[str, Any], to_addr: str, sharer: str,
               note: str, url: str) -> bool:
    """Mail ``job`` to somebody who did not run it.

    Deliberately *not* routed through ``notify``/``_claim``. That machinery exists
    to guarantee a finished run is announced exactly once, because two workers can
    both detect completion; a share is an explicit human act that may legitimately
    be repeated -- sharing the same report with a second colleague, or re-sending
    after a typo -- and a send-once marker keyed on the job would silently swallow
    the second one. What bounds this path is the daily counter in
    ``quota.try_consume_share``, taken by the caller before it gets here.
    """
    built = build(job, shared_by=sharer, note=note, link_override=url)
    if built is None:
        log.info("report_email: %s has no report body; not sharing", job.get("id"))
        return False
    subject, html, text = built
    return _ses_send(to_addr, subject, html, text,
                     what=f"share of {job.get('id')} ({job.get('ticker')})")


def notify(job: Optional[dict[str, Any]], background: bool = False) -> None:
    """Email the report for a job that has just finished, at most once.

    Safe to call on any job in any state and from any thread: everything that
    would make this the wrong moment is checked here rather than at the call
    sites, so adding a third completion path cannot forget one of the guards.

    ``background=True`` moves the SES call onto its own thread, for callers on
    the request path. The claim is always taken synchronously, so two concurrent
    callers cannot both dispatch.

    Never raises. A finished report is worth far more than a notification about
    it, and both callers are in the middle of recording one.
    """
    try:
        if not job or job.get("status") != "done":
            return
        if not enabled() or not _recipient(job):
            return
        job_id = job.get("id")
        if not job_id or not (job.get("report") or "").strip():
            return
        if not _claim(job_id):
            return
    except Exception as exc:  # noqa: BLE001
        log.warning("report_email: notify precheck failed for %s: %s",
                    (job or {}).get("id"), exc)
        return

    def _go() -> None:
        try:
            ok = send(job)
        except Exception as exc:  # noqa: BLE001 - render bugs must not escape
            log.exception("report_email: send raised for %s: %s", job_id, exc)
            ok = False
        if not ok:
            _release(job_id)

    try:
        if background:
            threading.Thread(target=_go, daemon=True,
                             name=f"agent-mail-{job_id}").start()
        else:
            _go()
    except Exception as exc:  # noqa: BLE001 - e.g. cannot start a thread
        log.warning("report_email: could not dispatch mail for %s: %s", job_id, exc)
        _release(job_id)
