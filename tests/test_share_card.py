"""Tests for ystocker/share_card.py — the 1200x630 og:image behind a shared
report link, plus the title/description text ``routes.py`` puts beside it.

Two tiers, because the module has two tiers of risk:

1. **Text and colour choices** (``summary``, ``card_text``, ``_chip_colors``,
   ``_is_cjk``) are plain string/dict logic with no canvas involved, so they run
   everywhere this suite runs and are exercised the same way the rest of the
   repo's pure-logic modules are.
2. **Actual pixel output** (``render``) needs a working Agg canvas, and this
   dev checkout does not have one -- this machine's Homebrew Python hits the
   same broken ``pyexpat`` that forces tests/test_import_graph.py to stub
   matplotlib out entirely for *every* module that touches it (see that file's
   module docstring). ``RenderTests`` below is skipped rather than failed when
   that is the case, and stubs matplotlib just enough to let the module *load*
   either way, so tier 1 is never held hostage by tier 2's environment problem.

Loaded by path (``importlib.util``), matching tests/test_share.py's own
technique and for the same reason: this file must control exactly what
``sys.modules['matplotlib...']`` holds before ``share_card.py``'s module-level
``import matplotlib.pyplot`` line runs, and a regular ``import
ystocker.share_card`` would not give it that chance.

That control has to survive running *alongside* tests/test_import_graph.py in
one ``unittest discover`` process, not just alone -- and the two files' module
scopes race for the same ``sys.modules`` entries. That file unconditionally
installs its own fake ``matplotlib.pyplot`` (``figure=noop``, etc.), on every
machine, specifically so ``ystocker.routes`` can be smoke-tested without a
working rendering stack; its own docstring already leans on alphabetical
discovery order ('i' sorts before 's') to guarantee it runs first. A detector
here that only asked "did the import statement raise" would find that stub,
conclude rendering works, and then crash inside ``share_card.render()`` on the
no-op's ``None`` standing in for a figure -- which is exactly what the first
version of this file did when run together with test_import_graph.py, though
it passed fine in isolation. ``_matplotlib_really_works()`` below asks a
question a no-op stub cannot pass by accident (draw one pixel, read its bytes
back), and the fallback -- when the answer is no -- reuses that same file's
``_install_plot_stubs()`` rather than a second, differently-shaped stub of its
own: both files' module scopes can now run in either order, ``setdefault``
makes installing the same stub twice harmless, and there is only ever one
definition in the whole suite of what the fake matplotlib looks like.
"""

from __future__ import annotations

import importlib.util
import io
import pathlib
import struct
import sys
import unittest

ROOT = pathlib.Path(__file__).parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


def _matplotlib_really_works() -> bool:
    """True only if matplotlib can actually render, not merely be imported.

    See the module docstring for why import success alone is not enough: a
    no-op stub already sitting in ``sys.modules`` "imports" fine by design.
    Actually drawing a pixel and reading the bytes back is the one thing such a
    stub cannot pass without deliberately faking a real PNG.
    """
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig = plt.figure(figsize=(1, 1))
        buf = io.BytesIO()
        fig.savefig(buf, format="png")
        plt.close(fig)
        return buf.getvalue()[:8] == b"\x89PNG\r\n\x1a\n"
    except Exception:  # noqa: BLE001 - any failure means "cannot render here"
        return False


HAVE_MPL = _matplotlib_really_works()

if not HAVE_MPL:
    # tests/test_import_graph.py's own stub, not a second one: see the module
    # docstring for why sharing one definition (rather than each file bringing
    # a partial, differently-shaped fake) is what makes this safe regardless of
    # which of the two files' module scope runs first.
    from tests.test_import_graph import _install_plot_stubs

    _install_plot_stubs()

share_card = _load("ystocker.share_card", "ystocker/share_card.py")


def make_job(**over) -> dict:
    job = {"ticker": "NVDA", "date": "2026-08-29",
           "decision": "BUY\n\nHigh conviction."}
    job.update(over)
    return job


# ── Text extraction ──────────────────────────────────────────────────────────

class SummaryTests(unittest.TestCase):
    def test_ticker_and_first_decision_line(self):
        self.assertEqual(share_card.summary(make_job()), ("NVDA", "BUY"))

    def test_missing_decision_is_empty_not_none(self):
        for bad in ("", None, "   "):
            _, decision = share_card.summary(make_job(decision=bad))
            self.assertEqual(decision, "", repr(bad))

    def test_missing_ticker_is_empty_not_a_placeholder(self):
        ticker, _ = share_card.summary({"decision": "BUY"})
        self.assertEqual(ticker, "")

    def test_ticker_is_bounded_at_sixteen(self):
        ticker, _ = share_card.summary(make_job(ticker="A" * 40))
        self.assertEqual(len(ticker), 16)

    def test_decision_is_first_line_only_and_bounded(self):
        long_line = "BUY " + "x" * 300
        _, decision = share_card.summary(make_job(decision=long_line + "\nignored"))
        self.assertEqual(len(decision), 120)
        self.assertNotIn("ignored", decision)

    def test_whitespace_around_the_decision_is_stripped(self):
        _, decision = share_card.summary(make_job(decision="  BUY  \nmore"))
        self.assertEqual(decision, "BUY")


