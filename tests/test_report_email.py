"""Agent report email tests — no Flask app, no network, no SES.

Covers ystocker/report_email.py, which turns a finished agent run into an HTML
mail. Three things here are worth guarding and nothing else really is:

1. **The report is untrusted.** It is LLM output, and the models sometimes answer
   in HTML. Every escaping and allowlist path is asserted on rendered output,
   because that is the only place a mistake becomes visible.

2. **Gmail clips silently at ~102 KB.** The budget must hold, and — since the
   Portfolio Manager's turn is the *last* section a report emits — the decision
   and its rationale must survive a clip that drops seven analysts. A naive
   in-order walk keeps the analysts and drops the answer.

3. **Send exactly once.** Completion is detected in two places (the supervising
   thread, and _reap on any read in any worker), so the claim has to be atomic
   rather than read-then-write.

The module is loaded by path against a stub ``ystocker`` package so that neither
Flask nor boto3 is imported, matching test_brief_formatters.py.
"""

from __future__ import annotations

import importlib.util
import pathlib
import re
import sys
import tempfile
import types
import unittest

ROOT = pathlib.Path(__file__).parents[1]


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(module)
    return module


# A stub package, so report_email's lazy `from ystocker.agents import JOB_DIR`
# and `from ystocker.agent_roles import split_sections` resolve without pulling
# in ystocker/__init__.py (which imports Flask). agent_roles is the real module:
# section splitting is exactly what these tests are checking against.
_pkg = types.ModuleType("ystocker")
_pkg.__path__ = [str(ROOT / "ystocker")]
sys.modules.setdefault("ystocker", _pkg)
agent_roles = _load("ystocker.agent_roles", "ystocker/agent_roles.py")
sys.modules["ystocker.agent_roles"] = agent_roles
_agents_stub = types.ModuleType("ystocker.agents")
_agents_stub.JOB_DIR = pathlib.Path(tempfile.gettempdir()) / "ystocker-test-jobs"
_agents_stub.EMAIL_MARKER_SUFFIX = ".emailed"
sys.modules["ystocker.agents"] = _agents_stub

mail = _load("ystocker.report_email", "ystocker/report_email.py")


# Anything that must never appear as *live* markup in output built from model
# text. Escaped text is inert by construction -- "&lt;img src=x onerror=..&gt;"
# is a string an email client displays, not an element it runs -- so escaped
# sequences are removed before the check. Without that this flags its own
# successes, which is the failure mode that makes a security test worthless.
FORBIDDEN = ("<script", "<iframe", "<style", "<svg", "<object", "<embed",
             "<img", "<input", "<form", "onerror=", "onclick=", "onload=",
             "onmouseover=", "javascript:", "data:text")

_ESCAPED = re.compile(r"&lt;.*?&gt;", re.S)

# The clip notice is the only dashed-border element in the mail. Identified by
# that rather than by its prose, which is localised and gets reworded — two
# earlier tests here asserted on copy and broke the moment it was aligned with
# i18n.js.
CLIP_MARKER = "border:1px dashed"


def assert_inert(case: unittest.TestCase, html: str, label: str = "") -> None:
    live = _ESCAPED.sub("", html).lower()
    for needle in FORBIDDEN:
        case.assertNotIn(needle, live, f"{label}: {needle!r} survived as live markup")


# ── Fixtures ────────────────────────────────────────────────────────────────

def _para(n: int) -> str:
    return ("Analysis paragraph with **bold** text and a figure of 123.45. " * 40
            + "\n\n") * n


