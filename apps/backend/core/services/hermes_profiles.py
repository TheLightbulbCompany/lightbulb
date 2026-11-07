"""The per-profile Hermes config writer — the replacement for ``write_openclaw_config``.

A Hermes profile IS a complete ``$HERMES_HOME`` (report 01 §3): its ``config.yaml`` is merged
over ``DEFAULT_CONFIG`` ALONE, with no inheritance from the root home. So every generated file
must be complete, produced from one template plus per-agent overrides — the same shape
``build_openclaw_config_dict`` has today, minus about two thirds of the keys (``_emit_81_shape``
gates, ``meta.lastTouchedVersion``, the Chrome recipe, the node trust store, the lightbulb channel
and bindings, the hooks ingress) which have no Hermes equivalent and are DROPPED, not ported.

Secrets NEVER land here (spec §4.5): ``config.yaml`` carries ``${VAR}`` references only and the
per-profile ``.env`` is the store. The non-negotiables an agent could otherwise edit out of its own
config live in the managed overlay (Task 11), which wins at the leaf after env expansion.

Fix round 1, C3 — three places a value can live, and this module owns exactly one of them:

======================  =========================================  =========================
Scope                   Members                                     Who writes it
======================  =========================================  =========================
Runtime environment     AWS_REGION, S3_BUCKET,                          Task 17, in the AgentCore
(process-wide, one per  identifiers (LIGHTBULB_SECRET_ID, ISOL8_*)      runtime's environmentVariables
container)                                                              (already-merged lane D's
                                                                         ``agentcore_runtime.build_runtime_env``)
Boot secret             hookToken, gatewayToken, bedrockBearer          The backend, into the ONE
(process-wide, resolved                                                 Secrets Manager blob
at bridge startup)                                                      ``LIGHTBULB_SECRET_ID``
                                                                         points at (M1)
Profile .env            per-profile secret VALUES only                 THIS module
(per-agent, from S3)    (``write_agent_profile``/``build_profile_env``)
======================  =========================================  =========================

``agent/secret_scope.py`` builds the profile scope as a Python dict ONLY — it never touches
``os.environ`` — and the bridge's direct ``AIAgent`` construction (``run_agent.py``,
``agent/agent_init.py``) installs no scope at all, so anything this module writes into ``.env``
that Hermes/botocore expect to find via ``os.environ`` (the runtime-environment row above) would
silently never be seen. That is why the endpoint URL and region do NOT belong here even though an
earlier version of this file put them here.
"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

import yaml

from core.config import settings
from core.dynamodb import run_in_thread
from core.services.heartbeat_checklist import HEARTBEAT_FILE, default_checklist


def premium_model_ref(provider_choice: str, byo_provider: str | None) -> str | None:
    """The premium model ref reserved for the CEO agent (Friday), per provider
    path — or ``None`` when there is no premium tier to reserve.

    The default agent runs on the cheaper standard tier; only the CEO is pinned
    to the top tier. ``chatgpt_oauth`` returns ``None`` because it's a flat-rate
    plan — there's no cheaper tier to demote the default to, so the CEO inherits
    the same model as everyone else.

    Returns ``<provider>/<id>``; ``_strip_provider_prefix`` takes the id half.
    """
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            # Anthropic-DIRECT id — no Bedrock "-v1" suffix / date-stamp. The CEO
            # pin has NO per-agent fallback, so a ref the catalog cannot resolve
            # does not degrade — it fails every Friday run.
            return "anthropic/claude-opus-5"
        if byo_provider == "openai":
            return "openai/gpt-5.6-sol"
    return None


def cheap_model_ref(provider_choice: str, byo_provider: str | None) -> str | None:
    """The cheapest model ref per provider path, routed to pure-overhead aux
    operations — or ``None`` when there is no cheaper tier to route to.

    History compaction, memory-flush and the maintenance heartbeat are overhead
    that doesn't need the premium default model. Routing them to the cheapest
    tier cuts spend — compaction especially, since it processes the largest
    contexts.

    ``chatgpt_oauth`` returns ``None``: its agents run on the Codex runtime,
    which uses native server-side compaction and does NOT honour an
    OpenClaw-side summarizer override — the aux call fails, compaction never
    runs, and the turn dies on a context overflow. ``None`` lets aux work
    inherit the primary, which the OAuth token can actually invoke.

    Returns ``<provider>/<id>``; ``_strip_provider_prefix`` takes the id half.
    """
    if provider_choice == "byo_key":
        if byo_provider == "anthropic":
            return "anthropic/claude-haiku-4-5"
        if byo_provider == "openai":
            # byo_key openai is the DIRECT OpenAI API (not Codex) — the owner's
            # key CAN invoke the mini tier as a summarizer.
            return "openai/gpt-5.4-mini"
    return None


#: The complete Hermes 0.21.3 auxiliary task list -- NOT "every task we pin": ``vision`` is the
#: one entry ``auxiliary_tasks_block`` (below) deliberately never writes, for any provider. The
#: rest of this note describes why the other 17 ARE pinned. Hermes's auto-chain is main provider
#: -> OpenRouter -> Nous Portal -> ... (auxiliary_client.py:3-6); an unpinned task on a
#: platform-billed path is egress to a
#: vendor we have no account with, and the Nous lane needs no credential of ours (``nous.guest:
#: true`` in the defaults). ``fallback_chain: []`` is the actual fence: verified in the image
#: (``auxiliary_client.py:6820-6837``) — an explicit, PINNED provider (ours) that fails to build a
#: client tries ONLY its own ``fallback_chain`` and, finding it empty, RAISES
#: (``RuntimeError("Provider '<x>' is set in config.yaml but no API key was found...")``); the
#: OpenRouter/Nous auto-chain is walked ONLY when the resolved provider is already "auto" (or
#: "openrouter"/"custom") — a pinned provider never reaches it. ``free_only`` is a cost knob ON
#: that auto-chain, not a fence of its own — it still lets a ":free" OpenRouter SKU through, which
#: only matters for the "auto" case this pin deliberately avoids. One residual: ``vision``
#: specifically has ITS OWN unconditional "fall back to auto vision backends" branch a few lines
#: above the general one (``resolve_vision_provider_client(provider="auto", ...)`` whenever the
#: pinned vision client fails to build), so a failed Bedrock vision call can still reach the auto
#: chain even with ``fallback_chain: []`` — the other 17 tasks cannot. ``bedrock.discovery.enabled:
#: false`` and a present ``AWS_BEARER_TOKEN_BEDROCK`` are what keep the pinned client from failing
#: to build in the first place, which is what actually closes that residual in practice.
#:
#: Fix round 1, C1: this is the literal, complete 18-name list of every provider-bearing key in
#: Hermes 0.21.3's ``hermes_cli.config_defaults.DEFAULT_CONFIG["auxiliary"]`` (every value built
#: via the module's own ``_aux()`` helper, plus ``review``/``background_review``/``moa_reference``/
#: ``moa_aggregator`` which inline the same shape) — verified directly against the image, not
#: against the bridge's test fixture (that fixture only modeled 4 of the 18 and was mistaken for
#: the full set in the first pass). Re-verify after any Hermes version bump with:
#:   docker run --rm --entrypoint sed lightbulb/hermes-spike:m1-rc4 -n '679,750p' \
#:     /opt/hermes/hermes_cli/config_defaults.py
#: Nothing else fills a missing task: the bridge leaves an unpinned task on Hermes's ``auto``
#: (main provider), and only validates the full list for managed-gate profiles. So a task Hermes
#: adds is unpinned until a human re-runs the command above and updates this tuple.
AUXILIARY_TASKS = (
    "approval",
    "background_review",
    "compression",
    "curator",
    "goal_judge",
    "kanban_decomposer",
    "mcp",
    "memory_query_rewrite",
    "moa_aggregator",
    "moa_reference",
    "monitor",
    "profile_describer",
    "review",
    "skills_hub",
    "title_generation",
    "triage_specifier",
    "tts_audio_tags",
    "vision",
)

#: Hard backstop: a match blocks even under ``--yolo``/mode=off (config_defaults.py:1582-1583).
APPROVALS_DENY = ("rm -rf /", "mkfs*", "dd if=* of=/dev/*", ":(){:|:&};:")

#: The ``TOOL_DENY_BASE`` port. canvas/music_generate have no Hermes toolset; cron is a switch
#: (``cron.allow_agent_scheduling``, below), not a toolset (report 01 §2.7). dashboard has no
#: equivalent switch this module emits at all — see the ``dashboard`` note below.
#: clarify: the bridge wires no clarify callback, so Hermes's ask-the-user tool only answers "not
#: available" and burns a step; the agent asks in its reply instead.
_DISABLED_TOOLSETS = ("cronjob", "tts", "video_gen", "clarify")

#: M4-R3: the opt-in is a flat allow-list (``plugins.enabled``), never ``plugins.entries`` —
#: Hermes's ``hermes_cli/plugins_discovery.py::gate_manifest`` reads only ``plugins.enabled``.
_LIGHTBULB_PLUGINS = ("lightbulb",)

#: ``skills.external_dirs`` — public (not ``_``-prefixed): the ops entry point
#: (``scripts/reapply_hermes_skills_dirs.py``) imports this same list so a re-apply can never
#: drift from what a fresh profile gets.
#:
#: List order here is COSMETIC — it does not resolve a naming collision. Verified against the
#: image's ``/opt/hermes/tools/skills_tool.py``: ``_find_all_skills`` (which builds the
#: agent-visible index) advertises a name that exists in more than one dir, but
#: ``_locate_skill``/``skill_view`` then REFUSES to load it
#: (``{"success": false, "error": "Ambiguous skill name '<name>': N skills match…"}``) — a
#: same-named skill in both dirs is worse than shadowing, since the index still offers a name
#: every load then rejects. No collision exists in the founder's profiles today, but the
#: openclaw->Hermes migration copies owner skills into the profile-local dir, and several
#: Hermes-bundled names below are common (``pdf``, ``docx``, ``xlsx``, ``github``, ``notion``,
#: ``systematic-debugging``, …) — a future collision has to be resolved by loading via its
#: categorised path (e.g. ``productivity/pdf``), not by this list's order.
SKILLS_DIR_LIGHTBULB = "/opt/hermes-skills"
#: Hermes's OWN bundled skills (58 of them), shipped inside the ``nousresearch/hermes-agent``
#: base image at its package root (``/opt/hermes``). The real Hermes CLI seeds these into every
#: fresh profile via ``tools.skills_sync.sync_skills`` (run by ``hermes chat``/``hermes profile
#: create``/``hermes update``) — our bridge never runs that CLI seeding step, so without this
#: entry a profile only ever sees the two baked skills above. Hermes's own sync code already
#: defers to an external dir over copying into it, so registering the path is the whole fix; no
#: copy step is needed, and this stays read-only (the image is immutable per profile).
SKILLS_DIR_HERMES_BUNDLED = "/opt/hermes/skills"
SKILLS_EXTERNAL_DIRS = [SKILLS_DIR_LIGHTBULB, SKILLS_DIR_HERMES_BUNDLED]

#: Fix round 1, I4: ONLY the Hermes provider NAME (a namespace this module alone owns — Hermes's
#: own "openai-api" differs from the "openai" ref prefix ``premium_model_ref``/``cheap_model_ref``
#: uses) and the "standard" (non-premium, non-cheap) default-tier id, which has no standalone
#: function anywhere — it only lives inline inside ``build_provider_block``'s ``byo_key`` branch.
#: The premium/cheap ids themselves are NOT duplicated here any more: they come straight from
#: ``premium_model_ref``/``cheap_model_ref`` above, the same functions the rest of this module
#: calls — those two have already broken a hardcoded ref across two catalog rotations (see their
#: own docstrings), and a second local copy is exactly the class of bug that causes.
_BYO_HERMES_PROVIDER = {"anthropic": ("anthropic", "claude-sonnet-5"), "openai": ("openai-api", "gpt-5.4")}


def _strip_provider_prefix(ref: str | None) -> str | None:
    """``"<provider>/<id>"`` -> ``"<id>"``; ``None`` stays ``None``."""
    return ref.split("/", 1)[1] if ref else None


def model_block(provider_choice, byo_provider, is_ceo, *, default_id=None) -> tuple[dict, str, str]:
    """(model block, aux model id, aux provider).

    Public (Q9a fix round 1, M6-R28): this is the ONE place that decides an agent's
    ``config.yaml`` ``model`` key from ``(provider_choice, byo_provider, is_ceo)`` --
    ``build_profile_config`` uses it for a fresh profile write, and ``runtime_rpc``'s
    ``agents.update`` door (an AgentCore role/model change) re-derives the same block
    through this function rather than hand-rolling or trusting a client-supplied ref.

    ``default_id`` (M6 task 10-C) pins the resolved default to a model the caller has ALREADY
    validated against this owner's own ``models.list`` (Hermes's provider catalog). Only
    ``default`` moves: ``provider`` and any ``context_length`` pin still come from here, so a
    picker choice can never invent a provider or silently drop the pin.
    """
    block, aux_model, aux_provider = _derive_model_block(provider_choice, byo_provider, is_ceo)
    return ({**block, "default": default_id} if default_id else block), aux_model, aux_provider


def _derive_model_block(provider_choice, byo_provider, is_ceo) -> tuple[dict, str, str]:
    if provider_choice == "openrouter_managed":
        from core.billing.openrouter_gate_url import gate_url

        model_id = settings.OPENROUTER_MANAGED_MODEL_ID.strip()
        if not model_id:
            raise ValueError("managed OpenRouter route requires a server model")
        return (
            {
                "default": model_id,
                "provider": "custom",
                "base_url": gate_url(),
                "api_key": "${LIGHTBULB_GATE_BEARER}",
                "context_length": 200_000,
            },
            model_id,
            "custom",
        )
    if provider_choice == "byo_key":
        hermes_provider, standard = _BYO_HERMES_PROVIDER[byo_provider or "anthropic"]
        premium = _strip_provider_prefix(premium_model_ref(provider_choice, byo_provider))
        cheap = _strip_provider_prefix(cheap_model_ref(provider_choice, byo_provider))
        default = premium if is_ceo else standard
        block = {"default": default, "provider": hermes_provider}
        if hermes_provider == "openai-api":
            # Verified in the image (tools/vision_tools.py's _TOOL_RESULT_MEDIA_PROVIDERS):
            # Hermes's tool-result image table lists "openai" but not its own "openai-api" id, and
            # no "openai-api" ProviderProfile fills the gap -- without this override, every
            # computer_use screenshot on this route is described by an aux call instead of seen.
            # This is the one correctness-required capability flag; it is NOT part of the
            # auxiliary.vision pin this module otherwise never writes (see auxiliary_tasks_block).
            block["supports_vision"] = True
        return block, cheap or standard, hermes_provider
    if provider_choice != "chatgpt_oauth":
        # No silent fallthrough: an unknown choice (e.g. a retired ``bedrock_claude``) must not
        # land the ChatGPT block on an owner who has no ChatGPT credential.
        raise ValueError(f"unsupported provider_choice {provider_choice!r}")
    # chatgpt_oauth — flat rate, one tier. Task 15's re-review ruling: aux tasks are pinned to
    # the "openai-codex" provider (never "auto") — a real, explicit Hermes aux provider (verified
    # in the image: hermes_cli/auth.py's ProviderConfig, auxiliary_client.py's dedicated
    # resolution branch for it, and its own "openai-codex has no safe default model; callers set
    # auxiliary.<task>.model" comment, confirming an explicit model is the intended usage). "auto"
    # would re-open the OpenRouter/Nous auto-chain (auxiliary_client.py exempts "auto" from the
    # raise a pinned-but-unavailable provider would otherwise hit). Aux inherits the SAME model
    # the primary uses (gpt-5.6-sol): there is no cheaper tier to route aux work to on a flat-rate
    # plan, the same reasoning the model block above already applies.
    return {"default": "gpt-5.6-sol", "provider": "openai-codex"}, "gpt-5.6-sol", "openai-codex"


def auxiliary_tasks_block(model_id: str, provider: str, *, base_url: str = "", api_key: str = "") -> dict:
    """Every ``AUXILIARY_TASKS`` entry EXCEPT ``vision`` pinned to one model + provider.

    The ONE builder for those entries (M6 task 10-C, scan rows 4/23): the fresh-profile write
    below and ``runtime_rpc``'s AgentCore overhead-model door both go through it, so the 17-task
    list and the ``fallback_chain: []`` fence (see ``AUXILIARY_TASKS``' own note -- it is the
    fence, not a preference) can never drift between them.

    ``vision`` is never written, for any provider (owner ruling 2026-09-28: "if we don't need the
    vision model then let's not pin it, we should just use whatever hermes natively uses here").
    Hermes 0.21.3 treats ANY explicit ``auxiliary.vision`` as "describe every image through that
    aux model" -- for computer_use captures, vision_analyze and attached images alike
    (tools/computer_use/vision_routing.py, step 1, which runs before any capability check). A pin
    there turned every screenshot into a 6-14 s text description the main model never saw the
    pixels of (prod, 2026-09-28). Leaving the key out entirely is equivalent to Hermes's own
    unset default (``AUX_VISION_UNSET`` below) -- both fail Hermes's own "explicit override" test
    (``vision_routing._explicit_aux_vision_override`` / ``image_routing._explicit_aux_vision_override``,
    verified in the image: a missing ``auxiliary.vision`` key and an explicit
    ``{provider: auto, model: "", base_url: ""}`` both read as "not explicit") -- so Hermes's own
    "auto" vision routing decides per turn (main provider if it can read images, else
    OpenRouter/Nous/DeepInfra, ``auxiliary_client._vision_auto_route``). This applies uniformly,
    including the managed OpenRouter gate model: no provider is special-cased any more.
    """
    route = {"provider": provider, "model": model_id, "fallback_chain": []}
    if base_url:
        route.update(base_url=base_url, api_key=api_key)
    return {task: dict(route) for task in AUXILIARY_TASKS if task != "vision"}


def stream_only_base_urls(base_url: str) -> list[str]:
    """``auxiliary.stream_only_base_urls`` for a gate-routed profile: the gate's host.

    The openrouter-gate admits only streaming requests (it reads the generation id settlement
    needs from the stream's first chunk). Hermes 0.21.3 sends a plain aux call -- vision_analyze,
    computer_use screenshot descriptions -- non-streaming unless its base URL matches one of these
    substrings (``auxiliary_client._provider_requires_stream``), which then forces ``stream=True``
    and re-aggregates. The host, not the full URL, so a trailing ``/v1`` Hermes adds or strips
    cannot break the match.
    """
    return [urlparse(base_url).netloc] if base_url else []


#: Hermes's own unset ``auxiliary.vision`` (config_defaults ``_aux``). Explicit blanks, not an
#: omitted key, so a deep-merge patch over an old pinned profile (``patch_profile_config`` cannot
#: delete a key, only overwrite it) actually clears a stale pin --
#: ``scripts/reapply_hermes_skills_dirs.py`` is the one writer of this stub; a fresh profile never
#: writes ``vision`` at all (see ``auxiliary_tasks_block`` above).
AUX_VISION_UNSET = {"provider": "auto", "model": "", "base_url": "", "api_key": ""}


#: Hermes's budget for ``memories/USER.md`` (its user-profile store). ``context_import`` merges a
#: pasted business context under this same number, so the two cannot drift.
USER_CHAR_LIMIT = 1375


def build_profile_config(
    *,
    agent_role: str,
    provider_choice: str,
    byo_provider: str | None = None,
    mcp_servers: dict | None = None,
    is_ceo: bool = False,
) -> dict:
    model, aux_model, aux_provider = model_block(provider_choice, byo_provider, is_ceo)
    aux: dict = {
        "free_only": True,
        "transient_retries": 2,
        **auxiliary_tasks_block(
            aux_model, aux_provider, base_url=model.get("base_url", ""), api_key=model.get("api_key", "")
        ),
    }
    if model.get("base_url"):
        aux["stream_only_base_urls"] = stream_only_base_urls(model["base_url"])
    config: dict = {
        "model": model,
        "auxiliary": aux,
        "toolsets": ["hermes-cli"],
        "max_concurrent_sessions": 3,
        "agent": {
            "disabled_toolsets": list(_DISABLED_TOOLSETS),
            "run_budget_seconds": 1800,
            "stall_guards": True,
        },
        "approvals": {
            "mode": "off",
            "cron_mode": "deny",
            "single_query_mode": "deny",
            "unattended_mode": "approve",
            "deny": list(APPROVALS_DENY),
        },
        "cron": {"allow_agent_scheduling": False},
        # I5 (fix round 1): the real "dashboard" block (config_defaults.py:931) has no "enabled"
        # field and nothing reads one — emitting {"enabled": False} was a false sense of coverage.
        # Nothing launches the dashboard in the microVM regardless, so no key is emitted at all.
        "memory": {
            "memory_enabled": True,
            "user_profile_enabled": True,
            "write_approval": False,
            "provider": "",  # built-in: never pays for embeddings on a platform-billed path
            "memory_char_limit": 2200,
            "user_char_limit": USER_CHAR_LIMIT,
        },
        # No "guard_agent_created" (fix round 1, minor): it is a real key (config_defaults.py:1373,
        # default False, gates skill_manager_tool's terminal() execution path), but the platform
        # has no signal that belongs there — "listening" is a per-room chat mode the backend
        # enforces, unrelated to skill-creation gating. Leaving it unset keeps Hermes's own default.
        "skills": {
            "write_approval": False,
            "external_dirs": list(SKILLS_EXTERNAL_DIRS),
        },
        "sessions": {
            "auto_prune": True,
            "retention_days": 90,
            "auto_archive": True,
            "vacuum_after_prune": True,
        },
        "terminal": {"backend": "local"},
        "mcp_servers": dict(mcp_servers or {}),
        "plugins": {"enabled": list(_LIGHTBULB_PLUGINS)},
    }
    return config


_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_PLAIN_RE = re.compile(r"^[A-Za-z0-9_./:@+=-]*$")


#: Fix round 1, C2: the profile-scope env var each BYO provider's own credential resolver reads
#: (``agent/anthropic_credentials.py``'s ``_first_env("ANTHROPIC_API_KEY")``; the OpenAI-compatible
#: path falls back to ``OPENAI_API_KEY`` the same way, per ``config_defaults.py``'s own
#: ``api_key falls back to OPENAI_API_KEY`` comments on the aux/delegation blocks).
_BYO_ENV_KEY = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY"}


def build_profile_env(
    *,
    env_vars: dict[str, str],
    byo_provider: str | None = None,
    byo_api_key: str | None = None,
    gate_bearer: str | None = None,
) -> dict[str, str]:
    """The profile's ``.env`` — per-profile secret VALUES only (see the module docstring's scope
    table). Never the gate endpoint, region, or a Secrets Manager REFERENCE name: the gate bearer
    arrives via the boot secret and the bridge exports ``AWS_BEARER_TOKEN_BEDROCK`` process-wide;
    ruling M4-R4/R5 cancelled the per-credential ``*_SECRET``-name resolver this function used to
    write, and nothing in ``bridge/`` ever read those names, so every value here is now the real,
    already-resolved secret — not a name for a later lookup.

    ``byo_provider``/``byo_api_key`` are OWNER-level (never read from the Paperclip roster row,
    which carries no such field — see ``write_agent_profile``'s C2 fix) and only emitted when both
    are given, so a non-BYO profile's ``.env`` carries neither key.

    Owner-provisioned values (``env_vars``, from ``build_provisioned_env``) are carried as-is — the
    same rail ``openclaw.json``'s ``env.vars`` used.
    """
    env = {}
    if gate_bearer:  # this agent's own openrouter-gate bearer; the bridge prefers it to the boot one
        env["LIGHTBULB_AGENT_GATE_BEARER"] = gate_bearer
    if byo_api_key:
        env[_BYO_ENV_KEY[byo_provider or "anthropic"]] = byo_api_key
    env.update(env_vars or {})
    return env


def render_dotenv(env: dict[str, str]) -> str:
    """Render Hermes's exact ``.env`` grammar — verified against ``agent/secret_scope.py``'s
    ``load_env_file``/``_parse_env_value``/``_strip_inline_comment`` in the
    ``lightbulb/hermes-spike:m1-rc4`` image (fix round 1, I3):

    * The parser is strictly LINE-based (``text.splitlines()`` runs before any value parsing), so
      a literal newline or carriage return in a value has NO valid encoding at all — it would
      truncate the assignment and reinterpret every following line as a fresh ``KEY=VALUE``.
      Reject those values outright.
    * When quoting is needed, the parser reverses exactly two escapes inside a double-quoted
      value: ``\\"`` and ``\\\\`` — nothing else. Escaping the backslash FIRST (before the quote)
      guarantees any run of backslashes immediately before the closing quote we add is always
      even, so the parser's escape-skip logic can never walk past our closing quote and treat it
      as escaped content (the "trailing backslash breaks the quoting" failure mode).
    """
    lines = []
    for key, value in env.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"illegal dotenv key: {key!r}")
        text = str(value)
        if "\n" in text or "\r" in text:
            raise ValueError(
                f"dotenv value for {key!r} contains a newline; Hermes's .env is line-based "
                "and has no valid encoding for one"
            )
        if _PLAIN_RE.match(text):
            lines.append(f"{key}={text}")
        else:
            escaped = text.replace("\\", "\\\\").replace('"', '\\"')
            lines.append(f'{key}="{escaped}"')
    return "\n".join(lines)


#: OpenClaw spelling -> Hermes spelling. camelCase -> snake_case is the trap (report 01 §2.8).
#: An unrecognised key is DROPPED rather than passed through: Hermes's mcp config is a closed
#: shape and a stray key is a spawn-time error, not a warning. No "oauth" (fix round 1, minor): no
#: producer here ever sets it (the real mcp_remote shape carries auth: "oauth" instead) and
#: Hermes's own OpenClaw->Hermes converter never emits it either.
_MCP_DIRECT = ("command", "args", "env", "cwd", "url", "headers", "auth", "enabled")
_MCP_MS_TO_S = {"requestTimeoutMs": "timeout", "connectionTimeoutMs": "connect_timeout"}
_MCP_SAMPLING = {
    "enabled": "enabled",
    "model": "model",
    "maxTokensCap": "max_tokens_cap",
    "timeout": "timeout",
    "maxRpm": "max_rpm",
}


#: Fix round 1, I1: ``env``/``headers`` values are real, already-resolved credentials — the
#: registry's ``mcp_server`` wiring rail "resolves each env var from the retained Secrets Manager
#: secret" (registry.py:107) — never a reference. Copying them verbatim would put a live API key
#: into an S3-resident ``config.yaml`` that M4-R1 also serves to the settings UI. Every value under
#: these two fields is moved to the returned secrets map and replaced with a ``${VAR}`` reference.
_MCP_SECRET_FIELDS = ("env", "headers")
_NON_IDENT_RE = re.compile(r"[^A-Za-z0-9]+")
# registry.McpRemote.server_block(brokered=True)'s header; bridge/turn.runtime_mcp_env vends the var.
_BROKERED_BEARER_REF = re.compile(r"Bearer \$\{MCP_[A-Z0-9_]+_ACCESS_TOKEN\}")


def _mcp_env_key(server_name: str, var_name: str) -> str:
    """``MCP_<SERVER>_<VAR>``, both segments sanitized to a valid dotenv/shell identifier."""
    server = _NON_IDENT_RE.sub("_", server_name).strip("_").upper() or "SERVER"
    var = _NON_IDENT_RE.sub("_", var_name).strip("_").upper() or "VALUE"
    return f"MCP_{server}_{var}"


#: Where the bridge unpacks an agent's profile inside the runtime (bridge ``sync._profile_dir``:
#: ``$HERMES_HOME/profiles/<agent>``, with ``HERMES_HOME=/opt/data`` baked in the image).
_RUNTIME_PROFILES_DIR = "/opt/data/profiles"


def with_profile_home(servers: dict, agent_id: str) -> dict:
    """``servers`` with every stdio server's ``HOME`` pinned to this agent's profile home.

    Hermes builds a stdio MCP child's env from the PROCESS environment, and one bridge process
    serves every agent, with ``HOME=/`` under AgentCore. A server that reads ``$HOME`` (the
    QuickBooks wrapper reads ``$HOME/quickbooks/.env``) therefore finds nothing and exits at
    startup. The server's own ``env`` is applied last, so pinning ``HOME`` there wins.
    """
    home = f"{_RUNTIME_PROFILES_DIR}/{agent_id}/home"
    return {
        name: {**entry, "env": {**(entry.get("env") or {}), "HOME": home}} if entry.get("command") else entry
        for name, entry in servers.items()
    }


def translate_mcp_servers(servers: dict | None) -> tuple[dict, dict[str, str]]:
    """Hermes ``mcp_servers`` config plus the secret values it references.

    Returns ``(config, secrets)``: ``config`` is what goes into ``config.yaml``'s ``mcp_servers``
    key (every ``env``/``headers`` value replaced by a ``${MCP_<SERVER>_<VAR>}`` reference);
    ``secrets`` is ``{that same name: the real value}``, meant to be merged into the profile
    ``.env`` by the caller (``write_agent_profile``).
    """
    config: dict = {}
    secrets: dict[str, str] = {}
    # Minor (re-review): _mcp_env_key sanitizes to a valid identifier, so two DIFFERENT server
    # names (e.g. "acme-crm" and "acme_crm") can produce the SAME env key. A silent merge would
    # let one server's secret value overwrite the other's with no signal; reject instead.
    env_key_origin: dict[str, tuple[str, str]] = {}
    for name, block in (servers or {}).items():
        entry: dict = {key: block[key] for key in _MCP_DIRECT if key in block}
        for field in _MCP_SECRET_FIELDS:
            values = entry.get(field)
            if isinstance(values, dict):
                moved = {}
                for var_name, value in values.items():
                    if isinstance(value, str) and _BROKERED_BEARER_REF.fullmatch(value):
                        # A brokered MCP bearer is a reference the bridge fills from the turn's
                        # secret scope. Hermes interpolates config ONCE and never expands .env
                        # values, so moving it into .env would send the literal "${...}" and 401.
                        moved[var_name] = value
                        continue
                    env_key = _mcp_env_key(name, var_name)
                    origin = env_key_origin.setdefault(env_key, (name, var_name))
                    if origin != (name, var_name):
                        raise ValueError(
                            f"MCP server names {origin[0]!r} and {name!r} collide on the same "
                            f"sanitized secret name {env_key!r} — rename one of the servers."
                        )
                    secrets[env_key] = value
                    moved[var_name] = f"${{{env_key}}}"
                entry[field] = moved
        for ms_key, s_key in _MCP_MS_TO_S.items():
            if isinstance(block.get(ms_key), int):
                entry[s_key] = block[ms_key] // 1000
        tools = block.get("tools")
        if isinstance(tools, dict):
            entry["tools"] = {k: tools[k] for k in ("include", "exclude") if k in tools}
        sampling = block.get("sampling")
        if isinstance(sampling, dict):
            entry["sampling"] = {v: sampling[k] for k, v in _MCP_SAMPLING.items() if k in sampling}
        config[name] = entry
    return config, secrets


_CEO_LINE = (
    "You are the CEO of this workspace. The other agents report to you; you set direction, "
    "approve spend and answer the owner first."
)


#: First line of a backend-GENERATED ``SOUL.md`` (M6-R48). It is the ONE signal that tells the
#: rename branch (``hermes_provisioning.refresh_agent_soul``) whether it may regenerate this file: the
#: Instructions-tab PUT strips the marker, so once an owner has saved their agent's identity the
#: file is theirs and a later rename never overwrites it. Text carried in verbatim from somewhere
#: else — ``write_agent_profile``'s ``soul_md_override`` (a migrated workspace's own EFS SOUL.md, a
#: marketplace seller's) — is owner content by the same rule and deliberately arrives unmarked.
#: An HTML comment because ``SOUL.md`` is a prompt document: it renders as nothing.
SOUL_GENERATED_MARKER = "<!-- lightbulb:generated -->"


def is_generated_soul(text: str | None) -> bool:
    """True while ``text`` is still the backend-generated ``SOUL.md`` — see the marker above."""
    return bool(text) and text.lstrip().startswith(SOUL_GENERATED_MARKER)


def strip_generated_marker(text: str) -> str:
    """Drop the generated marker — what the Instructions-tab PUT does, which is what makes the
    saved file the owner's. A no-op on text that never carried one."""
    stripped = text.lstrip()
    if stripped.startswith(SOUL_GENERATED_MARKER):
        return stripped[len(SOUL_GENERATED_MARKER) :].lstrip("\n")
    return text


def build_soul_md(agent: dict, *, company_name: str | None = None, front_desk_number: str | None = None) -> str:
    """The profile's ``SOUL.md`` — the agent's identity, from the Paperclip roster row.

    Hermes reads ``$HERMES_HOME/SOUL.md`` per profile (config_home.py:66). There is no
    ``agents.list[*].name`` to carry a display name any more, so the name lives here and in the
    Paperclip row, and nowhere in config.yaml.

    ``front_desk_number`` (the workspace's texting line, given only for the agent that owns it) adds
    the front-desk section.
    """
    name = agent.get("name") or agent["id"]
    role = agent.get("role") or "general"
    # The marker owns the first line, then a blank one: a heading butted straight against an HTML
    # comment is swallowed into the same HTML block by every CommonMark renderer.
    lines = [SOUL_GENERATED_MARKER, "", f"# {name}", ""]
    if agent.get("title"):
        lines += [f"**{agent['title']}**", ""]
    # Never print the Paperclip role code ("cfo", "researcher", "general") -- it reads as noise.
    # A title equal to the name (every starter) would just repeat it.
    title = agent.get("title")
    who = f", {title}" if title and title != name else ""
    where = f" at {company_name}" if company_name else " on the Lightbulb team"
    lines.append(f"You are {name}{who}{where}.")
    if role == "ceo":
        lines.append(_CEO_LINE)
    if agent.get("capabilities"):
        lines += ["", "## What you do", "", agent["capabilities"]]
    soul = "\n".join(lines) + "\n"
    return soul + "\n\n" + front_desk_section(front_desk_number) if front_desk_number else soul


FRONT_DESK_SOUL = """## Front desk

You answer the business's customers by text on {number}. A text that reaches you directly is from a customer. A workspace member's text arrives here as their room message, never through this channel.

- Answer from what the site says and what you have saved. Never invent a price, a slot, a policy or an account detail.
- If you have not learned the business's site yet, say so here and ask for the website; then follow references/front-desk.md to set it up.
- To act on the site, run the adapters you built with OpenCLI (`opencli <site> <command> ...`). Browse only to build or repair an adapter.
- Place an order or booking only after the customer has said yes or paid, always under the customer's own number.
- Payment: create a Stripe payment link for the amount with your Stripe tools and text it. If Stripe is not connected, say the customer pays at the business, or send the site's own checkout link.
- When you do not know: tell the customer you will get back to them, ask the CEO in a DM, and if the CEO cannot answer, raise a Needs-you question with request_approval. Text the answer back when it comes.
- Never text anyone who has not texted this number first. Never give out a member's personal number or email.
- Reply with /app/skills/lightbulb/scripts/lightbulb-text.sh send --reply <Reply-Key>. Keep replies short, one question at a time, and never send email.
"""


def front_desk_section(number: str) -> str:
    return FRONT_DESK_SOUL.format(number=number)


#: The managed scope (/etc/hermes/config.yaml). Hermes merges it AFTER env expansion and it wins at
#: the leaf, so an agent that rewrites its own profile config.yaml cannot undo anything here
#: (managed_scope.py:101-120, docs/design/managed-scope.md §4.1). Only non-negotiables belong
#: here; anything an owner may legitimately change stays in the per-profile file.
#: apps/infra/hermes/managed/config.yaml is a byte-identical copy, asserted by the unit test.
#: I5 (fix round 1): the real key is ``updates.check`` (default True, read at ``banner.py:387`` in
#: the ``lightbulb/hermes-spike:m1-rc4`` image) — ``updates.check_on_start`` has zero occurrences
#: anywhere in that image, so the previous pin was a no-op. Re-verify with:
#:   docker run --rm --entrypoint grep lightbulb/hermes-spike:m1-rc4 -n -B2 -A6 '"updates"' \
#:     /opt/hermes/hermes_cli/config_defaults.py
MANAGED_OVERLAY_YAML = """\
bedrock:
  discovery:
    # Default is TRUE and calls ListFoundationModels hourly; the per-owner role denies it.
    enabled: false
telemetry:
  shared_metrics:
    enabled: false
    send: false
updates:
  check: false
approvals:
  # Blocks even under mode=off / --yolo. Belt for the profile's own copy.
  deny:
    - "rm -rf /"
    - "mkfs*"
    - "dd if=* of=/dev/*"
    - ":(){:|:&};:"
nous:
  # The free/anonymous Nous tier is the one credential-less lane on Hermes's own vision
  # auto-chain (hermes_cli/anon_auth.py::guest_enabled is its single reader). Belt for the
  # profile's own copy -- normally inert (guest_enabled also requires
  # HERMES_GUEST_ONBOARDING=1, which this platform never sets), but the managed overlay wins
  # after env expansion and cannot be undone from inside a profile's own config.yaml.
  guest: false
browser:
  # REQUIRED, not cosmetic -- see the test in test_hermes_managed_overlay.py. Hermes 0.21.3
  # defaults to the Browser-Use CLI harness whenever uvx is runnable (our base image ships
  # /usr/local/bin/uvx); that harness cannot SigV4-sign the AgentCore CDP handshake and dies
  # with a 403. "off" is Hermes's own documented value for "keep the built-in browser_*
  # tools". The overlay is the permanent home because it wins over every profile's own
  # config.yaml and needs no backfill.
  backend: "off"
display:
  # Upstream appends a "File-mutation verifier" footer to the final reply whenever a
  # write_file/patch failed this turn (agent/turn_finalizer.py). That is a terminal-CLI
  # affordance: in a room it leaked "Write denied ... HERMES_WRITE_SAFE_ROOT" to a customer
  # (prod 2026-09-29). The agent still sees the tool error itself and can recover.
  file_mutation_verifier: false
auxiliary:
  title_generation:
    # Hermes titles every new session with an extra model call once it has a state.db
    # (agent/turn_context.py:155-182). Ours are per room, thread, call, mail thread, Paperclip
    # run and event: a title nobody reads, on the trial's daily cap, outside reported usage.
    enabled: false
  compression:
    # A compaction summary was 67-103 s on the main model at its default effort (prod 10-07),
    # inline in a chat turn or, since spoken turns stopped compacting, behind a call. The
    # provider and model stay each profile's own pin (hermes_profiles.auxiliary_tasks_block), so
    # every provider path keeps a model it can serve; only the effort drops
    # (auxiliary_client.py:5846-5870 folds it into the request).
    reasoning_effort: low
agent:
  turn_liveness:
    # Hermes's watchdog hard-cancels a turn with no activity for 600 s once it holds a session
    # turn lease, which every recorded turn after a session's first now does. A quiet long tool
    # (a clone, a build) is real work here; stops come from the run's own abort and the backend's
    # heartbeat sweeper, as before state.db. 0 = off (agent/turn_liveness.py).
    timeout_s: 0
compression:
  # A room runs on one Hermes transcript for its whole life, replayed on every model call of every
  # turn. Hermes compacts at 75% of the window by default (~150k tokens on our 200k pin), past the
  # openrouter-gate's paid-mode bound (a request body over max_prompt_tokens=400000 bytes is a 413,
  # roughly 100k tokens). This compacts at a predictable per-call size instead, under that bound
  # with room for the tool schemas and a turn's own tool output.
  threshold_tokens: 60000
"""


@dataclass
class ProfileFiles:
    """The three backend-owned files (fix round 1, I5). ``MANAGED_OVERLAY_YAML`` is baked into the
    image at ``/etc/hermes/config.yaml`` and never uploaded per profile: ``get_managed_dir()``
    resolves ``$HERMES_MANAGED_DIR`` or ``/etc/hermes`` only and never reads S3, so a per-profile
    copy was a harmless write nobody ever reads."""

    config_yaml: str
    dotenv: str
    soul_md: str
    #: The agent's heartbeat checklist seed. ``upload_profile`` creates ``HEARTBEAT.md`` only
    #: when absent, so an edited checklist is never overwritten.
    heartbeat_md: str | None = None


def write_agent_profile(
    owner_id: str,
    agent: dict,
    *,
    provider_choice: str,
    integrations: dict[str, dict],
    env_vars: dict[str, str],
    byo_provider: str | None = None,
    byo_api_key: str | None = None,
    soul_md_override: str | None = None,
    front_desk_number: str | None = None,
    gate_bearer: str | None = None,
) -> ProfileFiles:
    """The three files that ARE one agent's Hermes profile (fix round 1, I5 dropped the fourth,
    ``managed_overlay`` — the baked ``/etc/hermes/config.yaml`` is the sole enforcement point and
    nothing ever reads a per-profile copy).

    ``integrations`` is ``credentials.mcp_server.build_connected_integration_servers(owner_id)``'s
    own return shape (fix round 1, I2) — ``{server_key: mcp block}`` — taken as-is, not a
    ``list[dict]`` with a ``"key"`` field: that shape belonged to no real producer and raised
    ``TypeError: string indices must be integers`` the first time anything called this with the
    producer's actual output. ``env_vars`` is ``provisioning_landing.build_provisioned_env(owner_id)``.

    ``byo_provider``/``byo_api_key`` are OWNER-level keywords (fix round 1, C2) — never read off
    the Paperclip roster row (``agent``), whose documented shape is
    ``{"id","name","role","title","capabilities","icon"}`` and carries no such field. The provider
    choice is threaded next to ``provider_choice`` everywhere else in the codebase; reading it
    from the roster row silently pinned every BYO owner to Anthropic with no key delivered
    anywhere.

    ``soul_md_override`` (M6 lane R1): when given, replaces the generated ``build_soul_md``
    identity outright — the one hook the ECS-to-Hermes workspace migration job needs to carry
    an agent's existing EFS ``SOUL.md`` verbatim instead of a freshly generated identity.
    """
    mcp_config, mcp_secrets = translate_mcp_servers(integrations)
    config = build_profile_config(
        agent_role=agent.get("role") or "general",
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        mcp_servers=with_profile_home(mcp_config, agent["id"]),
        is_ceo=(agent.get("role") == "ceo"),
    )
    # mcp_secrets first, env_vars second: an owner-provisioned value should win a name collision
    # over a generated MCP_<SERVER>_<VAR> one (unlikely given the prefix, but never the reverse).
    env = build_profile_env(
        env_vars={**mcp_secrets, **(env_vars or {})},
        byo_provider=byo_provider,
        byo_api_key=byo_api_key,
        gate_bearer=gate_bearer,
    )
    # The lightbulb-*.sh scripts' default sender. PAPERCLIP_AGENT_ID (same value) is for images not yet rolled.
    env["LIGHTBULB_AGENT_ID"] = env["PAPERCLIP_AGENT_ID"] = agent["id"]
    return ProfileFiles(
        config_yaml=yaml.safe_dump(config, sort_keys=False),
        dotenv=render_dotenv(env),
        soul_md=(
            soul_md_override
            if soul_md_override is not None
            else build_soul_md(agent, front_desk_number=front_desk_number)
        ),
        heartbeat_md=default_checklist(agent.get("role")),
    )


def _new_s3_client():
    import boto3

    return boto3.client("s3")


def profile_prefix(owner_id: str, agent_id: str) -> str:
    """``<owner>/hermes/<agentId>/`` — owner FIRST.

    The per-owner IAM grant is ``arn:aws:s3:::<bucket>/<owner>/*``
    (bedrock_aip.build_owner_litestream_s3_policy). Any other order is outside every owner's
    grant and AccessDenies on the first restore.
    """
    return f"{owner_id}/hermes/{agent_id}/"


async def upload_profile(owner_id: str, agent_id: str, files: ProfileFiles) -> None:
    """Fix round 1, I6: async, ONE S3 client, every ``put_object`` off the event loop via
    ``core.dynamodb.run_in_thread``. The previous sync version built a fresh ``boto3.client("s3")``
    per file — 30 client constructions and 30 blocking round trips for a six-agent roster, on
    whatever thread called it; this repo has already taken a prod backend freeze from blocking I/O
    on the event loop (EFS tree walk, 2026-09-12). ``write_agent_profile`` itself stays pure/sync —
    only the network I/O here needs to move.
    """
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    # Minor (re-review): the client itself, not just each put_object — constructing a boto3
    # client does file I/O (credentials/config resolution) on first use.
    client = await run_in_thread(_new_s3_client)
    prefix = profile_prefix(owner_id, agent_id)
    # No per-profile managed/config.yaml (I5, fix round 1): the baked /etc/hermes/config.yaml is
    # the sole enforcement point and get_managed_dir() never reads S3, so this upload was a
    # harmless write nobody ever reads.
    for name, body in (("config.yaml", files.config_yaml), (".env", files.dotenv), ("SOUL.md", files.soul_md)):
        await run_in_thread(client.put_object, Bucket=bucket, Key=f"{prefix}{name}", Body=body.encode())
    if files.heartbeat_md is not None:
        try:
            await run_in_thread(
                client.put_object,
                Bucket=bucket,
                Key=f"{prefix}{HEARTBEAT_FILE}",
                Body=files.heartbeat_md.encode(),
                IfNoneMatch="*",
            )
        except Exception as exc:
            if not _is_precondition_failure(exc):
                raise
    # LAST, always: the bridge re-pulls a profile when this object's value changes (Task 13, this
    # lane's continuation). Writing it before the files it describes would advertise a config that
    # is not there yet. A uuid4 hex, not a timestamp (C4, fix round 1) — two uploads racing within
    # the same millisecond would otherwise write an identical ".revision" value and the bridge's
    # diff-and-repull could miss the second one.
    await run_in_thread(client.put_object, Bucket=bucket, Key=f"{prefix}.revision", Body=uuid.uuid4().hex.encode())


async def upload_profile_files(owner_id: str, agent_id: str, files: dict[str, bytes]) -> None:
    """Write named files into an agent's Hermes profile prefix, then bump ``.revision`` LAST.

    The non-tarball sibling of ``catalog_service.deploy_tarball_to_profile`` (M6-R33): content
    that already exists as a ``{relpath: bytes}`` map rather than a tar archive -- today, the
    company-deploy per-agent loop's rendered ``AGENTS.md`` (``company_agent_materializer.
    render_agent_identity_md``). Same posture as ``upload_profile`` above: one boto3 client,
    every ``put_object`` off the event loop via ``run_in_thread``, ``.revision`` written only
    after every named file has landed so the bridge never re-pulls a half-written profile.
    """
    # The backend-owned files are never content: a colliding name here would silently overwrite
    # the provider config / secrets `write_profile_for_agent` just uploaded.
    reserved = files.keys() & BACKEND_OWNED_FILES
    if reserved:
        raise ValueError(f"upload_profile_files: backend-owned names not allowed: {sorted(reserved)}")
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    client = await run_in_thread(_new_s3_client)
    prefix = profile_prefix(owner_id, agent_id)
    for name, data in files.items():
        await run_in_thread(client.put_object, Bucket=bucket, Key=f"{prefix}{name}", Body=data)
    await run_in_thread(client.put_object, Bucket=bucket, Key=f"{prefix}.revision", Body=uuid.uuid4().hex.encode())


#: Must match ``owner_facts.FACTS_FILENAME`` -- see the note on the (separate, deliberately
#: duplicated to avoid an import cycle) copy near ``write_facts_to_profiles`` below.
FACTS_FILENAME = "lightbulb-facts.json"

#: F3 (predeploy review, M6-R36): the ONE backend-owned file set, asserted identical across every
#: seam that must never let outside content collide with it -- this module's own guard above,
#: ``catalog_service._DEPLOY_PROFILE_SKIP_NAMES`` (a marketplace tarball must never overwrite a
#: profile's provider config or the owner's facts file), and (a separate literal, contract-tested
#: in ``tests/unit/contracts/test_hermes_backend_owned_files_contract.py``) the bridge's own
#: ``BACKEND_OWNED_FILES`` re-pull list. Before this fix the three disagreed: the writer's raise
#: set omitted the facts file, and the marketplace deploy skip list omitted SOUL.md/.revision/the
#: facts file entirely, so a tarball carrying either could silently overwrite it.
#: Voice notes (spec D §7): the agent's name line plus what people asked of its voice. The bridge
#: appends it to every turn's system prompt (``turn.with_profile_agents_md``).
VOICE_NOTES_FILENAME = "VOICE_NOTES.md"

BACKEND_OWNED_FILES = frozenset({"config.yaml", ".env", "SOUL.md", ".revision", FACTS_FILENAME, VOICE_NOTES_FILENAME})


def delete_profile(owner_id: str, agent_id: str) -> None:
    """``agents.delete`` on an AgentCore row (Q9a row #3): there is no ``openclaw.json`` entry to
    remove, so this agent's own S3 profile prefix -- config.yaml/.env/SOUL.md/mcp-tokens/.revision
    -- is the durable record to delete instead. One ``delete_objects`` call per
    ``list_objects_v2`` page (S3's own 1000-key batch cap covers both), sync boto3 -- call via
    ``run_in_thread``."""
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    client = _shared_s3_client()
    prefix = profile_prefix(owner_id, agent_id)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            client.delete_objects(Bucket=bucket, Delete={"Objects": keys})
    client.delete_object(Bucket=bucket, Key=f"marketplace-installs/{owner_id}/{agent_id}.json")


# ---- Task 13a: config.get/config.patch on a profile --------------------------------------------
# Spec §4.5: "Config is read once per AIAgent construction and cached on file mtime, so rewriting
# profiles/<id>/config.yaml takes effect on the agent's next turn. No watcher, no restart." The
# backend is the ONLY writer of a Hermes profile, so a hot patch is an S3 rewrite plus a revision
# bump — never a round trip to a live microVM, and no file lock at all.
#
# M4-R49 audit (fix round 2 continuation): every sync-boto3 writer in this module reached from a
# coroutine, and how it stays off the event loop --
#   config_snapshot / patch_profile_config -- one external caller each
#     (runtime_rpc.execute_sync), every call already wrapped in run_in_thread.
#   read_profile_config -- no external caller; only used internally by patch_profile_config
#     above, itself always thread-wrapped by ITS caller.
#   _upload_config_yaml -- no external caller; only used internally by patch_profile_config
#     above, same coverage.
#   stage_codex_auth -- one external caller (container_auth.push_oauth_credential_to_container),
#     already wrapped in run_in_thread.
#   refresh_integrations -- fixed THIS round: awaited directly from routers/integrations.py and
#     catalog_service.py, but its own per-agent S3 block ran sync boto3 straight on the loop; see
#     _refresh_one_profile below.
# None of the other four needed changing — each was already off the loop through its one caller.


#: Minors 1+2 (fix round 1 continuation): one boto3 client per module, not one per call —
#: ``_s3_put``/``_s3_get`` are each called up to several times per agent per reconcile
#: (``refresh_integrations``), and building a client does file I/O (credentials/config
#: resolution) on every construction. ``upload_profile`` keeps its own separate one-client-
#: per-*call* design (via ``_new_s3_client``, reused for that one profile's five PUTs) —
#: unrelated: that one already amortizes correctly within a single upload and has its own tests
#: pinning that exact shape.
_SHARED_S3_CLIENT = None


def _shared_s3_client():
    global _SHARED_S3_CLIENT
    if _SHARED_S3_CLIENT is None:
        import boto3

        _SHARED_S3_CLIENT = boto3.client("s3")
    return _SHARED_S3_CLIENT


def _s3_put(bucket: str, key: str, body: str) -> None:
    """Sync single-object PUT for the small, infrequent settings-mutation paths below
    (``config.patch``, and Task 14's token-file writes) — each stays a pure sync
    function (matching its own tests); their one async caller, ``runtime_rpc.execute_sync``,
    wraps the call in ``core.dynamodb.run_in_thread`` so the blocking I/O here never touches the
    event loop directly. ``upload_profile`` above is the higher-fanout path (up to five objects
    per agent) and owns its own one-client-reused async design instead."""
    _shared_s3_client().put_object(Bucket=bucket, Key=key, Body=body.encode())


def _s3_delete(bucket: str, key: str) -> None:
    _shared_s3_client().delete_object(Bucket=bucket, Key=key)


def _s3_put_bytes(bucket: str, key: str, body: bytes) -> None:
    """The one PUT (``_s3_put`` encodes and delegates). Bytes rather than text because the
    CLI-connector projection (``credentials.hermes_cli``) copies whatever a materializer
    produced verbatim, and gog's keyring is an encrypted JWE blob."""
    _shared_s3_client().put_object(Bucket=bucket, Key=key, Body=body)


def _s3_get_bytes(bucket: str, key: str) -> bytes | None:
    """``_s3_get``'s binary twin, with the same "None means genuinely ABSENT" contract."""
    import botocore.exceptions

    try:
        return _shared_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read()
    except botocore.exceptions.ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            return None
        raise


def put_owner_object(key: str, body: bytes) -> None:
    """PUT one object into the owner-state bucket by absolute key.

    The seam for owner-scoped blobs that are NOT part of an agent profile (messaging
    attachments). Raises rather than no-op when the bucket is unset, so a caller never
    reports a write that did not happen. Sync -- call via ``run_in_thread``."""
    _s3_put_bytes(_require_owner_bucket(), key, body)


def get_owner_object(key: str) -> bytes | None:
    """``put_owner_object``'s reader. ``None`` means genuinely ABSENT."""
    return _s3_get_bytes(_require_owner_bucket(), key)


def owner_object_size(key: str) -> int | None:
    """HEAD one owner object: its byte size, or ``None`` when it is genuinely ABSENT."""
    import botocore.exceptions

    try:
        return int(_shared_s3_client().head_object(Bucket=_require_owner_bucket(), Key=key)["ContentLength"])
    except botocore.exceptions.ClientError as exc:
        if (exc.response or {}).get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            return None
        raise


def get_owner_object_head(key: str, length: int) -> bytes:
    """The first ``length`` bytes of an owner object (a ranged GET -- the object may be 100 MB)."""
    return (
        _shared_s3_client()
        .get_object(Bucket=_require_owner_bucket(), Key=key, Range=f"bytes=0-{length - 1}")["Body"]
        .read()
    )


def presign_owner_object(key: str, *, content_type: str, disposition: str, expires_in: int) -> str:
    """A presigned GET for one owner object. SigV4 explicitly: the bucket is KMS-encrypted and a
    default-signed us-east-1 URL is not guaranteed to be v4."""
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3", region_name=settings.AWS_REGION, config=Config(signature_version="s3v4")
    ).generate_presigned_url(
        "get_object",
        Params={
            "Bucket": _require_owner_bucket(),
            "Key": key,
            "ResponseContentType": content_type,
            "ResponseContentDisposition": disposition,
        },
        ExpiresIn=expires_in,
    )


def _require_owner_bucket() -> str:
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        raise RuntimeError("OWNER_STATE_BUCKET is not configured")
    return bucket


def _s3_list_keys(bucket: str, prefix: str) -> list[str]:
    pages = _shared_s3_client().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix)
    return [obj["Key"] for page in pages for obj in page.get("Contents", []) if not obj["Key"].endswith("/")]


