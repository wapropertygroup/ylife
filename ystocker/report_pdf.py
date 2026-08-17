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
  CJK the document switches to an embedded TrueType CJK face -- see
  ``_register_cjk_font`` for why it must be *embedded* and why the Noto CJK
  .otf files cannot be used. Typographic characters (curly quotes, em dashes,
  arrows) are folded to ASCII on the Latin path only; on the CJK path they are
  kept, because folding "……" to "..." is a typographic error in Chinese.
"""
from __future__ import annotations

import io
import logging
import os
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

# TrueType-outline CJK font files, in preference order. These get *embedded* in
# the PDF, so it renders on any device. Paths differ between a laptop and the
# box, so several are tried.
#
# The face *inside* a .ttc is chosen by weight, never by a hardcoded index:
# index 0 of macOS Songti.ttc is the Black (900) cut, so assuming 0 meant the
# whole report rendered in the heaviest weight available.
_EMBEDDABLE_CJK: list[str] = [
    "/usr/share/fonts/cjkuni-uming/uming.ttc",          # Amazon Linux
    "/System/Library/Fonts/Supplemental/Songti.ttc",    # macOS
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",       # Debian/Ubuntu
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]
_env_font = os.environ.get("YSTOCKER_CJK_FONT", "").strip()
if _env_font:
    _EMBEDDABLE_CJK.insert(0, _env_font)


def _pick_faces(path: str) -> tuple[int, int]:
    """Return (regular_index, bold_index) for a font file.

    A .ttc packs several cuts, and which index is which varies by vendor, so
    the weights are read rather than assumed. Returns (0, 0) for a plain .ttf
    or when the collection cannot be inspected.
    """
    if not path.lower().endswith(".ttc"):
        return 0, 0
    try:
        from fontTools.ttLib import TTCollection

        with TTCollection(path, lazy=True) as coll:
            weights = []
            for i, f in enumerate(coll.fonts):
                try:
                    w = f["OS/2"].usWeightClass
                except Exception:  # noqa: BLE001 - malformed face, skip it
                    continue
                # Simplified Chinese where the collection says so: a TC cut has
                # different glyph shapes for the same codepoints.
                name = (f["name"].getDebugName(1) or "")
                weights.append((i, int(w), "TC" not in name and "HK" not in name))
        if not weights:
            return 0, 0
        sc = [x for x in weights if x[2]] or weights
        # Regular = closest to 400 from below; bold = closest to 700.
        reg = min(sc, key=lambda x: (abs(x[1] - 400), x[1]))
        bold = min(sc, key=lambda x: abs(x[1] - 700))
        return reg[0], (bold[0] if bold[1] >= 600 else reg[0])
    except Exception as exc:  # noqa: BLE001 - fall back to the first face
        log.warning("report_pdf: cannot inspect %s (%s); using face 0", path, exc)
        return 0, 0

# Last resort only. An Adobe CID font whose metrics ship inside reportlab, so
# it needs no file -- but it is *not embedded*, so the glyphs come from the
# reader. Fine on macOS/Acrobat, blank or boxed elsewhere.
_CID_FALLBACK = "STSong-Light"

# Resolved lazily at first use; registration is global state.
_cjk_ready = False
_cjk_embedded = False
# True when the file had no bold cut and weight must be faked by stroking.
_cjk_synth_bold = False
_cjk_font = _CID_FALLBACK
_cjk_bold = _CID_FALLBACK
_cjk_mono = _CID_FALLBACK


def has_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _register_cjk_font() -> bool:
    """Register a CJK face once, preferring one that can be embedded.

    Order matters. A ``UnicodeCIDFont`` is only a *reference* to one of Adobe's
    standard CJK fonts -- the PDF carries no glyph data, so the text renders
    only on a viewer that ships those fonts (macOS Preview, Acrobat) and comes
    out blank or boxed on Windows, Android and most Linux readers. A TrueType
    face is embedded as /FontFile2 and therefore renders anywhere, which for a
    report meant to be downloaded and shared is the whole point.

    reportlab's TTFont cannot read PostScript/CFF outlines, so the ubiquitous
    Noto Sans CJK ``.otf`` files (sfnt tag ``OTTO``) are not usable here despite
    being installed; AR PL UMing is the TrueType-outline CJK face available on
    Amazon Linux (package ``cjkuni-uming-fonts``).
    """
    global _cjk_ready, _cjk_font, _cjk_bold, _cjk_mono, _cjk_embedded, _cjk_synth_bold
    if _cjk_ready:
        return True
    from reportlab.pdfbase import pdfmetrics

    for path in _EMBEDDABLE_CJK:
        if not os.path.exists(path):
            continue
        try:
            from reportlab.pdfbase.ttfonts import TTFont

            reg_i, bold_i = _pick_faces(path)
            name, bold_name = "ystocker-cjk", "ystocker-cjk-b"
            pdfmetrics.registerFont(TTFont(name, path, subfontIndex=reg_i))
            if bold_i != reg_i:
                pdfmetrics.registerFont(TTFont(bold_name, path, subfontIndex=bold_i))
                _cjk_synth_bold = False
            else:
                # Only one weight in the file (uming is Light-only). Point bold
                # at the same face so <b> does not fall back to Helvetica-Bold,
                # which cannot draw CJK at all, and fake the weight by stroking.
                bold_name = name
                _cjk_synth_bold = True
            pdfmetrics.registerFontFamily(name, normal=name, bold=bold_name,
                                          italic=name, boldItalic=bold_name)
            _cjk_font, _cjk_bold, _cjk_mono = name, bold_name, name
            _cjk_embedded = True
            _cjk_ready = True
            log.info("report_pdf: embedding CJK font %s (regular=%d, bold=%d%s)",
                     path, reg_i, bold_i, ", synthesised" if _cjk_synth_bold else "")
            return True
        except Exception as exc:  # noqa: BLE001 - try the next candidate
            log.warning("report_pdf: cannot embed %s: %s", path, exc)

    # Nothing embeddable: fall back to the non-embedded CID font, which is
    # still far better than dropping every Han character.
    try:
        from reportlab.pdfbase.cidfonts import UnicodeCIDFont

        pdfmetrics.registerFont(UnicodeCIDFont(_CID_FALLBACK))
        pdfmetrics.registerFontFamily(
            _CID_FALLBACK, normal=_CID_FALLBACK, bold=_CID_FALLBACK,
            italic=_CID_FALLBACK, boldItalic=_CID_FALLBACK,
        )
        _cjk_font = _cjk_bold = _cjk_mono = _CID_FALLBACK
        _cjk_embedded = False
        _cjk_synth_bold = True
        _cjk_ready = True
        log.warning("report_pdf: no embeddable CJK font found; using "
                    "non-embedded %s (may not render off-macOS)", _CID_FALLBACK)
    except Exception as exc:  # noqa: BLE001 - fall back to Latin-only output
        log.warning("report_pdf: CJK font unavailable (%s); falling back", exc)
        _cjk_ready = False
    return _cjk_ready


# Characters _FOLD would damage in Chinese text but an embedded CJK face draws
# correctly. The ellipsis is the clearest case: Chinese uses a six-dot "……",
# and rewriting it to "..." is a typographic error, not a fallback.
_KEEP_IN_CJK = {"…", "·", "—", "–", "“", "”", "‘", "’"}


def _sanitize(text: str, cjk: bool = False) -> str:
    """Fold text to something the selected font can actually draw.

    ``cjk`` keeps Han/kana/Hangul and full-width punctuation, which the
    Helvetica path has to drop.
    """
    for src, dst in _FOLD.items():
        if cjk and src in _KEEP_IN_CJK:
            continue
        text = text.replace(src, dst)
    if cjk:
        # NFC, not NFKC. The compatibility forms are exactly the characters
        # Chinese typography depends on: NFKC rewrites the full-width comma,
        # colon and parentheses (：（）、) to their ASCII equivalents and expands
        # the ellipsis …… to six periods, which is simply wrong in Chinese text.
        # NFC composes without altering any of them.
        text = unicodedata.normalize("NFC", text)
        out = []
        for ch in text:
            if ch in "\n\t" or _CJK_RE.match(ch) or ch in _KEEP_IN_CJK:
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


# ---------------------------------------------------------------------------
# Emphasis
#
# Highlighting is deliberately scarce. These reports say "buy" and "看涨"
# constantly -- the bull and bear researchers argue for pages -- so shading
# every occurrence would leave a document that is uniformly yellow and
# therefore says nothing. Only the *actionable conclusions* are shaded: the
# decision itself and the handful of verdict lines a reader skims for. Ordinary
# ``**bold**`` stays bold, which is the second tier of emphasis, and the rest is
# plain.
# ---------------------------------------------------------------------------

# Tone -> (text colour, background). Green/red/amber read as buy/sell/hold to
# essentially everyone who reads a broker note.
_TONES = {
    "buy":  ("#166534", "#dcfce7"),
    "sell": ("#991b1b", "#fee2e2"),
    "hold": ("#92400e", "#fef3c7"),
    "flat": ("#334155", "#e8eefc"),
}

_TONE_WORDS = (
    ("buy",  ("buy", "overweight", "accumulate",
              "买入", "增持", "加仓", "超配")),
    ("sell", ("sell", "underweight", "reduce", "short",
              "卖出", "减持", "减仓", "低配", "做空")),
    ("hold", ("hold", "neutral", "market perform",
              "持有", "观望", "中性", "标配")),
)


def verdict_tone(text: str) -> str:
    """Classify a decision string as buy / sell / hold, else 'flat'.

    Checked in buy, sell, hold order and returns on the first hit, so a decision
    reading "Buy" wins over an incidental later "hold". A string with no verdict
    word gets the neutral tone rather than being left unstyled, since the
    decision box is drawn either way.
    """
    low = (text or "").casefold()
    for tone, words in _TONE_WORDS:
        if any(w in low for w in words):
            return tone
    return "flat"


# Lines a reader skims for: the explicit proposal, rating, target and horizon.
# Anchored at the start so a mid-sentence mention of a target does not promote a
# whole paragraph, and length-capped for the same reason.
_KEY_LINE_RE = re.compile(
    r"^\s*(?:\*\*)?\s*(?:"
    r"final\s+transaction\s+proposal|recommendation|rating|price\s+target|"
    r"time\s+horizon|action|verdict|"
    r"最终交易建议|投资建议|投资决策|评级|目标价|目標價|时间跨度|持有期限|操作建议"
    r")\s*(?:\*\*)?\s*[:：]",
    re.IGNORECASE)
_KEY_LINE_MAX = 200


def is_key_line(text: str) -> bool:
    """Whether this line is one of the report's actionable conclusions."""
    t = (text or "").strip()
    return bool(t) and len(t) <= _KEY_LINE_MAX and bool(_KEY_LINE_RE.match(t))


