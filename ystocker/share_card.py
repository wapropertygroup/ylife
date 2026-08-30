"""ystocker.share_card
~~~~~~~~~~~~~~~~~~~~~~
The 1200x630 preview image behind a shared report's ``og:image``.

Why this exists at all: before this module, ``/agents/shared/<token>`` carried no
Open Graph tags, so pasting a share link into iMessage, WhatsApp, Slack or any
other link-unfurling client produced a bare blue link and nothing else — no
title, no image, nothing to tell the recipient what they are about to open.
That is orthogonal to *how* the link reaches them (email, a paste into Messages,
a paste anywhere else); the same image and the same meta tags improve every
channel a link can travel through, which is why this lives beside ``share.py``
rather than inside the SMS-specific code in ``routes.py``.

Draws with matplotlib's Agg backend, the same one ``charts.py`` already uses for
every other server-rendered PNG in this app — no new plotting dependency, and
the backend/import shape is proven safe under ``tests/test_import_graph.py``'s
matplotlib stub because ``charts.py`` already exercises it there. Unlike
``charts.py``, the result is *not* base64-embedded in HTML this process also
renders: it is served as its own ``image/png`` response
(``routes.api_agents_shared_card``) so an unauthenticated link-preview fetcher —
Apple's ``LPMetadataProvider``, WhatsApp's or Slack's unfurler — can retrieve it
with a plain GET and no knowledge of this app at all.

Shows only what is already public through ``share.public_payload``: the ticker
and the first line of the decision, the same two fields
``/api/agents/shared/<token>`` already returns to anyone holding the token.
Drawing them into an image discloses nothing new — it just means a reader sees
"NVDA — BUY" before they tap the link, which is the entire point of a preview
card.

This checkout's own venv cannot import ``matplotlib.pyplot`` at all — this dev
machine's Homebrew Python hits the same broken ``pyexpat`` that forces
``tests/test_import_graph.py`` to stub matplotlib out entirely (see that file's
module docstring) — so the layout was proof-read against a *second*, throwaway
venv built on Xcode's system Python 3.9, which has a working ``pyexpat``. That
render caught a real bug a plausible-looking constant would not have: a fixed
96pt ticker size ran a 16-character ticker off the right edge of the canvas, and
the original chip-width estimate (characters times a guessed per-glyph width)
undersized the pill for a long decision line badly enough that the text
overflowed both the pill and the canvas. ``_fit_size``/``_text_width_px`` below
replace both guesses with an actual glyph-metric measurement. Still worth a
glance against a real render after any further layout change — this module has
no way to run that check itself, only to be caught being wrong once someone
does.
"""

from __future__ import annotations

import io
import logging
import os
import re
from typing import Any, Optional

import matplotlib

matplotlib.use("Agg")  # non-interactive backend — required for server use
import matplotlib.font_manager as font_manager
import matplotlib.patches as mpatches
import matplotlib.pyplot as plt

log = logging.getLogger(__name__)

#: 1200x630 is the size every major unfurler (iMessage, Facebook, Slack,
#: WhatsApp) expects for a "large image" card — anything smaller gets upscaled
#: or, on some clients, ignored in favour of no image at all.
CARD_W = 1200
CARD_H = 630


# ---------------------------------------------------------------------------
# Text — pulled out from rendering so routes.py can put the identical words in
# og:title/og:description that the picture itself shows. Two renderers (an
# HTML <head> and a PNG canvas) describing the same report differently would be
# a worse experience than either alone.
# ---------------------------------------------------------------------------

def summary(job: dict[str, Any]) -> tuple[str, str]:
    """(ticker, first-line decision) — the two facts a preview card needs.

    Mirrors ``report_email._decision_chip``'s extraction (first non-empty line,
    capped at 120 chars) but returns plain text rather than a coloured
    ``<span>``. Duplicated rather than imported: that function builds HTML for a
    mail, this returns text for a PNG and a ``<meta>`` attribute, and the two
    must not become coupled just to share six lines of string slicing.
    """
    ticker = str(job.get("ticker") or "").strip()[:16]
    raw = str(job.get("decision") or "").strip()
    first = raw.splitlines()[0].strip()[:120] if raw else ""
    return ticker, first