# ---- CLI-connector credential projection (supersedes the M6-R24 skip) --------------------------
# The mcp-tokens rail's shape, one level up: the unit is a SUBTREE of the profile rather than a
# single ``<server>.json``, because a CLI materializer owns a little directory (``.aws/``,
# ``.config/gh/``, gog's config + keyring, ``skills/instagram/``) or a single dotfile
# (``.xurl``). Per-agent for the same reason mcp-tokens is: ``sync.ensure_profile`` only ever
# pulls the agent's OWN prefix, so an owner-level object would never reach a microVM.


def fetch_profile_subtree(owner_id: str, agent_id: str, prefixes: Iterable[str], dest: Path) -> None:
    """Copy every object under each of ``prefixes`` in one profile down into ``dest``.

    The projector seeds its scratch root with this before running a materializer, so the
    materializers that OWN one entry inside a shared file still see the rest of it: gog's single
    keyring holds every Google account the owner has connected, and ``~/.xurl`` holds the
    owner's other xurl apps. Without the seed, connecting a second Google account would upload a
    keyring containing only that account."""
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    base = profile_prefix(owner_id, agent_id)
    for prefix in prefixes:
        # N4: a trailing "/" means a directory (every key under it); without one the prefix
        # names a single FILE and must match exactly -- ".xurl" must not pull ".xurlfoo".
        for key in _s3_list_keys(bucket, f"{base}{prefix}"):
            if not prefix.endswith("/") and key != f"{base}{prefix}":
                continue
            body = _s3_get_bytes(bucket, key)
            if body is None:
                continue
            target = dest / key[len(base) :]
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)