def build_report(per_section: int = 1, decision_body: str = "") -> str:
    """A report shaped like write_report_tree's, at a controllable size."""
    out = "# NVDA Analysis\n\nGenerated 2026-08-29 by TradingAgents\n\n"
    out += "## I. Analyst Team Reports\n\n"
    for name in ("Market Analyst", "Sentiment Analyst", "News Analyst",
                 "Fundamentals Analyst"):
        out += f"### {name}\n\n{_para(per_section)}"
    out += "## II. Research Team Decision\n\n"
    for name in ("Bull Researcher", "Bear Researcher", "Research Manager"):
        out += f"### {name}\n\n{_para(per_section)}"
    out += "## V. Portfolio Manager Decision\n\n### Portfolio Manager\n\n"
    out += decision_body or "**BUY** — high conviction.\n\nEntry 178, stop 165.\n"
    return out


def make_job(**over) -> dict:
    job = {
        "id": "abc123def4567890",
        "ticker": "NVDA",
        "date": "2026-08-29",
        "user": "reader@example.com",
        "lang": "en",
        "language": "English",
        "status": "done",
        "decision": "BUY",
        "report": build_report(),
        "elapsed_sec": 1834.2,
    }
    job.update(over)
    return job


# ── The Markdown renderer ───────────────────────────────────────────────────

class RenderTests(unittest.TestCase):
    def test_block_constructs_render(self):
        html = mail.render(
            "# Title\n\n## Sub\n\nA **bold** and *italic* line with `code`.\n\n"
            "- one\n- two\n\n1. first\n2. second\n\n> quoted\n\n---\n")
        for tag in ("<h1", "<h2", "<p", "<strong", "<em", "<code",
                    "<ul", "<ol", "<li", "<blockquote", "<hr"):
            self.assertIn(tag, html, f"missing {tag}")

    def test_table_renders_as_table_not_pipes(self):
        # The reason this module exists rather than reusing _build_email_sections,
        # which splits on blank lines into <p> and would deliver literal pipes.
        html = mail.render("| Level | Value |\n|---|---|\n| MA50 | 178.20 |\n")
        self.assertIn("<table", html)
        self.assertIn("<th", html)
        self.assertEqual(html.count("<td"), 2)
        self.assertNotIn("|---|", html)

    def test_every_element_carries_inline_style(self):
        # Gmail drops a <style> block, so an element styled only by stylesheet
        # renders as unformatted text there.
        html = mail.render("# H\n\ntext\n\n| a |\n|---|\n| b |\n\n- li\n")
        for tag in ("h1", "p", "table", "th", "td", "ul", "li"):
            # Anchored on the tag name plus a delimiter: "<th" also matches the
            # front of "<thead", which carries no style and never should.
            m = re.search(r"<" + tag + r"(?=[\s>])", html)
            self.assertIsNotNone(m, f"missing <{tag}>")
            self.assertTrue(html[m.start():].startswith(f"<{tag} style=\""),
                            f"<{tag}> has no inline style")

    def test_headings_clamp_at_h4(self):
        html = mail.render("##### deep\n\n###### deeper\n")
        self.assertIn("<h4", html)
        self.assertNotIn("<h5", html)
        self.assertNotIn("<h6", html)

    def test_markdown_link_becomes_anchor(self):
        html = mail.render("See [the filing](https://sec.gov/x?a=1&b=2).")
        self.assertIn('href="https://sec.gov/x?a=1&amp;b=2"', html)
        self.assertIn(">the filing</a>", html)

    def test_task_list_items_become_glyphs(self):
        html = mail.render("- [x] done\n- [ ] todo\n")
        self.assertIn("☑", html)
        self.assertIn("☐", html)
        self.assertNotIn("<input", html)

    def test_ampersand_and_quotes_escape(self):
        html = mail.render('P/E "rich" & rising, 5 > 3')
        self.assertIn("&amp;", html)
        self.assertIn("&quot;", html)
        self.assertIn("&gt;", html)

    def test_streaming_table_header_without_separator_terminates(self):
        # A header row can arrive before its |---| separator. The loop must
        # consume the line rather than spin on it.
        html = mail.render("| Level | Value |\n")
        self.assertIn("Level", html)

    def test_empty_input_is_empty_output(self):
        self.assertEqual(mail.render(""), "")
        self.assertEqual(mail.render(None), "")