def card_text(job: dict[str, Any], lang: str = "en") -> dict[str, str]:
    """The title/description pair for ``og:title``/``og:description``.

    Kept to two short, generic sentences rather than including the sharer's
    note: the note is a private-ish aside meant to be read on the page itself
    (``shared.html`` already shows it, and it can run to 500 characters), not
    something worth truncating into a ``<meta>`` tag that a platform may log or
    display as-is.
    """
    ticker, decision = summary(job)
    lang = "zh" if str(lang or "").lower() == "zh" else "en"
    label = ticker or ("股票" if lang == "zh" else "a stock")
    if lang == "zh":
        caption = "AI 多智能体研究报告"
        title = f"{label} — {decision}" if decision else label
        description = (f"{label}：{decision}。{caption}，与你分享。" if decision
                        else f"{label}的{caption}，与你分享。")
    else:
        caption = "AI multi-agent research report"
        title = f"{label} — {decision}" if decision else label
        description = (f"{label}: {decision}. {caption}, shared with you."
                        if decision else f"An {caption} for {label}, shared with you.")
    return {"title": title, "description": description, "caption": caption}


def _chip_colors(decision: str) -> tuple[str, str]:
    """(foreground, background) hex for the verdict chip.

    Same three buckets and the same hex pairs as
    ``report_email._decision_chip``, so a reader who saw the completion mail's
    chip sees the identical colour in the link preview. Duplicated for the same
    reason as ``summary()`` above.
    """
    low = decision.casefold()
    if "buy" in low or "买入" in decision or "增持" in decision:
        return "#4ade80", "#052e16"
    if "sell" in low or "卖出" in decision or "减持" in decision:
        return "#f87171", "#450a0a"
    return "#fbbf24", "#451a03"


# ---------------------------------------------------------------------------
# CJK font resolution — same question report_pdf.py asks, asked again here
# rather than imported from there, because report_pdf._EMBEDDABLE_CJK feeds
# reportlab's font *registration*, a different font stack than matplotlib's.
# Importing a private name across modules to save four file paths is not worth
# coupling a PDF renderer to a PNG one.
# ---------------------------------------------------------------------------

#: Han, kana, Hangul, and the full-width/CJK punctuation blocks — the same
#: ranges as report_pdf._CJK_RE, expressed as explicit code points rather than
#: literal or ``\u``-escaped characters embedded in a string: this file can be
#: edited by an agent as readily as by hand, and a stray character substituted
#: for a visually-identical one inside a hand-typed Unicode range would
#: silently change which glyphs count as CJK, in a way a diff would not make
#: obvious. Plain hex integers cannot suffer that.
_CJK_RANGES: tuple[tuple[int, int], ...] = (
    (0x1100, 0x11FF),   # Hangul Jamo
    (0x2E80, 0x9FFF),   # CJK radicals through the main CJK Unified Ideographs block
    (0xA960, 0xA97F),   # Hangul Jamo Extended-A
    (0xAC00, 0xD7FF),   # Hangul Syllables
    (0xF900, 0xFAFF),   # CJK Compatibility Ideographs
    (0xFE30, 0xFE4F),   # CJK Compatibility Forms
    (0xFF00, 0xFFEF),   # Halfwidth and Fullwidth Forms
)
_CJK_RE = re.compile("[" + "".join(f"{chr(lo)}-{chr(hi)}" for lo, hi in _CJK_RANGES) + "]")


def _is_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


#: Same candidate list and the same env override as report_pdf._EMBEDDABLE_CJK,
#: so a box that was set up for the PDF path is already set up for this one.
_CJK_FONT_CANDIDATES: tuple[str, ...] = (
    "/usr/share/fonts/cjkuni-uming/uming.ttc",          # Amazon Linux
    "/System/Library/Fonts/Supplemental/Songti.ttc",    # macOS
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/usr/share/fonts/truetype/arphic/uming.ttc",       # Debian/Ubuntu
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)

