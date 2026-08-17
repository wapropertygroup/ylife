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
* **The built-in fonts are WinAnsi.** Helvetica cannot encode CJK, and
  reportlab renders an unmappable glyph as a black box. Since reports are
  generated in Chinese by default, a Helvetica-only pipeline silently dropped
  every Han character and produced a near-empty PDF. When the report contains
  CJK the document switches to ``STSong-Light``, one of the Adobe CID fonts
  whose metrics ship inside reportlab -- so this needs no font file on disk,
  which matters because the production box has no CJK font installed at all.
  Typographic characters (curly quotes, em dashes, arrows) are still folded to
  ASCII in both modes, and anything the chosen font cannot draw is dropped
  rather than left to print as a black box.
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


# Han, kana, Hangul, and the full-width/CJK punctuation blocks. Used both to
# pick the font and to decide which characters survive sanitising.
_CJK_RE = re.compile(
    "[\u1100-\u11ff\u2e80-\u9fff\ua960-\ua97f\uac00-\ud7ff"
    "\uf900-\ufaff\ufe30-\ufe4f\uff00-\uffef]"
)

# An Adobe CID font: reportlab ships its metrics, so no font file is required
# on the host. Registered lazily because registration is global state.
_CJK_FONT = "STSong-Light"
_cjk_ready = False


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _register_cjk_font() -> bool:
    """Register the CID font once. Returns False if unavailable."""
    global _cjk_ready
    if _cjk_ready:
        return True
    try:
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        pdfmetrics.registerFont(UnicodeCIDFont(_CJK_FONT))
        # Without a family mapping, reportlab resolves the <b> tags that
        # markdown headings produce to Helvetica-Bold and the CJK inside them
        # vanishes again. There is no bold cut of this CID font, so every slot
        # maps to the one face.
        pdfmetrics.registerFontFamily(
            _CJK_FONT, normal=_CJK_FONT, bold=_CJK_FONT,
            italic=_CJK_FONT, boldItalic=_CJK_FONT,
        )
        _cjk_ready = True
    except Exception as exc:  # noqa: BLE001 - fall back to Latin-only output
        log.warning("report_pdf: CJK font unavailable (%s); falling back", exc)
        _cjk_ready = False
    return _cjk_ready


def _sanitize(text: str, cjk: bool = False) -> str:
    """Fold text to something the selected font can actually draw.

    ``cjk`` keeps Han/kana/Hangul and full-width punctuation, which the
    Helvetica path has to drop.
    """
    for src, dst in _FOLD.items():
        text = text.replace(src, dst)
    if cjk:
        # NFKC, not NFKD: decomposing would split Hangul syllables and, worse,
        # rewrite full-width forms in ways that change the text. Only drop what
        # is neither CJK nor WinAnsi-encodable.
        text = unicodedata.normalize("NFKC", text)
        out = []
        for ch in text:
            if ch in "\n\t" or _CJK_RE.match(ch):
                out.append(ch)
                continue
            try:
                ch.encode("cp1252")
                out.append(ch)
            except UnicodeEncodeError:
                continue
        return "".join(out)
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