# Sentinels, so emphasis can be marked on the *plain* text and turned into tags
# only after escaping. Marking after escaping would risk matching inside a tag
# we just emitted; marking before escaping would see our own tags escaped.
_H0, _H1 = "\x00", "\x01"


def _inline(text: str, mono: str = "Courier", tone: Optional[str] = None) -> str:
    """Escape, then re-apply the inline markdown reportlab understands.

    ``tone`` shades the verdict words in this line, and is passed only for the
    key lines identified by :func:`is_key_line`.
    """
    if tone:
        words = dict(_TONE_WORDS).get(tone, ())
        for w in sorted(words, key=len, reverse=True):
            text = re.sub(f"({re.escape(w)})", _H0 + r"\1" + _H1, text,
                          flags=re.IGNORECASE)
    text = _escape(text)
    text = _BOLD_RE.sub(r"<b>\1</b>", text)
    text = _ITALIC_RE.sub(r"<i>\1</i>", text)
    # face= must be a font that can draw the span's characters, so it follows
    # the document's monospace choice rather than always being Courier.
    text = _CODE_RE.sub(lambda m: f'<font face="{mono}">{m.group(1)}</font>', text)
    if tone:
        fg, bg = _TONES.get(tone, _TONES["flat"])
        text = text.replace(_H0, f'<span backColor="{bg}" color="{fg}"><b>')
        text = text.replace(_H1, "</b></span>")
    return text