# Resolved lazily and cached: "" means "not resolved yet", None means "resolved,
# and nothing was found" — the same three-state shape as report_pdf's
# _cjk_ready/_cjk_font pair, collapsed into one variable since this module only
# ever needs a path, never a registration handle.
_cjk_font_cache: Optional[str] = ""


def _cjk_font_path() -> Optional[str]:
    """First existing CJK font file on this box, or None.

    Unlike ``report_pdf._register_cjk_font``, this never embeds anything or
    picks a face out of a ``.ttc`` by weight — ``FontProperties(fname=...)``
    takes a bare path and matplotlib reads face 0. A big bold headline is not
    where a Regular-vs-Black mismatch is worth the extra bookkeeping
    ``report_pdf._pick_faces`` does for a multi-page report; face 0 of a CJK
    ``.ttc`` skewing heavy reads as an intentionally bold headline here, not as
    a mistake.
    """
    global _cjk_font_cache
    if _cjk_font_cache != "":
        return _cjk_font_cache
    env = os.environ.get("YSTOCKER_CJK_FONT", "").strip()
    for path in (env, *_CJK_FONT_CANDIDATES):
        if path and os.path.exists(path):
            _cjk_font_cache = path
            return path
    _cjk_font_cache = None
    log.warning("share_card: no CJK font file found on this box; Chinese text "
                "in the shared-link preview image will render as tofu boxes")
    return None


def _font_for(text: str, lang: str) -> Optional["font_manager.FontProperties"]:
    """A CJK ``FontProperties``, or None to fall back to matplotlib's default.

    Checked against the actual text rather than only ``lang``: a "zh" job can
    still have an ASCII ticker, and an "en" job's decision line can quote a
    Chinese phrase back (``report_email._decision_chip`` matches "买入"/"卖出"
    regardless of ``lang``) — so this asks the string in front of it, not the
    job's declared language.
    """
    if not (lang == "zh" or _is_cjk(text)):
        return None
    path = _cjk_font_path()
    if not path:
        return None
    try:
        return font_manager.FontProperties(fname=path)
    except Exception as exc:  # noqa: BLE001 - draw with the default face instead
        log.warning("share_card: could not load CJK font %s (%s)", path, exc)
        return None


# ---------------------------------------------------------------------------
# Text sizing — a ticker can be one character (``F``) or sixteen
# (``summary()``'s own cap), and a decision line is free text up to 120
# characters, so a fixed font size either wastes the card on short tickers or
# runs long ones off the canvas. ``ABCDEFGHIJKLMNOP`` at a fixed 96pt measured
# wider than the card itself in the first real render this module produced —
# caught by actually looking at the output, not by reasoning about it, which is
# the reason this exists rather than a plausible-looking constant.
# ---------------------------------------------------------------------------

def _text_width_px(text: str, size: float, dpi: int,
                    prop: Optional["font_manager.FontProperties"] = None,
                    weight: str = "bold") -> float:
    """Rendered width of ``text`` at ``size`` points, in pixels at ``dpi``.

    Measured with ``matplotlib.textpath.TextPath`` rather than a live renderer:
    ``TextPath`` lays glyphs out from the font's own metrics with no canvas or
    draw pass required, which matters because this is called before the figure
    has been drawn even once. Imported lazily (matching
    ``report_pdf._register_cjk_font``'s own ``from reportlab... import TTFont``
    inside the function that needs it) so a card with a short ticker and no
    decision — the common case — never pays for it.
    """
    from matplotlib.textpath import TextPath

    fp = prop or font_manager.FontProperties(weight=weight)
    path = TextPath((0, 0), text or " ", size=size, prop=fp)
    return path.get_extents().width * (dpi / 72.0)


def _fit_size(text: str, target_size: float, max_width: float, dpi: int,
              prop: Optional["font_manager.FontProperties"] = None,
              weight: str = "bold", min_size: float = 1.0) -> float:
    """``target_size``, or smaller if ``text`` would not fit in ``max_width`` px.

    Glyph width scales linearly with point size for a fixed string, so shrinking
    to fit is one division against a single measurement rather than a
    measure-shrink-remeasure search.
    """
    if not text:
        return target_size
    width = _text_width_px(text, target_size, dpi, prop=prop, weight=weight)
    if width <= max_width or width <= 0:
        return target_size
    return max(target_size * (max_width / width), min_size)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

