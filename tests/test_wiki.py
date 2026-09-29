"""The TradeAgents wiki: the registry, its content files, and paired languages.

Needs no app, no network and no browser — it reads ``ystocker/wiki.py`` and the
template files as text, in the house style of ``test_template_ids`` and
``test_i18n_completeness``.

Four failures this exists to catch, all of which render without complaint:

* **A page nobody can reach, or a link to a page that is not there.** The
  sidebar, prev/next and the Research Lab index are built from the registry; the
  bodies are files under ``templates/wiki/``. An entry with no file is a 500 on
  click, and a file with no entry is an orphan that silently stops being linked.
  Both directions are asserted.
* **One language missing.** Long-form pages are paired ``data-l="en"`` /
  ``data-l="zh"`` blocks and ``<html lang>`` shows one of each pair. A block
  written in English only is not a visible gap on an English page — it is a
  paragraph that vanishes from the Chinese one, and nobody reading in English
  would ever see the hole. The count per file must match.
* **A roster that describes a desk nobody runs.** ``wiki.desk()`` is built from
  the analyst tuples the runner actually passes, and ``agent_roles.ROLES`` still
  carries the retired Fundamentals Analyst so old reports render. Listing it on
  the landing would advertise a seat that has not existed since 2026-08-30.
* **The phone header sliding back.** On trade-agents.com the Markets bar stays
  pinned under the masthead at every width, and the header's controls share one
  size, both asked for from a phone on 2026-09-29. Both live in CSS, which no
  rendered-HTML check can see.
"""
from __future__ import annotations

import re
import unittest
from datetime import date
from pathlib import Path

from ystocker import agent_roles, wiki

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "ystocker" / "templates"
WIKI = TEMPLATES / "wiki"

#: The runner's analyst tuples, read as text rather than imported: importing
#: ystocker.agents pulls in the runner's environment handling, and this test must
#: run anywhere. The regex is checked against the real assignment below so it
#: cannot go vacuous.
_AGENTS = (ROOT / "ystocker" / "agents.py").read_text(encoding="utf-8")
_TUPLE = re.compile(r"^BASE_ANALYSTS\s*=\s*\(([^)]*)\)", re.M)
_ASTOCK = re.compile(r"^ASTOCK_ANALYSTS\s*=\s*BASE_ANALYSTS\s*\+\s*\(([^)]*)\)", re.M)


def _names(src: str) -> tuple[str, ...]:
    return tuple(re.findall(r"""["']([a-z_]+)["']""", src))


BASE = _names(_TUPLE.search(_AGENTS).group(1))
ASTOCK = BASE + _names(_ASTOCK.search(_AGENTS).group(1))

_EN = re.compile(r"""data-l\s*=\s*["']en["']""")
_ZH = re.compile(r"""data-l\s*=\s*["']zh["']""")
_COMMENT = re.compile(r"\{#.*?#\}", re.S)


def _pair_counts(path: Path) -> tuple[int, int]:
    # Jinja comments are stripped first: a comment explaining the convention
    # would otherwise be counted as markup.
    text = _COMMENT.sub("", path.read_text(encoding="utf-8"))
    return len(_EN.findall(text)), len(_ZH.findall(text))


