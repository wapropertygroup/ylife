"""
ystocker.report_pdf
~~~~~~~~~~~~~~~~~~~
Renders an agent run's markdown report to a downloadable PDF.

Uses reportlab's platypus flowables rather than an HTML-to-PDF engine on
purpose: weasyprint and wkhtmltopdf both pull in cairo/pango or a headless
browser, and the production box has ~1.3 GB of RAM free across eight apps (see
the measurement in agents.py). reportlab is pure Python and holds only the
document being built.

Two details that otherwise produce broken output rather than an error:

* **Paragraph text is mini-HTML.** reportlab parses ``<b>``, ``<i>`` and
  entities inside a Paragraph, so any ``&``, ``<`` or ``>`` in the report body
  has to be escaped first or the paragraph raises or silently drops content. A
  ratio written as ``P/E < 20`` is enough to trigger it.
* **The built-in fonts are WinAnsi.** Helvetica cannot encode CJK or many
  typographic characters, and reportlab renders an unmappable glyph as a black
  box. The report bodies are English, but LLM output is full of curly quotes,
  em dashes and arrows, so those are folded to ASCII and anything still
  unmappable is dropped rather than left to print as boxes.
"""
from __future__ import annotations

import io
import logging
import re
import unicodedata
from datetime import datetime, timezone
from typing import Any, Optional

log = logging.getLogger(__name__)

# Typography that LLM output produces constantly and WinAnsi cannot always
# represent. Folded rather than dropped so the sentence still reads.
_FOLD = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "–": "-", "—": "--", "―": "--", "−": "-",
    "…": "...", "•": "-", "·": "-", "⁃": "-",
    "→": "->", "←": "<-", "↔": "<->",
    "≥": ">=", "≤": "<=", "≠": "!=", "×": "x",
    " ": " ", "​": "", "﻿": "",
    "✅": "[ok]", "❌": "[x]", "⚠": "[!]",
}


def _to_winansi(text: str) -> str:
    """Fold text to something the built-in Helvetica can actually draw."""
    for src, dst in _FOLD.items():
        text = text.replace(src, dst)
    # Strip combining marks, then drop anything still outside WinAnsi so it
    # cannot render as a black box.
    text = unicodedata.normalize("NFKD", text)
    out = []
    for ch in text:
        if ch in "\n\t":
            out.append(ch)
            continue
        try:
            ch.encode("cp1252")
            out.append(ch)
        except UnicodeEncodeError:
            continue
    return "".join(out)


def _escape(text: str) -> str:
    """Escape for a reportlab Paragraph, whose text is parsed as mini-HTML."""
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`\n]+?)`")


def _inline(text: str) -> str:
    """Escape, then re-apply the inline markdown reportlab understands."""
    text = _escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    text = _CODE_RE.sub(r'<font face="Courier">\1</font>', text)
    return text