def write_profile_subtree(owner_id: str, agent_id: str, files: dict[str, bytes], removed: Iterable[str]) -> None:
    """Land a projected CLI subtree in one profile: write, then delete what the materializer
    dropped, then bump ``.revision`` LAST -- the same "content, then revision" discipline as
    ``_refresh_one_profile``/``write_facts_to_profiles``, so the bridge's re-pull never sees a
    bumped revision before the bytes it announces.

    Never touches ``.env``: a projected credential is a FILE the CLI reads, and the profile
    ``.env`` feeds Hermes's in-process secret scope, which never reaches a subprocess. A value
    a projected CLI needs in the shell goes to ``hermes_cli.CLI_ENV_FILE`` instead."""
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    prefix = profile_prefix(owner_id, agent_id)
    for rel, body in sorted(files.items()):
        _s3_put_bytes(bucket, f"{prefix}{rel}", body)
    for rel in sorted(removed):
        _s3_delete(bucket, f"{prefix}{rel}")
    _s3_put(bucket, f"{prefix}.revision", uuid.uuid4().hex)


def _s3_get(bucket: str, key: str) -> str | None:
    """``None`` means ABSENT (``NoSuchKey``/``404`` only) — every other ``ClientError`` (a
    503/SlowDown, a permissions blip, ...) is re-raised (C4, fix round 1 continuation). Before
    this fix every S3 failure looked identical to "the object was never written", so a patch or
    reconcile hitting a transient S3 error silently treated a live profile as freshly
    unprovisioned and merged onto ``{}``."""
    import botocore.exceptions

    try:
        return _shared_s3_client().get_object(Bucket=bucket, Key=key)["Body"].read().decode()
    except botocore.exceptions.ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            return None
        raise


