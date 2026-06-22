"""Unit tests asserting the Paperclip MCP server block is GONE from
build_openclaw_config_dict.

Paperclip is no longer wired as an MCP server: the MCP env is container-wide +
shared-frozen, so every agent that called it acted as Friday (wrong
attribution). Each agent now authenticates to Paperclip with its own per-agent
key via the bundled `paperclip` skill + a per-agent identity file. These tests
lock in the removal: config["mcp"]["servers"] must never carry a "paperclip"
entry, while integration servers (Stripe, …) still merge and runtimeScope is
unaffected.
"""

from __future__ import annotations

from core import openclaw_version
from core.containers.config import build_openclaw_config_dict

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


def _ceo_agent(uuid: str = "f47ac10b-58cc-4372-a567-0e02b2c3d479") -> dict:
    return {"id": uuid, "name": "Friday", "role": "ceo"}


def test_no_paperclip_mcp_block_even_with_a_ceo_roster():
    """A CEO in the roster used to trigger the paperclip MCP block; it is now
    removed entirely. No mcp block at all when there are no integration
    servers."""
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
        paperclip_agents=[_ceo_agent("friday-uuid")],
    )
    assert "mcp" not in cfg


def test_build_openclaw_config_dict_rejects_removed_paperclip_params():
    """The Friday-keyed paperclip_* params were dropped from the signature."""
    import pytest

    with pytest.raises(TypeError):
        build_openclaw_config_dict(
            user_id="user_x",
            gateway_token="t" * 32,
            provider_choice="bedrock_claude",
            aip_arns=_AIPS,
            paperclip_agents=[_ceo_agent("friday-uuid")],
            paperclip_api_key="pcli_live_abc",  # removed kwarg
        )


def test_integration_mcp_servers_still_merge_without_paperclip():
    """Connected integration servers (Stripe etc.) still land in
    config["mcp"]["servers"] — and there is no paperclip sibling."""
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
        paperclip_agents=[_ceo_agent("friday-uuid")],
        integration_mcp_servers={
            "stripe": {
                "command": "npx",
                "args": ["-y", "@stripe/mcp"],
                "env": {"STRIPE_SECRET_KEY": "sk_X"},
            },
        },
    )

    servers = cfg["mcp"]["servers"]
    assert servers["stripe"]["command"] == "npx"
    assert servers["stripe"]["env"]["STRIPE_SECRET_KEY"] == "sk_X"
    assert "paperclip" not in servers


def test_no_mcp_block_when_no_integration_servers_and_no_agents():
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
        paperclip_agents=[],
    )
    assert "mcp" not in cfg


def test_defaults_heartbeat_is_explicit_30m():
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
    )
    # Explicit cadence (not an empty dict) so the 30m internal heartbeat is
    # visible in our config, not silently inherited from upstream defaults.
    heartbeat = cfg["agents"]["defaults"]["heartbeat"]
    assert heartbeat["every"] == "30m"
    # The heartbeat is pure overhead and runs on the cheap tier — see
    # test_aux_operations_routed_to_cheap_tier for the full per-provider matrix.
    assert heartbeat["model"] == f"amazon-bedrock/{_AIPS['haiku']}"


def test_mcp_runtime_scope_shared_when_deployed_image_supports_it(monkeypatch):
    """When this env's deployed image is >= 2026.6.1, pin the bundled MCP
    runtime to "shared" so connected servers are reused across sessions (single
    owner per container, so no cross-session bleed). Only meaningful when an
    mcp block exists — exercised here via an integration server."""
    monkeypatch.setattr(openclaw_version, "deployed_upstream_at_least", lambda target: True)
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
        paperclip_agents=[_ceo_agent("friday-uuid")],
        integration_mcp_servers={
            "stripe": {
                "command": "npx",
                "args": ["-y", "@stripe/mcp"],
                "env": {"STRIPE_SECRET_KEY": "sk_X"},
            },
        },
    )
    assert cfg["mcp"]["runtimeScope"] == "shared"


def test_mcp_runtime_scope_omitted_on_pre_6_1_image(monkeypatch):
    """A container still on the pre-6.1 image must NOT receive runtimeScope — its
    strict MCP schema rejects the unknown key and crash-loops at boot."""
    monkeypatch.setattr(openclaw_version, "deployed_upstream_at_least", lambda target: False)
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        aip_arns=_AIPS,
        paperclip_agents=[_ceo_agent("friday-uuid")],
        integration_mcp_servers={
            "stripe": {
                "command": "npx",
                "args": ["-y", "@stripe/mcp"],
                "env": {"STRIPE_SECRET_KEY": "sk_X"},
            },
        },
    )
    assert "runtimeScope" not in cfg["mcp"]
