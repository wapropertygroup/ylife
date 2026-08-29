"""Navigation placement checks for the private assets page."""
from pathlib import Path
import unittest

from flask import Flask, render_template


BASE = (Path(__file__).parents[1] / "ystocker" / "templates" / "base.html")


class AssetsNavigationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.template = BASE.read_text(encoding="utf-8")

    def test_assets_is_not_a_primary_navigation_link(self) -> None:
        desktop_nav = self.template.split("<!-- Desktop nav -->", 1)[1].split(
            "<!-- Refresh button + Language toggle -->", 1)[0]
        self.assertNotIn("main.assets_page", desktop_nav)

        mobile_primary = self.template.split("<!-- Mobile menu -->", 1)[1].split(
            "<!-- Mobile auth -->", 1)[0]
        self.assertNotIn("main.assets_page", mobile_primary)

    def test_assets_links_live_only_in_signed_in_account_ui(self) -> None:
        self.assertEqual(self.template.count("main.assets_page"), 2)
        self.assertIn('data-account-assets="desktop"', self.template)
        self.assertIn('data-account-assets="mobile"', self.template)
        for marker in ('data-account-assets="desktop"', 'data-account-assets="mobile"'):
            before = self.template[:self.template.index(marker)]
            self.assertGreater(before.rfind("{% if current_user.email %}"),
                               before.rfind("{% endif %}"))

    def test_desktop_account_control_is_an_accessible_menu(self) -> None:
        self.assertIn('aria-haspopup="menu"', self.template)
        self.assertIn('id="accountMenu" x-show="accountOpen" role="menu"',
                      self.template)

    def test_server_render_omits_assets_links_for_anonymous_user(self) -> None:
        app = Flask(__name__, template_folder=str(BASE.parent))
        app.secret_key = "test"
        app.jinja_env.globals["url_for"] = (
            lambda endpoint, **_kwargs: "/" + endpoint.replace(".", "/"))
        common = {"brand_name": "TradeAgents", "cache_bust": "test",
                  "peer_groups": [], "google_client_id": "",
                  "agent_embedded": False}
        with app.test_request_context("/"):
            anonymous = render_template(
                "base.html", current_user={"email": "", "name": "", "picture": ""},
                **common)
            signed_in = render_template(
                "base.html", current_user={"email": "u@example.com", "name": "User",
                                           "picture": ""}, **common)
        self.assertNotIn("data-account-assets=", anonymous)
        self.assertEqual(signed_in.count("data-account-assets="), 2)
        self.assertIn('id="accountMenu"', signed_in)


if __name__ == "__main__":
    unittest.main()