def build_report_pdf(job: dict[str, Any]) -> Optional[bytes]:
    """Render a finished job's report to PDF bytes, or None if unavailable."""
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_LEFT
        from reportlab.lib.pagesizes import LETTER
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import inch
        from reportlab.platypus import (
            HRFlowable, PageBreak, Paragraph, Preformatted, SimpleDocTemplate, Spacer,
        )
    except ImportError as exc:
        log.warning("report_pdf: reportlab unavailable: %s", exc)
        return None

    body_md = (job.get("report") or "").strip()
    decision = (job.get("decision") or "").strip()
    if not body_md and not decision:
        return None

    ticker = job.get("ticker", "?")
    day = job.get("date", "")

    base = getSampleStyleSheet()
    styles = {
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontSize=18, leading=22,
                             spaceBefore=2, spaceAfter=8, textColor=colors.HexColor("#1e293b")),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontSize=13, leading=17,
                             spaceBefore=14, spaceAfter=5, textColor=colors.HexColor("#334155")),
        "h3": ParagraphStyle("h3", parent=base["Heading3"], fontSize=11, leading=15,
                             spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#475569")),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontSize=9.5, leading=13.5,
                               alignment=TA_LEFT, spaceAfter=6),
        "bullet": ParagraphStyle("bullet", parent=base["BodyText"], fontSize=9.5, leading=13.5,
                                 leftIndent=14, bulletIndent=4, spaceAfter=3),
        "meta": ParagraphStyle("meta", parent=base["BodyText"], fontSize=8.5, leading=12,
                               textColor=colors.HexColor("#64748b")),
        "decision": ParagraphStyle("decision", parent=base["BodyText"], fontSize=13, leading=17,
                                   spaceAfter=4, textColor=colors.HexColor("#1e293b")),
    }

    flow: list[Any] = []
    flow.append(Paragraph(_inline(_to_winansi(f"{ticker} — Trading Agents Report")), styles["h1"]))
    meta_bits = [f"Analysis date: {day}"]
    if job.get("finished_at"):
        meta_bits.append(f"Generated: {job['finished_at']}")
    if job.get("elapsed_sec"):
        meta_bits.append(f"Runtime: {job['elapsed_sec']}s")
    if job.get("selftest"):
        meta_bits.append("SELF-TEST — no LLM call was made")
    flow.append(Paragraph(_to_winansi(" · ".join(meta_bits)), styles["meta"]))
    flow.append(Spacer(1, 8))
    flow.append(HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#cbd5e1")))
    flow.append(Spacer(1, 10))

    if decision:
        flow.append(Paragraph("Decision", styles["h2"]))
        flow.append(Paragraph(_inline(_to_winansi(decision)), styles["decision"]))
        flow.append(Spacer(1, 6))

    in_code = False
    code_buf: list[str] = []

    for raw_line in _to_winansi(body_md).splitlines():
        line = raw_line.rstrip()

        # Fenced code: accumulate verbatim, since Preformatted must not be
        # escaped as mini-HTML the way Paragraph is.
        if line.strip().startswith("```"):
            if in_code:
                if code_buf:
                    flow.append(Preformatted("\n".join(code_buf), base["Code"]))
                    flow.append(Spacer(1, 6))
                code_buf = []
            in_code = not in_code
            continue
        if in_code:
            code_buf.append(raw_line)
            continue

        if not line.strip():
            continue

        if line.startswith("#### "):
            flow.append(Paragraph(_inline(line[5:].strip()), styles["h3"]))
        elif line.startswith("### "):
            flow.append(Paragraph(_inline(line[4:].strip()), styles["h3"]))
        elif line.startswith("## "):
            flow.append(Paragraph(_inline(line[3:].strip()), styles["h2"]))
        elif line.startswith("# "):
            flow.append(Paragraph(_inline(line[2:].strip()), styles["h2"]))
        elif re.match(r"^\s*[-*+]\s+", line):
            flow.append(Paragraph(_inline(re.sub(r"^\s*[-*+]\s+", "", line)),
                                  styles["bullet"], bulletText="•"))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            flow.append(Paragraph(_inline(line.strip()), styles["bullet"]))
        elif re.match(r"^\s*\|.*\|\s*$", line):
            # Markdown tables are common in these reports. Rendering them as a
            # real table needs column parsing that breaks on ragged rows, so
            # they go through monospaced and stay legible either way.
            flow.append(Preformatted(line.strip(), base["Code"]))
        elif re.match(r"^\s*[-=]{3,}\s*$", line):
            flow.append(HRFlowable(width="100%", thickness=0.5,
                                   color=colors.HexColor("#e2e8f0")))
        else:
            flow.append(Paragraph(_inline(line.strip()), styles["body"]))

    if in_code and code_buf:
        flow.append(Preformatted("\n".join(code_buf), base["Code"]))

    flow.append(Spacer(1, 14))
    flow.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e2e8f0")))
    flow.append(Paragraph(
        _to_winansi("Generated by yStocker from the TradingAgents multi-agent analysis. "
                    "Not investment advice."), styles["meta"]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=f"{ticker} Trading Agents Report {day}",
        author="yStocker",
        subject="Multi-agent trading analysis",
    )

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont("Helvetica", 7.5)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawString(0.85 * inch, 0.5 * inch,
                          f"{ticker} · {day} · yStocker")
        canvas.drawRightString(LETTER[0] - 0.85 * inch, 0.5 * inch, f"Page {doc_.page}")
        canvas.restoreState()

    try:
        doc.build(flow, onFirstPage=_footer, onLaterPages=_footer)
    except Exception as exc:
        log.error("report_pdf: build failed for %s: %s", job.get("id"), exc, exc_info=True)
        return None

    return buf.getvalue()


def pdf_filename(job: dict[str, Any]) -> str:
    ticker = re.sub(r"[^A-Za-z0-9.\-]", "", str(job.get("ticker", "report")))[:12] or "report"
    day = re.sub(r"[^0-9\-]", "", str(job.get("date", "")))[:10]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"ystocker-{ticker}-{day or stamp}-agents.pdf"