# ── Sanitising model-authored HTML ──────────────────────────────────────────

class SanitiseTests(unittest.TestCase):
    def test_script_and_style_dropped_with_contents(self):
        html = mail.render("<div>keep<script>alert(1)</script>"
                           "<style>body{display:none}</style>gone?</div>")
        assert_inert(self, html)
        self.assertIn("keep", html)
        self.assertNotIn("alert(1)", html)
        self.assertNotIn("display:none", html)

    def test_event_handler_attributes_never_emitted(self):
        for src in ('<p onclick="evil()">x</p>',
                    '<div onmouseover="evil()">x</div>',
                    '<p>text <b onclick="evil()">b</b></p>'):
            html = mail.render(src)
            assert_inert(self, html, src)

    def test_dangerous_hrefs_refused(self):
        # Wrapped in a block tag so these take the HTML path. A bare inline <a>
        # does not -- see test_bare_inline_anchor_is_escaped_not_honoured.
        for src in ('<p><a href="javascript:alert(1)">x</a></p>',
                    '<p><a href="data:text/html,<b>x</b>">x</a></p>',
                    '<div><a href="vbscript:msgbox(1)">x</a></div>',
                    "[click](javascript:alert(1))",
                    "[click](data:text/html;base64,PHN2Zz4=)"):
            html = mail.render(src)
            assert_inert(self, html, src)
            self.assertNotIn("href=", html, f"{src}: emitted an href")

    def test_safe_hrefs_kept(self):
        for scheme in ("https://x.test/a", "http://x.test/a", "mailto:a@x.test",
                       "/agents?job=1", "#section"):
            html = mail.render(f'<p><a href="{scheme}">x</a></p>')
            self.assertIn(f'href="{scheme}"', html, scheme)

    def test_bare_inline_anchor_is_escaped_not_honoured(self):
        # <a> is absent from the inline-restore list on purpose: restoring it
        # would mean honouring an attribute without vetting it. The link is lost
        # and shown as text, which is the safe direction to fail in.
        html = mail.render('text <a href="https://x.test">link</a> more')
        assert_inert(self, html)
        self.assertNotIn("href=", _ESCAPED.sub("", html))
        self.assertIn("&lt;a href=", html)

    def test_unknown_tags_unwrap_keeping_text(self):
        html = mail.render("<div><font color=red>visible</font></div>")
        self.assertIn("visible", html)
        self.assertNotIn("<font", html.lower())

    def test_closing_tag_gets_no_style_attribute(self):
        # </b style="..."> is not a closing tag; a client that repairs it guesses.
        html = mail.render("plain <b>bold</b> end")
        self.assertIn("</b>", html)
        self.assertNotRegex(html, r"</\w+\s+style=")

    def test_unbalanced_input_is_closed_out(self):
        html = mail.render("<table><tr><td><b>x</td><td>y</td></tr></table>")
        self.assertEqual(html.count("<td"), html.count("</td"))
        self.assertEqual(html.count("<b "), html.count("</b>"))

    def test_numeric_span_attributes_validated(self):
        html = mail.render('<table><tr><td colspan="2 onload=x">c</td></tr></table>')
        assert_inert(self, html)
        self.assertNotIn("colspan", html)
        ok = mail.render('<table><tr><td colspan="2">c</td></tr></table>')
        self.assertIn('colspan="2"', ok)

    def test_markdown_path_survives_one_stray_div(self):
        # Deciding on the *first* tag, not "contains HTML", keeps a Markdown
        # report that mentions a <div> on the Markdown path.
        html = mail.render("# Real heading\n\ntext <div>x</div> more\n")
        self.assertIn("<h1", html)


# ── Assembling the message ──────────────────────────────────────────────────

