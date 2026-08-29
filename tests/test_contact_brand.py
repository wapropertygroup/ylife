"""The /contact address is brand-derived — no Flask app, no network.

The app answers on stock.li-family.us and on trade-agents.com, and /contact is a
`mailto:` form, so the address is baked into the HTML at render time rather than
posted anywhere. That makes a wrong address silent in both directions: nothing
errors, the visitor's mail client just opens addressed to the other brand, and
whether the mail arrives at all depends on a mailbox nobody on this side can see.

So two things are pinned:

1. **The address is not a literal in the template.** That is the regression a
   future edit reintroduces, and it fails invisibly — the page renders fine.
2. **Address and brand name come from the same verdict.** They are derived from
   one `is_ta` in one context processor precisely so the masthead cannot say
   TradeAgents while the form says admin@li-family.us.

Source-grepped rather than imported, matching test_report_email.py: importing the
package pulls in Flask and boto3, and create_app() would start the background
refresh threads.
"""

from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).parents[1]
INIT = (ROOT / "ystocker" / "__init__.py").read_text(encoding="utf-8")
CONTACT = (ROOT / "ystocker" / "templates" / "contact.html").read_text(encoding="utf-8")


def _const(name: str) -> str | None:
    m = re.search(rf"^{name}\s*=\s*['\"]([^'\"]+)['\"]", INIT, re.M)
    return m.group(1) if m else None


class ContactAddressConstants(unittest.TestCase):
    def test_both_addresses_declared_at_module_scope(self):
        self.assertEqual(_const("CONTACT_EMAIL"), "admin@li-family.us")
        self.assertEqual(_const("TA_CONTACT_EMAIL"), "admin@trade-agents.com")

    def test_ta_address_is_on_the_ta_domain(self):
        # The whole point of the second address is that it matches the domain
        # serving the page; an admin@ on some third domain would be worse than
        # not splitting at all.
        m = re.search(r"^TA_HOSTS\s*=\s*\{([^}]*)\}", INIT, re.M)
        self.assertIsNotNone(m, "TA_HOSTS is no longer a module-scope literal")
        hosts = set(re.findall(r"['\"]([^'\"]+)['\"]", m.group(1)))
        domain = (_const("TA_CONTACT_EMAIL") or "").split("@")[-1]
        self.assertTrue(
            any(h == domain or h.endswith("." + domain) for h in hosts),
            f"TA_CONTACT_EMAIL domain {domain!r} is not one of TA_HOSTS {hosts}",
        )

    def test_address_is_chosen_by_the_same_verdict_as_the_brand_name(self):
        # One is_ta, one context processor. Two independent host tests would be
        # free to disagree, which is the failure this guards.
        block = re.search(
            r"def _inject_brand\(\):.*?(?=\n    @app\.|\n    def |\Z)", INIT, re.S
        )
        self.assertIsNotNone(block, "_inject_brand() not found")
        body = block.group(0)
        self.assertIn("brand_email", body)
        self.assertIn("TA_CONTACT_EMAIL if is_ta else CONTACT_EMAIL", body)


class ContactTemplate(unittest.TestCase):
    def test_no_hardcoded_address(self):
        found = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", CONTACT)
        self.assertEqual(found, [], f"contact.html hardcodes an address: {found}")

    def test_every_mailto_uses_the_context_variable(self):
        mailtos = re.findall(r"mailto:([^\"']*)", CONTACT)
        self.assertTrue(mailtos, "contact.html has no mailto: at all")
        for target in mailtos:
            normalised = re.sub(r"\s+", "", target)
            self.assertEqual(
                normalised, "{{brand_email}}", f"stray mailto target: {target!r}"
            )

    def test_the_visible_address_is_rendered_too(self):
        # Not just the href: the anchor text is what a reader copies by hand, and
        # a stale one there is the same bug with no redness.
        self.assertGreaterEqual(
            CONTACT.count("{{ brand_email }}"), 3,
            "expected brand_email in the href, the link text, and the form action",
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