class RegistryTests(unittest.TestCase):
    def test_the_roster_regexes_found_the_real_tuples(self):
        self.assertIn("market", BASE)
        self.assertGreater(len(ASTOCK), len(BASE))

    def test_slugs_are_path_safe_and_unique(self):
        for kind, entries in (("docs", wiki.DOCS), ("posts", wiki.POSTS)):
            slugs = [e["slug"] for e in entries]
            self.assertEqual(len(slugs), len(set(slugs)), f"duplicate {kind} slug")
            for slug in slugs:
                self.assertRegex(slug, wiki.SLUG_RE, f"{kind} slug {slug!r}")

    def test_every_pair_has_both_languages_and_neither_is_blank(self):
        """A blank translation renders as a gap in the nav or an empty heading."""
        def pairs(entry):
            for key in ("title", "summary", "kicker"):
                if key in entry:
                    yield key, entry[key]
            for i, tag in enumerate(entry.get("tags", ())):
                yield f"tags[{i}]", tag

        for entry in (*wiki.DOCS, *wiki.POSTS):
            for key, pair in pairs(entry):
                for lang in ("en", "zh"):
                    self.assertTrue((pair.get(lang) or "").strip(),
                                    f"{entry['slug']}.{key} has no {lang}")
        for group in (*wiki.DOC_GROUPS, *wiki.DESK_TEAMS):
            for lang in ("en", "zh"):
                self.assertTrue(group["title"][lang].strip())

    def test_every_doc_is_in_a_known_group(self):
        keys = {g["key"] for g in wiki.DOC_GROUPS}
        for page in wiki.DOCS:
            self.assertIn(page["group"], keys, page["slug"])
        # And the nav drops no page: the groups partition the list.
        self.assertEqual(sum(len(g["pages"]) for g in wiki.docs_nav()), len(wiki.DOCS))

    def test_default_doc_exists(self):
        self.assertIsNotNone(wiki.doc(wiki.DEFAULT_DOC))

    def test_posts_are_dated_newest_first(self):
        """The tuple order is the order a reader sees; a typo'd date would put a
        post in the wrong place, so the order is asserted, not re-sorted."""
        dates = [date.fromisoformat(p["date"]) for p in wiki.POSTS]
        self.assertEqual(dates, sorted(dates, reverse=True))
        for p in wiki.POSTS:
            self.assertGreater(p["minutes"], 0)
            self.assertTrue(p["tags"], p["slug"])

    def test_neighbours_walk_the_reading_order(self):
        first, last = wiki.DOCS[0]["slug"], wiki.DOCS[-1]["slug"]
        self.assertEqual(wiki.neighbours(first)[0], None)
        self.assertEqual(wiki.neighbours(last)[1], None)
        prev_page, next_page = wiki.neighbours(wiki.DOCS[1]["slug"])
        self.assertEqual(prev_page["slug"], first)
        self.assertEqual(next_page["slug"], wiki.DOCS[2]["slug"])
        self.assertEqual(wiki.neighbours("no-such-page"), (None, None))

        newer, older = wiki.post_neighbours(wiki.POSTS[0]["slug"])
        self.assertIsNone(newer)
        if len(wiki.POSTS) > 1:
            self.assertEqual(older["slug"], wiki.POSTS[1]["slug"])

    def test_lookups_miss_cleanly(self):
        self.assertIsNone(wiki.doc("nope"))
        self.assertIsNone(wiki.post("nope"))


class ContentFileTests(unittest.TestCase):
    def test_every_entry_has_a_content_file_and_every_file_an_entry(self):
        for sub, entries in (("docs", wiki.DOCS), ("research", wiki.POSTS)):
            registered = {e["slug"] for e in entries}
            on_disk = {p.stem for p in (WIKI / sub).glob("*.html")}
            self.assertEqual(registered - on_disk, set(),
                             f"registered in wiki.py with no templates/wiki/{sub}/ file")
            self.assertEqual(on_disk - registered, set(),
                             f"templates/wiki/{sub}/ files nothing links to")

    def test_every_english_block_has_its_chinese_partner(self):
        # Every template, not a list of them: the pairing spread beyond the wiki
        # to the landing, and then to trade-agents.com's shell and the pages in
        # it (sign-in, contact, the run page, shared reports), and a list is the
        # thing a new page forgets to join.
        paths = sorted(TEMPLATES.rglob("*.html"))
        uneven = {}
        for path in paths:
            en, zh = _pair_counts(path)
            if en != zh:
                uneven[str(path.relative_to(TEMPLATES))] = (en, zh)
        self.assertEqual(uneven, {}, "data-l en/zh counts differ (en, zh)")

    def test_the_pairing_guard_reads_the_shell(self):
        # Non-vacuous: the masthead and the footer are paired markup, and the
        # rglob above has to have reached them.
        for name in ("_ta_masthead.html", "_ta_footer.html"):
            en, zh = _pair_counts(TEMPLATES / name)
            self.assertGreater(en, 3, name)

    def test_the_pair_count_is_not_vacuous(self):
        en, zh = _pair_counts(WIKI / "docs" / "overview.html")
        self.assertGreater(en, 5)
        self.assertEqual(en, zh)

    def test_content_sections_carry_an_id_and_a_heading(self):
        """The contents list is read off `section[id] > h2`. A section without an
        id cannot be linked to; one without an h2 silently drops out of it."""
        opener = re.compile(r"<section\b([^>]*)>\s*(<h2\b)?", re.S)
        for sub in ("docs", "research"):
            for path in (WIKI / sub).glob("*.html"):
                text = _COMMENT.sub("", path.read_text(encoding="utf-8"))
                for attrs, h2 in opener.findall(text):
                    self.assertRegex(attrs, r"""\bid\s*=\s*["'][a-z0-9-]+["']""",
                                     f"{path.name}: <section{attrs}> has no id")
                    self.assertTrue(h2, f"{path.name}: <section{attrs}> does not open with an h2")

    def test_the_css_that_hides_the_other_language_is_in_base(self):
        base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
        self.assertIn('html[lang^="zh"] [data-l="en"]', base)
        self.assertIn('html:not([lang^="zh"]) [data-l="zh"]', base)


