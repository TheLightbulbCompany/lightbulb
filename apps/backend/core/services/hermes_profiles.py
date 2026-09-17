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
Runtime environment     AWS_ENDPOINT_URL_BEDROCK_RUNTIME, AWS_REGION,   Task 17, in the AgentCore
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

import hashlib
import re
import uuid
from dataclasses import dataclass

import yaml

from core.config import settings
from core.containers.config import cheap_model_ref, premium_model_ref
from core.dynamodb import run_in_thread

_BEDROCK = "bedrock"

#: Every auxiliary task we pin. Hermes's auto-chain is main provider -> OpenRouter -> Nous Portal
#: -> ... (auxiliary_client.py:3-6); an unpinned task on a platform-billed path is egress to a
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
#: The bridge (``apps/infra/hermes/bridge/sync.py::auxiliary_pin``) reads the real package at
#: runtime and so stays correct across a Hermes bump on its own; this backend-side constant does
#: not, and needs a human to re-run the command above and update the tuple when Hermes moves.
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
_DISABLED_TOOLSETS = ("cronjob", "tts", "video_gen")

#: M4-R3: the opt-in is a flat allow-list (``plugins.enabled``), never ``plugins.entries`` —
#: Hermes's ``hermes_cli/plugins_discovery.py::gate_manifest`` reads only ``plugins.enabled``.
_LIGHTBULB_PLUGINS = ("lightbulb", "lightbulb_node")

#: Fix round 1, I4: ONLY the Hermes provider NAME (a namespace this module alone owns — Hermes's
#: own "openai-api" differs from the openclaw-style "openai" ref prefix ``core.containers.config``
#: uses) and the "standard" (non-premium, non-cheap) default-tier id, which has no standalone
#: function anywhere — it only lives inline inside ``build_provider_block``'s ``byo_key`` branch.
#: The premium/cheap ids themselves are NOT duplicated here any more: they come straight from
#: ``premium_model_ref``/``cheap_model_ref`` below, the same functions the Bedrock branch already
#: calls — those two have already broken a hardcoded ref across two catalog rotations (see their
#: own docstrings), and a second local copy is exactly the class of bug that causes.
_BYO_HERMES_PROVIDER = {"anthropic": ("anthropic", "claude-sonnet-5"), "openai": ("openai-api", "gpt-5.4")}


def _strip_provider_prefix(ref: str | None) -> str | None:
    """``"<provider>/<id>"`` -> ``"<id>"``; ``None`` stays ``None``."""
    return ref.split("/", 1)[1] if ref else None


def _bedrock_ids(provider_choice, byo_provider, aip_arns, is_ceo):
    default = aip_arns["sonnet"]
    premium = _strip_provider_prefix(premium_model_ref(provider_choice, byo_provider, aip_arns=aip_arns))
    cheap = _strip_provider_prefix(cheap_model_ref(provider_choice, byo_provider, aip_arns=aip_arns))
    return (premium or default) if is_ceo else default, cheap or default


def model_block(provider_choice, byo_provider, aip_arns, is_ceo) -> tuple[dict, str, str]:
    """(model block, aux model id, aux provider).

    Public (Q9a fix round 1, M6-R28): this is the ONE place that decides an agent's
    ``config.yaml`` ``model`` key from ``(provider_choice, byo_provider, aip_arns, is_ceo)`` --
    ``build_profile_config`` uses it for a fresh profile write, and ``runtime_rpc``'s
    ``agents.update`` door (an AgentCore role/model-tier change) re-derives the same block
    through this function rather than hand-rolling or trusting a client-supplied ref.
    """
    if provider_choice in ("bedrock_claude", "bedrock_trial"):
        if not aip_arns:
            raise ValueError(f"{provider_choice} requires aip_arns")
        default, cheap = _bedrock_ids(provider_choice, byo_provider, aip_arns, is_ceo)
        return {"default": default, "provider": _BEDROCK}, cheap, _BEDROCK
    if provider_choice == "byo_key":
        hermes_provider, standard = _BYO_HERMES_PROVIDER[byo_provider or "anthropic"]
        premium = _strip_provider_prefix(premium_model_ref(provider_choice, byo_provider, aip_arns=aip_arns))
        cheap = _strip_provider_prefix(cheap_model_ref(provider_choice, byo_provider, aip_arns=aip_arns))
        default = premium if is_ceo else standard
        return {"default": default, "provider": hermes_provider}, cheap or standard, hermes_provider
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