def _s3_get_versioned(bucket: str, key: str) -> tuple[str | None, str | None]:
    """Object text plus its quoted S3 ETag; ``(None, None)`` only for a genuine miss."""
    import botocore.exceptions

    try:
        response = _shared_s3_client().get_object(Bucket=bucket, Key=key)
    except botocore.exceptions.ClientError as exc:
        code = (exc.response or {}).get("Error", {}).get("Code")
        if code in ("NoSuchKey", "404"):
            return None, None
        raise
    etag = response.get("ETag")
    if not etag:
        raise RuntimeError(f"S3 returned {key} without an ETag; refusing an unfenced write")
    return response["Body"].read().decode(), etag


def _s3_put_conditionally(bucket: str, key: str, body: str, etag: str | None) -> None:
    condition = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
    _shared_s3_client().put_object(Bucket=bucket, Key=key, Body=body.encode(), **condition)


def _is_precondition_failure(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    code = (response or {}).get("Error", {}).get("Code") if isinstance(response, dict) else None
    return code in ("PreconditionFailed", "412", "ConditionalRequestConflict", "409")


def seed_profile_file(owner_id: str, agent_id: str, rel: str, body: str) -> bool:
    """Create ``rel`` in the profile only when it does not exist (``IfNoneMatch``), so a file the
    owner or the agent already wrote is never overwritten. True when this call created it. Does
    not bump ``.revision``: callers seed before the ``upload_profile`` that bumps it. Sync --
    call via ``run_in_thread``."""
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return False
    try:
        _s3_put_conditionally(bucket, f"{profile_prefix(owner_id, agent_id)}{rel}", body, None)
    except Exception as exc:
        if _is_precondition_failure(exc):
            return False
        raise
    return True


def read_profile_config(owner_id: str, agent_id: str, *, required: bool = False) -> dict:
    """``config.get`` — reads ``config.yaml`` alone. NEVER ``.env``: that file holds secret
    values (M4-R1), and this is the one path a settings-read RPC can reach.

    ``required=True`` (C4, fix round 1 continuation) raises ``FileNotFoundError`` instead of
    silently treating an absent profile as ``{}`` — for a WRITER (patch/refresh) a missing
    config.yaml is a provisioning defect, never a legitimate empty state. Left ``False`` by
    default for GET, where "nothing provisioned yet" is a real, non-error answer."""
    body = _s3_get(settings.OWNER_STATE_BUCKET, f"{profile_prefix(owner_id, agent_id)}config.yaml")
    if body is None:
        if required:
            raise FileNotFoundError(f"no config.yaml for {profile_prefix(owner_id, agent_id)}")
        return {}
    return yaml.safe_load(body) or {}


def config_snapshot(owner_id: str, agent_id: str) -> tuple[dict, str]:
    """``(config, hash)`` for ``config.get`` — ``hash`` is the sha256 hex of the STORED
    ``config.yaml`` TEXT (M4-R43), not a re-serialization of the parsed dict, so it changes
    exactly when the persisted bytes actually change and nothing else. This is what
    ``useConfigPatch.ts`` gates ``configReady`` on and sends back as ``baseHash``."""
    text = _s3_get(settings.OWNER_STATE_BUCKET, f"{profile_prefix(owner_id, agent_id)}config.yaml") or ""
    config = (yaml.safe_load(text) or {}) if text else {}
    return config, hashlib.sha256(text.encode()).hexdigest()


def _deep_merge(base: dict, patch: dict) -> dict:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value
    return base


def _upload_config_yaml(owner_id: str, agent_id: str, config: dict) -> None:
    prefix = profile_prefix(owner_id, agent_id)
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}config.yaml", yaml.safe_dump(config, sort_keys=False))
    # LAST, always — same reasoning as upload_profile's .revision write above.
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}.revision", uuid.uuid4().hex)


