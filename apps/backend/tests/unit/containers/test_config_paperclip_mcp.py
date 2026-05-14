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