def build_profile_config(
    *,
    agent_role: str,
    provider_choice: str,
    byo_provider: str | None = None,
    aip_arns: dict[str, str] | None = None,
    mcp_servers: dict | None = None,
    is_ceo: bool = False,
) -> dict:
    model, aux_model, aux_provider = model_block(provider_choice, byo_provider, aip_arns, is_ceo)
    aux: dict = {"free_only": True, "transient_retries": 2}
    for task in AUXILIARY_TASKS:
        aux[task] = {"provider": aux_provider, "model": aux_model, "fallback_chain": []}
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
            "user_char_limit": 1375,
        },
        # No "guard_agent_created" (fix round 1, minor): it is a real key (config_defaults.py:1373,
        # default False, gates skill_manager_tool's terminal() execution path), but the platform
        # has no signal that belongs there — "listening" is a per-room chat mode the backend
        # enforces, unrelated to skill-creation gating. Leaving it unset keeps Hermes's own default.
        "skills": {
            "write_approval": False,
            "external_dirs": ["/opt/hermes-skills"],
        },
        "sessions": {
            "auto_prune": True,
            "retention_days": 90,
            "auto_archive": True,
            "vacuum_after_prune": True,
        },
        "terminal": {"backend": "lightbulb_node"},
        "mcp_servers": dict(mcp_servers or {}),
        "plugins": {"enabled": list(_LIGHTBULB_PLUGINS)},
    }
    if model["provider"] == _BEDROCK:
        # One source for the region (fix round 1, minor): settings.AWS_REGION, same as .env's
        # AWS_REGION used to read before C3 moved that var to Task 17's runtime environment.
        config["bedrock"] = {"region": settings.AWS_REGION, "discovery": {"enabled": False}}
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
    paperclip_api_key: str,
    env_vars: dict[str, str],
    byo_provider: str | None = None,
    byo_api_key: str | None = None,
) -> dict[str, str]:
    """The profile's ``.env`` — per-profile secret VALUES only (see the module docstring's scope
    table). Never the gate endpoint, region, or a Secrets Manager REFERENCE name: the gate bearer
    arrives via the boot secret and the bridge exports ``AWS_BEARER_TOKEN_BEDROCK`` process-wide;
    ruling M4-R4/R5 cancelled the per-credential ``*_SECRET``-name resolver this function used to
    write, and nothing in ``bridge/`` ever read those names, so every value here is now the real,
    already-resolved secret — not a name for a later lookup.

    ``paperclip_api_key`` is the resolved Paperclip API key value for this owner+agent.
    ``byo_provider``/``byo_api_key`` are OWNER-level (never read from the Paperclip roster row,
    which carries no such field — see ``write_agent_profile``'s C2 fix) and only emitted when both
    are given, so a non-BYO profile's ``.env`` carries neither key.

    Owner-provisioned values (``env_vars``, from ``build_provisioned_env``) are carried as-is — the
    same rail ``openclaw.json``'s ``env.vars`` used.
    """
    env = {"PAPERCLIP_API_KEY": paperclip_api_key}
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


def _mcp_env_key(server_name: str, var_name: str) -> str:
    """``MCP_<SERVER>_<VAR>``, both segments sanitized to a valid dotenv/shell identifier."""
    server = _NON_IDENT_RE.sub("_", server_name).strip("_").upper() or "SERVER"
    var = _NON_IDENT_RE.sub("_", var_name).strip("_").upper() or "VALUE"
    return f"MCP_{server}_{var}"


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