def patch_profile_config(owner_id: str, agent_id: str, patch: dict) -> dict:
    """``config.patch`` — deep merge onto the stored config, then re-upload.

    C4 (fix round 1 continuation): ``required=True`` — a patch onto a profile that has no
    ``config.yaml`` yet is a provisioning defect, not something to paper over by merging the
    patch onto ``{}`` and uploading that as if it were the whole profile."""
    config = _deep_merge(read_profile_config(owner_id, agent_id, required=True), patch or {})
    _upload_config_yaml(owner_id, agent_id, config)
    return config


def upload_soul_md(owner_id: str, agent_id: str, soul_md: str) -> None:
    """Rewrite ONLY this agent's ``SOUL.md`` (+ ``.revision``) -- Q9a fix round 1, I2: an
    identity-only ``agents.update`` (rename/title/capabilities/role) must refresh the file
    ``build_soul_md`` renders into, but never touch ``config.yaml``/``.env`` alongside it the
    way a full ``upload_profile`` would. Sync -- call via ``run_in_thread``."""
    prefix = profile_prefix(owner_id, agent_id)
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}SOUL.md", soul_md)
    # LAST, always -- same reasoning as upload_profile/_upload_config_yaml's .revision write.
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}.revision", uuid.uuid4().hex)


def read_soul_md(owner_id: str, agent_id: str) -> str | None:
    """The profile's stored ``SOUL.md``, or ``None`` when it was never written. Sync -- call via
    ``run_in_thread``. Its one reader is the rename branch's generated-marker check (M6-R48)."""
    return _s3_get(settings.OWNER_STATE_BUCKET, f"{profile_prefix(owner_id, agent_id)}SOUL.md")