_CSS = re.sub(r"/\*.*?\*/", "", (ROOT / "ystocker" / "static" / "wiki.css").read_text(encoding="utf-8"),
              flags=re.S)


def _media_blocks(css: str) -> list[tuple[str, str]]:
    """Each @media block as (query, body), by brace matching. Comments are gone
    first, since several quote a rule, braces and all."""
    out = []
    for m in re.finditer(r"@media([^{]*)\{", css):
        depth, i = 1, m.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(css[i], 0)
            i += 1
        out.append((m.group(1).strip(), css[m.end():i - 1]))
    return out


class PhoneHeaderTests(unittest.TestCase):
    """trade-agents.com's header on a phone: CSS, so asserted as text."""

    def test_the_markets_bar_is_pinned_at_every_width(self):
        # It used to scroll away below 881px, which left a dashboard's navigation
        # out of reach from halfway down it. Pinned also means --w-sub-h keeps the
        # bar's height there, since the pages' own sticky bars sit under it.
        self.assertRegex(_CSS, r"\.w-sub \{\s*position: sticky;")
        self.assertIn("body.w-markets { --w-sub-h: 45px; }", _CSS)
        blocks = _media_blocks(_CSS)
        self.assertTrue(any(q == "(max-width: 880px)" for q, _ in blocks), "the phone block moved")
        for query, body in blocks:
            self.assertNotRegex(body, r"\.w-sub\s*\{[^}]*position:\s*static", query)
            self.assertNotRegex(body, r"--w-sub-h:\s*0", query)

    def test_the_language_toggle_is_a_fixed_square_on_a_phone(self):
        # Sized by its label it was 36px wide in Chinese ("EN") and 44px in
        # English ("中文"), beside 36px squares, so every switch moved the bar.
        phone = [body for query, body in _media_blocks(_CSS) if query == "(max-width: 880px)"]
        self.assertTrue(any(".w-top .lang-toggle-btn { width: 36px; padding: 0; }" in b for b in phone))
        # A narrower block padding it again would squeeze 中文 out of the box.
        for query, body in _media_blocks(_CSS):
            width = re.search(r"max-width:\s*(\d+)px", query)
            if width and int(width.group(1)) < 880:
                self.assertNotIn("lang-toggle-btn", body, query)

    def test_markets_keeps_its_clearance_off_html_there(self):
        # A scroll-padding on <html> covers the pinned header's own controls:
        # opening ticker search from 2,200px down a phone scrolled the page up
        # 1,100px. The clearance is a scroll-margin on what is scrolled to.
        tpl = (TEMPLATES / "markets.html").read_text(encoding="utf-8")
        self.assertIn(":root:has(> body.w-markets) { scroll-padding-top: 0; }", tpl)
        self.assertIn("body.w-markets main :not(.mk-jump, .mk-jump *) { scroll-margin-top:", tpl)

    def test_off_the_dashboards_the_links_get_a_row_of_their_own(self):
        # Below 960px the bar has no room for its links, and a phone had no way
        # to them at all. Off the dashboards they become a second, sideways-
        # scrolling row inside the masthead -- whose height every sticky offset
        # reads as --w-top-h, so the declared height must be the rows it draws
        # plus the 1px rule under the masthead, or everything pinned under it
        # sits a few pixels wrong without anything failing.
        blocks = dict((q, b) for q, b in _media_blocks(_CSS) if "w-markets" in b and ".w-top-nav" in b)
        for query, top_h, first in (("(max-width: 960px)", 114, 68), ("(max-width: 640px)", 106, 60)):
            body = blocks.get(query, "")
            self.assertIn(f"body:not(.w-markets) {{ --w-top-h: {top_h}px; }}", body, query)
            rows = re.search(r"grid-template-rows:\s*(\d+)px\s+(\d+)px", body)
            self.assertTrue(rows, query)
            self.assertEqual(int(rows.group(1)), first, query)
            self.assertEqual(int(rows.group(1)) + int(rows.group(2)) + 1, top_h, query)
        row = re.search(r"body:not\(\.w-markets\) \.w-top \.w-top-nav \{([^}]*)\}", blocks["(max-width: 960px)"])
        self.assertTrue(row and "display: flex" in row.group(1) and "overflow-x: auto" in row.group(1))

    def test_the_footer_links_are_a_grid_on_a_phone(self):
        # A wrapping line broke seven links after "What's new" at 390px, each a
        # 20px target; on a phone they are 40px rows in columns.
        phone = "".join(b for q, b in _media_blocks(_CSS) if q == "(max-width: 640px)")
        nav = re.search(r"\.w-footer nav \{([^}]*)\}", phone)
        self.assertTrue(nav and "display: grid" in nav.group(1))
        self.assertRegex(phone, r"\.w-footer nav a \{[^}]*min-height: 40px")


