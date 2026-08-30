"""
Tests for the /agents model + thinking picker.

Two things are being defended here, and both fail *quietly* in production if they
regress:

1. **An unsupported thinking level must be unrepresentable.** TradingAgents does
   not validate one. ``google_client.py`` remaps ``minimal`` on Pro to ``low``
   and forwards everything else verbatim, so ``medium`` on Pro reaches the API
   and 400s -- minutes into a run, after the credit was spent.
2. **A per-job model choice must beat the inherited environment.** ``_child_env``
   builds the child's environment with ``setdefault`` for the shared knobs, which
   means "whatever this process inherited wins". A choice written with
   ``setdefault`` would be accepted by the UI, recorded on the job, shown on the
   finished report, and ignored by the process that ran it.

No app, no network, no subprocess.
"""
from __future__ import annotations

import os
import re
import unittest
from pathlib import Path

from ystocker import agent_models, agents


class ResolveTests(unittest.TestCase):
    """resolve() is the whole boundary: a client string in, model ids out."""

    def test_empty_choice_is_server_default(self):
        self.assertIsNone(agent_models.resolve(""))

    def test_unknown_choice_falls_back_rather_than_raising(self):
        # Model ids churn -- gemini-3.1-pro-preview is a *preview* id. A reader
        # whose browser restored a retired key must still get a run.
        self.assertIsNone(agent_models.resolve("google-pro-2019"))
        self.assertIsNone(agent_models.resolve("../../etc/passwd"))

    def test_a_model_id_is_not_accepted_as_a_choice(self):
        # The client sends table keys, never ids. If an id resolved, the door
        # this design closes would be open again.
        self.assertIsNone(agent_models.resolve("gemini-3.1-pro-preview"))

    def test_known_choice_yields_catalog_ids(self):
        got = agent_models.resolve("google-pro")
        self.assertEqual(got["provider"], "google")
        self.assertEqual(got["deep_model"], "gemini-3.1-pro-preview")
        self.assertEqual(got["quick_model"], "gemini-3.1-pro-preview")
        self.assertEqual(got["thinking"], "high")
        self.assertEqual(got["model_choice"], "google-pro")

    def test_pro_never_receives_medium(self):
        """The 400. Pro accepts low/high and medium is *not* remapped upstream."""
        self.assertEqual(agent_models.resolve("google-pro", "medium")["thinking"],
                         "high")

    def test_pro_never_receives_minimal(self):
        """Accepted upstream only by being silently rewritten to "low"."""
        self.assertEqual(agent_models.resolve("google-pro", "minimal")["thinking"],
                         "high")

    def test_pro_honours_its_own_levels(self):
        self.assertEqual(agent_models.resolve("google-pro", "low")["thinking"], "low")
        self.assertEqual(agent_models.resolve("google-pro", "high")["thinking"], "high")

    def test_flash_takes_all_four(self):
        for level in ("minimal", "low", "medium", "high"):
            with self.subTest(level=level):
                self.assertEqual(
                    agent_models.resolve("google-flash", level)["thinking"], level)

    def test_thinking_is_case_and_space_insensitive(self):
        self.assertEqual(agent_models.resolve("google-flash", " HIGH ")["thinking"],
                         "high")

    def test_provider_without_a_thinking_knob_gets_none(self):
        # Only google/openai/anthropic are read by _get_provider_kwargs. Carrying
        # a level for DeepSeek would claim the run did something it did not.
        for level in ("", "high", "medium", "nonsense"):
            with self.subTest(level=level):
                self.assertEqual(
                    agent_models.resolve("deepseek-pro", level)["thinking"], "")

    def test_garbage_thinking_clamps_to_the_choice_default(self):
        self.assertEqual(
            agent_models.resolve("google-flash", "ultra")["thinking"], "high")
        self.assertEqual(
            agent_models.resolve("google-lite", "ultra")["thinking"], "low")