class BuildTests(unittest.TestCase):
    def test_subject_carries_ticker_decision_and_date(self):
        subject, _, _ = mail.build(make_job())
        self.assertIn("NVDA", subject)
        self.assertIn("BUY", subject)
        # Long form, not ISO, matching the daily broadcast's subject convention
        # in routes._build_daily_email_cache.
        self.assertIn("August 29, 2026", subject)
        zh_subject, _, _ = mail.build(make_job(lang="zh"))
        self.assertIn("2026年8月29日", zh_subject)

    def test_chinese_report_gets_chinese_chrome(self):
        subject, html, _ = mail.build(make_job(lang="zh"))
        self.assertIn("分析完成", subject)
        self.assertIn("打开完整报告", html)
        self.assertIn("投资组合经理", html)          # role name, localised

    def test_english_report_uses_english_role_names(self):
        _, html, _ = mail.build(make_job())
        self.assertIn("Portfolio Manager", html)
        self.assertIn("Market Analyst", html)

    def test_deep_link_points_at_the_job(self):
        _, html, text = mail.build(make_job(), link_base="https://trade-agents.com")
        self.assertIn("https://trade-agents.com/agents?job=abc123def4567890", html)
        self.assertIn("https://trade-agents.com/agents?job=abc123def4567890", text)

    def test_no_report_returns_none(self):
        self.assertIsNone(mail.build(make_job(report="")))
        self.assertIsNone(mail.build(make_job(report=None)))
        self.assertIsNone(mail.build(make_job(report="   \n  ")))

    def test_text_alternative_is_the_raw_markdown(self):
        job = make_job()
        _, _, text = mail.build(job)
        self.assertIn(job["report"].strip()[:60], text)

    def test_degraded_and_recovered_are_stated(self):
        _, html, _ = mail.build(make_job(degraded=True,
                                         fallback_models=["gemini-2.5-flash"]))
        self.assertIn("gemini-2.5-flash", html)
        _, html2, _ = mail.build(make_job(recovered=True))
        self.assertIn("recovered", html2.lower())

    def test_decision_chip_is_colour_coded(self):
        buy = mail.build(make_job(decision="BUY"))[1]
        sell = mail.build(make_job(decision="SELL"))[1]
        hold = mail.build(make_job(decision="HOLD"))[1]
        self.assertIn("#4ade80", buy)
        self.assertIn("#f87171", sell)
        self.assertIn("#fbbf24", hold)

    def test_missing_decision_omits_the_chip(self):
        _, html, _ = mail.build(make_job(decision=None))
        self.assertNotIn("border-radius:999px", html)

    def test_decision_text_is_escaped_in_chip_and_subject(self):
        subject, html, _ = mail.build(make_job(decision='BUY <script>x</script>'))
        assert_inert(self, html)
        self.assertNotIn("<script", subject)

    def test_no_unescaped_report_text_reaches_the_shell(self):
        nasty = ('# T\n\n<script>alert(1)</script>\n\n'
                 '<img src=x onerror=alert(1)>\n\n### Portfolio Manager\n\nBUY\n')
        _, html, _ = mail.build(make_job(report=nasty, ticker='<b>X</b>'))
        assert_inert(self, html)


# ── Localisation ────────────────────────────────────────────────────────────