#: The notes BODY, excluding the name line, on every door (persona, the agent tool, the settings
#: route). The whole file stays under ~1,100 chars with the name line.
VOICE_NOTES_MAX = 1000
_NAME_LINE = re.compile(r"^Your name is (.+)\.$")


def build_voice_notes_md(name: str | None, notes: str) -> str:
    """``VOICE_NOTES.md`` text: the name line, a blank line, the notes. ``""`` when both are empty
    (an empty file means nothing; the file is never deleted)."""
    notes = (notes or "").strip()
    return f"Your name is {name}.\n\n{notes}" if name else notes


def parse_voice_notes_md(text: str | None) -> tuple[str | None, str]:
    """Inverse of ``build_voice_notes_md``: ``(name or None, notes)``."""
    first, _, rest = (text or "").partition("\n")
    match = _NAME_LINE.match(first.strip())
    if match:
        return match.group(1), rest.strip()
    return None, (text or "").strip()


def upload_voice_notes(owner_id: str, agent_id: str, text: str) -> None:
    """Rewrite ONLY this agent's ``VOICE_NOTES.md`` (+ ``.revision`` LAST), like
    ``upload_soul_md``. Sync -- call via ``run_in_thread``."""
    prefix = profile_prefix(owner_id, agent_id)
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}{VOICE_NOTES_FILENAME}", text)
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}.revision", uuid.uuid4().hex)


def read_voice_notes(owner_id: str, agent_id: str) -> str | None:
    """The stored ``VOICE_NOTES.md``, or ``None`` when never written. Sync -- call via
    ``run_in_thread``."""
    return _s3_get(settings.OWNER_STATE_BUCKET, f"{profile_prefix(owner_id, agent_id)}{VOICE_NOTES_FILENAME}")


# ---- Task 10-B: the owner-facing file routes on an AgentCore row (M6-R48) ----------------------
# The migration COPIES the EFS tree into the profile and leaves the original in place
# (migrate_workspace_to_hermes.py's tail stops the ECS service and never deletes
# /mnt/efs/users/<owner>/), so routers/workspace_files.py has to read and write the S3 profile on a
# migrated workspace or the Files panel and the Instructions tab edit a stale copy nobody reads --
# a green save with no effect. These helpers are the S3 side of that branch; the writes go through
# upload_profile_files/upload_soul_md, which bump .revision LAST -- that bump is the SIGNAL the
# bridge watches. Acting on it for a non-backend-owned file (re-pull by recency, and save back only
# what the turn changed) is Ruling M6-R51, landing in lane 10-F: until that ships, only SOUL.md is
# on the bridge's re-pull list, so a USER.md/AGENTS.md/Files-panel write reaches a warm microVM on
# its next COLD start rather than its next turn.

#: Never listed, read or written through an owner-facing file route.
_HIDDEN_PROFILE_DIRS = frozenset({"mcp-tokens"})

#: Fix round 1, I1: ``lightbulb-facts.json`` is backend-owned (the owner's plan, subscription
#: status, provider choice and agent e-mail) and is NOT a dotfile, so it slipped the filter that
#: hides ``.env``/``.revision``. On EFS it lives at the user root, outside the ``workspace-{id}/``
#: subtree the Files panel is scoped to, so no owner-facing route has ever served it -- hiding it
#: here keeps that true rather than inventing a new exposure on the profile layout.
_HIDDEN_PROFILE_FILES = frozenset({FACTS_FILENAME})


def is_hidden_profile_path(rel: str) -> bool:
    """``mcp-tokens/`` (live MCP OAuth tokens), ``lightbulb-facts.json``, and every dotfile --
    ``.env`` (the profile's secret store) and ``.revision`` (the bridge's re-pull marker) chief
    among them. A hidden path is ABSENT to every owner-facing route, read AND write: 404, never a
    403/422, so the write route cannot confirm what the read route refuses to."""
    parts = PurePosixPath(rel).parts
    if not parts or parts[0] in _HIDDEN_PROFILE_DIRS or parts[-1] in _HIDDEN_PROFILE_FILES:
        return True
    return any(p.startswith(".") for p in parts)


def normalize_profile_rel(rel_path: str) -> str:
    """A clean, profile-relative POSIX path, or ``ValueError``.

    Normalizing is not cosmetic on S3, which has no path semantics of its own: ``./SOUL.md`` would
    otherwise be a DIFFERENT key from ``SOUL.md`` and slip straight past the ``BACKEND_OWNED_FILES``
    guard in ``write_profile_file``.
    """
    if rel_path.startswith(("/", "\\")):
        raise ValueError("path must be a non-empty relative path")
    parts = [p for p in PurePosixPath(rel_path.replace("\\", "/")).parts if p not in (".", "")]
    if any(p == ".." for p in parts):
        raise ValueError("path must not contain '..' segments")
    if not parts:
        raise ValueError("path must be a non-empty relative path")
    return "/".join(parts)


def list_profile_dir(owner_id: str, agent_id: str, rel_path: str = "") -> list[dict]:
    """ONE level of a Hermes profile, in the entry shape ``Workspace.list_directory`` returns
    (dirs first, then files, each name-ordered) so the Files panel renders it unchanged.

    S3 has no directories: ``Delimiter="/"`` turns the shared key prefixes into the dir rows, and
    they carry no mtime -- ``modified_at: 0``, the same value the tree already synthesizes for its
    ghost entries. Deliberately non-recursive, like the EFS route it mirrors. Sync -- call via
    ``run_in_thread``.
    """
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return []
    sub = f"{normalize_profile_rel(rel_path)}/" if rel_path else ""
    if sub and is_hidden_profile_path(sub.rstrip("/")):
        return []
    prefix = f"{profile_prefix(owner_id, agent_id)}{sub}"
    dirs: list[dict] = []
    files: list[dict] = []
    pages = _shared_s3_client().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/")
    for page in pages:
        for common in page.get("CommonPrefixes", []):
            name = common["Prefix"][len(prefix) :].rstrip("/")
            if not name or is_hidden_profile_path(f"{sub}{name}"):
                continue
            dirs.append({"name": name, "path": f"{sub}{name}", "type": "dir", "size": None, "modified_at": 0})
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix) :]
            if not name or "/" in name or is_hidden_profile_path(f"{sub}{name}"):
                continue
            files.append(
                {
                    "name": name,
                    "path": f"{sub}{name}",
                    "type": "file",
                    "size": obj["Size"],
                    "modified_at": obj["LastModified"].timestamp(),
                }
            )
    dirs.sort(key=lambda e: e["name"])
    files.sort(key=lambda e: e["name"])
    return dirs + files


_XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
# The browser's sheet viewer stops at 5 MiB (``SheetGrid``); past it the tree shows the binary card.
_XLSX_PREVIEW_MAX_BYTES = 5 * 1024 * 1024


def _profile_file_info(rel: str, body: bytes, modified_at: float) -> dict:
    """``Workspace.read_file_info``'s payload for one profile object -- same keys, same
    text/base64/None content rule, so ``FileContentViewer`` needs no branch of its own."""
    name = rel.rsplit("/", 1)[-1]
    # Python's own table has no xlsx entry (3.12), so the image's /etc/mime.types would decide it.
    mime_type = (
        _XLSX_MIME if name.lower().endswith(".xlsx") else mimetypes.guess_type(name)[0] or "application/octet-stream"
    )
    info = {"name": name, "path": rel, "size": len(body), "modified_at": modified_at, "mime_type": mime_type}
    if (
        mime_type.startswith("image/")
        or mime_type == "application/pdf"
        or (mime_type == _XLSX_MIME and len(body) <= _XLSX_PREVIEW_MAX_BYTES)
    ):
        return {**info, "binary": True, "content": base64.b64encode(body).decode("ascii")}
    try:
        return {**info, "binary": False, "content": body.decode()}
    except UnicodeDecodeError:
        # Opaque binary -- metadata only, exactly as the EFS reader answers for one.
        return {**info, "binary": True, "content": None}