class TableInvariantTests(unittest.TestCase):
    """Properties every row must hold, so a new row cannot ship broken."""

    def test_default_thinking_is_always_accepted(self):
        for key, spec in agent_models.CHOICES.items():
            with self.subTest(choice=key):
                if spec["thinking"]:
                    self.assertIn(spec["thinking_default"], spec["thinking"])
                else:
                    self.assertEqual(spec["thinking_default"], "")

    def test_every_thinking_level_is_a_known_level(self):
        for key, spec in agent_models.CHOICES.items():
            for level in spec["thinking"]:
                with self.subTest(choice=key, level=level):
                    self.assertIn(level, agent_models.THINKING_ORDER)

    def test_every_provider_has_a_credential_env_var(self):
        for key, spec in agent_models.CHOICES.items():
            with self.subTest(choice=key):
                self.assertIn(spec["provider"], agent_models.PROVIDER_KEY_ENV)

    def test_choice_keys_carry_no_version(self):
        """A key outliving a model rename is what keeps stored preferences and
        historical jobs readable across a catalog bump."""
        for key in agent_models.CHOICES:
            with self.subTest(choice=key):
                self.assertNotRegex(key, r"\d", f"{key} embeds a version")

    def test_no_choice_offers_the_custom_sentinel(self):
        """``custom`` is the CLI's "prompt me" marker and leaks into the
        catalog's known-model sets; sent as a model id it is not a model."""
        for key, spec in agent_models.CHOICES.items():
            with self.subTest(choice=key):
                self.assertNotEqual(spec["deep"], "custom")
                self.assertNotEqual(spec["quick"], "custom")


class CatalogAgreementTests(unittest.TestCase):
    """Every id offered must exist in TradingAgents' own catalog.

    An id outside it does not fail fast: ``base_client.warn_if_unknown_model``
    emits a RuntimeWarning saying "Continuing anyway", and the run then dies in
    the vendor SDK. Checked by reading the catalog as text rather than importing
    it, because TradingAgents lives in a separate virtualenv that this app's
    interpreter cannot import from.
    """

    def setUp(self):
        catalog = (Path(agents.TA_DIR) / "tradingagents" / "llm_clients"
                   / "model_catalog.py")
        if not catalog.is_file():
            self.skipTest(f"no TradingAgents checkout at {agents.TA_DIR}")
        self.text = catalog.read_text(encoding="utf-8")

    def test_offered_ids_appear_in_the_catalog(self):
        for key, spec in agent_models.CHOICES.items():
            for role in ("deep", "quick"):
                with self.subTest(choice=key, role=role):
                    self.assertIn(f'"{spec[role]}"', self.text,
                                  f"{spec[role]} is not in model_catalog.py")

    def test_the_apps_own_defaults_appear_too(self):
        for model in (agents.DEFAULT_DEEP_MODEL, agents.DEFAULT_QUICK_MODEL):
            with self.subTest(model=model):
                self.assertIn(f'"{model}"', self.text)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in
                       ("GOOGLE_API_KEY", "GEMINI_API_KEY", "DEEPSEEK_API_KEY")}
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_absent_credential_is_unavailable(self):
        self.assertFalse(agent_models.provider_available("google"))
        self.assertFalse(agent_models.provider_available("deepseek"))

    def test_google_is_satisfied_by_either_key_name(self):
        # This app's secret is GEMINI_API_KEY; TradingAgents wants
        # GOOGLE_API_KEY. Checking one name reports a runnable box as broken.
        os.environ["GEMINI_API_KEY"] = "x"
        self.assertTrue(agent_models.provider_available("google"))
        os.environ.pop("GEMINI_API_KEY")
        os.environ["GOOGLE_API_KEY"] = "x"
        self.assertTrue(agent_models.provider_available("google"))

    def test_whitespace_is_not_a_credential(self):
        os.environ["DEEPSEEK_API_KEY"] = "   "
        self.assertFalse(agent_models.provider_available("deepseek"))

    def test_unknown_provider_is_unavailable_not_assumed_open(self):
        self.assertFalse(agent_models.provider_available("anthropic"))
        self.assertFalse(agent_models.provider_available(""))

    def test_options_public_reports_availability(self):
        os.environ["GEMINI_API_KEY"] = "x"
        by_key = {o["key"]: o for o in agent_models.options_public()}
        self.assertTrue(by_key["google-pro"]["available"])
        self.assertFalse(by_key["deepseek-pro"]["available"])
        # The client rebuilds the thinking control from this, so it has to travel.
        self.assertEqual(by_key["google-pro"]["thinking"], ["low", "high"])
        self.assertEqual(by_key["deepseek-pro"]["thinking"], [])


