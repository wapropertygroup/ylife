"""
ystocker.agent_models
~~~~~~~~~~~~~~~~~~~~~
The menu of LLM configurations a ``/agents`` run may be launched with, and the
resolution of a caller's request into one of them.

Why a table of opaque keys rather than a model id from the client
----------------------------------------------------------------
TradingAgents does **not** fail fast on an unknown model. ``base_client.py``
emits a ``RuntimeWarning`` ("Continuing anyway") and the run then dies inside
the vendor SDK on its first call -- which here means minutes after the credit
was spent, with the reader watching a progress bar. There is no pre-flight
validator for a (provider, model, thinking) triple either: ``validators.py``
checks only the pair, returns ``True`` for any provider it does not know, and
lets the ``"custom"`` sentinel through.

So the client sends a *key* -- ``"google-pro"`` -- and this module maps it to
model ids. A string the client invented cannot reach the child environment at
all, which is a stronger guarantee than validating one that can. It also keeps
the vendor catalog off the wire, so the UI is not a way to enumerate it.

The keys carry no version, deliberately. ``gemini-3.1-pro-preview`` is a preview
id and will be renamed; ``google-pro`` outlives that rename, so a bumped catalog
does not invalidate every reader's stored preference or make historical jobs
unreadable.

Why thinking depth is a property of the choice, not a free parameter
-------------------------------------------------------------------
The accepted values differ **per model**, and the mismatch is silent. Gemini Pro
accepts ``low``/``high``; Flash also accepts ``minimal``/``medium``.
``google_client.py`` remaps only one of the four mismatches -- ``minimal`` on Pro
becomes ``low`` -- and forwards ``medium`` on Pro verbatim, so it reaches the API
and 400s. Rather than reproduce that asymmetry in the UI and hope, each choice
carries the exact set it accepts and :func:`resolve` clamps anything else to that
choice's default. An out-of-range thinking value is therefore unrepresentable
downstream.

Providers with no thinking knob at all get an empty set. Only ``google``,
``openai`` and ``anthropic`` are read by ``TradingAgentsGraph._get_provider_kwargs``;
for everything else the parameter is inert, so offering the control would be a
lie about what the run does.

Why the deep and quick roles are named separately per choice
------------------------------------------------------------
``quick_think_llm`` backs all seven analysts plus the researchers, trader and
risk debators, and is the role that calls ``bind_tools``. ``deep_think_llm`` is
used only by the research manager and portfolio manager. The catalog splits its
lists along exactly that line, so a "cheapest" tier is properly *Flash deciding,
Lite fetching* rather than one id in both slots -- the final decision keeps a
capable model while the many tool calls get the cheap one.
"""
from __future__ import annotations

import os
from typing import Any, Optional

# Which environment variable holds each provider's credential.
#
# ``google`` lists both names because this app's own secret is GEMINI_API_KEY
# (SSM ``/ystocker/GEMINI_API_KEY``) while TradingAgents resolves the google
# provider through GOOGLE_API_KEY; agents._child_env bridges the two. Checking
# only one name would report the provider unavailable on a box that can in fact
# run it.
PROVIDER_KEY_ENV: dict[str, tuple[str, ...]] = {
    "google": ("GOOGLE_API_KEY", "GEMINI_API_KEY"),
    "deepseek": ("DEEPSEEK_API_KEY",),
}

# Thinking levels, cheapest first. Display order in the UI, and the order a
# reader reads them as a scale -- so this list is presentation, while the per
# choice ``thinking`` tuple below is the authority on what is *accepted*.
THINKING_ORDER = ("minimal", "low", "medium", "high")

_GOOGLE_THINKING = ("minimal", "low", "medium", "high")
# Pro's real range. "minimal" is absent because google_client silently rewrites
# it to "low" -- offering a control whose value the vendor quietly changes is
# worse than not offering it. "medium" is absent because it is *not* rewritten:
# it is forwarded as-is and rejected.
_PRO_THINKING = ("low", "high")