def _inline(text: str, mono: str = "Courier") -> str:
    """Escape, then re-apply the inline markdown reportlab understands."""
    text = _escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    # face= must be a font that can draw the span's characters, so it follows
    # the document's monospace choice rather than always being Courier.
    text = _CODE_RE.sub(lambda m: f'<font face="{mono}">{m.group(1)}</font>', text)
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

    # Reports are generated in Chinese by default, so pick the font from the
    # content rather than from configuration -- an English run still gets
    # Helvetica, which kerns better for Latin.
    cjk = has_cjk(body_md) or has_cjk(decision)
    if cjk and not _register_cjk_font():
        cjk = False   # registration failed; degrade to Latin rather than crash
    sn = _CJK_FONT if cjk else "Helvetica"
    sb = _CJK_FONT if cjk else "Helvetica-Bold"
    mono = _CJK_FONT if cjk else "Courier"

    def clean(t: str) -> str:
        return _sanitize(t, cjk=cjk)

    base = getSampleStyleSheet()
    if cjk:
        # Reports contain markdown tables and fenced blocks; Courier cannot draw
        # CJK either, so the preformatted style has to move as well.
        base["Code"].fontName = mono
        base["Code"].fontSize = 8
        base["Code"].leading = 11

    styles = {
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontName=sb, fontSize=18, leading=22,
                             spaceBefore=2, spaceAfter=8, textColor=colors.HexColor("#1e293b")),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName=sb, fontSize=13, leading=17,
                             spaceBefore=14, spaceAfter=5, textColor=colors.HexColor("#334155")),
        "h3": ParagraphStyle("h3", parent=base["Heading3"], fontName=sb, fontSize=11, leading=15,
                             spaceBefore=10, spaceAfter=4, textColor=colors.HexColor("#475569")),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontName=sn, fontSize=9.5, leading=13.5,
                               alignment=TA_LEFT, spaceAfter=6, wordWrap="CJK" if cjk else None),
        "bullet": ParagraphStyle("bullet", parent=base["BodyText"], fontName=sn, fontSize=9.5, leading=13.5,
                                 leftIndent=14, bulletIndent=4, spaceAfter=3,
                                 wordWrap="CJK" if cjk else None),
        "meta": ParagraphStyle("meta", parent=base["BodyText"], fontName=sn, fontSize=8.5, leading=12,
                               textColor=colors.HexColor("#64748b")),
        "decision": ParagraphStyle("decision", parent=base["BodyText"], fontName=sb, fontSize=13, leading=17,
                                   spaceAfter=4, textColor=colors.HexColor("#1e293b")),
    }

    flow: list[Any] = []
    title = f"{ticker} 交易智能体分析报告" if cjk else f"{ticker} — Trading Agents Report"
    flow.append(Paragraph(_inline(clean(title), mono=mono), styles["h1"]))
    meta_bits = [f"分析日期：{day}" if cjk else f"Analysis date: {day}"]
    if job.get("finished_at"):
        meta_bits.append((f"生成时间：{job['finished_at']}" if cjk
                          else f"Generated: {job['finished_at']}"))
    if job.get("elapsed_sec"):
        meta_bits.append((f"耗时：{job['elapsed_sec']} 秒" if cjk
                          else f"Runtime: {job['elapsed_sec']}s"))
    if job.get("selftest"):
        meta_bits.append("自检运行 — 未调用大模型" if cjk
                         else "SELF-TEST — no LLM call was made")
    flow.append(Paragraph(clean(" · ".join(meta_bits)), styles["meta"]))
    flow.append(Spacer(1, 8))
    flow.append(HRFlowable(width="100%", thickness=0.7, color=colors.HexColor("#cbd5e1")))
    flow.append(Spacer(1, 10))

    if decision:
        flow.append(Paragraph(clean("投资决策" if cjk else "Decision"), styles["h2"]))
        flow.append(Paragraph(_inline(clean(decision), mono=mono), styles["decision"]))
        flow.append(Spacer(1, 6))

    in_code = False
    code_buf: list[str] = []

    for raw_line in clean(body_md).splitlines():
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
            flow.append(Paragraph(_inline(line[5:].strip(), mono=mono), styles["h3"]))
        elif line.startswith("### "):
            flow.append(Paragraph(_inline(line[4:].strip(), mono=mono), styles["h3"]))
        elif line.startswith("## "):
            flow.append(Paragraph(_inline(line[3:].strip(), mono=mono), styles["h2"]))
        elif line.startswith("# "):
            flow.append(Paragraph(_inline(line[2:].strip(), mono=mono), styles["h2"]))
        elif re.match(r"^\s*[-*+]\s+", line):
            flow.append(Paragraph(_inline(re.sub(r"^\s*[-*+]\s+", "", line), mono=mono),
                                  styles["bullet"], bulletText="•"))
        elif re.match(r"^\s*\d+[.)]\s+", line):
            flow.append(Paragraph(_inline(line.strip(), mono=mono), styles["bullet"]))
        elif re.match(r"^\s*\|.*\|\s*$", line):
            # Markdown tables are common in these reports. Rendering them as a
            # real table needs column parsing that breaks on ragged rows, so
            # they go through monospaced and stay legible either way.
            flow.append(Preformatted(line.strip(), base["Code"]))
        elif re.match(r"^\s*[-=]{3,}\s*$", line):
            flow.append(HRFlowable(width="100%", thickness=0.5,
                                   color=colors.HexColor("#e2e8f0")))
        else:
            flow.append(Paragraph(_inline(line.strip(), mono=mono), styles["body"]))

    if in_code and code_buf:
        flow.append(Preformatted("\n".join(code_buf), base["Code"]))

    flow.append(Spacer(1, 14))
    flow.append(HRFlowable(width="100%", thickness=0.5, color=colors.HexColor("#e2e8f0")))
    flow.append(Paragraph(
        clean("由 yStocker 基于 TradingAgents 多智能体分析生成，不构成投资建议。"
              if cjk else
              "Generated by yStocker from the TradingAgents multi-agent analysis. "
              "Not investment advice."), styles["meta"]))

    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf, pagesize=LETTER,
        leftMargin=0.85 * inch, rightMargin=0.85 * inch,
        topMargin=0.8 * inch, bottomMargin=0.8 * inch,
        title=(f"{ticker} 交易智能体分析报告 {day}" if cjk
               else f"{ticker} Trading Agents Report {day}"),
        author="yStocker",
        subject="Multi-agent trading analysis",
    )

    def _footer(canvas, doc_):
        canvas.saveState()
        canvas.setFont(sn, 7.5)
        canvas.setFillColor(colors.HexColor("#94a3b8"))
        canvas.drawString(0.85 * inch, 0.5 * inch,
                          clean(f"{ticker} · {day} · yStocker"))
        canvas.drawRightString(LETTER[0] - 0.85 * inch, 0.5 * inch,
                               clean(f"第 {doc_.page} 页" if cjk else f"Page {doc_.page}"))
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