class ChoiceForTests(unittest.TestCase):
    def test_every_choice_round_trips(self):
        for key, spec in agent_models.CHOICES.items():
            with self.subTest(choice=key):
                self.assertEqual(
                    agent_models.choice_for(spec["provider"], spec["deep"],
                                            spec["quick"]),
                    key)

    def test_unmatched_triple_is_empty_not_nearest(self):
        # Empty makes the page offer an explicit "server default" row. Returning
        # a near match would highlight a row that misstates what a run will do.
        self.assertEqual(
            agent_models.choice_for("google", "gemini-3.1-pro-preview",
                                    "gemini-3.5-flash"), "")
        self.assertEqual(agent_models.choice_for("openai", "gpt-5.5", "gpt-5.5"), "")


class ResolveModelsTests(unittest.TestCase):
    """agents.resolve_models() adds the server-default arm on top of resolve()."""

    def test_no_choice_yields_this_deployments_defaults(self):
        got = agents.resolve_models("")
        self.assertEqual(got["model_choice"], "")
        self.assertEqual(got["provider"], agents.DEFAULT_PROVIDER)
        self.assertEqual(got["deep_model"], agents.DEFAULT_DEEP_MODEL)
        self.assertEqual(got["quick_model"], agents.DEFAULT_QUICK_MODEL)
        self.assertEqual(got["thinking"], agents.DEFAULT_THINKING)

    def test_unknown_choice_yields_defaults_too(self):
        self.assertEqual(agents.resolve_models("nope")["provider"],
                         agents.DEFAULT_PROVIDER)

    def test_always_returns_the_full_shape(self):
        # _run and the job record both index these unconditionally.
        for choice in ("", "google-pro", "deepseek-flash", "junk"):
            with self.subTest(choice=choice):
                self.assertEqual(
                    set(agents.resolve_models(choice)),
                    {"model_choice", "provider", "deep_model", "quick_model",
                     "thinking"})


class JobModelsTests(unittest.TestCase):
    def test_record_without_a_provider_falls_back(self):
        """Every job written before the picker existed."""
        self.assertEqual(agents.job_models({})["provider"], agents.DEFAULT_PROVIDER)
        self.assertEqual(agents.job_models({"provider": ""})["deep_model"],
                         agents.DEFAULT_DEEP_MODEL)

    def test_recorded_triple_is_used_verbatim(self):
        job = {"model_choice": "google-lite", "provider": "google",
               "deep_model": "gemini-3.5-flash",
               "quick_model": "gemini-3.1-flash-lite", "thinking": "low"}
        self.assertEqual(agents.job_models(job), job)

    def test_recorded_triple_wins_over_the_table(self):
        """A queued job must run what its record claims, even if the table moved
        under it -- otherwise the report names a configuration that never ran."""
        job = {"model_choice": "google-pro", "provider": "google",
               "deep_model": "gemini-9.9-retired",
               "quick_model": "gemini-9.9-retired", "thinking": "low"}
        got = agents.job_models(job)
        self.assertEqual(got["deep_model"], "gemini-9.9-retired")
        self.assertEqual(got["thinking"], "low")


