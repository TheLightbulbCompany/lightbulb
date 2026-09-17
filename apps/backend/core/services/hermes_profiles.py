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

import re
import uuid
from dataclasses import dataclass

import yaml

from core.config import settings
from core.containers.config import cheap_model_ref, premium_model_ref

_BEDROCK = "bedrock"

#: Every auxiliary task we pin. Hermes's auto-chain is main provider -> OpenRouter -> Nous Portal
#: -> ... (auxiliary_client.py:3-6); an unpinned task on a platform-billed path is egress to a
#: vendor we have no account with, and the Nous lane needs no credential of ours (``nous.guest:
#: true`` in the defaults). ``fallback_chain: []`` is the actual fence (an empty configured chain
#: skips straight to no-fallback rather than the auto-chain); ``free_only`` is a cost knob ON that
#: auto-chain, not a second fence — it still lets a ":free" OpenRouter SKU through, and Hermes
#: falls through to auto-detection even for an explicit ``provider`` the account can't invoke
#: (auxiliary_client.py's unavailable-provider fallback), which is why ``bedrock.discovery.enabled:
#: false`` and a present ``AWS_BEARER_TOKEN_BEDROCK`` both have to hold too.
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

#: The ``TOOL_DENY_BASE`` port. canvas/music_generate have no Hermes toolset; dashboard and cron
#: are switches, not toolsets (report 01 §2.7).
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


def _model_block(provider_choice, byo_provider, aip_arns, is_ceo) -> tuple[dict, str, str]:
    """(model block, aux model id, aux provider)."""
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
    # chatgpt_oauth — flat rate, one tier; aux inherits the main model ("auto"), because the Codex
    # backend rejects an aux-side summariser override (config.py cheap_model_ref docstring).
    return {"default": "gpt-5.6-sol", "provider": "openai-codex"}, "", "auto"


def build_profile_config(
    *,
    agent_id: str,
    agent_role: str,
    provider_choice: str,
    byo_provider: str | None = None,
    aip_arns: dict[str, str] | None = None,
    mcp_servers: dict | None = None,
    is_ceo: bool = False,
    listening_paid: bool = True,
) -> dict:
    model, aux_model, aux_provider = _model_block(provider_choice, byo_provider, aip_arns, is_ceo)
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
        "dashboard": {"enabled": False},
        "memory": {
            "memory_enabled": True,
            "user_profile_enabled": True,
            "write_approval": False,
            "provider": "",  # built-in: never pays for embeddings on a platform-billed path
            "memory_char_limit": 2200,
            "user_char_limit": 1375,
        },
        "skills": {
            "write_approval": False,
            "guard_agent_created": not listening_paid,
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
        config["bedrock"] = {"region": "us-east-1", "discovery": {"enabled": False}}
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
#: shape and a stray key is a spawn-time error, not a warning.
_MCP_DIRECT = ("command", "args", "env", "cwd", "url", "headers", "auth", "oauth", "enabled")
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
    for name, block in (servers or {}).items():
        entry: dict = {key: block[key] for key in _MCP_DIRECT if key in block}
        for field in _MCP_SECRET_FIELDS:
            values = entry.get(field)
            if isinstance(values, dict):
                moved = {}
                for var_name, value in values.items():
                    env_key = _mcp_env_key(name, var_name)
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
  check_on_start: false
approvals:
  # Blocks even under mode=off / --yolo. Belt for the profile's own copy.
  deny:
    - "rm -rf /"
    - "mkfs*"
    - "dd if=* of=/dev/*"
    - ":(){:|:&};:"
"""


@dataclass
class ProfileFiles:
    config_yaml: str
    dotenv: str
    soul_md: str
    managed_overlay: str


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
) -> ProfileFiles:
    """The four files that ARE one agent's Hermes profile.

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
    """
    mcp_config, mcp_secrets = translate_mcp_servers(integrations)
    config = build_profile_config(
        agent_id=agent["id"],
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
        soul_md=build_soul_md(agent),
        managed_overlay=MANAGED_OVERLAY_YAML,
    )


def _s3_put(bucket: str, key: str, body: str) -> None:
    import boto3

    boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode())


def profile_prefix(owner_id: str, agent_id: str) -> str:
    """``<owner>/hermes/<agentId>/`` — owner FIRST.

    The per-owner IAM grant is ``arn:aws:s3:::<bucket>/<owner>/*``
    (bedrock_aip.build_owner_litestream_s3_policy). Any other order is outside every owner's
    grant and AccessDenies on the first restore.
    """
    return f"{owner_id}/hermes/{agent_id}/"


def upload_profile(owner_id: str, agent_id: str, files: ProfileFiles) -> None:
    bucket = settings.OWNER_STATE_BUCKET
    if not bucket:
        return
    prefix = profile_prefix(owner_id, agent_id)
    _s3_put(bucket, f"{prefix}config.yaml", files.config_yaml)
    _s3_put(bucket, f"{prefix}.env", files.dotenv)
    _s3_put(bucket, f"{prefix}SOUL.md", files.soul_md)
    _s3_put(bucket, f"{prefix}managed/config.yaml", files.managed_overlay)
    # LAST, always: the bridge re-pulls a profile when this object's value changes (Task 13, this
    # lane's continuation). Writing it before the files it describes would advertise a config that
    # is not there yet. A uuid4 hex, not a timestamp (C4, fix round 1) — two uploads racing within
    # the same millisecond would otherwise write an identical ".revision" value and the bridge's
    # diff-and-repull could miss the second one.
    _s3_put(bucket, f"{prefix}.revision", uuid.uuid4().hex)
