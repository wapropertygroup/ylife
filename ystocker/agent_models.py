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
The accepted values differ **per model**, and the mismatch is silent: a level the
model does not take is a 400 from the API, minutes into a paid run. Measured
against the Gemini API on 2026-10-04, model by model: Pro and Flash 3.8 refuse
``minimal`` and take ``low``/``medium``/``high``; 3.5 Flash and both Flash Lites
take all four. (Pro used to refuse ``medium`` too; it no longer does.)
``google_client.py`` quietly rewrites ``minimal`` to ``low`` for Pro and 3.8+.
Rather than reproduce any of that in the UI and hope, each choice carries the
exact set it accepts and :func:`resolve` clamps anything else to that choice's
default, so an out-of-range level is unrepresentable downstream. A choice that
pairs two models carries the levels *both* accept, since one level is sent to
each.

Providers with no thinking knob at all get an empty set. Only ``google``,
``openai`` and ``anthropic`` are read by ``tradingagents.llm_clients.build_llm_kwargs``;
for everything else the parameter is inert, so offering the control would be a
lie about what the run does.

Why four rows, and no thinking control on the page (2026-10-07)
-----------------------------------------------------------------
Asked for: "simplify the TradeAgents choices". There were eight rows and a
thinking menu beside them. Four remain, one per thing a reader picks for: free
(DeepSeek V4 Flash), the best report (Gemini 3.1 Pro deciding, 3.8 Flash
researching), speed for many tickers (3.8 Flash throughout) and the lowest cost
of a paid run (DeepSeek V4 Pro deciding, V4 Flash researching). Pro in every seat
was dropped because the analysts' many tool calls gain little from it and cost
the most; the Flash Lite tiers and V4 Pro in every seat added rows without a
reason to pick them. A retired key resolves to its nearest kept row
(:data:`RETIRED`), never to the deployment default, which is the most expensive
configuration there is.

Each row runs at its own ``thinking_default`` (high on both Gemini rows), so the
page offers no thinking menu. :func:`resolve` still clamps a level a client sends,
so an old tab or a script cannot reach the vendor with one the model refuses.

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

# Thinking levels, cheapest first: the order a reader reads them as a scale
# wherever a run's level is shown. Presentation only; the per choice
# ``thinking`` tuple below is the authority on what is *accepted*.
THINKING_ORDER = ("minimal", "low", "medium", "high")

# Pro's and Flash 3.8's real range. "minimal" is absent because the API refuses it
# (400) and google_client silently rewrites it to "low" -- offering a control
# whose value is quietly changed is worse than not offering it.
_PRO_THINKING = ("low", "medium", "high")
_FLASH38_THINKING = ("low", "medium", "high")

# provider / deep_think_llm / quick_think_llm / accepted thinking / default.
# ``name`` is the models alone, for prose that names the free choice (the plans
# page, the note under the picker), so it follows AGENTS_FREE_MODEL.
#
# Every model id here is copied from tradingagents/llm_clients/model_catalog.py,
# never invented -- an id outside that catalog only warns and then fails in the
# SDK. ``label`` is the English fallback the template inlines; ``label_key`` is
# the i18n key that relabels it on a language toggle.
#: The only choice a free run may use (asked for 2026-10-04): DeepSeek V4 Flash,
#: the cheapest model that writes a whole report. Every other row is for a paid
#: run -- Pro, a trial, a VIP, or a run paid with a purchased credit -- and is
#: shown locked to everyone else. AGENTS_FREE_MODEL names another key; an
#: unknown one falls back here rather than to the deployment default, which is
#: the most expensive configuration there is.
FREE_CHOICE_DEFAULT = "deepseek-flash"

