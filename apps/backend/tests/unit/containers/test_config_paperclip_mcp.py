"""Unit tests for the Paperclip MCP server block in build_openclaw_config_dict.

The block lives at config["mcp"]["servers"]["paperclip"] and only
emits when we have:
  - paperclip_api_key
  - paperclip_company_id
  - paperclip_api_url
  - a CEO agent in paperclip_agents

Routers/integrations.py keys on the same path, so the block is also
visible from the MCP Servers control-panel surface.
"""

from __future__ import annotations

import pytest

from core import openclaw_version
from core.containers.config import build_openclaw_config_dict


def _ceo_agent(uuid: str = "f47ac10b-58cc-4372-a567-0e02b2c3d479") -> dict:
    return {"id": uuid, "name": "Friday", "role": "ceo"}


def test_mcp_block_emitted_when_all_inputs_present():
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[_ceo_agent("friday-uuid")],
        paperclip_api_key="pcli_live_abc",
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
    )

    entry = cfg["mcp"]["servers"]["paperclip"]
    # Stays on npx until the baked image tag is rolled out (the bare
    # `paperclip-mcp-server` command requires the new image to be live).
    assert entry["command"] == "npx"
    assert entry["args"] == ["-y", "@paperclipai/mcp-server"]
    env = entry["env"]
    assert env["PAPERCLIP_API_URL"] == "http://paperclip.internal:3100"
    assert env["PAPERCLIP_API_KEY"] == "pcli_live_abc"
    assert env["PAPERCLIP_COMPANY_ID"] == "comp-1"
    assert env["PAPERCLIP_AGENT_ID"] == "friday-uuid"


def test_mcp_block_omitted_when_api_key_missing():
    """Most common failure mode: Paperclip outage during provision means
    ensure_paperclip_api_key returns None. The openclaw config should
    still be valid — just no MCP block. Next reprovision self-heals."""
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[_ceo_agent()],
        paperclip_api_key=None,
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
    )
    assert "mcp" not in cfg


@pytest.mark.parametrize(
    "missing",
    ["paperclip_company_id", "paperclip_api_url"],
)
def test_mcp_block_omitted_when_any_required_input_missing(missing: str):
    kwargs = dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[_ceo_agent()],
        paperclip_api_key="pcli_live_abc",
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
    )
    kwargs[missing] = None

    cfg = build_openclaw_config_dict(**kwargs)
    assert "mcp" not in cfg


def test_mcp_block_omitted_when_no_ceo_in_roster():
    """Edge case: paperclip_agents exists but no agent has role='ceo'
    (operator manually deleted Friday). Skip the block — there's no
    canonical agent to bind to."""
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[
            {"id": "atlas-uuid", "name": "Atlas", "role": "recruiter"},
        ],
        paperclip_api_key="pcli_live_abc",
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
    )
    assert "mcp" not in cfg


def test_mcp_block_picks_ceo_not_first_agent():
    """When paperclip_agents has multiple agents and the CEO isn't first,
    PAPERCLIP_AGENT_ID still resolves to the CEO."""
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[
            {"id": "atlas-uuid", "name": "Atlas", "role": "recruiter"},
            {"id": "friday-uuid", "name": "Friday", "role": "ceo"},
        ],
        paperclip_api_key="pcli_live_abc",
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
    )
    assert cfg["mcp"]["servers"]["paperclip"]["env"]["PAPERCLIP_AGENT_ID"] == "friday-uuid"


def test_mcp_block_omitted_when_paperclip_agents_empty():
    """No agents seeded yet → no MCP block. The fallback main agent isn't
    a Paperclip agent and shouldn't authenticate against Paperclip's API."""
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[],
        paperclip_api_key="pcli_live_abc",
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
    )
    assert "mcp" not in cfg


def test_integration_mcp_servers_merged_alongside_paperclip():
    """Connected integration servers (Stripe etc.) are merged into
    config["mcp"]["servers"] at provision time so a reprovision's full
    overwrite no longer drops them — and the paperclip block survives
    alongside them."""
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[_ceo_agent("friday-uuid")],
        paperclip_api_key="pcli_live_abc",
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
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
    # The paperclip block is preserved alongside the integration server.
    assert servers["paperclip"]["command"] == "npx"
    assert servers["paperclip"]["env"]["PAPERCLIP_AGENT_ID"] == "friday-uuid"


def test_integration_mcp_servers_merged_when_paperclip_absent():
    """When no paperclip block is emitted (config["mcp"] absent), the
    integration servers still land — the merge creates mcp/servers."""
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[],
        integration_mcp_servers={
            "stripe": {
                "command": "npx",
                "args": ["-y", "@stripe/mcp"],
                "env": {"STRIPE_SECRET_KEY": "sk_X"},
            },
        },
    )

    assert cfg["mcp"]["servers"]["stripe"]["command"] == "npx"
    assert "paperclip" not in cfg["mcp"]["servers"]


def test_defaults_heartbeat_is_explicit_30m():
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
    )
    # Explicit cadence (not an empty dict) so the 30m internal heartbeat is
    # visible in our config, not silently inherited from upstream defaults.
    assert cfg["agents"]["defaults"]["heartbeat"] == {"every": "30m"}


def test_mcp_runtime_scope_shared_when_deployed_image_supports_it(monkeypatch):
    """When this env's deployed image is >= 2026.6.1, pin the bundled MCP
    runtime to "shared" so connected servers are reused across sessions (single
    owner per container, so no cross-session bleed)."""
    monkeypatch.setattr(openclaw_version, "deployed_upstream_at_least", lambda target: True)
    cfg = build_openclaw_config_dict(
        user_id="user_x",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[_ceo_agent("friday-uuid")],
        paperclip_api_key="pcli_live_abc",
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
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
        paperclip_agents=[_ceo_agent("friday-uuid")],
        paperclip_api_key="pcli_live_abc",
        paperclip_company_id="comp-1",
        paperclip_api_url="http://paperclip.internal:3100",
    )
    assert "runtimeScope" not in cfg["mcp"]
