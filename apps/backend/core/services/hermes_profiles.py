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
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

import yaml

from core.config import settings
from core.containers.config import cheap_model_ref, premium_model_ref

_BEDROCK = "bedrock"

#: Every auxiliary task we pin. Hermes's auto-chain is main provider -> OpenRouter -> Nous Portal
#: -> ... (auxiliary_client.py:3-6); an unpinned task on a platform-billed path is egress to a
#: vendor we have no account with. ``fallback_chain: []`` plus ``free_only`` is belt and braces.
#:
#: M4-R2: mirrors the task names M1's bridge (``apps/infra/hermes/bridge/sync.py::auxiliary_pin``)
#: derives dynamically from Hermes's OWN ``hermes_cli.config_defaults.DEFAULT_CONFIG["auxiliary"]``
#: (any dict-valued key carrying a ``provider`` field). This backend module can't read that
#: package at request time, so it is pinned to the same set the bridge's own tests
#: (``apps/infra/hermes/tests/test_auxiliary_pin.py::AUX_DEFAULTS``) exercise against the real
#: bridge code — the only in-repo ground truth for the task-block shape.
AUXILIARY_TASKS = ("vision", "compression", "title_generation", "review")

#: Hard backstop: a match blocks even under ``--yolo``/mode=off (config_defaults.py:1582-1583).
APPROVALS_DENY = ("rm -rf /", "mkfs*", "dd if=* of=/dev/*", ":(){:|:&};:")

#: The ``TOOL_DENY_BASE`` port. canvas/music_generate have no Hermes toolset; dashboard and cron
#: are switches, not toolsets (report 01 §2.7).
_DISABLED_TOOLSETS = ("cronjob", "tts", "video_gen")

#: M4-R3: the opt-in is a flat allow-list (``plugins.enabled``), never ``plugins.entries`` —
#: Hermes's ``hermes_cli/plugins_discovery.py::gate_manifest`` reads only ``plugins.enabled``.
_LIGHTBULB_PLUGINS = ("lightbulb", "lightbulb_node")

_BYO_MODELS = {
    "anthropic": ("anthropic", "claude-sonnet-5", "claude-opus-5", "claude-haiku-4-5"),
    "openai": ("openai-api", "gpt-5.4", "gpt-5.6-sol", "gpt-5.4-mini"),
}


def _bedrock_ids(provider_choice, byo_provider, aip_arns, is_ceo):
    default = aip_arns["sonnet"]
    strip = lambda ref: ref.split("/", 1)[1] if ref else None  # noqa: E731 — "amazon-bedrock/<arn>"
    premium = strip(premium_model_ref(provider_choice, byo_provider, aip_arns=aip_arns))
    cheap = strip(cheap_model_ref(provider_choice, byo_provider, aip_arns=aip_arns))
    return (premium or default) if is_ceo else default, cheap or default


def _model_block(provider_choice, byo_provider, aip_arns, is_ceo) -> tuple[dict, str, str]:
    """(model block, aux model id, aux provider)."""
    if provider_choice in ("bedrock_claude", "bedrock_trial"):
        if not aip_arns:
            raise ValueError(f"{provider_choice} requires aip_arns")
        default, cheap = _bedrock_ids(provider_choice, byo_provider, aip_arns, is_ceo)
        return {"default": default, "provider": _BEDROCK}, cheap, _BEDROCK
    if provider_choice == "byo_key":
        provider, standard, premium, cheap = _BYO_MODELS[byo_provider or "anthropic"]
        return {"default": premium if is_ceo else standard, "provider": provider}, cheap, provider
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


