"""Every ``I18n.<member>`` a yStocker page uses must be one i18n.js exports.

A member that does not exist is not an error in JavaScript. It is ``undefined``,
and the two ways this codebase reads one both fail quietly: ``I18n.lang === 'zh'``
is simply false, so the Chinese branch never runs, and ``I18n.foo(...)`` throws
only when that line is reached.

Both shipped, from one misreading of the API, which is ``getLang()``. The
earnings list on /markets dated every reporter in English on a Chinese page,
and /13f's AUM explanation was always requested in English, because each read
``I18n.lang``.

Comments are stripped before scanning, so prose that names a private helper
("see I18n._syncDocumentLang()") is not mistaken for a call.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "ystocker"
I18N = ROOT / "static" / "i18n.js"

_USE = re.compile(r"\bI18n\.([A-Za-z_]\w*)")
_COMMENTS = [
    re.compile(r"/\*.*?\*/", re.S),          # JS / CSS block
    re.compile(r"\{#.*?#\}", re.S),          # Jinja
    re.compile(r"<!--.*?-->", re.S),         # HTML
    re.compile(r"(?m)(^|\s)//.*$"),          # JS line; not the // inside https://
]


def _exports() -> set[str]:
    src = I18N.read_text()
    block = src[src.rindex("return {"):]
    block = block[:block.index("}")]
    return set(re.findall(r"[A-Za-z_]\w*", block[len("return {"):]))


def _uses() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    files = sorted((ROOT / "templates").rglob("*.html")) + sorted((ROOT / "static").rglob("*.js"))
    for path in files:
        text = path.read_text(errors="replace")
        for pattern in _COMMENTS:
            text = pattern.sub(" ", text)
        for member in _USE.findall(text):
            found.setdefault(member, set()).add(str(path.relative_to(ROOT)))
    return found


class I18nApiTests(unittest.TestCase):

    def test_the_export_list_parses(self):
        # Guards the check below: an empty set would fail everything loudly,
        # but a wrong one could pass something it should not.
        self.assertTrue({"t", "getLang", "apply", "label"} <= _exports())

    def test_every_member_used_is_exported(self):
        exported = _exports()
        missing = {m: sorted(p) for m, p in _uses().items() if m not in exported}
        self.assertEqual(missing, {}, "I18n has no such member; is it getLang()?")


if __name__ == "__main__":
    unittest.main()