def read_profile_file(owner_id: str, agent_id: str, rel_path: str) -> dict:
    """One profile object in ``read_file_info``'s shape. ``FileNotFoundError`` when it is absent
    OR hidden (see ``is_hidden_profile_path``), ``ValueError`` for a malformed path. Sync -- call
    via ``run_in_thread``."""
    import botocore.exceptions

    rel = normalize_profile_rel(rel_path)
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket or is_hidden_profile_path(rel):
        raise FileNotFoundError(rel_path)
    try:
        obj = _shared_s3_client().get_object(Bucket=bucket, Key=f"{profile_prefix(owner_id, agent_id)}{rel}")
    except botocore.exceptions.ClientError as exc:
        # Same split as _s3_get: only a genuine miss is "absent"; a 503/permissions blip propagates.
        if (exc.response or {}).get("Error", {}).get("Code") in ("NoSuchKey", "404"):
            raise FileNotFoundError(rel_path) from exc
        raise
    return _profile_file_info(rel, obj["Body"].read(), obj["LastModified"].timestamp())


async def write_profile_file(owner_id: str, agent_id: str, rel_path: str, content: str) -> str:
    """Write one owner-editable file into the profile, bumping ``.revision`` LAST -- delegated to
    ``upload_profile_files``, which already does both in that order.

    ``FileNotFoundError`` for a hidden path (fix round 1, m3: a write must not confirm what
    ``read_profile_file`` hides behind a 404) and ``PermissionError`` for a visible but
    ``BACKEND_OWNED_FILES`` name -- ``config.yaml`` is listable and readable, and ``SOUL.md`` has
    its own writer (``upload_soul_md``, reached from the Instructions tab) that keeps the
    generated-marker rule. ``ValueError`` for a malformed path.
    """
    rel = normalize_profile_rel(rel_path)
    if is_hidden_profile_path(rel):
        raise FileNotFoundError(rel)
    if rel in BACKEND_OWNED_FILES:
        raise PermissionError(rel)
    if not settings.OWNER_STATE_BUCKET:
        # Fix round 1, m2: upload_profile_files no-ops on an empty bucket name, so without this
        # the route would answer {"status": "ok"} having written nothing -- the same green-save
        # shape this whole branch exists to remove, for a misconfigured environment.
        raise RuntimeError("OWNER_STATE_BUCKET is not configured; refusing to report a write that did not happen")
    await upload_profile_files(owner_id, agent_id, {rel: content.encode()})
    return rel


# ---- Task 14: integration token refresh rewrites the profile -----------------------------------
def _require_text(bucket: str, key: str, what: str) -> str:
    """C4 (fix round 1 continuation): raise instead of silently treating an absent object as an
    empty/unprovisioned one — a WRITER (refresh/patch) landing on a profile that has no
    ``config.yaml``/``.env`` yet is a provisioning defect, never something to merge onto ``{}``/
    write a ``.env`` holding only MCP lines."""
    body = _s3_get(bucket, key)
    if body is None:
        raise FileNotFoundError(f"no {what} at {key}")
    return body


def _put_token_file(owner_id: str, agent_id: str, name: str, blob: dict) -> None:
    import json as _json

    key = f"{profile_prefix(owner_id, agent_id)}mcp-tokens/{name}.json"
    _s3_put(settings.OWNER_STATE_BUCKET, key, _json.dumps(blob))


def _write_mcp_token_files(
    owner_id: str,
    agent_id: str,
    servers: dict,
    credentials: dict[str, dict] | None = None,
    *,
    previously_oauth: frozenset[str] = frozenset(),
) -> None:
    """Seed ``$HERMES_HOME/mcp-tokens/<server>.json`` for OAuth-remote servers — WRITE ONCE
    (M4-R47, fix round 1 continuation): only when the object is absent. After the seed, Hermes's
    own ``OAuthClientProvider`` refreshes the token IN PLACE for the life of the process (report
    06 §4.4) — the container is the sole rotator from then on, so re-seeding on every reconcile
    would clobber an already-rotated live token with a stale copy from the credential store.

    ``previously_oauth`` (the OAuth server names the CALLER's stored config.yaml had before this
    reconcile) names what just disconnected: that server's token file is deleted so a later
    reconnect seeds a fresh one instead of resurrecting the old grant.

    On-disk shape verified against ``tools/mcp_oauth.py``'s ``HermesTokenStorage`` in the
    ``lightbulb/hermes-spike:m1-rc4`` image (``set_tokens``/``get_tokens``/``_fixup_loaded_tokens``,
    :471-499): the file at ``mcp-tokens/<server>.json`` is the JSON dump of the MCP SDK's
    ``OAuthToken`` model plus an absolute ``expires_at`` ``set_tokens`` derives from ``expires_in``
    at write time, and an optional ``hermes_issuer`` (only written once Hermes itself has bound an
    issuer via ``bind_issuer``). We write ``access_token``/``refresh_token`` verbatim from the
    retained credential.
    # ponytail: unverified — this repo's credential store does not currently retain
    # token_type/scope/expiry, so a seeded grant carries no expires_in/expires_at; whether
    # Hermes's own OAuthToken validation and refresh-on-first-use treat a token with no expiry
    # field as immediately-refreshable (rather than erroring) was NOT confirmed against the SDK
    # class itself (only HermesTokenStorage's own file format was read). Confirm in the dev proof
    # before this ships past the spike.
    # ponytail: filenames use the raw server name, not tools/mcp_oauth.py's own
    # _safe_filename(name) sanitizer (strips to [\\w-], 128 chars) — identical for every real
    # provider name today (stripe, qbo, posthog, ...), so this only diverges for a hypothetical
    # exotic server key.

    Key-based and stdio servers carry their credential through the profile ``.env`` instead and
    get no token file.
    """
    bucket = settings.OWNER_STATE_BUCKET
    credentials = credentials or {}
    now_oauth = {name for name, block in (servers or {}).items() if block.get("auth") == "oauth"}
    for name in previously_oauth - now_oauth:
        _s3_delete(bucket, f"{profile_prefix(owner_id, agent_id)}mcp-tokens/{name}.json")
    for name in now_oauth:
        key = f"{profile_prefix(owner_id, agent_id)}mcp-tokens/{name}.json"
        if _s3_get(bucket, key) is not None:
            continue  # already seeded once — the container is the sole rotator from here on
        _put_token_file(owner_id, agent_id, name, credentials.get(name) or {})


def _rewrite_mcp_env_lines(dotenv_text: str, new_secrets: dict[str, str]) -> str:
    """Replace every existing ``MCP_<SERVER>_<VAR>`` line with a freshly rendered set — a
    disconnected provider's old secret must not survive a refresh — while every other line
    (a BYO key, an owner-provisioned env var) passes through untouched.
    Line-level surgery, not a full dotenv parse: identifying an ``MCP_``-prefixed KEY only needs
    the text before the first ``=``, which ``render_dotenv`` always writes verbatim regardless of
    whether the VALUE side is quoted.

    Minor 5 (fix round 1 continuation): the trailing newline the STORED ``.env`` carried is
    preserved (``"\n".join`` alone drops it, since ``splitlines()`` never yields a trailing empty
    element) — an empty starting dotenv (nothing stored yet) carries no trailing newline to
    preserve, matching a from-scratch write."""
    trailing_newline = dotenv_text.endswith("\n")
    kept = [line for line in dotenv_text.splitlines() if not line.split("=", 1)[0].startswith("MCP_")]
    new_lines = render_dotenv(new_secrets).splitlines() if new_secrets else []
    out = "\n".join(kept + new_lines)
    return out + "\n" if out and trailing_newline else out


async def refresh_integrations(owner_id: str, agent_ids: list[str], *, pending: str | None = None) -> None:
    """Re-assert every connected integration into each profile's ``config.yaml``
    (``mcp_servers``) and ``.env`` (the secret values I1's ``${MCP_<SERVER>_<VAR>}`` refs point
    at), then bump the revision.

    ``pending`` (M6-R26 fix round 1, Critical 1): forwarded verbatim to
    ``build_connected_integration_servers`` — see its docstring. Lets a
    same-beat first connect see its own not-yet-``put_status``'d provider.

    The ``mcp_servers`` block is REPLACED, not merged: a disconnected provider has to disappear,
    and a merge would leave its server wired forever (the same reason ``reconcile_integrations``
    rewrites rather than patches today for the ECS/openclaw.json path). ``.revision`` is written
    LAST, after both files, for the same reason ``upload_profile``/``_upload_config_yaml`` do:
    the bridge's ``ensure_profile`` must never see a revision bump before the files it describes
    have actually landed.

    I1 (fix round 1 continuation, M4-R46/M4-R11): genuinely ``async`` — awaits its two producers
    directly instead of the old ``asyncio.run()``-based sync wrappers (``_connected_servers``/
    ``_integration_credential``, both deleted). Those blew up with ``RuntimeError`` the moment a
    caller (``reconcile_integrations`` is itself ``async``) awaited this from inside a running
    loop, which is exactly the calling context.

    M4-R49 (fix round 2 continuation): the per-agent S3 block below is plain sync boto3 (five
    round trips per agent between ``_require_text``/``_s3_put``/``_write_mcp_token_files``'s own
    ``_s3_get``/``_s3_delete``) and this function is now AWAITED DIRECTLY from FastAPI routes and
    ``catalog_service`` (``routers/integrations.py``, ``core/services/catalog_service.py``) — the
    exact class of blocking I/O on the event loop this repo already took a prod backend freeze
    from (EFS tree walk, 2026-09-12). ``_refresh_one_profile`` isolates that block so it can run
    off the loop via ``run_in_thread``, one hop per agent (fine — an owner's roster is small).
    """
    from core.services.credentials import integration_secret_store
    from core.services.credentials.mcp_server import build_connected_integration_servers

    connected = await build_connected_integration_servers(owner_id, pending=pending)
    servers, secrets = translate_mcp_servers(connected)
    # Minor 8: one Secrets Manager read per OAuth server per reconcile, not per (agent, server)
    # pair — hoisted out of the per-agent loop below.
    credentials = {
        name: (await integration_secret_store.get(owner_id, name)) or {}
        for name, block in connected.items()
        if block.get("auth") == "oauth"
    }
    for agent_id in agent_ids:
        await run_in_thread(_refresh_one_profile, owner_id, agent_id, connected, servers, secrets, credentials)


def _refresh_one_profile(
    owner_id: str, agent_id: str, connected: dict, servers: dict, secrets: dict, credentials: dict
) -> None:
    """The per-agent S3 work of ``refresh_integrations`` — pure sync boto3 (M4-R49), meant to be
    called through ``run_in_thread`` and never directly from a coroutine."""
    bucket = settings.OWNER_STATE_BUCKET
    prefix = profile_prefix(owner_id, agent_id)
    config = yaml.safe_load(_require_text(bucket, f"{prefix}config.yaml", "config.yaml")) or {}
    dotenv_text = _require_text(bucket, f"{prefix}.env", ".env")
    previously_oauth = frozenset(
        name
        for name, block in (config.get("mcp_servers") or {}).items()
        if isinstance(block, dict) and block.get("auth") == "oauth"
    )
    config["mcp_servers"] = with_profile_home(servers, agent_id)
    _write_mcp_token_files(owner_id, agent_id, connected, credentials, previously_oauth=previously_oauth)
    _s3_put(bucket, f"{prefix}config.yaml", yaml.safe_dump(config, sort_keys=False))
    _s3_put(bucket, f"{prefix}.env", _rewrite_mcp_env_lines(dotenv_text, secrets))
    _s3_put(bucket, f"{prefix}.revision", uuid.uuid4().hex)