class DeskTests(unittest.TestCase):
    def setUp(self):
        self.teams = wiki.desk(agent_roles.ROLES, BASE, ASTOCK)
        self.seats = {r["key"]: r for t in self.teams for r in t["roles"]}

    def test_every_analyst_the_runner_uses_has_a_seat(self):
        for key in ASTOCK:
            role = wiki._ANALYST_ROLE.get(key, key)
            self.assertIn(role, self.seats, f"analyst {key!r} missing from the desk")

    def test_the_package_name_for_sentiment_maps_to_the_heading_role(self):
        self.assertIn("social", BASE)
        self.assertIn("sentiment", self.seats)
        self.assertNotIn("social", self.seats)

    def test_a_retired_role_is_not_advertised(self):
        self.assertNotIn("fundamentals", self.seats)

    def test_a_share_specialists_are_flagged_and_only_they_are(self):
        for key, seat in self.seats.items():
            analyst = {v: k for k, v in wiki._ANALYST_ROLE.items()}.get(key, key)
            expected = seat["group"] == "analysts" and analyst not in BASE
            self.assertEqual(seat["a_share_only"], expected, key)
        self.assertTrue(any(s["a_share_only"] for s in self.seats.values()))

    def test_teams_come_in_report_order(self):
        order = [t["key"] for t in wiki.DESK_TEAMS]
        seen = [t["key"] for t in self.teams]
        self.assertEqual(seen, [k for k in order if k in seen])
        self.assertEqual(seen[0], "analysts")
        self.assertEqual(seen[-1], "decision")

    def test_every_seat_has_notes_in_both_languages(self):
        for key, seat in self.seats.items():
            for field in ("short", "desc"):
                pair = seat[field]
                self.assertIsNotNone(pair, f"{key} has no {field} note")
                for lang in ("en", "zh"):
                    self.assertTrue(pair[lang].strip(), f"{key}.{field} has no {lang}")

    def test_no_note_describes_a_role_that_does_not_exist(self):
        keys = {r["key"] for r in agent_roles.ROLES}
        self.assertEqual(set(wiki.ROLE_NOTES) - keys, set())

    def test_an_empty_roster_drops_the_team(self):
        teams = wiki.desk(agent_roles.ROLES, (), ())
        self.assertNotIn("analysts", [t["key"] for t in teams])


if __name__ == "__main__":
    unittest.main()