class LocalisationTests(unittest.TestCase):
    """The email is written in the language the *report* was written in.

    That language is frozen at submit time (agents.submit stores `language`/
    `lang`), so it is the reader's own choice from the moment they queued the run
    — and pairing a Chinese report with English chrome would be worse than
    either. _t() falls back to English for a missing key, which is the right
    thing at runtime and exactly why a missing translation needs a test: it would
    otherwise ship silently.
    """

    def test_no_string_is_missing_a_translation(self):
        en = set(mail._STR["en"])
        for code, table in mail._STR.items():
            self.assertEqual(en, set(table), f"{code} key set differs from en")
            for key, value in table.items():
                self.assertTrue(str(value).strip(), f"{code}.{key} is empty")

    def test_translations_are_actually_translated(self):
        # Guards the copy-paste failure: a zh entry left as its English text.
        same = [k for k, v in mail._STR["zh"].items()
                if v == mail._STR["en"][k] and k not in ("html_lang",)]
        self.assertEqual(same, [], f"untranslated zh keys: {same}")

    def test_format_placeholders_match_across_languages(self):
        # A zh string missing {models} or {mins} renders a sentence with a hole
        # in it, and .format() silently accepts the extra keyword.
        holes = re.compile(r"\{(\w+)\}")
        for key, en_value in mail._STR["en"].items():
            for code, table in mail._STR.items():
                self.assertEqual(
                    sorted(holes.findall(en_value)),
                    sorted(holes.findall(table[key])),
                    f"{code}.{key} placeholders differ from en")

    def test_unknown_language_code_falls_back_to_english(self):
        for code in ("fr", "", None, "EN", "zh-TW"):
            _, html, _ = mail.build(make_job(lang=code))
            self.assertIn("AI research report", html, repr(code))

    def test_zh_is_selected_by_the_report_language(self):
        _, html, _ = mail.build(make_job(lang="zh"))
        self.assertIn("AI 研究报告", html)
        self.assertNotIn("AI research report", html)

    def test_html_lang_attribute_is_set(self):
        self.assertIn('<html lang="en"', mail.build(make_job())[1])
        self.assertIn('<html lang="zh-CN"', mail.build(make_job(lang="zh"))[1])

    def test_dates_are_written_the_local_way(self):
        _, en_html, _ = mail.build(make_job(date="2026-08-29"))
        self.assertIn("August 29, 2026", en_html)
        _, zh_html, _ = mail.build(make_job(date="2026-08-29", lang="zh"))
        self.assertIn("2026年8月29日", zh_html)

    def test_malformed_dates_degrade_to_the_stored_string(self):
        for bad in ("", "not-a-date", "2026-13-01", "2026-08", "20260829"):
            built = mail.build(make_job(date=bad))
            self.assertIsNotNone(built, repr(bad))

    def test_elapsed_time_is_localised(self):
        self.assertIn("31 min", mail.build(make_job(elapsed_sec=1834))[1])
        self.assertIn("31 分钟", mail.build(make_job(elapsed_sec=1834, lang="zh"))[1])
        self.assertIn("45s", mail.build(make_job(elapsed_sec=45))[1])
        self.assertIn("45 秒", mail.build(make_job(elapsed_sec=45, lang="zh"))[1])

    def test_missing_elapsed_omits_the_clause(self):
        for value in (None, "", "abc"):
            _, html, _ = mail.build(make_job(elapsed_sec=value))
            self.assertNotIn("Completed in", html)

    def test_role_names_and_team_dividers_are_localised(self):
        _, html, _ = mail.build(make_job(lang="zh"))
        self.assertIn("市场分析师", html)          # role, from agent_roles.ROLES
        self.assertIn("投资组合经理", html)
        self.assertIn("分析师团队报告", html)      # team divider, team_label_zh
        self.assertNotIn("Market Analyst", html)

    def test_advisories_are_localised(self):
        job = make_job(lang="zh", degraded=True, fallback_models=["gemini-2.5-flash"])
        _, html, _ = mail.build(job)
        self.assertIn("已用完当日额度", html)
        self.assertIn("gemini-2.5-flash", html)
        _, html2, _ = mail.build(make_job(lang="zh", recovered=True))
        self.assertIn("由实时流恢复", html2)

    def test_clip_notice_is_localised(self):
        _, html, _ = mail.build(make_job(lang="zh", report=build_report(8)))
        self.assertIn("邮件仅显示到此处", html)

    def test_text_alternative_is_localised_too(self):
        _, _, text = mail.build(make_job(lang="zh"))
        self.assertIn("AI 研究报告", text)
        self.assertIn("决策", text)
        self.assertIn("打开完整报告", text)