# ---- Ruling M6-R31: an openclaw_paths-wiring provider with a bare-env-var Hermes equivalent ----
def _rewrite_named_env_lines(dotenv_text: str, keys_to_drop: set[str], live: dict[str, str]) -> str:
    """Like ``_rewrite_mcp_env_lines`` but keyed on an explicit name set rather than an ``MCP_``
    prefix -- for a provider secret with no MCP shape and no ``config.yaml`` field of its own
    (M6-R31, e.g. ``parallel``'s ``PARALLEL_API_KEY``). ``keys_to_drop`` always includes every
    name being touched (so a clear-to-"" on disconnect actually removes the old line); ``live``
    is the (possibly smaller) subset actually re-rendered."""
    trailing_newline = dotenv_text.endswith("\n")
    kept = [line for line in dotenv_text.splitlines() if line.split("=", 1)[0] not in keys_to_drop]
    new_lines = render_dotenv(live).splitlines() if live else []
    out = "\n".join(kept + new_lines)
    return out + "\n" if out and trailing_newline else out


def patch_profile_env_and_config(owner_id: str, agent_id: str, env_updates: dict[str, str], config_patch: dict) -> None:
    """Merge named ``key=value`` ``env_updates`` into a profile's stored ``.env`` AND deep-merge
    ``config_patch`` into its ``config.yaml``, bumping ``.revision`` ONCE after BOTH have landed
    -- the combined write for a provider whose Hermes equivalent is a bare environment variable
    Hermes reads directly (no ``mcp_servers`` shape, no ``config.yaml`` field of its own: M6-R31,
    e.g. ``parallel``'s ``PARALLEL_API_KEY``, verified against the image -- ``tools/web_tools.py``'s
    ``_has_env``/``_env_value`` read it straight from the environment) that ALSO needs a
    config.yaml selector patched alongside it (e.g. ``web.backend``).

    Fix round 1, Important 1: this used to be two separate calls
    (``patch_profile_env`` then ``patch_profile_config``) — if the second raised (most likely:
    ``read_profile_config(required=True)`` finding no ``config.yaml`` yet), the ``.env`` PUT from
    the first call had already landed with no ``.revision`` bump to ever surface it to the bridge
    -- a silently orphaned partial write. Reading BOTH current files before EITHER ``put_object``
    closes that failure window (the likeliest raise, a not-yet-provisioned profile, now happens
    before any write); an S3 outage strictly between the two PUTs below is the same residual
    "not transactional" risk every other multi-object writer in this module already accepts (see
    ``config_patch_roster``'s own ponytail note). Raises (``FileNotFoundError``/``ValueError``/a
    transient ``ClientError``) rather than swallowing -- the caller decides whether that's
    fatal (a connect: yes) or best-effort (a disconnect: it wraps this itself).

    A value of ``""`` in ``env_updates`` clears that key (disconnect) rather than leaving a stale
    one."""
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    prefix = profile_prefix(owner_id, agent_id)
    config = _deep_merge(read_profile_config(owner_id, agent_id, required=True), config_patch or {})
    dotenv_text = _require_text(bucket, f"{prefix}.env", ".env")
    live = {k: v for k, v in env_updates.items() if v}
    new_dotenv = _rewrite_named_env_lines(dotenv_text, set(env_updates), live)
    _s3_put(bucket, f"{prefix}.env", new_dotenv)
    _s3_put(bucket, f"{prefix}config.yaml", yaml.safe_dump(config, sort_keys=False))
    _s3_put(bucket, f"{prefix}.revision", uuid.uuid4().hex)


# ---- Row #15 / Ruling M6-R30: owner facts staged into every profile ----------------------------
# FACTS_FILENAME now lives with BACKEND_OWNED_FILES above (F3) -- it must match
# owner_facts.FACTS_FILENAME, kept as a literal there (not an import) to avoid a
# hermes_profiles <-> owner_facts import cycle: owner_facts imports THIS module to call
# write_facts_to_profiles below, so the reverse import would be circular.


def write_facts_to_profiles(owner_id: str, agent_ids: list[str], facts_json: str) -> None:
    """Upload the rendered ``lightbulb-facts.json`` to EVERY roster profile (M6-R30) -- an
    AgentCore agent has no EFS path at all (``runtime_rpc.py``'s own comment on why
    ``agents.list``/``config.*`` are served from S3), so ``owner_facts.write_owner_facts``'s EFS
    write is meaningless there; this is its per-profile equivalent, one shared boto3 client
    (``_shared_s3_client``) for the whole roster. Sync -- meant to be called through
    ``run_in_thread`` (same convention as ``refresh_integrations``'s per-agent S3 work).

    Per agent: the facts file, THEN ``.revision`` -- same "content, then revision LAST"
    discipline as ``_upload_config_yaml``/``upload_profile``/``_refresh_one_profile``, so the
    bridge's ``ensure_profile()`` re-pull never sees a bumped revision before the file it
    describes has actually landed."""
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    for agent_id in agent_ids:
        prefix = profile_prefix(owner_id, agent_id)
        _s3_put(bucket, f"{prefix}{FACTS_FILENAME}", facts_json)
        _s3_put(bucket, f"{prefix}.revision", uuid.uuid4().hex)


# ---- Task 15: ChatGPT OAuth staged into the ROOT auth.json --------------------------------------
def root_home_prefix(owner_id: str) -> str:
    """``<owner>/hermes/`` — the workspace's Hermes ROOT home; profiles live under it."""
    return f"{owner_id}/hermes/"


def _mutate_root_auth(owner_id: str, mutate) -> bool:
    """Conditionally read-modify-write root ``auth.json`` without clobbering a warm rotator."""
    import json as _json

    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        raise RuntimeError("OWNER_STATE_BUCKET is not configured; refusing to report an auth write")
    prefix = root_home_prefix(owner_id)
    key = f"{prefix}auth.json"
    for _attempt in range(3):
        existing, etag = _s3_get_versioned(bucket, key)
        blob = _json.loads(existing) if existing else {}
        if not isinstance(blob, dict):
            raise ValueError(f"root auth document for {owner_id} is not a JSON object")
        if not mutate(blob, existing is not None):
            return False
        try:
            _s3_put_conditionally(bucket, key, _json.dumps(blob), etag)
        except Exception as exc:  # noqa: BLE001 — classify S3's conditional conflict only
            if _is_precondition_failure(exc):
                continue
            raise
        # Retained while older bridge images still poll this marker. New images fence and detect
        # changes from auth.json's own ETag, so a marker race cannot resurrect a grant.
        _s3_put(bucket, f"{prefix}auth.revision", uuid.uuid4().hex)
        return True
    raise RuntimeError(f"root auth document for {owner_id} changed during three conditional writes")


#: Spec 13 §3.4: what root ``auth.json`` holds for a backend-custodied ChatGPT grant. No tokens --
#: the bridge vends them per turn and never writes this file back.
CODEX_MARKER: dict = {"custody": "backend", "tokens": {}}


class MarkerRaced(Exception):
    """``stage_codex_marker``'s one conditional put lost: ``auth.json`` moved since it was read."""


def stage_codex_marker(owner_id: str, *, expected_etag: str | None, existing: dict | None) -> None:
    """Cutover step 5 (spec 13 §6.1): replace the ``openai-codex`` entry of the ``auth.json`` the
    caller read (``existing``, at ``expected_etag``) with the marker, in ONE conditional put --
    no retry loop, because the caller holds the fences and a lost write means one was missed
    (:class:`MarkerRaced`; the caller undoes its row). Other providers' state survives; the
    compatibility ``auth.revision`` is bumped last. Sync -- call via ``run_in_thread``."""
    import json

    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        raise RuntimeError("OWNER_STATE_BUCKET is not configured; refusing to report an auth write")
    prefix = root_home_prefix(owner_id)
    blob = dict(existing or {})
    providers = blob.get("providers")
    providers = dict(providers) if isinstance(providers, dict) else {}
    providers["openai-codex"] = dict(CODEX_MARKER)
    blob["providers"] = providers
    blob["active_provider"] = "openai-codex"
    try:
        _s3_put_conditionally(bucket, f"{prefix}auth.json", json.dumps(blob), expected_etag)
    except Exception as exc:  # noqa: BLE001 -- classify S3's conditional conflict only
        if _is_precondition_failure(exc):
            raise MarkerRaced(owner_id) from exc
        raise
    _s3_put(bucket, f"{prefix}auth.revision", uuid.uuid4().hex)


def stage_codex_auth(owner_id: str, tokens: dict, *, only_if_absent: bool = False, custody: str | None = None) -> None:
    """Write the owner's ChatGPT grant into the ROOT ``auth.json``, once per connect — not into
    any per-profile ``config.yaml``/``.env``.

    Report 06 §2.2: Hermes's ``openai-codex`` provider implements natively the shape this
    platform used to hand-roll on OpenClaw — a profile with no ``providers.openai-codex`` block
    reads the ROOT home's grant through the fallback (``hermes_cli/auth.py``), and a refresh
    under that profile write-throughs to the ROOT chain (``hermes_cli/auth_codex.py``, "own
    providers.openai-codex block reads root's grant via the fallback, so a refresh under that
    profile rotates ROOT's chain"). Six per-profile copies would each independently consume the
    single-use refresh token and OpenAI would revoke the whole family. This replaces the EFS
    ``credentials/oauth.json`` + clear-auth-sentinel + boot ``doctor --fix`` choreography
    entirely for an AgentCore row.

    The write is a conditional read-modify-write against ``auth.json``'s ETag, so another
    provider and a concurrent refresh/reconnect survive rather than being clobbered. The
    compatibility ``auth.revision`` marker is written last for bridge images that still poll it;
    current images also fence warm write-through with the auth object's own ETag.

    ``custody="backend"`` (spec 13): the grant is the backend's, so what lands here is the
    :data:`CODEX_MARKER`, never the tokens -- a reconnect must not hand the VM a refresh token
    the broker is also rotating.
    """
    from datetime import datetime, timezone

    payload = {k: v for k, v in tokens.items() if k in ("access_token", "refresh_token", "id_token") and v}

    def _stage(blob: dict, _exists: bool) -> bool:
        providers = blob.get("providers")
        providers = dict(providers) if isinstance(providers, dict) else {}
        if only_if_absent and providers.get("openai-codex"):
            return False  # a reconcile never replaces a grant the runtime may have rotated
        if custody == "backend":
            providers["openai-codex"] = dict(CODEX_MARKER)
        else:
            providers["openai-codex"] = {
                "tokens": payload,
                "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "auth_mode": "chatgpt",
            }
        blob["providers"] = providers
        blob["active_provider"] = "openai-codex"
        return True

    _mutate_root_auth(owner_id, _stage)


def unstage_codex_auth(owner_id: str) -> bool:
    """Remove the owner's ChatGPT grant from the ROOT ``auth.json`` — ``stage_codex_auth``'s
    missing twin (M6 task-14 row 3 / task-15 item 2).

    Without it, "Disconnect ChatGPT" reported success and disconnected nothing on an AgentCore
    workspace: the DDB token row went, both EFS unlinks hit a tree nothing reads, and the live
    ``models.authLogout`` RPC does not exist on the bridge — so the agents kept running on, and
    billing to, the account the owner had just revoked. A credential revoke that silently no-ops
    is the one failure in that audit that is not merely a broken feature.

    Same conditional read-modify-write posture as ``stage_codex_auth``: only the
    ``openai-codex`` entry is dropped, any other provider's state survives, and the compatibility
    ``auth.revision`` marker is written last. Returns True when a grant was actually removed and
    False when no grant exists. Missing authoritative storage or a failed write raises so callers
    cannot report a false successful disconnect. Sync — call via ``run_in_thread``.
    """

    def _unstage(blob: dict, exists: bool) -> bool:
        if not exists:
            return False
        providers = blob.get("providers")
        providers = dict(providers) if isinstance(providers, dict) else {}
        if providers.pop("openai-codex", None) is None:
            return False
        blob["providers"] = providers
        if blob.get("active_provider") == "openai-codex":
            blob.pop("active_provider", None)
        return True

    return _mutate_root_auth(owner_id, _unstage)