# provider / deep_think_llm / quick_think_llm / accepted thinking / default.
#
# Every model id here is copied from tradingagents/llm_clients/model_catalog.py,
# never invented -- an id outside that catalog only warns and then fails in the
# SDK. ``label`` is the English fallback the template inlines; ``label_key`` is
# the i18n key that relabels it on a language toggle.
CHOICES: dict[str, dict[str, Any]] = {
    "google-pro": {
        "provider": "google",
        "deep": "gemini-3.1-pro-preview",
        "quick": "gemini-3.1-pro-preview",
        "thinking": _PRO_THINKING,
        "thinking_default": "high",
        "label": "Gemini 3.1 Pro — highest quality",
        "label_key": "agents.model_google_pro",
    },
    "google-flash": {
        "provider": "google",
        "deep": "gemini-3.5-flash",
        "quick": "gemini-3.5-flash",
        "thinking": _GOOGLE_THINKING,
        "thinking_default": "high",
        "label": "Gemini 3.5 Flash — faster",
        "label_key": "agents.model_google_flash",
    },
    "google-lite": {
        # Flash decides, Lite fetches. Lite appears only in the catalog's
        # "quick" list, and it is the analysts' many tool calls that make a run
        # expensive -- putting Lite in the deep slot too would cheapen the one
        # output the reader actually acts on to save the least of the spend.
        "provider": "google",
        "deep": "gemini-3.5-flash",
        "quick": "gemini-3.1-flash-lite",
        "thinking": _GOOGLE_THINKING,
        # Low, not high: this tier is chosen for cost, and a high thinking level
        # would spend back most of what the cheaper model saved.
        "thinking_default": "low",
        "label": "Gemini 3.5 Flash + 3.1 Flash Lite — cheapest",
        "label_key": "agents.model_google_lite",
    },
    "deepseek-pro": {
        # v4-pro is deep-only in the catalog and v4-flash is the quick model, so
        # this pairing is the catalog's own division of labour rather than a
        # judgement made here.
        "provider": "deepseek",
        "deep": "deepseek-v4-pro",
        "quick": "deepseek-v4-flash",
        "thinking": (),
        "thinking_default": "",
        "label": "DeepSeek V4 Pro",
        "label_key": "agents.model_deepseek_pro",
    },
    "deepseek-flash": {
        "provider": "deepseek",
        "deep": "deepseek-v4-flash",
        "quick": "deepseek-v4-flash",
        "thinking": (),
        "thinking_default": "",
        "label": "DeepSeek V4 Flash — fastest",
        "label_key": "agents.model_deepseek_flash",
    },
}


def provider_available(provider: str) -> bool:
    """Whether a credential for this provider is present in the environment.

    A provider with no key is not merely degraded: TradingAgents raises on the
    missing variable before the first token, so a run started against one is a
    wasted slot. The UI uses this to disable the option rather than let a reader
    discover it by spending a run.
    """
    return any(os.environ.get(name, "").strip()
               for name in PROVIDER_KEY_ENV.get(provider, ()))


def resolve(choice: str, thinking: str = "") -> Optional[dict[str, str]]:
    """The models a run should use, or ``None`` for "whatever the box defaults to".

    ``None`` is returned for both an empty choice and an *unrecognised* one, and
    the caller is expected to substitute its own defaults. Falling back rather
    than raising is deliberate: model ids churn, so a reader whose browser
    restored a preference for a key that has since been retired gets a run on
    the server default instead of a hard failure they cannot clear without
    knowing to reload. What actually ran is recorded on the job either way, so
    the substitution is visible rather than silent.

    ``thinking`` outside the chosen model's accepted set is clamped to that
    model's default, which is what keeps an unsupported level from reaching the
    vendor. For a provider with no thinking knob the result is always ``""``.
    """
    spec = CHOICES.get((choice or "").strip())
    if not spec:
        return None
    accepted = spec["thinking"]
    want = (thinking or "").strip().lower()
    level = want if want in accepted else spec["thinking_default"]
    return {
        "model_choice": (choice or "").strip(),
        "provider": spec["provider"],
        "deep_model": spec["deep"],
        "quick_model": spec["quick"],
        "thinking": level,
    }


def choice_for(provider: str, deep: str, quick: str) -> str:
    """The choice key matching a (provider, deep, quick) triple, ``""`` if none.

    Lets the page preselect the control on whatever the box is configured for,
    instead of asserting a default that a deployment may have overridden through
    ``TRADINGAGENTS_DEEP_THINK_LLM`` and friends. An unmatched triple returns
    empty, which the UI shows as an explicit "server default" row -- naming the
    configuration it cannot offer as a choice is honest, where quietly
    highlighting the nearest row would misreport what a run will do.
    """
    for key, spec in CHOICES.items():
        if (spec["provider"] == provider and spec["deep"] == deep
                and spec["quick"] == quick):
            return key
    return ""


def options_public() -> list[dict[str, Any]]:
    """The menu, for the template and the client.

    Carries ``thinking`` per option because the client has to rebuild the
    thinking control when the model changes -- the accepted set is a property of
    the model, and shipping it here keeps the two ends from disagreeing about
    which levels are legal for Pro.
    """
    out: list[dict[str, Any]] = []
    for key, spec in CHOICES.items():
        out.append({
            "key": key,
            "label": spec["label"],
            "label_key": spec["label_key"],
            "provider": spec["provider"],
            "deep_model": spec["deep"],
            "quick_model": spec["quick"],
            "thinking": list(spec["thinking"]),
            "thinking_default": spec["thinking_default"],
            "available": provider_available(spec["provider"]),
        })
    return out