#: The favicon's mark (static/favicon.svg / report_email._LOGO_BARS): four
#: rounded bars rising left to right. Redrawn as rectangles rather than shared
#: with either of those, because neither exposes anything to draw into a
#: matplotlib axes — one is inline SVG markup, the other an HTML table.
_LOGO_BARS: tuple[tuple[int, int, str], ...] = (
    (0, 24, "#6366f1"), (1, 40, "#38bdf8"), (2, 52, "#34d399"), (3, 34, "#4338ca"),
)


def render(job: dict[str, Any], lang: str = "en", brand: str = "yStocker") -> bytes:
    """A 1200x630 PNG preview card for ``og:image``, as raw bytes.

    Never raises over a rendering nicety: a missing CJK font degrades to tofu
    boxes (``_font_for`` already handles that), and this function additionally
    never lets the *chip* — present only when the run produced a decision — take
    the whole card down, since a card with no chip is a strictly better outcome
    than a 500 on an unauthenticated image route that a crawler will retry.
    """
    ticker, decision = summary(job)
    lang = "zh" if str(lang or "").lower() == "zh" else "en"
    caption = ("AI 多智能体研究报告 · 分享给你" if lang == "zh"
               else "AI multi-agent research report · shared with you")

    ticker_font = _font_for(ticker, lang)
    body_font = _font_for(decision + caption, lang)

    dpi = 100
    fig = plt.figure(figsize=(CARD_W / dpi, CARD_H / dpi), dpi=dpi)
    fig.patch.set_facecolor("#0f172a")     # same navy as the site's dark theme
    ax = fig.add_axes((0, 0, 1, 1))
    ax.set_facecolor("#0f172a")
    ax.set_xlim(0, CARD_W)
    ax.set_ylim(CARD_H, 0)                 # inverted: data-y grows downward
    ax.axis("off")

    left = 64
    avail_w = CARD_W - 2 * left     # symmetric margins for anything full-width
    for i, h, color in _LOGO_BARS:
        ax.add_patch(mpatches.Rectangle((left + i * 14, 64 - h), 8, h, color=color))
    ax.text(left + 68, 40, brand, fontsize=20, fontweight="bold", color="#e2e8f0",
            va="center", ha="left")

    ticker_label = ticker or "—"
    ticker_size = _fit_size(ticker_label, 96, avail_w, dpi, prop=ticker_font,
                            min_size=40)
    ax.text(left, 220, ticker_label, fontsize=ticker_size, fontweight="bold",
            color="#f8fafc", va="center", ha="left", fontproperties=ticker_font)

    if decision:
        try:
            fg, bg = _chip_colors(decision)
            chip_text = decision[:60]
            pad = 28
            chip_size = _fit_size(chip_text, 30, avail_w - 2 * pad, dpi,
                                  prop=body_font, min_size=14)
            chip_w = _text_width_px(chip_text, chip_size, dpi,
                                    prop=body_font) + 2 * pad
            ax.add_patch(mpatches.FancyBboxPatch(
                (left, 300), chip_w, 64,
                boxstyle="round,pad=0,rounding_size=32",
                linewidth=0, facecolor=bg))
            ax.text(left + chip_w / 2, 332, chip_text, fontsize=chip_size,
                    fontweight="bold", color=fg, va="center", ha="center",
                    fontproperties=body_font)
        except Exception as exc:  # noqa: BLE001 - the card still works without a chip
            log.warning("share_card: could not draw the decision chip (%s)", exc)

    ax.text(left, 470, caption, fontsize=22, color="#94a3b8", va="center",
            ha="left", fontproperties=body_font)

    date = str(job.get("date") or "").strip()
    if date:
        ax.text(CARD_W - left, CARD_H - 40, date, fontsize=18, color="#64748b",
                va="center", ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
