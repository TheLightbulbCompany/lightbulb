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