class ChildEnvTests(unittest.TestCase):
    """The setdefault trap, which is the one that fails silently in production."""

    _VARS = ("TRADINGAGENTS_LLM_PROVIDER", "TRADINGAGENTS_DEEP_THINK_LLM",
             "TRADINGAGENTS_QUICK_THINK_LLM", "TRADINGAGENTS_GOOGLE_THINKING_LEVEL")

    def setUp(self):
        self._saved = {k: os.environ.get(k) for k in self._VARS}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def _pin(self):
        """A box whose unit file pins the models, as production may."""
        os.environ["TRADINGAGENTS_LLM_PROVIDER"] = "google"
        os.environ["TRADINGAGENTS_DEEP_THINK_LLM"] = "gemini-3.1-pro-preview"
        os.environ["TRADINGAGENTS_QUICK_THINK_LLM"] = "gemini-3.1-pro-preview"
        os.environ["TRADINGAGENTS_GOOGLE_THINKING_LEVEL"] = "high"

    def test_per_job_choice_beats_an_inherited_pin(self):
        self._pin()
        env = agents._child_env("English",
                                models=agent_models.resolve("google-lite", "low"))
        self.assertEqual(env["TRADINGAGENTS_DEEP_THINK_LLM"], "gemini-3.5-flash")
        self.assertEqual(env["TRADINGAGENTS_QUICK_THINK_LLM"],
                         "gemini-3.1-flash-lite")
        self.assertEqual(env["TRADINGAGENTS_GOOGLE_THINKING_LEVEL"], "low")

    def test_provider_switch_beats_an_inherited_pin(self):
        self._pin()
        env = agents._child_env("English",
                                models=agent_models.resolve("deepseek-pro"))
        self.assertEqual(env["TRADINGAGENTS_LLM_PROVIDER"], "deepseek")
        self.assertEqual(env["TRADINGAGENTS_DEEP_THINK_LLM"], "deepseek-v4-pro")
        self.assertEqual(env["TRADINGAGENTS_QUICK_THINK_LLM"], "deepseek-v4-flash")

    def test_thinkingless_provider_clears_the_inherited_level(self):
        self._pin()
        env = agents._child_env("English",
                                models=agent_models.resolve("deepseek-pro"))
        self.assertNotIn("TRADINGAGENTS_GOOGLE_THINKING_LEVEL", env)

    def test_no_models_preserves_the_pin(self):
        """The pre-picker call shape, still used by any record without a provider."""
        self._pin()
        env = agents._child_env("English")
        self.assertEqual(env["TRADINGAGENTS_DEEP_THINK_LLM"],
                         "gemini-3.1-pro-preview")

    def test_language_is_still_assigned(self):
        env = agents._child_env("Simplified Chinese (简体中文)",
                                models=agent_models.resolve("google-flash"))
        self.assertEqual(env["TRADINGAGENTS_OUTPUT_LANGUAGE"],
                         "Simplified Chinese (简体中文)")

    def test_no_model_id_reaches_the_env_unless_the_table_named_it(self):
        """The property the opaque-key design buys: nothing a client typed can
        appear as a model id in the child's environment."""
        env = agents._child_env("English", models=agents.resolve_models("google-pro"))
        offered = {s[r] for s in agent_models.CHOICES.values() for r in ("deep", "quick")}
        offered |= {agents.DEFAULT_DEEP_MODEL, agents.DEFAULT_QUICK_MODEL}
        self.assertIn(env["TRADINGAGENTS_DEEP_THINK_LLM"], offered)
        self.assertIn(env["TRADINGAGENTS_QUICK_THINK_LLM"], offered)


class KillSwitchTests(unittest.TestCase):
    def setUp(self):
        self._saved = os.environ.get("AGENTS_MODEL_CHOICE")

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("AGENTS_MODEL_CHOICE", None)
        else:
            os.environ["AGENTS_MODEL_CHOICE"] = self._saved

    def test_on_by_default(self):
        os.environ.pop("AGENTS_MODEL_CHOICE", None)
        self.assertTrue(agents.model_choice_enabled())

    def test_off_forms(self):
        for raw in ("0", "false", "no", "FALSE"):
            with self.subTest(raw=raw):
                os.environ["AGENTS_MODEL_CHOICE"] = raw
                self.assertFalse(agents.model_choice_enabled())

    def test_anything_else_is_on(self):
        for raw in ("1", "true", "yes", ""):
            with self.subTest(raw=raw):
                os.environ["AGENTS_MODEL_CHOICE"] = raw
                self.assertTrue(agents.model_choice_enabled())


class PublishedFieldsTests(unittest.TestCase):
    """The picker adds fields to the job record; two surfaces are allowlists."""

    def test_showcase_publishes_what_ran_but_not_the_table_key(self):
        for field in ("provider", "deep_model", "quick_model", "thinking"):
            with self.subTest(field=field):
                self.assertIn(field, agents._PUBLIC_FIELDS)
        self.assertNotIn("model_choice", agents._PUBLIC_FIELDS)

    def test_share_publishes_the_same_set(self):
        from ystocker import share
        for field in ("provider", "deep_model", "quick_model", "thinking"):
            with self.subTest(field=field):
                self.assertIn(field, share._SHAREABLE_JOB_FIELDS)
        self.assertNotIn("model_choice", share._SHAREABLE_JOB_FIELDS)

    def test_neither_surface_leaks_the_owner(self):
        from ystocker import share
        self.assertNotIn("user", agents._PUBLIC_FIELDS)
        self.assertNotIn("user", share._SHAREABLE_JOB_FIELDS)


if __name__ == "__main__":
    unittest.main()