_PREAMBLE_DROP = re.compile(
    r"^\s*(?:#{1,3}\s*)?(?:trading\s+analysis\s+report\b.*|generated\s*[:：].*)$",
    re.IGNORECASE)


def _strip_redundant_preamble(text: str) -> str:
    """Drop the report's own title/stamp, which the PDF header already shows."""
    kept = [ln for ln in (text or "").splitlines() if not _PREAMBLE_DROP.match(ln)]
    return "\n".join(kept).strip()


def _charts_for(job: dict[str, Any], report_text: str) -> list[dict[str, Any]]:
    """Chart flowables for this job, or [] when none can be built.

    Wrapped so that a missing cache file, an unreadable CSV or a matplotlib
    problem costs the report its pictures and nothing else -- a text-only PDF is
    still the analysis the user paid for.
    """
    try:
        from reportlab.lib.units import inch
        from reportlab.platypus import Image

        from ystocker import report_charts

        out = []
        for spec in report_charts.build_all(job.get("ticker", ""), report_text):
            out.append({
                "flowable": Image(io.BytesIO(spec["png"]),
                                  width=spec["w"] * inch, height=spec["h"] * inch),
                "caption": spec["caption"],
            })
        return out
    except Exception as exc:  # noqa: BLE001
        log.warning("report_pdf: charts unavailable for %s: %s",
                    job.get("ticker"), exc)
        return []


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
    sn = _cjk_font if cjk else "Helvetica"
    sb = _cjk_bold if cjk else "Helvetica-Bold"
    mono = _cjk_mono if cjk else "Courier"

    def clean(t: str) -> str:
        return _sanitize(t, cjk=cjk)

    base = getSampleStyleSheet()
    if cjk:
        # Reports contain markdown tables and fenced blocks; Courier cannot draw
        # CJK either, so the preformatted style has to move as well.
        base["Code"].fontName = mono
        base["Code"].fontSize = 8
        base["Code"].leading = 11

    # uming has a single Light weight, so headings would otherwise be
    # indistinguishable from body text. Stroking the glyph outline in its own
    # colour thickens it; this is how reportlab fakes a bold cut.
    fake_bold = cjk and _cjk_synth_bold

    def heading(hex_colour: str) -> dict[str, Any]:
        c = colors.HexColor(hex_colour)
        if not fake_bold:
            return {"textColor": c}
        return {"textColor": c, "strokeColor": c, "strokeWidth": 0.3}

    styles = {
        "h1": ParagraphStyle("h1", parent=base["Heading1"], fontName=sb, fontSize=18, leading=22,
                             spaceBefore=2, spaceAfter=8, **heading("#1e293b")),
        "h2": ParagraphStyle("h2", parent=base["Heading2"], fontName=sb, fontSize=13, leading=17,
                             spaceBefore=14, spaceAfter=5, **heading("#334155")),
        "h3": ParagraphStyle("h3", parent=base["Heading3"], fontName=sb, fontSize=11, leading=15,
                             spaceBefore=10, spaceAfter=4, **heading("#475569")),
        "body": ParagraphStyle("body", parent=base["BodyText"], fontName=sn, fontSize=9.5, leading=13.5,
                               alignment=TA_LEFT, spaceAfter=6, wordWrap="CJK" if cjk else None),
        "bullet": ParagraphStyle("bullet", parent=base["BodyText"], fontName=sn, fontSize=9.5, leading=13.5,
                                 leftIndent=14, bulletIndent=4, spaceAfter=3,
                                 wordWrap="CJK" if cjk else None),
        "meta": ParagraphStyle("meta", parent=base["BodyText"], fontName=sn, fontSize=8.5, leading=12,
                               textColor=colors.HexColor("#64748b")),
        "decision": ParagraphStyle("decision", parent=base["BodyText"], fontName=sb, fontSize=13, leading=17,
                                   spaceAfter=4, **heading("#1e293b")),
        "caption": ParagraphStyle("caption", parent=base["BodyText"], fontName=sn, fontSize=7.5,
                                  leading=10, spaceBefore=2, spaceAfter=10,
                                  textColor=colors.HexColor("#64748b")),
    }

    def callout(tone: str, size: float = 13) -> ParagraphStyle:
        """A shaded box: used for the decision and the key verdict lines."""
        fg, bg = _TONES.get(tone, _TONES["flat"])
        return ParagraphStyle(
            f"callout-{tone}-{size}", parent=base["BodyText"], fontName=sb,
            fontSize=size, leading=size * 1.35,
            textColor=colors.HexColor(fg),
            backColor=colors.HexColor(bg),
            borderColor=colors.HexColor(fg), borderWidth=0,
            borderPadding=(6, 8, 6, 8), leftIndent=0,
            spaceBefore=2, spaceAfter=8,
            wordWrap="CJK" if cjk else None,
        )

    def badge(role: dict[str, str]) -> ParagraphStyle:
        """The role's name reversed out of its accent colour."""
        return ParagraphStyle(
            f"badge-{role['key']}", parent=base["BodyText"], fontName=sb,
            fontSize=9.5, leading=13, textColor=colors.white,
            backColor=colors.HexColor(role["color"]),
            borderPadding=(3, 7, 3, 7),
            spaceBefore=14, spaceAfter=6,
            wordWrap="CJK" if cjk else None,
        )

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

    tone = verdict_tone(decision or body_md[:400])
    if decision:
        flow.append(Paragraph(clean("投资决策" if cjk else "Decision"), styles["h2"]))
        flow.append(Paragraph(_inline(clean(decision), mono=mono, tone=tone),
                              callout(tone)))

    # Charts, from the OHLCV the run itself cached. Placed after the decision so
    # the reader sees the conclusion, then the price context behind it.
    for chart in _charts_for(job, body_md):
        flow.append(chart["flowable"])
        flow.append(Paragraph(clean(chart["caption"]), styles["caption"]))

    def emit_markdown(md: str) -> None:
        """Append one block of report markdown to the flow."""
        in_code = False
        code_buf: list[str] = []

        for raw_line in clean(md).splitlines():
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
            elif is_key_line(line):
                # An actionable conclusion: shade the whole line and tint the
                # verdict word inside it. Checked before the list and paragraph
                # branches so a bulleted "- **Rating**: Buy" is still promoted.
                body = re.sub(r"^\s*[-*+]\s+", "", line).strip()
                lt = verdict_tone(body)
                flow.append(Paragraph(_inline(body, mono=mono, tone=lt),
                                      callout(lt, size=10)))
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

    # Walk the report as role turns, badging each speaker, so the PDF shows the
    # same conversation structure as the page. A report that does not match the
    # expected shape yields one unbadged section, i.e. exactly the old output.
    from ystocker.agent_roles import split_sections

    for sec in split_sections(body_md):
        role = sec.get("role")
        if role:
            label = f"{role['zh']} · {role['name']}" if cjk else role["name"]
            flow.append(Paragraph(clean(label), badge(role)))
            emit_markdown(sec["body"])
            continue
        # The preamble is the report's own title and generation stamp, both of
        # which this document already prints in its header. Emitting them again
        # would put two titles two inches apart.
        body = _strip_redundant_preamble(sec["body"])
        if body.strip():
            emit_markdown(body)

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
