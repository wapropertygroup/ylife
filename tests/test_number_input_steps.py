"""Static check that a ``type="number"`` input accepts the value it suggests.

HTML5 measures ``step`` from a **step base**, and that base is ``min`` when a
``min`` is present — not zero. So ``min="0.01" step="0.1"`` does not mean "one
decimal place, at least 0.01"; it means the only acceptable values are 0.01,
0.11, 0.21 … 7.91, 8.01. Every whole number is a ``stepMismatch``.

That is invisible in the markup and loud in the worst place. On a form with a
submit button the browser runs constraint validation *before* dispatching
``submit``, so the listener never runs: no request, no error, no console line,
just a native bubble reading "the two nearest valid values are 7.91 and 8.01".
The Limits form on ``/assets`` shipped exactly this and could not save a whole
number at all, while ``tests/check_assets_policy_endpoints.py`` went on proving
the server stored ``8.0`` perfectly — because nothing typed in a browser could
ever reach it.

The invariant asserted here is the one both bugs broke, and it needs no browser:
**a number input must accept the value it puts in front of the reader.** A
``placeholder`` is a suggestion and a ``value`` is a default, so if either fails
the field's own ``min``/``max``/``step`` the field contradicts itself. Checking
representative whole numbers as well catches the same mismatch on a field that
happens to suggest nothing.
"""

from __future__ import annotations

import re
import unittest
from decimal import Decimal
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parent.parent / "ystocker" / "templates"

# Every <input ...> tag; the type/attr sniffing happens per match below. Jinja
# and JS-built markup both appear in these files, so anything whose numeric
# attributes are not plain literals is skipped rather than guessed at.
_INPUT = re.compile(r"<input\b[^>]*>", re.I | re.S)
_ATTR = re.compile(r"""(?P<name>[a-zA-Z_:.-]+)\s*=\s*(?P<q>['"])(?P<val>.*?)(?P=q)""", re.S)

#: Whole numbers a reader plausibly types into a percentage or a money field.
#: Deliberately integers: the trap makes *every* integer invalid, so these are
#: what a human loses, and a field that refuses all of them is broken whatever
#: its placeholder says.
_HUMAN_VALUES = (1, 5, 8, 10, 20, 25, 50, 100)


def _iter_templates() -> list[Path]:
    return sorted(TEMPLATES.glob("*.html"))


def _decimal(raw: str) -> Decimal | None:
    """``raw`` as an exact Decimal, or None if it is not a plain literal.

    Decimal rather than float on purpose: this is the arithmetic the bug lives
    in, and ``(8 - 0.01) % 0.1`` in binary floating point is 0.09999999999999995
    — a check written with floats would report a mismatch on values that are
    actually fine, or miss one that is not.
    """
    try:
        return Decimal(raw.strip())
    except Exception:  # noqa: BLE001 — a Jinja expression, a JS template, etc.
        return None


def _step_valid(value: Decimal, minimum: Decimal | None, step: Decimal) -> bool:
    """Does *value* sit on the step ladder, as a browser computes it?

    The step base is ``min`` when present, else 0 (the ``value`` attribute can
    also serve as the base, but only when there is no ``min``; every input here
    that sets a step also sets a min, so that branch is not reachable).
    """
    base = minimum if minimum is not None else Decimal(0)
    return (value - base) % step == 0


class NumberInputStepTest(unittest.TestCase):

    def _fields(self):
        """(template, tag, min, max, step) for every literal-numeric number input."""
        for path in _iter_templates():
            text = path.read_text(encoding="utf-8")
            for tag in _INPUT.findall(text):
                attrs = {m.group("name").lower(): m.group("val")
                         for m in _ATTR.finditer(tag)}
                if attrs.get("type", "").lower() != "number":
                    continue
                raw_step = attrs.get("step", "").strip().lower()
                if raw_step in ("", "any"):
                    continue           # no ladder, so nothing can mismatch
                step = _decimal(raw_step)
                if step is None or step <= 0:
                    continue
                yield (path.name, tag, _decimal(attrs.get("min", "")),
                       _decimal(attrs.get("max", "")), step, attrs)

    def test_suggested_value_satisfies_the_fields_own_step(self) -> None:
        """A placeholder or default that the field itself rejects."""
        for name, tag, lo, hi, step, attrs in self._fields():
            for kind in ("value", "placeholder"):
                suggested = _decimal(attrs.get(kind, ""))
                if suggested is None:
                    continue
                self.assertTrue(
                    _step_valid(suggested, lo, step),
                    f"{name}: {kind}=\"{attrs[kind]}\" is a stepMismatch against "
                    f'min="{attrs.get("min")}" step="{attrs.get("step")}" — the '
                    f"browser rejects the value the field itself suggests.\n  {tag}")

    def test_whole_numbers_in_range_are_acceptable(self) -> None:
        """A field that refuses every round number a reader would type."""
        for name, tag, lo, hi, step, attrs in self._fields():
            in_range = [Decimal(v) for v in _HUMAN_VALUES
                        if (lo is None or Decimal(v) >= lo)
                        and (hi is None or Decimal(v) <= hi)]
            if not in_range:
                continue
            accepted = [v for v in in_range if _step_valid(v, lo, step)]
            self.assertTrue(
                accepted,
                f'{name}: min="{attrs.get("min")}" step="{attrs.get("step")}" '
                f"rejects every whole number in range "
                f"({', '.join(str(v) for v in in_range)}). HTML5 counts step from "
                f"min, not from zero.\n  {tag}")

    def test_the_scan_finds_the_inputs_it_is_meant_to_guard(self) -> None:
        """Guards the guard.

        Both assertions above iterate ``_fields()`` and pass vacuously if it
        yields nothing — so a regex that stops matching, a moved template
        directory or a wholesale switch to ``step="any"`` would turn this file
        into two tests that can never fail again, silently. Asserting the scan
        still sees the markup is what keeps that from happening quietly.
        """
        every_number_input = 0
        for path in _iter_templates():
            for tag in _INPUT.findall(path.read_text(encoding="utf-8")):
                attrs = {m.group("name").lower(): m.group("val")
                         for m in _ATTR.finditer(tag)}
                if attrs.get("type", "").lower() == "number":
                    every_number_input += 1
        self.assertGreater(
            every_number_input, 3,
            "the number-input scan found almost nothing — the regex or the "
            "template path is probably wrong, and both step assertions in this "
            "file would be passing vacuously")


if __name__ == "__main__":
    unittest.main(verbosity=2)
