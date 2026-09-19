"""No template may render the same element id twice.

A duplicate id is invalid HTML that does not fail: `getElementById` returns the
first match, so two renderers silently fight over one element and whichever runs
last wins. The symptom is a correct-looking value that is not the one the code
beside it computed.

Caught for real on 2026-09-19: a new balance-sheet card reused `statCurrentRatio`,
which the Profitability block already owned. Both renderers wrote to the first
element, the pre-existing one won, and the new card displayed a value its own
formatter never produced — visible only because the rendered text carried an "x"
the new formatter does not add.

Two kinds of false positive have to be excluded or this test is noise, and both
were found by running it:

* **Ids built in JavaScript.** `id="turn-' + n + '"` and ``id=`${x}tab` `` are one
  declaration producing many distinct ids at runtime. Excluded structurally by
  scanning only outside `<script>`, rather than by pattern-guessing.
* **Ids in mutually exclusive Jinja branches.** `fedwatch.html` and `housing.html`
  each declare their error div twice, once in the warming branch and once in the
  loaded branch of the same `{% if %}`. Only one is ever rendered, so that is
  correct code — and an exemption list would have hidden the next real one. The
  scan tracks branch context instead and only reports ids that can co-occur.
"""
from __future__ import annotations

import re
import unittest
from collections import defaultdict
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "ystocker" / "templates"

#: `id="..."` in template source. Single and double quoted both appear.
_ID = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")

#: A Jinja expression inside an id makes it a family, not one id.
_JINJA_IN_VALUE = re.compile(r"\{\{.*?\}\}|\{%.*?%\}")

#: Script bodies, stripped before scanning.
_SCRIPT = re.compile(r"<script\b.*?</script>", re.S | re.I)

#: Branch control and id declarations, matched together so they can be walked
#: in document order. `elif` opens a new arm of the same conditional.
_TOKEN = re.compile(
    r"""\{%-?\s*(?P<branch>if|elif|else|endif)\b"""
    r"""|\bid\s*=\s*["'](?P<id>[^"']+)["']""")


def _id_paths(markup: str) -> dict[str, list[tuple]]:
    """Map each literal id to the branch paths it is declared under.

    A path is a tuple of ``(conditional_index, arm_index)`` pairs — the chain of
    `{% if %}` arms enclosing the declaration. Two declarations can co-occur in
    one render unless they disagree on the arm of some shared conditional.
    """
    found: dict[str, list[tuple]] = defaultdict(list)
    stack: list[list[int]] = []          # [conditional_id, arm_index]
    conditionals = 0

    # Walked in *token order* across the whole document, not line by line.
    # A line-at-a-time loop that handles branch markers before ids mis-attributes
    # anything sharing a line — `{% if %}<div id="x">{% else %}<div id="x">{% endif %}`
    # collapses to two ids at the same (empty) path and reads as a real
    # duplicate. Found by this file's own exclusive-branch test.
    for match in _TOKEN.finditer(markup):
        keyword = match.group("branch")
        if keyword:
            if keyword == "if":
                conditionals += 1
                stack.append([conditionals, 0])
            elif keyword in ("elif", "else") and stack:
                stack[-1][1] += 1
            elif keyword == "endif" and stack:
                stack.pop()
            continue
        value = match.group("id")
        if value is None or _JINJA_IN_VALUE.search(value) or "${" in value:
            continue
        found[value].append(tuple(tuple(frame) for frame in stack))
    return found


def _can_co_occur(a: tuple, b: tuple) -> bool:
    """True when two branch paths could both be rendered in one pass."""
    arms_a = {cond: arm for cond, arm in a}
    for cond, arm in b:
        if cond in arms_a and arms_a[cond] != arm:
            return False          # different arms of the same conditional
    return True


class DuplicateIdTests(unittest.TestCase):
    def test_no_template_renders_an_id_twice(self):
        offenders: dict[str, list[str]] = {}
        for path in sorted(TEMPLATES.glob("*.html")):
            markup = _SCRIPT.sub("", path.read_text())
            dupes = []
            for name, paths in _id_paths(markup).items():
                if len(paths) < 2:
                    continue
                if any(_can_co_occur(paths[i], paths[j])
                       for i in range(len(paths))
                       for j in range(i + 1, len(paths))):
                    dupes.append(name)
            if dupes:
                offenders[path.name] = sorted(dupes)

        self.assertEqual(
            offenders, {},
            "duplicate element ids — getElementById returns the first, so two "
            f"writers silently fight over one element: {offenders}")

    def test_the_scan_still_catches_a_real_duplicate(self):
        """Guards the branch logic itself. Without this, making `_can_co_occur`
        permissive would silence the whole test and it would pass for ever."""
        markup = '<div id="a"></div><div id="a"></div>'
        paths = _id_paths(markup)
        self.assertTrue(_can_co_occur(paths["a"][0], paths["a"][1]))

    def test_exclusive_branches_are_not_flagged(self):
        markup = (
            '{% if warming %}<div id="err"></div>'
            '{% else %}<div id="err"></div>{% endif %}'
        )
        paths = _id_paths(markup)
        self.assertEqual(len(paths["err"]), 2)
        self.assertFalse(_can_co_occur(paths["err"][0], paths["err"][1]))

    def test_the_same_arm_twice_is_still_a_duplicate(self):
        """Being inside a conditional is not a licence — two ids in the *same*
        arm collide exactly as they would at the top level."""
        markup = '{% if x %}<div id="d"></div><div id="d"></div>{% endif %}'
        paths = _id_paths(markup)
        self.assertTrue(_can_co_occur(paths["d"][0], paths["d"][1]))

    def test_sibling_conditionals_can_co_occur(self):
        """Two separate `{% if %}` blocks are not exclusive — both can be true."""
        markup = '{% if a %}<div id="s"></div>{% endif %}{% if b %}<div id="s"></div>{% endif %}'
        paths = _id_paths(markup)
        self.assertTrue(_can_co_occur(paths["s"][0], paths["s"][1]))


if __name__ == "__main__":
    unittest.main()