# ── Chip colour ───────────────────────────────────────────────────────────────

class ChipColorTests(unittest.TestCase):
    def test_buy_is_green(self):
        for word in ("BUY", "Strong Buy", "增持", "买入 with high conviction"):
            self.assertEqual(share_card._chip_colors(word), ("#4ade80", "#052e16"), word)

    def test_sell_is_red(self):
        for word in ("SELL", "sell now", "卖出", "减持"):
            self.assertEqual(share_card._chip_colors(word), ("#f87171", "#450a0a"), word)

    def test_everything_else_is_amber(self):
        for word in ("HOLD", "持有", "观望", "", "Neutral"):
            self.assertEqual(share_card._chip_colors(word), ("#fbbf24", "#451a03"), word)

    def test_matches_report_emails_chip_palette(self):
        # The two palettes are deliberately duplicated (see the module
        # docstring) rather than shared code, so this pins them to the same
        # literal hex pairs report_email._decision_chip uses -- if either one's
        # colours are ever tweaked without the other, this fails instead of the
        # two silently drifting apart.
        mail = _load("ystocker.report_email", "ystocker/report_email.py")
        for word in ("BUY", "SELL", "HOLD"):
            html = mail._decision_chip({"decision": word}, "en")
            fg, bg = share_card._chip_colors(word)
            self.assertIn(fg, html, word)
            self.assertIn(bg, html, word)


# ── CJK detection ─────────────────────────────────────────────────────────────

class IsCjkTests(unittest.TestCase):
    def test_chinese_text_is_detected(self):
        self.assertTrue(share_card._is_cjk("买入"))
        self.assertTrue(share_card._is_cjk("NVDA 买入"))

    def test_plain_ascii_is_not(self):
        for text in ("BUY", "", "NVDA — $178.32", "Hold, for now."):
            self.assertFalse(share_card._is_cjk(text), text)

    def test_japanese_and_korean_also_count(self):
        # The range table is Han/Hangul/Kana, matching report_pdf._CJK_RE's own
        # scope -- not narrowed to Simplified Chinese.
        self.assertTrue(share_card._is_cjk("こんにちは"))
        self.assertTrue(share_card._is_cjk("안녕하세요"))

    def test_none_and_non_strings_do_not_raise(self):
        for bad in (None, 0, False):
            self.assertFalse(share_card._is_cjk(bad), repr(bad))


# ── og:title / og:description ────────────────────────────────────────────────

class CardTextTests(unittest.TestCase):
    def test_title_includes_ticker_and_decision(self):
        text = share_card.card_text(make_job())
        self.assertIn("NVDA", text["title"])
        self.assertIn("BUY", text["title"])

    def test_no_decision_falls_back_to_the_ticker_alone(self):
        text = share_card.card_text(make_job(decision=""))
        self.assertEqual(text["title"], "NVDA")

    def test_chinese_is_actually_chinese(self):
        en = share_card.card_text(make_job())
        zh = share_card.card_text(make_job(), lang="zh")
        self.assertNotEqual(en["description"], zh["description"])
        self.assertNotEqual(en["caption"], zh["caption"])
        self.assertTrue(share_card._is_cjk(zh["caption"]))
        self.assertFalse(share_card._is_cjk(en["caption"]))

    def test_an_unrecognised_language_is_english(self):
        for code in ("fr", "", None, "EN", "zh-TW"):
            text = share_card.card_text(make_job(), lang=code)
            self.assertEqual(text["caption"], "AI multi-agent research report",
                             repr(code))

    def test_no_ticker_still_produces_a_full_sentence(self):
        text = share_card.card_text({"decision": "BUY"})
        self.assertTrue(text["title"])
        self.assertTrue(text["description"])
        self.assertIn("BUY", text["description"])

    def test_no_ticker_and_no_decision_is_still_not_empty(self):
        for lang in ("en", "zh"):
            text = share_card.card_text({}, lang=lang)
            self.assertTrue(text["title"], lang)
            self.assertTrue(text["description"], lang)


# ── Text fitting ──────────────────────────────────────────────────────────────
# Needs matplotlib.textpath for real measurement, same as RenderTests below --
# unlike summary()/card_text()/_chip_colors()/_is_cjk(), this is not pure-Python
# logic, so it is skipped under the same HAVE_MPL guard rather than left to fail
# on a machine with no working matplotlib.

@unittest.skipUnless(HAVE_MPL, "a working matplotlib.pyplot is not importable "
                     "in this environment")
