"""Unit tests for paperclip_agents handling in build_openclaw_config_dict."""

from core.containers.config import build_openclaw_config_dict


def test_empty_paperclip_agents_falls_back_to_main():
    """When paperclip_agents=[] (provision before any Paperclip agents seeded),
    keep the legacy 'main' entry as the default so /chat still works."""
    cfg = build_openclaw_config_dict(
        user_id="user_abc",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[],
    )
    agents = cfg["agents"]["list"]
    assert len(agents) == 1
    assert agents[0]["id"] == "main"
    assert agents[0]["default"] is True


def test_single_paperclip_agent_replaces_main():
    """One Paperclip agent → one entry, marked default. No 'main' entry."""
    pc_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    cfg = build_openclaw_config_dict(
        user_id="user_abc",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[
            {"id": pc_uuid, "name": "Friday", "role": "ceo"},
        ],
    )
    agents = cfg["agents"]["list"]
    assert len(agents) == 1
    assert agents[0]["id"] == pc_uuid
    assert agents[0]["default"] is True
    assert agents[0]["workspace"] == f"/home/node/.openclaw/workspaces/{pc_uuid}"
    assert agents[0]["reasoningDefault"] == "stream"


def test_multiple_paperclip_agents_ceo_is_default():
    """When multiple Paperclip agents exist, the one with role='ceo' is the
    default. Others are listed but not default."""
    ceo_uuid = "f47ac10b-58cc-4372-a567-0e02b2c3d479"
    other_uuid = "11111111-1111-1111-1111-111111111111"
    cfg = build_openclaw_config_dict(
        user_id="user_abc",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[
            {"id": other_uuid, "name": "Code Reviewer", "role": "engineer"},
            {"id": ceo_uuid, "name": "Friday", "role": "ceo"},
        ],
    )
    agents = cfg["agents"]["list"]
    assert len(agents) == 2
    by_id = {a["id"]: a for a in agents}
    assert by_id[ceo_uuid]["default"] is True
    assert "default" not in by_id[other_uuid] or by_id[other_uuid]["default"] is False


def test_multiple_agents_no_ceo_first_is_default():
    """If no Paperclip agent has role='ceo' (edge case — seed failed), the
    first one in the list becomes the default."""
    a_uuid = "11111111-1111-1111-1111-111111111111"
    b_uuid = "22222222-2222-2222-2222-222222222222"
    cfg = build_openclaw_config_dict(
        user_id="user_abc",
        gateway_token="t" * 32,
        provider_choice="bedrock_claude",
        paperclip_agents=[
            {"id": a_uuid, "name": "First", "role": "engineer"},
            {"id": b_uuid, "name": "Second", "role": "engineer"},
        ],
    )
    by_id = {a["id"]: a for a in cfg["agents"]["list"]}
    assert by_id[a_uuid]["default"] is True
    assert "default" not in by_id[b_uuid] or by_id[b_uuid]["default"] is False