CHOICES: dict[str, dict[str, Any]] = {
    # ── Free ──────────────────────────────────────────────────────────────
    "deepseek-flash": {
        # deepseek-v4-flash, not the bare "deepseek-flash" DeepSeek now lists:
        # the API serves both as the same model, but TradingAgents keys
        # DeepSeek's thinking-model handling (no tool_choice, reasoning echoed
        # back each turn) on the versioned id, and the bare one would fall
        # through to generic defaults. The versioned id is what 29 finished
        # deepseek-pro runs used for every analyst.
        "provider": "deepseek",
        "deep": "deepseek-v4-flash",
        "quick": "deepseek-v4-flash",
        "thinking": (),
        "thinking_default": "",
        "name": "DeepSeek V4 Flash",
        "label": "DeepSeek V4 Flash — free",
        "label_key": "agents.model_deepseek_flash",
    },
    # ── Paid ──────────────────────────────────────────────────────────────
    "google-pro-flash": {
        # Pro rules, Flash researches: the two rulings on the strongest model,
        # the analysts' many tool calls on the newest Flash. The best report
        # this table offers, and the deployment default (agents.py).
        "provider": "google",
        "deep": "gemini-3.1-pro-preview",
        "quick": "gemini-3.8-flash",
        "thinking": _PRO_THINKING,
        "thinking_default": "high",
        "name": "Gemini 3.1 Pro + 3.8 Flash",
        "label": "Gemini 3.1 Pro + 3.8 Flash — best quality",
        "label_key": "agents.model_google_pro_flash",
    },
    "google-flash": {
        # 3.8 Flash since 2026-10-04 (was 3.5): the key names the tier, not the
        # version, so a stored preference follows the newest Flash. For many
        # tickers at a time: fast, long context, cheap per run.
        "provider": "google",
        "deep": "gemini-3.8-flash",
        "quick": "gemini-3.8-flash",
        "thinking": _FLASH38_THINKING,
        "thinking_default": "high",
        "name": "Gemini 3.8 Flash",
        "label": "Gemini 3.8 Flash — fast",
        "label_key": "agents.model_google_flash",
    },
    "deepseek-pro": {
        # v4-pro is deep-only in the catalog and v4-flash is the quick model, so
        # this pairing is the catalog's own division of labour rather than a
        # judgement made here. The cheapest paid row.
        "provider": "deepseek",
        "deep": "deepseek-v4-pro",
        "quick": "deepseek-v4-flash",
        "thinking": (),
        "thinking_default": "",
        "name": "DeepSeek V4 Pro + V4 Flash",
        "label": "DeepSeek V4 Pro + V4 Flash — best value",
        "label_key": "agents.model_deepseek_pro",
    },
}

#: Rows retired on 2026-10-07, each to the kept row nearest it. A reader's stored
#: preference or a stale tab may still send one; it runs on the successor rather
#: than falling through to the deployment default.
RETIRED: dict[str, str] = {
    "google-pro": "google-pro-flash",          # Pro in every seat
    "google-flash-lite": "google-flash",       # 3.8 Flash + 3.5 Flash Lite
    "google-lite": "google-flash",             # 3.5 Flash + 3.1 Flash Lite
    "deepseek-pro-max": "deepseek-pro",        # V4 Pro in every seat
}


def free_choice() -> str:
    """The key a free run uses. Read per call so a changed AGENTS_FREE_MODEL
    needs only a restart; an unknown key is ignored, never the default."""
    key = os.environ.get("AGENTS_FREE_MODEL", "").strip()
    key = RETIRED.get(key, key)
    return key if key in CHOICES else FREE_CHOICE_DEFAULT


def free_choice_name() -> str:
    """The free choice's models by name, e.g. "DeepSeek V4 Flash"."""
    return CHOICES[free_choice()]["name"]


def tier(choice: str) -> str:
    """"free" for the free run's choice, "pro" for every other row."""
    return "free" if (choice or "").strip() == free_choice() else "pro"


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
    key = (choice or "").strip()
    key = RETIRED.get(key, key)
    spec = CHOICES.get(key)
    if not spec:
        return None
    accepted = spec["thinking"]
    want = (thinking or "").strip().lower()
    level = want if want in accepted else spec["thinking_default"]
    return {
        "model_choice": key,
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
            "tier": tier(key),
            "name": spec["name"],
        })
    return out