# ── The Gmail clip budget ───────────────────────────────────────────────────
class BudgetTests(unittest.TestCase):
    def _roles_in(self, html: str) -> list[str]:
        return re.findall(r">([A-Za-z][A-Za-z \-]+?)</span></div>", html)

    def test_small_report_arrives_whole(self):
        _, html, _ = mail.build(make_job(report=build_report(1)))
        self.assertLess(len(html), mail._HTML_BUDGET)
        self.assertNotIn(CLIP_MARKER, html)
        self.assertIn("Market Analyst", html)
        self.assertIn("Portfolio Manager", html)

    def test_budget_holds_for_an_enormous_report(self):
        for size in (3, 8, 20):
            _, html, _ = mail.build(make_job(report=build_report(size)))
            self.assertLess(len(html), 102_000,
                            f"per_section={size} would be clipped by Gmail")

    def test_clip_is_announced_with_a_link(self):
        _, html, _ = mail.build(make_job(report=build_report(8)))
        self.assertIn(CLIP_MARKER, html)
        self.assertIn("/agents?job=", html)

    def test_decision_survives_any_clip(self):
        # The point of reserving it: it is the last section emitted, so an
        # in-order budget walk drops the answer and keeps the analysts.
        for size in (3, 8, 20):
            _, html, _ = mail.build(make_job(
                report=build_report(size,
                                    decision_body="**BUY**\n\nEntry 178, stop 165.\n")))
            self.assertIn("Portfolio Manager", html, f"per_section={size}")
            self.assertIn("Entry 178, stop 165", html, f"per_section={size}")

    def test_clip_drops_from_the_middle_not_the_end(self):
        _, html, _ = mail.build(make_job(report=build_report(8)))
        roles = self._roles_in(html)
        self.assertIn("Market Analyst", roles)
        self.assertEqual(roles[-1], "Portfolio Manager")

    def test_a_single_huge_section_still_sends(self):
        report = ("### Market Analyst\n\n" + _para(60)
                  + "### Portfolio Manager\n\nBUY\n")
        built = mail.build(make_job(report=report))
        self.assertIsNotNone(built)
        self.assertIn("Portfolio Manager", built[1])

    def test_report_with_no_role_headings_still_sends(self):
        built = mail.build(make_job(report="Just prose, no headings at all."))
        self.assertIsNotNone(built)
        self.assertIn("Just prose", built[1])


# ── Recipient guards, the kill switch, and sending once ─────────────────────

class GuardTests(unittest.TestCase):
    def test_selftest_runs_are_never_mailed(self):
        self.assertIsNone(mail._recipient(make_job(selftest=True)))

    def test_only_plausible_addresses_are_accepted(self):
        for bad in ("", None, "   ", "nobody", "no@domain", "a b@x.test", "@x.test"):
            self.assertIsNone(mail._recipient(make_job(user=bad)), repr(bad))
        self.assertEqual(mail._recipient(make_job(user="a.b+c@x.co.uk")),
                         "a.b+c@x.co.uk")

    def test_enabled_requires_a_from_address(self):
        import os
        old = dict(os.environ)
        try:
            os.environ.pop("AGENTS_EMAIL_REPORT", None)
            os.environ.pop("SES_FROM_EMAIL", None)
            self.assertFalse(mail.enabled())
            os.environ["SES_FROM_EMAIL"] = "from@x.test"
            self.assertTrue(mail.enabled())
            for off in ("0", "false", "no", "off", "OFF"):
                os.environ["AGENTS_EMAIL_REPORT"] = off
                self.assertFalse(mail.enabled(), off)
        finally:
            os.environ.clear()
            os.environ.update(old)


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        _agents_stub.JOB_DIR = pathlib.Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def test_claim_succeeds_once_and_then_never(self):
        self.assertTrue(mail._claim("job1"))
        self.assertFalse(mail._claim("job1"))
        self.assertFalse(mail._claim("job1"))

    def test_release_allows_a_retry(self):
        self.assertTrue(mail._claim("job2"))
        mail._release("job2")
        self.assertTrue(mail._claim("job2"))

    def test_claims_are_per_job(self):
        self.assertTrue(mail._claim("job3"))
        self.assertTrue(mail._claim("job4"))

    def test_marker_is_not_mistakable_for_a_job_record(self):
        # agents._record_paths() globs *.json and would read a marker as a job.
        mail._claim("job5")
        self.assertEqual(list(pathlib.Path(self._tmp.name).glob("*.json")), [])


