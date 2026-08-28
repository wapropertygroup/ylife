"""Static check on every ``DeferLoad.when()`` anchor in the templates.

``tests/check_deferload.mjs`` covers the *module*: given a boxless target it
loads eagerly and warns, given a visible one it defers. What nothing covered is
the *call sites*, and that is where the mistake actually gets made — the anchor
is a selector in a 5000-line template, and getting it wrong fails in the two
quietest ways this codebase has:

* **A typo'd or renamed id.** ``observeNow`` returns without running the loader
  when the selector matches nothing, so the panel stays on its spinner forever.
  No console error, no failed request, nothing in the log.
* **An anchor that is hidden until its data arrives.** It has no box, so
  ``IntersectionObserver`` could never fire for it; deferload loads it eagerly
  instead. The page still works, which is the problem — it looks like it defers
  while fetching everything on load. ``#yieldSpreadChartWrap`` shipped like this
  and was only caught by reading the code (52e09ae).

Neither shows up in a smoke test of the page, so this asserts it from the text:
every anchor exists in the template that references it, and is in flow at parse
time. It needs no browser, no app and no network, so it runs in the default
suite.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "ystocker" / "templates"

# DeferLoad.when('#foo', ...) / DeferLoad.when("#foo", ...). Anchors passed as a
# variable rather than a literal are skipped: there is no id to look for.
_CALL = re.compile(r"""DeferLoad\.when\(\s*(['"])(?P<sel>[^'"]+)\1""")

# Tailwind's `hidden`, and the two ways of spelling it by hand. `overflow-hidden`
# and friends must not match, hence the class list is tokenised rather than
# substring-searched.
_DISPLAY_NONE = re.compile(r"display\s*:\s*none", re.I)
# A bare `hidden` boolean attribute: preceded by whitespace, followed by
# whitespace, `>` or `/`, so `data-hidden="1"` and `hidden-thing` do not match.
_BARE_HIDDEN = re.compile(r"\shidden(?=[\s/>])")


def _iter_templates() -> list[Path]:
    return sorted(TEMPLATES.glob("*.html"))


def _find_open_tag(html: str, ident: str) -> str | None:
    """Return the opening tag carrying ``id="ident"``, or None if absent.

    Walks back to the nearest ``<`` and forward to the matching ``>`` so a tag
    whose attributes span several lines is still read whole.
    """
    for m in re.finditer(r"""id\s*=\s*(['"])%s\1""" % re.escape(ident), html):
        start = html.rfind("<", 0, m.start())
        end = html.find(">", m.end())
        if start == -1 or end == -1:
            continue
        return html[start:end + 1]
    return None


def _classes(tag: str) -> list[str]:
    m = re.search(r"""class\s*=\s*(['"])(.*?)\1""", tag, re.S)
    return m.group(2).split() if m else []


def _style(tag: str) -> str:
    m = re.search(r"""style\s*=\s*(['"])(.*?)\1""", tag, re.S)
    return m.group(2) if m else ""


def _is_hidden(tag: str) -> bool:
    return ("hidden" in _classes(tag)
            or bool(_BARE_HIDDEN.search(tag))
            or bool(_DISPLAY_NONE.search(_style(tag))))


_CONTAINER = re.compile(r"<(/?)(div|section|main|form|details)\b([^>]*)>", re.S)


def _hidden_ancestors(html: str, ident: str) -> list[str]:
    """Open container ancestors of ``id=ident`` that have no box and never gain one.

    A visible anchor inside a `display:none` card is just as dead as a hidden
    anchor — the box it needs belongs to the ancestor.

    The limit of a static check: an ancestor hidden in the markup is often
    revealed at runtime *before* the registration runs, which is fine and very
    common here (`#cmdMain` on /commodities is `hidden` until `/api/commodities`
    lands, and every `DeferLoad.when` on that page is registered after that).
    Whether the reveal happens before the observer is created is a question about
    execution order that the text cannot answer. So this only reports an ancestor
    that is hidden and is *never* revealed anywhere in the template — an anchor
    that provably never gets a box, rather than one that might get it late.
    """
    m = re.search(r"""id\s*=\s*(['"])%s\1""" % re.escape(ident), html)
    if not m:
        return []
    stack: list[tuple[str, int, str]] = []
    for t in _CONTAINER.finditer(html[:m.start()]):
        closing, attrs = t.group(1), t.group(3)
        if closing:
            if stack:
                stack.pop()
        elif not attrs.rstrip().endswith("/"):
            line = html.count("\n", 0, t.start()) + 1
            aid = re.search(r"""id\s*=\s*(['"])(.*?)\1""", attrs)
            stack.append((t.group(0), line, aid.group(2) if aid else ""))
    return [f"#{aid or '?'} at line {line}"
            for tag, line, aid in stack
            if _is_hidden(tag) and not _is_revealed(html, aid)]


def _is_revealed(html: str, ident: str) -> bool:
    """True when the template contains code that gives ``ident`` a box."""
    if not ident:
        return False
    q = re.escape(ident)
    return bool(
        re.search(r"""['"]%s['"]\s*\)\s*\.classList\.remove\(\s*['"]hidden""" % q, html)
        or re.search(r"""['"]%s['"]\s*\)\s*\.style\.display\s*=""" % q, html)
        or re.search(r"""%s['"]?\s*\)?\.classList\.remove\(\s*['"]hidden""" % q, html)
    )


class DeferLoadAnchors(unittest.TestCase):
    def test_every_anchor_exists(self) -> None:
        """A selector that matches nothing leaves the panel on its spinner."""
        missing = []
        for path in _iter_templates():
            html = path.read_text(encoding="utf-8")
            for m in _CALL.finditer(html):
                sel = m.group("sel")
                if not sel.startswith("#") or " " in sel:
                    continue  # not a plain id anchor; nothing to resolve
                if _find_open_tag(html, sel[1:]) is None:
                    line = html.count("\n", 0, m.start()) + 1
                    missing.append(f"{path.name}:{line} {sel}")
        self.assertFalse(
            missing,
            "DeferLoad anchor not found in its own template — the loader will "
            "never run and the panel stays empty:\n  " + "\n  ".join(missing),
        )

    def test_no_anchor_is_hidden_on_load(self) -> None:
        """A boxless anchor cannot intersect, so its panel loads eagerly."""
        bad = []
        for path in _iter_templates():
            html = path.read_text(encoding="utf-8")
            for m in _CALL.finditer(html):
                sel = m.group("sel")
                if not sel.startswith("#") or " " in sel:
                    continue
                tag = _find_open_tag(html, sel[1:])
                if tag is None:
                    continue  # reported by the test above
                line = html.count("\n", 0, m.start()) + 1
                where = f"{path.name}:{line} {sel}"
                if "hidden" in _classes(tag):
                    bad.append(f"{where} — class=\"hidden\"")
                elif _BARE_HIDDEN.search(tag):
                    bad.append(f"{where} — bare hidden attribute")
                elif _DISPLAY_NONE.search(_style(tag)):
                    bad.append(f"{where} — style=\"display:none\"")
        self.assertFalse(
            bad,
            "DeferLoad anchor has no box at parse time, so it defers nothing "
            "and quietly loads on page load. Anchor on the panel's visible "
            "loading placeholder instead:\n  " + "\n  ".join(bad),
        )

    def test_no_anchor_is_inside_a_hidden_ancestor(self) -> None:
        """A visible anchor in a display:none card has no box either."""
        bad = []
        for path in _iter_templates():
            html = path.read_text(encoding="utf-8")
            for m in _CALL.finditer(html):
                sel = m.group("sel")
                if not sel.startswith("#") or " " in sel:
                    continue
                buried = _hidden_ancestors(html, sel[1:])
                if buried:
                    line = html.count("\n", 0, m.start()) + 1
                    bad.append(f"{path.name}:{line} {sel} — hidden ancestor at "
                               + ", ".join(buried))
        self.assertFalse(
            bad,
            "DeferLoad anchor sits inside a container that has no box, so it "
            "cannot intersect and its panel loads eagerly:\n  "
            + "\n  ".join(bad),
        )

    def test_finds_the_markets_anchors(self) -> None:
        """Guard against the regexes silently matching nothing at all.

        Both tests above pass trivially if ``_CALL`` stops matching — a rename of
        the helper, or a move to a wrapper function, would make this file a
        no-op that still reports success.
        """
        total = sum(len(_CALL.findall(p.read_text(encoding="utf-8")))
                    for p in _iter_templates())
        self.assertGreaterEqual(
            total, 10,
            "found only %d DeferLoad.when() call sites across the templates; "
            "the matcher is probably stale" % total,
        )


if __name__ == "__main__":
    unittest.main()
