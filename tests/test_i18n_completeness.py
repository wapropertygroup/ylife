"""No translation may be an empty string.

Every lookup in this codebase is spelled ``I18n.t(key) || fallback``, and in
JavaScript an empty string is falsy — so a translation of ``""`` does not render
as nothing, it renders as the **English fallback**. The one place that is never
what was meant is a Chinese page.

Caught for real on 2026-09-19. A three-fragment sentence needed no trailing word
in Chinese, so its key was given ``zh: ''`` — and the page printed
``可用权重 40%，最低要求 50% minimum``. The fix was structural rather than a
better empty value: one key carrying both placeholders, so no translation has to
be empty to get the word order right.

Also checks that every key has both languages at all, which is the same failure
one step earlier.
"""
from __future__ import annotations

import re
import unittest
from pathlib import Path

I18N = Path(__file__).resolve().parent.parent / "ystocker" / "static" / "i18n.js"

#: `'key': { en: '...', zh: '...' }`, tolerant of line breaks inside the braces.
#:
#: The body allows one level of nested braces. An earlier `[^{}]*` looked right
#: and silently skipped every entry containing a `{placeholder}` — which is
#: precisely the population the placeholder check below exists for, so that check
#: passed vacuously. Proven by mutating a `{need}` out of a translation and
#: watching the suite stay green.
_ENTRY = re.compile(
    r"""['"]([\w.\-]+)['"]\s*:\s*\{((?:[^{}]|\{[^{}]*\})*)\}""", re.S)
_LANG = re.compile(r"""\b(en|zh)\s*:\s*(['"])(.*?)(?<!\\)\2""", re.S)


def _entries() -> list[tuple[str, dict[str, str]]]:
    text = I18N.read_text()
    out = []
    for key, body in _ENTRY.findall(text):
        langs = {lang: value for lang, _q, value in _LANG.findall(body)}
        if langs:
            out.append((key, langs))
    return out


class TranslationTests(unittest.TestCase):
    def test_the_file_parses_into_something(self):
        """Guards the regex. A pattern that matched nothing would make every
        assertion below vacuously true."""
        entries = _entries()
        self.assertGreater(len(entries), 500, f"only parsed {len(entries)} entries")

    def test_the_parser_sees_entries_with_placeholders(self):
        """The specific vacuity that already happened once: a body pattern of
        `[^{}]*` parses 500+ entries and none of the ones with `{placeholders}`,
        so the placeholder check below has nothing to check."""
        with_slots = [k for k, langs in _entries()
                      if any("{" in v for v in langs.values())]
        self.assertGreater(len(with_slots), 5,
                           f"only {len(with_slots)} placeholder entries parsed — "
                           "the entry regex is probably excluding braces")

    def test_no_translation_is_empty(self):
        empty = [
            f"{key}.{lang}"
            for key, langs in _entries()
            for lang, value in langs.items()
            if not value.strip()
        ]
        self.assertEqual(
            empty, [],
            "empty translations fall through `I18n.t(k) || fallback` and render "
            f"the English fallback instead of nothing: {empty}")

    def test_every_key_has_both_languages(self):
        missing = [
            f"{key}: has {sorted(langs)}"
            for key, langs in _entries()
            if set(langs) != {"en", "zh"}
        ]
        self.assertEqual(missing, [], f"keys without both languages: {missing}")

    def test_placeholders_match_across_languages(self):
        """A `{n}` present in one language and absent in the other leaves a
        number unsubstituted, or prints a literal `{n}` to the reader."""
        mismatched = []
        for key, langs in _entries():
            if set(langs) != {"en", "zh"}:
                continue
            slots = {lang: set(re.findall(r"\{(\w+)\}", value))
                     for lang, value in langs.items()}
            if slots["en"] != slots["zh"]:
                mismatched.append(f"{key}: en={sorted(slots['en'])} zh={sorted(slots['zh'])}")
        self.assertEqual(mismatched, [], f"placeholder mismatch: {mismatched}")


if __name__ == "__main__":
    unittest.main()
