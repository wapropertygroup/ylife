"""
ystocker.report_pdf
~~~~~~~~~~~~~~~~~~~
Typesets an agent run's markdown report as a downloadable research note.

Uses reportlab's platypus flowables rather than an HTML-to-PDF engine on
purpose: weasyprint and wkhtmltopdf both pull in cairo/pango or a headless
browser, and the production box has ~1.3 GB of RAM free across eight apps (see
the measurement in agents.py). reportlab is pure Python and holds only the
document being built.

Three details that otherwise produce broken output rather than an error:

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
* **An over-wide flowable does not raise.** ``Frame._add`` only complains when
  something is too *tall*; something too wide is drawn straight through the
  right margin. Both the chart images (7.0 in from report_charts, against a
  6.8 in text column) and the report's markdown tables hit this, so every
  fixed-width flowable here is clamped to ``_Layout.avail`` explicitly.

Layout
------
The document is typeset as a research note rather than as a dump of markdown:

* Page 1 is the summary -- masthead, the decision in a signal panel, a strip of
  run facts, any caveats, and a linked table of contents. The reader gets the
  conclusion and the map before any prose.
* ``agent_roles.split_sections`` already knows the team each speaker belongs to,
  so teams become numbered parts and speakers become sections under them. Both
  are registered as PDF outline bookmarks, which is what makes a 20-page report
  navigable in a reader's sidebar.
* Markdown tables are rendered as real tables with wrapped cells and a repeated
  header row. They are the densest thing in these reports and the previous
  monospaced-passthrough both leaked ``|:---|`` separator rows and ran off the
  page.
* Emphasis is scarce on purpose: these reports say "buy" and "看涨" constantly,
  so shading every occurrence would leave a uniformly yellow document that says
  nothing. Only actionable conclusions get a tinted signal block, and a
  section's own ``FINAL TRANSACTION PROPOSAL`` is lifted out of the prose into a
  chip on the speaker's header, where it reads as a rating rather than as noise.
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
    " ": " ", "​": "", "﻿": "",
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


def _display_width(text: str) -> int:
    """Width of a string in "columns", counting full-width glyphs as two.

    Used to size table columns and to bound how long a line may be before it
    stops being treated as a heading. Han characters are twice as wide as
    Latin ones at the same point size, so counting codepoints would give a
    Chinese table nonsensically narrow columns.
    """
    return sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1
               for ch in text or "")


_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)")
_CODE_RE = re.compile(r"`([^`\n]+?)`")


# ---------------------------------------------------------------------------
# Palette
#
# One place for every colour in the document. The slate ramp is the same one
# report_charts draws with, so a chart dropped into the page shares the text's
# greys instead of introducing a second, slightly different set.
# ---------------------------------------------------------------------------
_INK = "#0f172a"        # titles and headings
_TEXT = "#1f2937"       # body copy: near-black, but not pure black
_TEXT_STRONG = "#000000"  # inline emphasis when the face has no bold cut
_SOFT = "#334155"        # subheadings
_MUTED = "#64748b"      # labels, captions
_FAINT = "#94a3b8"      # page furniture
_RULE = "#94a3b8"       # structural rules
_HAIR = "#e2e8f0"       # hairlines inside tables
_PANEL = "#f1f5f9"      # table header fill
_PANEL_LT = "#f8fafc"   # code block fill
_NOTE_FG = "#92400e"    # advisory text
_NOTE_BG = "#fffbeb"    # advisory fill


def _tint(hex_colour: str, keep: float):
    """Mix a colour towards white, keeping ``keep`` of it.

    Used for the speaker headers: the role accents in agent_roles are chosen
    for a dark web page, and at full saturation across a 6.8 in text column
    they print as twelve fluorescent bands. At 12% they read as a quiet wash
    that still identifies the speaker.
    """
    from reportlab.lib.colors import Color, HexColor

    c = HexColor(hex_colour)
    return Color(1 - (1 - c.red) * keep,
                 1 - (1 - c.green) * keep,
                 1 - (1 - c.blue) * keep)


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


# The proposal every analyst section opens with. Pulled out of the prose and
# shown as a chip on the speaker's header instead, because upstream emits it
# once per section and twelve identical full-width banners is noise.
_PROPOSAL_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:\*\*)?\s*"
    r"(?:final\s+transaction\s+proposal|最终交易建议)"
    r"\s*(?:\*\*)?\s*[:：]\s*(.+?)\s*$", re.IGNORECASE)

# Sentinels, so emphasis can be marked on the *plain* text and turned into tags
# only after escaping. Marking after escaping would risk matching inside a tag
# we just emitted; marking before escaping would see our own tags escaped.
_H0, _H1 = "\x00", "\x01"


def _inline(text: str, mono: str = "Courier", tone: Optional[str] = None,
            bold_colour: Optional[str] = None) -> str:
    """Escape, then re-apply the inline markdown reportlab understands.

    ``tone`` shades the verdict words in this line, and is passed only for the
    key lines identified by :func:`is_key_line`.

    ``bold_colour`` is the fallback for a CJK face with a single weight (uming,
    which is what the box has): ``<b>`` then resolves to the same outlines as
    the body, so ``**bold**`` would vanish entirely. Darkening the span is the
    one emphasis a single-weight font can still express -- reportlab's mini-HTML
    has no inline equivalent of the stroke trick used on headings, since
    ``<span>`` accepts only face, size and colour.
    """
    if tone:
        words = dict(_TONE_WORDS).get(tone, ())
        for w in sorted(words, key=len, reverse=True):
            text = re.sub(f"({re.escape(w)})", _H0 + r"\1" + _H1, text,
                          flags=re.IGNORECASE)
    text = _escape(text)
    if bold_colour:
        text = _BOLD_RE.sub(
            lambda m: f'<b><font color="{bold_colour}">{m.group(1)}</font></b>', text)
    else:
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


# ---------------------------------------------------------------------------
# Markdown block grammar
#
# Only the subset these reports actually contain. Everything unmatched falls
# through to a body paragraph, which is always safe.
# ---------------------------------------------------------------------------
_TBL_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TBL_SEP = re.compile(r"^\s*\|(?:\s*:?-{2,}:?\s*\|)+\s*$")
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)(\d{1,2})[.)]\s+(.*)$")
_BQ_RE = re.compile(r"^\s*>\s?(.*)$")
_HR_RE = re.compile(r"^\s*(?:[-=_]\s*){3,}$")
# A line that is nothing but bold text: the model's own run-in subheading
# ("**1. 均线系统：完美的多头排列**"). Bounded, or a bolded sentence would be
# promoted to a heading.
_LEAD_RE = re.compile(r"^\*\*(.+?)\*\*\s*[:：]?\s*$")
_LEAD_MAX = 72

_ROMAN = ["", "I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X"]
_ZH_NUM = ["", "一", "二", "三", "四", "五", "六", "七", "八", "九", "十"]

# A speaker's turn *inside* a section. The risk debate and the bull/bear
# exchange are one section each, and the model marks who is talking with a
# run-in "Aggressive Analyst:" rather than with a heading -- which is why
# agent_roles.split_sections cannot see these and the page ends up a wall of
# prose. Recognised only when the label resolves to a role the cast knows, so
# "Executive Summary:" and "Overall Sentiment:" stay ordinary paragraphs.
_TURN_RE = re.compile(r"^(?:\*\*)?([A-Za-z][A-Za-z /]{2,28}?)(?:\*\*)?\s*[:：]\s+(\S.*)$")
_TURN_NOUNS = ("analyst", "researcher", "research", "manager", "mgr", "trader")


def _role_for_turn(label: str) -> Optional[dict[str, str]]:
    """The role a run-in turn label names, or None.

    Matched on the distinguishing word rather than the whole string, because
    upstream is not consistent about the noun: the section heading says "Bull
    Researcher" and the debate text says "Bull Analyst".
    """
    from ystocker.agent_roles import ROLES

    words = [w for w in re.split(r"[\s/]+", label.casefold()) if w]
    # "Research Manager" is nothing but role nouns, so dropping them all would
    # leave nothing to match on; keep the first word in that case.
    words = [w for w in words if w not in _TURN_NOUNS] or words[:1]
    if len(words) != 1:
        return None
    word = {"risky": "aggressive", "safe": "conservative",
            "social": "sentiment"}.get(words[0], words[0])
    for role in ROLES:
        if role["short"].casefold().split()[0] == word or role["key"] == word:
            return role
    return None


def _first_drawable(font: str, candidates: str) -> str:
    """The first of ``candidates`` the font can actually draw.

    Bullets and separators are drawn as-is rather than passed through
    :func:`_sanitize`, and reportlab renders a codepoint the face has no glyph
    for as an empty box or nothing at all. A Chinese-only face is not obliged to
    carry U+2022, so it is asked instead of assumed: with the wrong bullet the
    lists in a 25-page report either sprout tofu or lose their markers.
    """
    from reportlab.pdfbase import pdfmetrics

    try:
        face = pdfmetrics.getFont(font).face
        c2g = face.charToGlyph
    except (AttributeError, KeyError):
        return candidates[0]   # a builtin or CID font; its coverage is fixed
    for ch in candidates:
        if ord(ch) in c2g:
            return ch
    return candidates[-1]


class _Layout:
    """Styles and flowable builders for one report.

    Instantiated per document because every style depends on which font was
    resolved, which in turn depends on whether the report contains CJK.
    """

    def __init__(self, cjk: bool, avail: float):
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet

        self.cjk = cjk
        self.avail = avail
        self.normal = _cjk_font if cjk else "Helvetica"
        self.bold = _cjk_bold if cjk else "Helvetica-Bold"
        self.mono = _cjk_mono if cjk else "Courier"
        # uming has a single Light weight, so <b> and the heading styles would
        # otherwise be indistinguishable from body text.
        self.fake_bold = cjk and _cjk_synth_bold
        self.bold_colour = _TEXT_STRONG if self.fake_bold else None
        self.figures = 0
        self._toc_n = 0
        # Marks drawn outside the text stream, so they need a glyph the chosen
        # face really has -- see _first_drawable.
        self.bullets = (_first_drawable(self.normal, "•·-"),
                        _first_drawable(self.normal, "–-"))
        self.sep = _first_drawable(self.normal, "·-")

        wrap = "CJK" if cjk else None
        base = getSampleStyleSheet()

        def hx(name: str):
            return colors.HexColor(name)

        def heavy(colour: str) -> dict[str, Any]:
            """Heading colour, stroking the outline when there is no bold cut."""
            c = hx(colour)
            if not self.fake_bold:
                return {"textColor": c}
            return {"textColor": c, "strokeColor": c, "strokeWidth": 0.3}

        self.hx = hx
        S = {}
        S["eyebrow"] = ParagraphStyle(
            "eyebrow", parent=base["BodyText"], fontName=self.bold, fontSize=7.5,
            leading=10, textColor=hx(_MUTED), spaceAfter=3, wordWrap=wrap)
        S["title"] = ParagraphStyle(
            "title", parent=base["Title"], fontName=self.bold, fontSize=21,
            leading=25, alignment=TA_LEFT, spaceBefore=6, spaceAfter=4,
            wordWrap=wrap, **heavy(_INK))
        S["subtitle"] = ParagraphStyle(
            "subtitle", parent=base["BodyText"], fontName=self.normal, fontSize=9,
            leading=13, textColor=hx(_MUTED), spaceAfter=2, wordWrap=wrap)

        # Parts (a team) and sections (a speaker).
        S["part"] = ParagraphStyle(
            "part", parent=base["Heading1"], fontName=self.bold, fontSize=14,
            leading=18, spaceBefore=3, spaceAfter=9, keepWithNext=1,
            wordWrap=wrap, **heavy(_INK))
        S["part_no"] = ParagraphStyle(
            "part_no", parent=base["BodyText"], fontName=self.bold, fontSize=7.5,
            leading=10, textColor=hx(_MUTED), spaceBefore=2, spaceAfter=0,
            wordWrap=wrap)
        S["speaker"] = ParagraphStyle(
            "speaker", parent=base["BodyText"], fontName=self.bold, fontSize=11.5,
            leading=15, wordWrap=wrap)
        S["chip"] = ParagraphStyle(
            "chip", parent=base["BodyText"], fontName=self.bold, fontSize=8,
            leading=12, alignment=TA_RIGHT, wordWrap=wrap)

        # The model's own headings inside a section body. Three levels is all
        # the depth a section needs; #### and deeper collapse into h3.
        S["h2"] = ParagraphStyle(
            "h2", parent=base["Heading2"], fontName=self.bold, fontSize=12,
            leading=16, spaceBefore=13, spaceAfter=5, keepWithNext=1,
            wordWrap=wrap, **heavy(_INK))
        S["h3"] = ParagraphStyle(
            "h3", parent=base["Heading3"], fontName=self.bold, fontSize=10.5,
            leading=14, spaceBefore=10, spaceAfter=4, keepWithNext=1,
            wordWrap=wrap, **heavy(_SOFT))
        S["h4"] = ParagraphStyle(
            "h4", parent=base["Heading4"], fontName=self.bold, fontSize=9.8,
            leading=13, spaceBefore=8, spaceAfter=3, keepWithNext=1,
            wordWrap=wrap, **heavy(_SOFT))

        # Ragged right on both paths. Han glyphs are all one width, so unjustified
        # Chinese already sits all but flush and loses nothing; and reportlab's
        # justification does not stretch a run of Han at all -- it only widens
        # the *spaces* on a line, so a mixed line carrying two of them gets one
        # gaping hole instead of an even measure. Latin justified without a
        # hyphenation dictionary opens rivers for the same reason.
        S["body"] = ParagraphStyle(
            "body", parent=base["BodyText"], fontName=self.normal, fontSize=9.5,
            leading=14, textColor=hx(_TEXT), spaceAfter=6,
            alignment=TA_LEFT, wordWrap=wrap)
        # Hanging indents: the wrapped lines of a list item align under the
        # item's text, never under its bullet.
        S["ul"] = ParagraphStyle(
            "ul", parent=S["body"], leftIndent=15, bulletIndent=3,
            spaceAfter=3, bulletFontName=self.normal, bulletFontSize=8)
        S["ul2"] = ParagraphStyle(
            "ul2", parent=S["ul"], leftIndent=30, bulletIndent=18)
        S["ol"] = ParagraphStyle(
            "ol", parent=S["body"], leftIndent=20, bulletIndent=3,
            spaceAfter=3, bulletFontName=self.normal, bulletFontSize=9.5)
        S["quote"] = ParagraphStyle(
            "quote", parent=S["body"], fontSize=9, leading=13.5,
            textColor=hx(_SOFT), leftIndent=10, spaceBefore=2, spaceAfter=6)
        # A turn opens with its speaker's name, so it gets the air a new speaker
        # deserves and a rule above to separate it from the previous one.
        S["turn"] = ParagraphStyle(
            "turn", parent=S["body"], spaceBefore=9, spaceAfter=6)
        S["meta_label"] = ParagraphStyle(
            "meta_label", parent=base["BodyText"], fontName=self.normal,
            fontSize=7.5, leading=10, textColor=hx(_MUTED), wordWrap=wrap)
        S["meta_value"] = ParagraphStyle(
            "meta_value", parent=base["BodyText"], fontName=self.bold,
            fontSize=9.5, leading=13, textColor=hx(_INK), wordWrap=wrap)
        S["note"] = ParagraphStyle(
            "note", parent=base["BodyText"], fontName=self.normal, fontSize=8.5,
            leading=12.5, textColor=hx(_NOTE_FG), wordWrap=wrap)
        S["caption"] = ParagraphStyle(
            "caption", parent=base["BodyText"], fontName=self.normal, fontSize=7.5,
            leading=10.5, alignment=TA_CENTER, textColor=hx(_MUTED),
            spaceBefore=3, wordWrap=wrap)
        S["colophon"] = ParagraphStyle(
            "colophon", parent=base["BodyText"], fontName=self.normal, fontSize=8,
            leading=12, textColor=hx(_MUTED), wordWrap=wrap)
        S["decision_label"] = ParagraphStyle(
            "decision_label", parent=base["BodyText"], fontName=self.bold,
            fontSize=7.5, leading=11, textColor=hx(_MUTED), spaceAfter=2,
            wordWrap=wrap)

        # Table cells. One style per alignment, because a Paragraph inside a
        # cell is aligned by its own style -- the table's ALIGN command only
        # moves plain strings. The "_lat" variants drop CJK word wrapping: it
        # breaks between any two characters, which in a numeric cell splits
        # "-16.17%" across two lines with the percent sign stranded.
        for key, align in (("l", TA_LEFT), ("c", TA_CENTER), ("r", TA_RIGHT)):
            S[f"td_{key}"] = ParagraphStyle(
                f"td_{key}", parent=base["BodyText"], fontName=self.normal,
                fontSize=8.5, leading=12, textColor=hx(_TEXT),
                alignment=align, spaceAfter=0, wordWrap=wrap)
            S[f"th_{key}"] = ParagraphStyle(
                f"th_{key}", parent=S[f"td_{key}"], fontName=self.bold,
                fontSize=8.5, textColor=hx(_INK), **({} if not self.fake_bold else {
                    "strokeColor": hx(_INK), "strokeWidth": 0.25}))
            S[f"td_{key}_lat"] = ParagraphStyle(
                f"td_{key}_lat", parent=S[f"td_{key}"], wordWrap=None)
            S[f"th_{key}_lat"] = ParagraphStyle(
                f"th_{key}_lat", parent=S[f"th_{key}"], wordWrap=None)

        # Fenced blocks and any table too ragged to parse. XPreformatted, not
        # Preformatted: it wraps, and an unwrapped 200-character table row runs
        # straight off the page.
        S["code"] = ParagraphStyle(
            "code", parent=base["Code"], fontName=self.mono, fontSize=8,
            leading=11, textColor=hx(_TEXT), wordWrap="CJK",
            leftIndent=0, firstLineIndent=0, spaceBefore=0, spaceAfter=0)
        S["toc0"] = ParagraphStyle(
            "toc0", parent=base["BodyText"], fontName=self.bold, fontSize=9.5,
            leading=15, textColor=hx(_INK), spaceBefore=7, rightIndent=34,
            wordWrap=wrap)
        S["toc1"] = ParagraphStyle(
            "toc1", parent=base["BodyText"], fontName=self.normal, fontSize=9,
            leading=13.5, textColor=hx(_SOFT), leftIndent=15, rightIndent=34,
            wordWrap=wrap)
        self.styles = S

    # -- text helpers -------------------------------------------------------

    def mark(self, flowable, level: int, text: str):
        """Tag a flowable as a contents entry and an outline bookmark.

        The key has to be derived from the story, not from a counter on the
        document: ``multiBuild`` lays the whole thing out repeatedly and stops
        only once two passes produce identical entries, so a key that counted
        page-draw order would differ every pass and the build would never
        converge.
        """
        self._toc_n += 1
        flowable._toc_entry = (level, _escape(self.clean(text)),
                               f"sec{self._toc_n}")
        return flowable

    def clean(self, text: str) -> str:
        return _sanitize(text, cjk=self.cjk)

    def md(self, text: str, tone: Optional[str] = None) -> str:
        """Sanitise and convert one line of markdown to Paragraph mini-HTML."""
        return _inline(self.clean(text), mono=self.mono, tone=tone,
                       bold_colour=self.bold_colour)

    def para(self, text: str, style: str, **kw):
        from reportlab.platypus import Paragraph

        return Paragraph(self.md(text), self.styles[style], **kw)

    def plain(self, text: str, style: str, **kw):
        """A Paragraph with no markdown interpretation, only escaping."""
        from reportlab.platypus import Paragraph

        return Paragraph(_escape(self.clean(text)), self.styles[style], **kw)

    def rule(self, thickness: float = 0.5, colour: str = _HAIR,
             before: float = 0, after: float = 0):
        from reportlab.platypus import HRFlowable

        return HRFlowable(width="100%", thickness=thickness,
                          color=self.hx(colour), spaceBefore=before,
                          spaceAfter=after)

    # -- boxes --------------------------------------------------------------

    def accent_box(self, rows: list[Any], fg: str, bg: Any,
                   pad: tuple[float, float, float, float] = (9, 11, 9, 11),
                   bar: float = 3.0, space_before: float = 4,
                   space_after: float = 9):
        """A block with a coloured bar down its left edge.

        The house shape for anything that carries a signal -- the decision, a
        verdict line, a caveat, a speaker's header. One shape used four times
        reads as a system; four different shapes read as decoration.
        """
        from reportlab.platypus import Table, TableStyle

        top, right, bottom, left = pad
        # splitInRow, so a box holding more than a page of text splits instead
        # of raising: a single-row table cannot break between rows, and
        # reportlab treats an unsplittable oversized flowable as a fatal layout
        # error rather than letting it overflow.
        t = Table([[r] for r in rows], colWidths=[self.avail], hAlign="LEFT",
                  splitInRow=1)
        style = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), left),
            ("RIGHTPADDING", (0, 0), (-1, -1), right),
            ("TOPPADDING", (0, 0), (-1, 0), top),
            ("BOTTOMPADDING", (0, -1), (-1, -1), bottom),
            ("TOPPADDING", (0, 1), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -2), 0),
            ("LINEBEFORE", (0, 0), (0, -1), bar, self.hx(fg)),
        ]
        if bg is not None:
            style.append(("BACKGROUND", (0, 0), (-1, -1), bg))
        t.setStyle(TableStyle(style))
        t.spaceBefore = space_before
        t.spaceAfter = space_after
        return t

    def decision_panel(self, decision: str, tone: str):
        """The conclusion, at the top of page one, in its verdict colour."""
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph

        fg, bg = _TONES.get(tone, _TONES["flat"])
        label = "投资决策" if self.cjk else "DECISION"
        value = ParagraphStyle(
            f"decision-{tone}", parent=self.styles["body"], fontName=self.bold,
            fontSize=15, leading=20, textColor=self.hx(fg), spaceAfter=0,
            **({"strokeColor": self.hx(fg), "strokeWidth": 0.35}
               if self.fake_bold else {}))
        # No per-word shading inside: the panel already carries the tone, and
        # highlighting "buy" inside a green box says the same thing twice.
        return self.accent_box(
            [self.plain(label, "decision_label"),
             Paragraph(self.md(decision), value)],
            fg, self.hx(bg), pad=(10, 12, 11, 13), bar=3.5,
            space_before=2, space_after=12)

    def fact_strip(self, facts: list[tuple[str, str]]):
        """Run metadata as a labelled strip rather than a run-on line.

        Four columns per band: labels above values, hairlines above and below.
        A dot-joined sentence of six facts is a paragraph the reader has to
        parse; this is a table they can scan.
        """
        from reportlab.platypus import Table, TableStyle

        out = []
        for start in range(0, len(facts), 4):
            band = facts[start:start + 4]
            cols = len(band)
            widths = [self.avail / cols] * cols
            data = [[self.plain(k, "meta_label") for k, _ in band],
                    [self.plain(v, "meta_value") for _, v in band]]
            t = Table(data, colWidths=widths, hAlign="LEFT")
            t.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, 0), 6),
                ("BOTTOMPADDING", (0, 0), (-1, 0), 1),
                ("TOPPADDING", (0, 1), (-1, 1), 0),
                ("BOTTOMPADDING", (0, 1), (-1, 1), 7),
                ("LINEABOVE", (0, 0), (-1, 0), 0.5, self.hx(_HAIR)),
                ("LINEBELOW", (0, -1), (-1, -1), 0.5, self.hx(_HAIR)),
            ]))
            t.spaceAfter = 0 if start + 4 < len(facts) else 12
            out.append(t)
        return out

    def advisory(self, lines: list[str]):
        """Caveats that travel with the artefact.

        The PDF is the thing that gets saved and forwarded, so a downgraded or
        recovered run has to disclose itself on the document, not only in the
        web UI.
        """
        label = "运行提示" if self.cjk else "PLEASE NOTE"
        rows = [self.plain(label, "decision_label")]
        rows += [self.para(f"— {ln}", "note") for ln in lines]
        return self.accent_box(rows, _NOTE_FG, self.hx(_NOTE_BG),
                               pad=(8, 11, 9, 11), bar=2.5,
                               space_before=0, space_after=12)

    def speaker_header(self, role: dict[str, str], chip: Optional[str]):
        """A speaker's name, with their standing proposal as a chip.

        The accent is the same one the web page gives the role, washed out to
        12% -- see :func:`_tint`.
        """
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.platypus import Paragraph, Table, TableStyle

        accent = role["color"]
        if self.cjk:
            name, sub = role["zh"], role["name"]
        else:
            name, sub = role["name"], role.get("zh", "")
        title = ParagraphStyle(
            f"sp-{role['key']}", parent=self.styles["speaker"],
            textColor=self.hx(accent),
            **({"strokeColor": self.hx(accent), "strokeWidth": 0.3}
               if self.fake_bold else {}))
        head = _escape(self.clean(name))
        if sub:
            head += (f'  <font size="8" face="{self.normal}" color="{_MUTED}">'
                     f'{_escape(self.clean(sub))}</font>')
        cells: list[Any] = [Paragraph(head, title)]
        widths = [self.avail]
        if chip:
            tone = verdict_tone(chip)
            fg, bg = _TONES.get(tone, _TONES["flat"])
            chip_txt = (f'<span backColor="{bg}" color="{fg}"> '
                        f'{_escape(self.clean(chip))} </span>')
            cells.append(Paragraph(chip_txt, self.styles["chip"]))
            widths = [self.avail * 0.62, self.avail * 0.38]

        t = Table([cells], colWidths=widths, hAlign="LEFT")
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("BACKGROUND", (0, 0), (-1, -1), _tint(accent, 0.12)),
            ("LINEBEFORE", (0, 0), (0, -1), 3, self.hx(accent)),
            ("LEFTPADDING", (0, 0), (0, -1), 9),
            ("RIGHTPADDING", (-1, 0), (-1, -1), 9),
            ("TOPPADDING", (0, 0), (-1, -1), 5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ]))
        t.spaceBefore = 15
        t.spaceAfter = 8
        t.keepWithNext = True
        return self.mark(t, 1, name)

    def part_header(self, index: int, label: str):
        """A team divider, numbered, as the document's top-level structure."""
        from reportlab.platypus import KeepTogether

        if self.cjk:
            no = f"第{_ZH_NUM[index] if index < len(_ZH_NUM) else index}部分"
        else:
            no = f"PART {_ROMAN[index] if index < len(_ROMAN) else index}"
        head = self.mark(self.plain(label, "part"), 0, label)
        return [KeepTogether([self.rule(1.1, _INK, after=0),
                              self.plain(no, "part_no"), head])]

    def code_block(self, lines: list[str]):
        from reportlab.platypus import Table, TableStyle, XPreformatted

        body = XPreformatted("\n".join(self._fold_mono(lines)),
                             self.styles["code"])
        t = Table([[body]], colWidths=[self.avail], hAlign="LEFT",
                  splitInRow=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, -1), self.hx(_PANEL_LT)),
            ("BOX", (0, 0), (-1, -1), 0.4, self.hx(_HAIR)),
            ("LEFTPADDING", (0, 0), (-1, -1), 8),
            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
            ("TOPPADDING", (0, 0), (-1, -1), 7),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 7),
        ]))
        t.spaceBefore = 4
        t.spaceAfter = 8
        return t

    def _fold_mono(self, lines: list[str]) -> list[str]:
        """Hard-wrap monospaced lines to the panel width.

        Preformatted text is the one place a line cannot be re-flowed, so
        reportlab draws it at whatever width it comes out as -- a single long
        token walks off the edge of the *paper*, not just the margin. Folding it
        here is the only way to keep it on the page, and the fold is measured
        rather than counted because the CJK monospace face draws Han at twice
        the width of ASCII.
        """
        from reportlab.pdfbase.pdfmetrics import stringWidth

        size = self.styles["code"].fontSize
        room = self.avail - 18   # the panel's own left and right padding
        out: list[str] = []
        for raw in lines:
            line = self.clean(raw)
            cur, width = "", 0.0
            for ch in line:
                cw = stringWidth(ch, self.mono, size)
                if cur and width + cw > room:
                    out.append(cur)
                    cur, width = ch, cw
                else:
                    cur += ch
                    width += cw
            out.append(cur)
        return out

    def figure(self, png: bytes, w_in: float, h_in: float, caption: str):
        """A chart, numbered, never separated from its caption."""
        from reportlab.lib.units import inch
        from reportlab.platypus import Image, KeepTogether

        # report_charts draws 7.0 in wide, which is wider than the text column.
        # Over-wide flowables do not raise -- they print through the margin --
        # so the clamp is not optional.
        w, h = w_in * inch, h_in * inch
        if w > self.avail:
            h *= self.avail / w
            w = self.avail
        self.figures += 1
        label = (f"图 {self.figures}" if self.cjk else f"Figure {self.figures}")
        img = Image(io.BytesIO(png), width=w, height=h)
        img.hAlign = "CENTER"
        cap = self.para(f"**{label}** {self.sep} {caption}", "caption")
        return KeepTogether([img, cap])

    # -- tables -------------------------------------------------------------

    def markdown_table(self, block: list[str]):
        """Render a run of ``|``-delimited lines as a real table.

        Returns None when the block is too ragged to be trustworthy, so the
        caller can fall back to showing it verbatim. That matters more than it
        sounds: these tables carry the numbers the whole report argues about,
        and a mis-split row silently moves a value into the wrong column.
        """
        from reportlab.platypus import Paragraph, Table, TableStyle

        rows: list[list[str]] = []
        aligns: Optional[list[str]] = None
        for line in block:
            if _TBL_SEP.match(line):
                if aligns is None and rows:
                    aligns = [self._align_of(c) for c in self._cells(line)]
                continue
            cells = self._cells(line)
            if cells:
                rows.append(cells)
        if len(rows) < 2:
            return None
        ncols = max(len(r) for r in rows)
        if ncols < 2 or ncols > 10:
            return None
        # Ragged rows are normal in LLM output. Pad short ones; fold the
        # overflow of a long one into its last cell rather than dropping data.
        norm = []
        for r in rows:
            if len(r) < ncols:
                r = r + [""] * (ncols - len(r))
            elif len(r) > ncols:
                r = r[:ncols - 1] + [" ".join(r[ncols - 1:])]
            norm.append(r)
        has_head = aligns is not None
        if aligns is None or len(aligns) != ncols:
            aligns = ["l"] * ncols
        widths = self._col_widths(norm, ncols)

        data = []
        for i, r in enumerate(norm):
            kind = "th" if (has_head and i == 0) else "td"
            data.append([
                Paragraph(self.md(c),
                          self.styles[f"{kind}_{a}{self._cell_wrap(c, w)}"])
                for c, a, w in zip(r, aligns, widths)])

        cmds = [
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
            ("RIGHTPADDING", (0, 0), (-1, -1), 5),
            ("TOPPADDING", (0, 0), (-1, -1), 4.5),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4.5),
            # Horizontal rules only. Vertical rules and zebra fills are what
            # make a dense table look like a spreadsheet screenshot.
            ("LINEABOVE", (0, 0), (-1, 0), 0.9, self.hx(_INK)),
            ("LINEBELOW", (0, -1), (-1, -1), 0.9, self.hx(_INK)),
        ]
        if has_head:
            cmds += [
                ("BACKGROUND", (0, 0), (-1, 0), self.hx(_PANEL)),
                ("LINEBELOW", (0, 0), (-1, 0), 0.6, self.hx(_RULE)),
            ]
            if len(data) > 2:
                cmds.append(("LINEBELOW", (0, 1), (-1, -2), 0.25, self.hx(_HAIR)))
        elif len(data) > 1:
            cmds.append(("LINEBELOW", (0, 0), (-1, -2), 0.25, self.hx(_HAIR)))

        t = Table(data, colWidths=widths, hAlign="LEFT", splitInRow=1,
                  repeatRows=1 if has_head else 0)
        t.setStyle(TableStyle(cmds))
        t.spaceBefore = 6
        t.spaceAfter = 10
        return t

    @staticmethod
    def _cells(line: str) -> list[str]:
        s = line.strip()
        if s.startswith("|"):
            s = s[1:]
        if s.endswith("|"):
            s = s[:-1]
        return [c.strip() for c in s.split("|")]

    @staticmethod
    def _align_of(spec: str) -> str:
        spec = spec.strip()
        if spec.startswith(":") and spec.endswith(":"):
            return "c"
        if spec.endswith(":"):
            return "r"
        return "l"

    def _cell_wrap(self, cell: str, width: float) -> str:
        """"_lat" when this cell should wrap on spaces rather than anywhere.

        A cell of Latin text -- which in these tables means a number, a ticker
        or a short label -- must not be broken mid-token. It is only safe to say
        so when every token in it actually fits the column, since reportlab
        cannot hyphenate and would otherwise push the token out of the cell.
        """
        from reportlab.pdfbase.pdfmetrics import stringWidth

        if not cell or has_cjk(cell):
            return ""
        room = width - 12
        size = self.styles["td_l"].fontSize
        plain = re.sub(r"[*`]", "", cell)
        if all(stringWidth(tok, self.normal, size) <= room
               for tok in plain.split()):
            return "_lat"
        return ""

    def _col_widths(self, rows: list[list[str]], ncols: int) -> list[float]:
        """Proportional column widths, bounded at both ends.

        The weight is the widest cell in the column, but clamped: one column of
        prose commentary would otherwise squeeze a four-column table's labels to
        a character and a half, and every column needs enough room for a couple
        of characters or reportlab wraps it one glyph per line.
        """
        weights = []
        for c in range(ncols):
            longest = max(_display_width(re.sub(r"[*`]", "", r[c])) for r in rows)
            weights.append(min(max(longest, 6), 34))
        total = float(sum(weights)) or 1.0
        widths = [self.avail * w / total for w in weights]
        floor = min(0.55 * 72, self.avail / (ncols + 1))
        short = [i for i, w in enumerate(widths) if w < floor]
        if short:
            debt = sum(floor - widths[i] for i in short)
            spare = [i for i in range(ncols) if i not in short]
            pool = sum(widths[i] for i in spare) or 1.0
            for i in short:
                widths[i] = floor
            for i in spare:
                widths[i] -= debt * widths[i] / pool
        return widths

    # -- the body -----------------------------------------------------------

    def emit(self, md: str, flow: list[Any]) -> None:
        """Append one section's markdown to the story."""
        lines = self.clean(md).splitlines()
        i, n = 0, len(lines)
        while i < n:
            line = lines[i].rstrip()
            s = line.strip()

            # Fenced code: collected verbatim, since XPreformatted must not be
            # escaped as mini-HTML the way Paragraph is.
            if s.startswith("```"):
                j, buf = i + 1, []
                while j < n and not lines[j].strip().startswith("```"):
                    buf.append(lines[j])
                    j += 1
                if buf:
                    flow.append(self.code_block(buf))
                i = j + 1
                continue

            if not s:
                i += 1
                continue

            # A table is a block, not a line, so it has to be collected before
            # anything can be emitted.
            if _TBL_ROW.match(line):
                j = i
                while j < n and _TBL_ROW.match(lines[j]):
                    j += 1
                block = [lines[k] for k in range(i, j)]
                tbl = self.markdown_table(block)
                if tbl is not None:
                    flow.append(tbl)
                else:
                    # Verbatim as a last resort, minus the alignment row, which
                    # is markdown scaffolding and never something to show.
                    flow.append(self.code_block(
                        [ln for ln in block if not _TBL_SEP.match(ln)]))
                i = j
                continue

            if s.startswith("#"):
                depth = len(s) - len(s.lstrip("#"))
                text = s[depth:].strip()
                if text:
                    flow.append(self.para(
                        text, "h2" if depth <= 2 else ("h3" if depth == 3 else "h4")))
                i += 1
                continue

            if _HR_RE.match(s):
                flow.append(self.rule(0.5, _HAIR, before=5, after=5))
                i += 1
                continue

            if is_key_line(s):
                # An actionable conclusion: a signal block, tinted by its own
                # verdict. Checked before the list branches so a bulleted
                # "- **Rating**: Buy" is still promoted.
                body = re.sub(r"^\s*[-*+]\s+", "", s).strip()
                tone = verdict_tone(body)
                fg, bg = _TONES.get(tone, _TONES["flat"])
                flow.append(self.accent_box(
                    [self.para(body, "body")], fg, self.hx(bg),
                    pad=(6, 9, 6, 9), bar=2.5, space_before=4, space_after=8))
                i += 1
                continue

            m = _BQ_RE.match(line)
            if m:
                j, buf = i, []
                while j < n and _BQ_RE.match(lines[j]):
                    buf.append(_BQ_RE.match(lines[j]).group(1).strip())
                    j += 1
                text = " ".join(x for x in buf if x)
                if text:
                    flow.append(self.accent_box(
                        [self.para(text, "quote")], _RULE, None,
                        pad=(2, 4, 2, 10), bar=2, space_before=4, space_after=7))
                i = j
                continue

            m = _UL_RE.match(line)
            if m:
                depth = min(len(m.group(1)) // 2, 1)
                flow.append(self.para(m.group(2), "ul" if not depth else "ul2",
                                      bulletText=self.bullets[depth]))
                i += 1
                continue

            m = _OL_RE.match(line)
            if m:
                flow.append(self.para(m.group(3), "ol",
                                      bulletText=f"{m.group(2)}."))
                i += 1
                continue

            m = _LEAD_RE.match(s)
            if m and _display_width(m.group(1)) <= _LEAD_MAX:
                flow.append(self.para(m.group(1), "h4"))
                i += 1
                continue

            m = _TURN_RE.match(s)
            role = _role_for_turn(m.group(1)) if m else None
            if role:
                # A run-in label in the speaker's own accent: enough to show the
                # debate changing hands without another banner every paragraph.
                # Built as mini-HTML directly, since self.para would escape the
                # tags along with the text.
                from reportlab.platypus import Paragraph

                flow.append(Paragraph(
                    f'<font color="{role["color"]}"><b>'
                    f"{_escape(self.clean(m.group(1)))}</b></font>  "
                    f"{self.md(m.group(2))}", self.styles["turn"]))
                i += 1
                continue

            flow.append(self.para(s, "body"))
            i += 1


def _charts_for(job: dict[str, Any], report_text: str) -> list[dict[str, Any]]:
    """Chart specs for this job, or [] when none can be built.

    Wrapped so that a missing cache file, an unreadable CSV or a matplotlib
    problem costs the report its pictures and nothing else -- a text-only PDF is
    still the analysis the user paid for.
    """
    try:
        from ystocker import report_charts

        return report_charts.build_all(job.get("ticker", ""), report_text)
    except Exception as exc:  # noqa: BLE001
        log.warning("report_pdf: charts unavailable for %s: %s",
                    job.get("ticker"), exc)
        return []


# Page geometry, in points. Shared with the canvas and the page furniture,
# which draw outside the frame and so cannot ask the document for its margins.
_PAGE_W, _PAGE_H = 8.5 * 72, 11 * 72
_MARGIN_X = 0.85 * 72
_MARGIN_TOP = 0.88 * 72
_MARGIN_BOT = 0.85 * 72
_FOOTER_Y = _MARGIN_BOT - 33
_HEADER_Y = _PAGE_H - _MARGIN_TOP + 15

# Reserved to the right of the measure on the CJK path. reportlab's CJK line
# breaker will not let a line *begin* with punctuation that Chinese forbids
# there (、。」etc.), and it enforces that by keeping the character on the line
# it has already filled -- overflowing the measure by up to one em rather than
# rebreaking. That is the correct typographic rule (悬挂标点, hanging
# punctuation) but it needs somewhere to hang: without this gutter the overhang
# lands past the right margin and the text block loses its edge. Every
# full-width flowable is laid out to the same reduced measure, so the hang is
# the only thing in the gutter.
_CJK_GUTTER = 10.0


def _report_doc(buf, lay: _Layout, running: str, footer: str,
                pages: dict[str, int], **meta):
    """A document template with running furniture, bookmarks and a TOC hook.

    SimpleDocTemplate cannot do any of the three: page furniture that knows
    which section it is on, PDF outline entries, or a table of contents (which
    needs the build to run more than once, since an entry's page number is not
    known until that page has been laid out).

    ``pages`` carries the page total across builds. It cannot be discovered
    during the build that needs it -- the denominator of "3 / 26" is only known
    once page 26 exists -- and the usual trick of holding every page back until
    save() and stamping them then is not available here: deferring showPage
    means no page object exists while the story is being laid out, so every
    bookmark and internal link resolves to page 1. Since a document with a
    contents page is laid out repeatedly anyway, the count from the previous
    pass is used, which is exact as soon as pagination has settled.
    """
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import LETTER
    from reportlab.platypus import BaseDocTemplate, Frame, PageTemplate

    class _ReportDoc(BaseDocTemplate):
        def __init__(self):
            super().__init__(buf, pagesize=LETTER,
                             leftMargin=_MARGIN_X, rightMargin=_MARGIN_X,
                             topMargin=_MARGIN_TOP, bottomMargin=_MARGIN_BOT,
                             **meta)
            frame = Frame(self.leftMargin, self.bottomMargin,
                          lay.avail, self.height, id="body",
                          leftPadding=0, rightPadding=0,
                          topPadding=0, bottomPadding=0)
            self.addPageTemplates([
                PageTemplate("body", [frame], onPageEnd=self._furniture)])
            self._section = ""
            self._depth = -1

        def afterFlowable(self, flowable):
            """Register parts and speakers as bookmarks and TOC entries."""
            entry = getattr(flowable, "_toc_entry", None)
            if not entry:
                return
            level, text, key = entry
            self.canv.bookmarkPage(key)
            # The outline is a tree, and reportlab raises rather than inventing
            # a parent, so a level may never skip one. A report whose team
            # headings are missing -- an interrupted run, or an upstream change
            # to write_report_tree -- has speakers (level 1) and no parts
            # (level 0), and that must cost the document its nesting, not the
            # whole PDF.
            level = min(level, self._depth + 1)
            self.canv.addOutlineEntry(text, key, level=level, closed=False)
            self._depth = level
            self.notify("TOCEntry", entry[:2] + (self.page, key))
            self._section = text

        def beforeDocument(self):
            """Adopt the page count the previous layout pass arrived at.

            Also clears the running head: a document with a contents page is
            laid out more than once, and a section name left over from the end
            of the previous pass would otherwise be printed on the front pages
            of this one.
            """
            pages["total"] = pages.get("counted", 0)
            self._section = ""
            self._depth = -1

        def handle_pageEnd(self):
            pages["counted"] = self.page
            super().handle_pageEnd()

        def _furniture(self, canv, doc):
            """Header and footer, drawn at page end.

            At page end ``_section`` names the last section this page started,
            which is the convention a reader expects from a running head.
            """
            total = pages.get("total") or 0
            if lay.cjk:
                label = (f"第 {doc.page} / {total} 页" if total
                         else f"第 {doc.page} 页")
            else:
                label = f"{doc.page} / {total}" if total else str(doc.page)
            right = _MARGIN_X + lay.avail
            canv.saveState()
            canv.setFont(lay.normal, 7.5)
            canv.setFillColor(colors.HexColor(_FAINT))
            canv.setStrokeColor(colors.HexColor(_HAIR))
            canv.setLineWidth(0.4)
            if doc.page > 1:
                canv.drawString(_MARGIN_X, _HEADER_Y, running)
                if self._section:
                    canv.drawRightString(right, _HEADER_Y, self._section[:60])
                canv.line(_MARGIN_X, _HEADER_Y - 5, right, _HEADER_Y - 5)
            canv.line(_MARGIN_X, _FOOTER_Y + 10, right, _FOOTER_Y + 10)
            canv.drawString(_MARGIN_X, _FOOTER_Y, footer)
            canv.drawRightString(right, _FOOTER_Y, label)
            canv.restoreState()

    return _ReportDoc()


def _local_stamp(iso: str, tz: str = "") -> str:
    """A UTC ISO timestamp rendered in ``tz``, with the zone named.

    The previous version was ``str(iso).replace("T", " ")[:16]``, which cut the
    "+00:00" off a UTC timestamp and printed it bare. A reader in Pacific time saw
    a number seven hours ahead of the truth with nothing to indicate it was not
    their own clock -- silently wrong, which is worse than obviously wrong.

    Falls back through the caller's zone, the configured quota zone (the users and
    the box are Pacific) and finally UTC, and always names whichever it used.
    """
    from datetime import datetime as _dt

    try:
        moment = _dt.fromisoformat(str(iso))
    except (TypeError, ValueError):
        return str(iso or "--")
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)

    for name in (tz, os.environ.get("AGENTS_QUOTA_TZ", "America/Los_Angeles")):
        if not name:
            continue
        try:
            from zoneinfo import ZoneInfo

            local = moment.astimezone(ZoneInfo(name))
            # %Z gives the abbreviation the reader recognises (PDT), not the id.
            return local.strftime("%Y-%m-%d %H:%M %Z")
        except Exception:  # noqa: BLE001 - unknown zone, try the next
            continue
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _job_facts(job: dict[str, Any], day: str, cjk: bool,
               tz: str = "") -> list[tuple[str, str]]:
    """Label/value pairs for the metadata strip on page one."""
    facts = [("分析日期" if cjk else "Analysis date", day or "--")]
    if job.get("finished_at"):
        facts.append(("生成时间" if cjk else "Generated",
                      _local_stamp(job["finished_at"], tz)))
    if job.get("elapsed_sec"):
        facts.append(("运行耗时" if cjk else "Runtime",
                      f"{job['elapsed_sec']} 秒" if cjk else f"{job['elapsed_sec']}s"))
    facts.append(("分析引擎" if cjk else "Engine", "TradingAgents"))
    return facts