class FitSizeTests(unittest.TestCase):
    """Pins the bug an actual rendered image caught: a fixed 96pt ticker ran a
    16-character symbol off the right edge of the card, and the old
    characters-times-a-guessed-width chip estimate undersized the pill badly
    enough that a long decision line overflowed both the pill and the canvas.
    These assert the fix (measure, then shrink proportionally) rather than the
    rendered pixels, which RenderTests already covers end to end.
    """

    def test_short_text_is_not_shrunk(self):
        self.assertEqual(share_card._fit_size("F", 96, 1000, dpi=100), 96)

    def test_empty_text_is_not_shrunk(self):
        self.assertEqual(share_card._fit_size("", 96, 10, dpi=100), 96)

    def test_long_text_is_shrunk_to_fit(self):
        size = share_card._fit_size("ABCDEFGHIJKLMNOP", 96, 1072, dpi=100)
        self.assertLess(size, 96)
        width = share_card._text_width_px("ABCDEFGHIJKLMNOP", size, dpi=100)
        self.assertLessEqual(width, 1072 + 1)   # +1: floating-point slack

    def test_never_shrinks_below_min_size(self):
        # An absurd budget still returns at least min_size rather than a font
        # size of zero, or negative.
        size = share_card._fit_size("X" * 200, 96, 1, dpi=100, min_size=10)
        self.assertEqual(size, 10)

    def test_width_scales_linearly_with_point_size(self):
        w1 = share_card._text_width_px("NVDA", 48, dpi=100)
        w2 = share_card._text_width_px("NVDA", 96, dpi=100)
        self.assertAlmostEqual(w2 / w1, 2.0, places=2)

    def test_a_longer_ticker_is_never_drawn_larger_than_a_shorter_one(self):
        avail = share_card.CARD_W - 2 * 64
        short = share_card._fit_size("F", 96, avail, dpi=100, min_size=40)
        long_ = share_card._fit_size("ABCDEFGHIJKLMNOP", 96, avail, dpi=100,
                                     min_size=40)
        self.assertLessEqual(long_, short)


# ── Actual rendering ──────────────────────────────────────────────────────────

@unittest.skipUnless(HAVE_MPL, "a working matplotlib.pyplot is not importable "
                     "in this environment")
class RenderTests(unittest.TestCase):
    """Needs a real Agg canvas — see HAVE_MPL above for when this is skipped."""

    @staticmethod
    def _png_size(data: bytes) -> tuple[int, int]:
        # A PNG always opens with an 8-byte signature followed by a 25-byte
        # IHDR chunk: 4-byte length, 4-byte "IHDR", then big-endian width and
        # height as two uint32s. Parsed by hand rather than pulling in Pillow
        # for a dimension check inside a test that is exercising a
        # Pillow-free renderer.
        assert data[:8] == b"\x89PNG\r\n\x1a\n", "output is not a PNG"
        width, height = struct.unpack(">II", data[16:24])
        return width, height

    def test_it_returns_a_png_of_the_documented_size(self):
        png = share_card.render(make_job())
        self.assertEqual(self._png_size(png),
                         (share_card.CARD_W, share_card.CARD_H))

    def test_it_does_not_raise_without_a_decision(self):
        png = share_card.render(make_job(decision=""))
        self.assertEqual(self._png_size(png), (1200, 630))

    def test_it_does_not_raise_without_a_ticker(self):
        png = share_card.render({"decision": "BUY"})
        self.assertEqual(self._png_size(png), (1200, 630))

    def test_it_does_not_raise_for_a_chinese_report(self):
        png = share_card.render(make_job(decision="买入\n\n高确定性。"), lang="zh")
        self.assertEqual(self._png_size(png), (1200, 630))

    def test_an_entirely_empty_job_still_renders(self):
        png = share_card.render({})
        self.assertEqual(self._png_size(png), (1200, 630))

    def test_an_overlong_decision_does_not_crash_the_chip(self):
        png = share_card.render(make_job(decision="B" * 500))
        self.assertEqual(self._png_size(png), (1200, 630))

    def test_a_maximally_long_ticker_and_decision_still_render(self):
        # The exact shape of the bug an actual rendered image caught: a
        # 16-character ticker (summary()'s own cap) alongside a long decision
        # line. FitSizeTests proves the maths stays within budget; this proves
        # the combination reaches render() without raising.
        png = share_card.render(make_job(
            ticker="ABCDEFGHIJKLMNOP",
            decision="STRONG BUY WITH HIGH CONVICTION AND A LONG THESIS"))
        self.assertEqual(self._png_size(png), (1200, 630))

    def test_different_verdicts_render_different_bytes(self):
        # Not a pixel comparison, just proof the chip colour actually reaches
        # the canvas rather than every render producing an identical image.
        buy = share_card.render(make_job(decision="BUY"))
        sell = share_card.render(make_job(decision="SELL"))
        self.assertNotEqual(buy, sell)


if __name__ == "__main__":
    unittest.main(verbosity=2)