class NotifyTests(unittest.TestCase):
    """notify() end to end, with SES stubbed at the boto3 boundary."""

    def setUp(self):
        import os
        self._tmp = tempfile.TemporaryDirectory()
        _agents_stub.JOB_DIR = pathlib.Path(self._tmp.name)
        self._env = dict(os.environ)
        os.environ["SES_FROM_EMAIL"] = "from@x.test"
        os.environ.pop("AGENTS_EMAIL_REPORT", None)
        self.sent: list[dict] = []
        self.fail_next = False
        outer = self

        class _Client:
            def send_email(self, **kw):
                if outer.fail_next:
                    raise RuntimeError("SES refused")
                outer.sent.append(kw)

        self._boto = types.ModuleType("boto3")
        self._boto.client = lambda *a, **k: _Client()
        self._saved_boto = sys.modules.get("boto3")
        sys.modules["boto3"] = self._boto

    def tearDown(self):
        import os
        if self._saved_boto is None:
            sys.modules.pop("boto3", None)
        else:
            sys.modules["boto3"] = self._saved_boto
        os.environ.clear()
        os.environ.update(self._env)
        self._tmp.cleanup()

    def test_finished_run_is_mailed_once(self):
        job = make_job()
        mail.notify(job)
        mail.notify(job)
        mail.notify(job)
        self.assertEqual(len(self.sent), 1)
        msg = self.sent[0]
        self.assertEqual(msg["Destination"]["ToAddresses"], ["reader@example.com"])
        self.assertEqual(msg["Source"], "from@x.test")
        self.assertIn("Html", msg["Message"]["Body"])
        self.assertIn("Text", msg["Message"]["Body"])

    def test_unfinished_and_failed_runs_are_not_mailed(self):
        for status in ("queued", "running", "error"):
            mail.notify(make_job(id=f"j-{status}", status=status))
        self.assertEqual(self.sent, [])

    def test_reportless_and_anonymous_runs_are_not_mailed(self):
        mail.notify(make_job(id="j-empty", report=""))
        mail.notify(make_job(id="j-anon", user="nobody"))
        mail.notify(make_job(id="j-self", selftest=True))
        self.assertEqual(self.sent, [])

    def test_kill_switch_stops_it(self):
        import os
        os.environ["AGENTS_EMAIL_REPORT"] = "0"
        mail.notify(make_job())
        self.assertEqual(self.sent, [])

    def test_a_failed_send_releases_the_claim_so_reap_can_retry(self):
        job = make_job()
        self.fail_next = True
        mail.notify(job)
        self.assertEqual(self.sent, [])
        self.fail_next = False
        mail.notify(job)
        self.assertEqual(len(self.sent), 1)

    def test_notify_never_raises_on_garbage(self):
        for bad in (None, {}, {"status": "done"}, {"id": "x", "status": "done"}):
            mail.notify(bad)
        self.assertEqual(self.sent, [])

    def test_a_render_failure_does_not_escape(self):
        # A report object that explodes on use stands in for a renderer bug: the
        # run is already durable by this point and must not be disturbed.
        class Boom(str):
            def strip(self):
                raise ValueError("boom")

        mail.notify(make_job(report=Boom("x")))
        self.assertEqual(self.sent, [])

    def test_background_send_is_dispatched(self):
        import threading
        before = threading.active_count()
        mail.notify(make_job(), background=True)
        for t in threading.enumerate():
            if t.name.startswith("agent-mail-"):
                t.join(timeout=5)
        self.assertEqual(len(self.sent), 1)
        self.assertLessEqual(threading.active_count(), before + 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