def _job_advisories(job: dict[str, Any], cjk: bool) -> list[str]:
    """Caveats that have to appear on the document itself."""
    out = []
    if job.get("recovered"):
        out.append("本报告由实时流恢复：运行中断，最终汇总可能缺失。" if cjk else
                   "Recovered from the live stream: the run was interrupted, so "
                   "the final synthesis may be missing.")
    fell = job.get("fallback_models") or []
    if fell:
        joined = ", ".join(str(f) for f in fell)
        out.append(f"使用降级模型 {joined}（主模型当日额度已用尽）。" if cjk else
                   f"Fallback model used ({joined}): the configured model hit "
                   f"its daily quota.")
    return out


def build_report_pdf(job: dict[str, Any], tz: str = "") -> Optional[bytes]:
    """Render a finished job's report to PDF bytes, or None if unavailable."""
    try:
        from reportlab.platypus import (
            CondPageBreak, KeepTogether, PageBreak, Spacer,
        )
        from reportlab.platypus.tableofcontents import TableOfContents
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

    from ystocker.agent_roles import split_sections

    lay = _Layout(cjk, avail=_PAGE_W - 2 * _MARGIN_X - (_CJK_GUTTER if cjk else 0))
    sections = split_sections(body_md)
    story: list[Any] = []

    # -- page one: the summary ----------------------------------------------
    story.append(lay.plain(
        (f"yStocker {lay.sep} 多智能体研究" if cjk
         else f"yStocker {lay.sep} Multi-Agent Research"),
        "eyebrow"))
    story.append(lay.rule(1.2, _INK, after=0))
    title = (f"{ticker} 交易智能体分析报告" if cjk
             else f"{ticker} Trading Agents Report")
    story.append(lay.plain(title, "title"))
    story.append(lay.plain(
        (f"标的 {ticker} {lay.sep} 分析日期 {day}" if cjk
         else f"Ticker {ticker} {lay.sep} Analysis date {day}"), "subtitle"))
    story.append(Spacer(1, 10))

    tone = verdict_tone(decision or body_md[:400])
    if decision:
        story.append(lay.decision_panel(decision, tone))
    story.extend(lay.fact_strip(_job_facts(job, day, cjk, tz)))
    advisories = _job_advisories(job, cjk)
    if advisories:
        story.append(lay.advisory(advisories))

    # A contents page only earns its space once there is something to list.
    listed = [s for s in sections if s.get("role")]
    toc = None
    if len(listed) >= 3:
        toc = TableOfContents(dotsMinLevel=0)
        toc.levelStyles = [lay.styles["toc0"], lay.styles["toc1"]]
        story.append(lay.plain("目录" if cjk else "Contents", "h2"))
        story.append(lay.rule(0.5, _HAIR, after=2))
        story.append(toc)

    # -- charts, then the report itself -------------------------------------
    charts = _charts_for(job, body_md)
    # Page one is a cover only when it has a contents list or figures to hold;
    # a two-speaker report would otherwise get a page break and then two inches
    # of prose.
    if toc is not None or charts:
        story.append(PageBreak())
    if charts:
        head_text = "价格与技术背景" if cjk else "Price and technical context"
        head = lay.mark(lay.plain(head_text, "part"), 0, head_text)
        story.append(KeepTogether([lay.rule(1.1, _INK, after=0), head]))
        for spec in charts:
            try:
                caption = spec.get("caption_zh") if cjk else None
                story.append(lay.figure(spec["png"], spec["w"], spec["h"],
                                        caption or spec.get("caption", "")))
            except Exception as exc:  # noqa: BLE001 - a chart is never fatal
                log.warning("report_pdf: cannot place chart: %s", exc)
        story.append(Spacer(1, 6))

    seen_team: Optional[str] = None
    part_no = 0
    for sec in sections:
        team = sec.get("team")
        if team and team != seen_team:
            seen_team = team
            part_no += 1
            label = (sec.get("team_zh") or team) if cjk else team
            # Start a part on a fresh page only when the current one is nearly
            # spent. A hard break per team wastes a page each time a team has
            # one short speaker; no break at all strands the heading.
            story.append(CondPageBreak(2.4 * 72))
            story.extend(lay.part_header(part_no, label))

        role = sec.get("role")
        if role:
            body, chip = _lift_proposal(sec["body"])
            story.append(lay.speaker_header(role, chip))
            lay.emit(body, story)
            continue
        # The preamble is the report's own title and generation stamp, both of
        # which this document already prints in its masthead. Emitting them
        # again would put two titles two inches apart.
        body = _strip_redundant_preamble(sec["body"])
        if body.strip():
            lay.emit(body, story)

    # -- colophon -----------------------------------------------------------
    story.append(Spacer(1, 16))
    story.append(KeepTogether([
        lay.rule(0.9, _INK, after=6),
        lay.plain("重要声明" if cjk else "Disclaimer", "decision_label"),
        lay.plain(
            "本报告由 yStocker 基于 TradingAgents 多智能体分析自动生成，"
            "内容源自大语言模型对公开数据的推理，可能包含错误或过时信息，"
            "不构成投资建议。" if cjk else
            "Generated by yStocker from the TradingAgents multi-agent "
            "analysis. The content is a language model's reasoning over public "
            "data, may be wrong or out of date, and is not investment advice.",
            "colophon"),
    ]))

    # Running head on continuation pages: the document on the left, the section
    # the page belongs to on the right.
    running = lay.clean(title)
    footer = lay.clean(f"{ticker} {lay.sep} {day} {lay.sep} yStocker")
    doc_meta = {
        "title": (f"{ticker} 交易智能体分析报告 {day}" if cjk
                  else f"{ticker} Trading Agents Report {day}"),
        "author": "yStocker",
        "subject": "Multi-agent trading analysis",
        "creator": "yStocker report_pdf",
    }

    buf = io.BytesIO()
    pages: dict[str, int] = {"total": 0, "counted": 0}
    doc = _report_doc(buf, lay, running, footer, pages, **doc_meta)
    try:
        if toc is not None:
            # multiBuild lays the document out repeatedly until the contents
            # page and the pages it points at agree.
            doc.multiBuild(story[:])
        else:
            # No contents page, so nothing forces a second pass -- but the page
            # total still has to come from somewhere. The first build is the
            # count; only reports short enough not to earn a contents page take
            # this path, so the repeat is cheap.
            doc.build(story[:])
            if pages["counted"] > 1:
                buf = io.BytesIO()
                doc = _report_doc(buf, lay, running, footer, pages, **doc_meta)
                doc.build(story[:])
    except Exception as exc:
        log.error("report_pdf: build failed for %s: %s", job.get("id"), exc,
                  exc_info=True)
        # A layout problem in the contents machinery must not cost the reader
        # the report, so it is worth one retry without it.
        if toc is None:
            return None
        try:
            buf = io.BytesIO()
            plain = [f for f in story if f is not toc]
            doc = _report_doc(buf, lay, running, footer, pages, **doc_meta)
            doc.build(plain)
            log.warning("report_pdf: rebuilt %s without a contents page",
                        job.get("id"))
        except Exception as exc2:  # noqa: BLE001
            log.error("report_pdf: fallback build failed for %s: %s",
                      job.get("id"), exc2, exc_info=True)
            return None

    return buf.getvalue()


def _lift_proposal(body: str) -> tuple[str, Optional[str]]:
    """Split a leading ``FINAL TRANSACTION PROPOSAL`` off a section body.

    Returns (body without that line, the proposal) so the caller can show it as
    a chip on the speaker's header. Only the section's *opening* line is lifted;
    a proposal argued for further down stays in the prose where it belongs.
    """
    lines = (body or "").splitlines()
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = _PROPOSAL_RE.match(line)
        if not m:
            return body, None
        value = re.sub(r"[*`]", "", m.group(1)).strip()
        del lines[i]
        return "\n".join(lines).strip(), (value[:40] or None)
    return body, None


def pdf_filename(job: dict[str, Any]) -> str:
    ticker = re.sub(r"[^A-Za-z0-9.\-]", "", str(job.get("ticker", "report")))[:12] or "report"
    day = re.sub(r"[^0-9\-]", "", str(job.get("date", "")))[:10]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"ystocker-{ticker}-{day or stamp}-agents.pdf"
