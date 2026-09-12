"""Unit tests for friday_identity: the per-agent Paperclip identity files."""

from unittest.mock import MagicMock

import pytest

from core.services import friday_identity

# ---------------------------------------------------------------------------
# pre_stage_paperclip_identities
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_paperclip_identities_writes_per_agent_file():
    """Each agent with a minted key gets its own claimed-key file in its own
    workspace with the exact Isol8-defined shape."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-friday", "role": "ceo", "name": "Friday"},
            {"id": "agent-atlas", "role": "general", "name": "Atlas"},
        ],
        agent_keys={"agent-friday": "key-f", "agent-atlas": "key-a"},
        company_id="comp-1",
        api_url="http://paperclip.internal:3100",
        workspace=workspace,
    )

    calls = {c.args[1]: c.args[2] for c in workspace.write_file_if_missing.call_args_list}
    assert set(calls) == {
        "workspace-agent-friday/paperclip-claimed-api-key.json",
        "workspace-agent-atlas/paperclip-claimed-api-key.json",
    }
    friday_doc = _json.loads(calls["workspace-agent-friday/paperclip-claimed-api-key.json"])
    assert friday_doc == {
        "token": "key-f",
        "agentId": "agent-friday",
        "companyId": "comp-1",
        "apiUrl": "http://paperclip.internal:3100",
    }
    atlas_doc = _json.loads(calls["workspace-agent-atlas/paperclip-claimed-api-key.json"])
    assert atlas_doc["token"] == "key-a"
    assert atlas_doc["agentId"] == "agent-atlas"


@pytest.mark.asyncio
async def test_paperclip_identities_skips_agents_without_a_minted_key():
    """An agent whose key mint failed (absent from agent_keys) gets no file."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-friday", "role": "ceo"},
            {"id": "agent-nokey", "role": "general"},
        ],
        agent_keys={"agent-friday": "key-f"},  # agent-nokey missing
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    paths = [c.args[1] for c in workspace.write_file_if_missing.call_args_list]
    assert paths == ["workspace-agent-friday/paperclip-claimed-api-key.json"]


@pytest.mark.asyncio
async def test_paperclip_identities_skipped_when_no_keys():
    """No minted keys → nothing staged (Paperclip unreachable at provision)."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={},
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_paperclip_identities_skipped_when_no_company_or_url():
    """Missing company_id or api_url → skip entirely (incomplete context)."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[{"id": "agent-friday", "role": "ceo"}],
        agent_keys={"agent-friday": "key-f"},
        company_id=None,
        api_url="http://pc:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_paperclip_identities_per_file_failure_does_not_block_rest():
    """One agent's write raising must not stop the others (best-effort)."""
    workspace = MagicMock()
    # First write raises, second succeeds.
    workspace.write_file_if_missing = MagicMock(side_effect=[OSError("disk full"), True])

    # Should not raise.
    await friday_identity.pre_stage_paperclip_identities(
        owner_id="user_abc",
        paperclip_agents=[
            {"id": "agent-a", "role": "ceo"},
            {"id": "agent-b", "role": "general"},
        ],
        agent_keys={"agent-a": "ka", "agent-b": "kb"},
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )


# ---------------------------------------------------------------------------
# stage_paperclip_identity_for_agent (single-agent, post-create path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stage_single_agent_writes_identity_file():
    """The single-agent seeder writes one claimed-key file with the exact
    Isol8-defined shape into the agent's own workspace."""
    import json as _json

    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(return_value=True)

    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token="key-live",
        company_id="comp-1",
        api_url="http://paperclip.internal:3100",
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_called_once()
    rel_path = workspace.write_file_if_missing.call_args.args[1]
    assert rel_path == "workspace-agent-live/paperclip-claimed-api-key.json"
    doc = _json.loads(workspace.write_file_if_missing.call_args.args[2])
    assert doc == {
        "token": "key-live",
        "agentId": "agent-live",
        "companyId": "comp-1",
        "apiUrl": "http://paperclip.internal:3100",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "token,company_id,api_url",
    [
        ("", "comp-1", "http://pc:3100"),  # key mint failed
        ("key", None, "http://pc:3100"),  # company unresolved
        ("key", "comp-1", None),  # paperclip url unset
    ],
)
async def test_stage_single_agent_skips_on_incomplete_context(token, company_id, api_url):
    """A missing token/company_id/api_url skips staging (no file written);
    the next reprovision self-heals."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock()

    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token=token,
        company_id=company_id,
        api_url=api_url,
        workspace=workspace,
    )

    workspace.write_file_if_missing.assert_not_called()


@pytest.mark.asyncio
async def test_stage_single_agent_write_failure_is_swallowed():
    """A write error must not raise — best-effort, the next reprovision retries."""
    workspace = MagicMock()
    workspace.write_file_if_missing = MagicMock(side_effect=OSError("disk full"))

    # Should not raise.
    await friday_identity.stage_paperclip_identity_for_agent(
        owner_id="user_abc",
        agent_id="agent-live",
        token="key",
        company_id="comp-1",
        api_url="http://pc:3100",
        workspace=workspace,
    )

    # The single write was attempted (and its error swallowed).
    workspace.write_file_if_missing.assert_called_once()