def build_profile_env(
    *,
    gate_bearer_ref: str,
    gate_url: str,
    region: str,
    paperclip_key_ref: str,
    env_vars: dict[str, str],
    byo_key_ref: str | None = None,
) -> dict[str, str]:
    """The profile's ``.env``.

    Credentials the PLATFORM owns arrive as a Secrets Manager NAME, not a value: AgentCore
    ``environmentVariables`` are plaintext to anyone with ``GetAgentRuntime`` (spec §4.9) and this
    file rides S3. ``sync.load_secrets`` (Task 20) resolves every ``*_SECRET`` name at boot with the
    execution role. Owner-provisioned values (``env_vars``, from ``build_provisioned_env``) are
    carried as-is — the same rail ``openclaw.json``'s ``env.vars`` used.
    """
    env = {
        "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": gate_url,
        "AWS_REGION": region,
        "LIGHTBULB_GATE_BEARER_SECRET": gate_bearer_ref,
        "LIGHTBULB_PAPERCLIP_KEY_SECRET": paperclip_key_ref,
    }
    if byo_key_ref:
        env["LIGHTBULB_BYO_KEY_SECRET"] = byo_key_ref
    env.update(env_vars or {})
    return env


def render_dotenv(env: dict[str, str]) -> str:
    lines = []
    for key, value in env.items():
        if not _KEY_RE.match(key):
            raise ValueError(f"illegal dotenv key: {key!r}")
        text = str(value)
        if _PLAIN_RE.match(text):
            lines.append(f"{key}={text}")
        else:
            lines.append(f'{key}="' + text.replace('"', '\\"') + '"')
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


def translate_mcp_servers(servers: dict | None) -> dict:
    out: dict = {}
    for name, block in (servers or {}).items():
        entry: dict = {key: block[key] for key in _MCP_DIRECT if key in block}
        for ms_key, s_key in _MCP_MS_TO_S.items():
            if isinstance(block.get(ms_key), int):
                entry[s_key] = block[ms_key] // 1000
        tools = block.get("tools")
        if isinstance(tools, dict):
            entry["tools"] = {k: tools[k] for k in ("include", "exclude") if k in tools}
        sampling = block.get("sampling")
        if isinstance(sampling, dict):
            entry["sampling"] = {v: sampling[k] for k, v in _MCP_SAMPLING.items() if k in sampling}
        out[name] = entry
    return out


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


def _gate_bearer_ref(owner_id: str) -> str:
    return f"lightbulb/{settings.ENVIRONMENT}/gate-bearer/{owner_id}"


def _paperclip_key_ref(owner_id: str, agent_id: str) -> str:
    return f"lightbulb/{settings.ENVIRONMENT}/paperclip/{owner_id}/{agent_id}"


def write_agent_profile(
    owner_id: str,
    agent: dict,
    *,
    provider_choice: str,
    aip_arns: dict[str, str],
    integrations: list[dict],
    env_vars: dict[str, str],
) -> ProfileFiles:
    """The four files that ARE one agent's Hermes profile.

    ``integrations`` is the owner's connected-integration list (each row carries the MCP block
    ``build_connected_integration_servers`` emits under ``key``); ``env_vars`` is
    ``provisioning_landing.build_provisioned_env(owner_id)``.
    """
    from core.billing.bedrock_gate_url import gate_url

    servers = {row["key"]: {k: v for k, v in row.items() if k != "key"} for row in integrations or []}
    config = build_profile_config(
        agent_id=agent["id"],
        agent_role=agent.get("role") or "general",
        provider_choice=provider_choice,
        byo_provider=agent.get("byo_provider"),
        aip_arns=aip_arns,
        mcp_servers=translate_mcp_servers(servers),
        is_ceo=(agent.get("role") == "ceo"),
    )
    env = build_profile_env(
        gate_bearer_ref=_gate_bearer_ref(owner_id),
        gate_url=gate_url() or "",
        region=settings.AWS_REGION,
        paperclip_key_ref=_paperclip_key_ref(owner_id, agent["id"]),
        env_vars=env_vars,
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
    # LAST, always: the bridge re-pulls a profile when this object's value changes (Task 13).
    # Writing it before the files it describes would advertise a config that is not there yet.
    _s3_put(bucket, f"{prefix}.revision", str(int(time.time() * 1000)))