def build_soul_md(agent: dict, *, company_name: str | None = None) -> str:
    """The profile's ``SOUL.md`` — the agent's identity, from the Paperclip roster row.

    Hermes reads ``$HERMES_HOME/SOUL.md`` per profile (config_home.py:66). There is no
    ``agents.list[*].name`` to carry a display name any more, so the name lives here and in the
    Paperclip row, and nowhere in config.yaml.
    """
    name = agent.get("name") or agent["id"]
    role = agent.get("role") or "general"
    lines = [f"# {name}", ""]
    if agent.get("title"):
        lines += [f"**{agent['title']}**", ""]
    where = f" at {company_name}" if company_name else ""
    lines.append(f"You are {name}, the {role} agent{where} on Lightbulb.")
    if role == "ceo":
        lines.append(_CEO_LINE)
    if agent.get("capabilities"):
        lines += ["", "## What you do", "", agent["capabilities"]]
    return "\n".join(lines) + "\n"


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


def write_agent_profile(
    owner_id: str,
    agent: dict,
    *,
    provider_choice: str,
    aip_arns: dict[str, str],
    paperclip_api_key: str,
    integrations: dict[str, dict],
    env_vars: dict[str, str],
    byo_provider: str | None = None,
    byo_api_key: str | None = None,
    soul_md_override: str | None = None,
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
    choice is threaded next to ``provider_choice`` everywhere else in the codebase
    (``ecs_manager.py``); reading it from the roster row silently pinned every BYO owner to
    Anthropic with no key delivered anywhere.

    ``soul_md_override`` (M6 lane R1): when given, replaces the generated ``build_soul_md``
    identity outright — the one hook the ECS-to-Hermes workspace migration job needs to carry
    an agent's existing EFS ``SOUL.md`` verbatim instead of a freshly generated identity.
    """
    mcp_config, mcp_secrets = translate_mcp_servers(integrations)
    config = build_profile_config(
        agent_role=agent.get("role") or "general",
        provider_choice=provider_choice,
        byo_provider=byo_provider,
        aip_arns=aip_arns,
        mcp_servers=mcp_config,
        is_ceo=(agent.get("role") == "ceo"),
    )
    # mcp_secrets first, env_vars second: an owner-provisioned value should win a name collision
    # over a generated MCP_<SERVER>_<VAR> one (unlikely given the prefix, but never the reverse).
    env = build_profile_env(
        paperclip_api_key=paperclip_api_key,
        env_vars={**mcp_secrets, **(env_vars or {})},
        byo_provider=byo_provider,
        byo_api_key=byo_api_key,
    )
    return ProfileFiles(
        config_yaml=yaml.safe_dump(config, sort_keys=False),
        dotenv=render_dotenv(env),
        soul_md=soul_md_override if soul_md_override is not None else build_soul_md(agent),
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
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    client = await run_in_thread(_new_s3_client)
    prefix = profile_prefix(owner_id, agent_id)
    for name, data in files.items():
        await run_in_thread(client.put_object, Bucket=bucket, Key=f"{prefix}{name}", Body=data)
    await run_in_thread(client.put_object, Bucket=bucket, Key=f"{prefix}.revision", Body=uuid.uuid4().hex.encode())


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


# ---- Task 13a: config.get/config.patch/config.set on an AgentCore row --------------------------
# Spec §4.5: "Config is read once per AIAgent construction and cached on file mtime, so rewriting
# profiles/<id>/config.yaml takes effect on the agent's next turn. No watcher, no restart." The
# backend is the ONLY writer of a Hermes profile, so a hot patch is an S3 rewrite plus a revision
# bump — never a round trip to a live microVM. This is what deletes config_patcher.py's EFS locks
# for an AgentCore row (the ECS path is untouched and keeps using them).
#
# M4-R49 audit (fix round 2 continuation): every sync-boto3 writer in this module reached from a
# coroutine, and how it stays off the event loop --
#   config_snapshot / patch_profile_config / set_profile_config -- one external caller each
#     (runtime_rpc.execute_sync), every call already wrapped in run_in_thread.
#   read_profile_config -- no external caller; only used internally by patch_profile_config
#     above, itself always thread-wrapped by ITS caller.
#   _upload_config_yaml -- no external caller; only used internally by patch_profile_config/
#     set_profile_config above, same coverage.
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
    (``config.patch``/``config.set``, and Task 14's token-file writes) — each stays a pure sync
    function (matching its own tests); their one async caller, ``runtime_rpc.execute_sync``,
    wraps the call in ``core.dynamodb.run_in_thread`` so the blocking I/O here never touches the
    event loop directly. ``upload_profile`` above is the higher-fanout path (up to five objects
    per agent) and owns its own one-client-reused async design instead."""
    _shared_s3_client().put_object(Bucket=bucket, Key=key, Body=body.encode())


def _s3_delete(bucket: str, key: str) -> None:
    _shared_s3_client().delete_object(Bucket=bucket, Key=key)


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


def set_profile_config(owner_id: str, agent_id: str, config: dict) -> dict:
    """``config.set`` — a full REPLACE, not a merge (unlike ``patch_profile_config``): a key
    present in the stored config but absent from ``config`` does not survive. The caller is
    responsible for handing over a complete profile shape."""
    config = config or {}
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
    (``PAPERCLIP_API_KEY``, a BYO key, an owner-provisioned env var) passes through untouched.
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
    config["mcp_servers"] = servers
    _write_mcp_token_files(owner_id, agent_id, connected, credentials, previously_oauth=previously_oauth)
    _s3_put(bucket, f"{prefix}config.yaml", yaml.safe_dump(config, sort_keys=False))
    _s3_put(bucket, f"{prefix}.env", _rewrite_mcp_env_lines(dotenv_text, secrets))
    _s3_put(bucket, f"{prefix}.revision", uuid.uuid4().hex)


# ---- Task 15: ChatGPT OAuth staged into the ROOT auth.json --------------------------------------
def root_home_prefix(owner_id: str) -> str:
    """``<owner>/hermes/`` — the workspace's Hermes ROOT home; profiles live under it."""
    return f"{owner_id}/hermes/"


def stage_codex_auth(owner_id: str, tokens: dict) -> None:
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

    C3 (fix round 1 continuation, M4-R44) + minor 6: read-modify-write, not an unconditional
    overwrite — an existing ``auth.json`` can hold OTHER providers' state, and clobbering the
    whole document would silently drop it. ``<owner>/hermes/auth.revision`` is written LAST,
    always, for the same reason a profile's own ``.revision`` is: the bridge's
    ``restore_root_auth`` (per-turn revision compare, same shape as ``ensure_profile``) must
    never see a revision bump before ``auth.json`` itself has landed.
    """
    import json as _json
    from datetime import datetime, timezone

    if not settings.OWNER_STATE_BUCKET:
        return
    prefix = root_home_prefix(owner_id)
    existing = _s3_get(settings.OWNER_STATE_BUCKET, f"{prefix}auth.json")
    blob = _json.loads(existing) if existing else {}
    if not isinstance(blob, dict):
        blob = {}
    providers = blob.get("providers")
    providers = dict(providers) if isinstance(providers, dict) else {}
    payload = {k: v for k, v in tokens.items() if k in ("access_token", "refresh_token", "id_token") and v}
    providers["openai-codex"] = {
        "tokens": payload,
        "last_refresh": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "auth_mode": "chatgpt",
    }
    blob["providers"] = providers
    blob["active_provider"] = "openai-codex"
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}auth.json", _json.dumps(blob))
    _s3_put(settings.OWNER_STATE_BUCKET, f"{prefix}auth.revision", uuid.uuid4().hex)
