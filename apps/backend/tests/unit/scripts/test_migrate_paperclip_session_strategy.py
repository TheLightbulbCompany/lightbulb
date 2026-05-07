"""Unit tests for the session-strategy migration script."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.migrate_paperclip_session_strategy import (
    migrate_one_agent,
    needs_migration,
)


def test_needs_migration_true_for_fixed_strategy():
    assert needs_migration(
        {"adapterConfig": {"sessionKeyStrategy": "fixed", "sessionKey": "user_x"}}
    )


def test_needs_migration_false_for_issue_strategy():
    assert not needs_migration(
        {"adapterConfig": {"sessionKeyStrategy": "issue", "sessionKey": "user_x"}}
    )


def test_needs_migration_false_when_adapter_config_missing():
    """Non-openclaw_gateway agents (e.g., upstream openclaw-cli) skip."""
    assert not needs_migration({"adapterConfig": {}})
    assert not needs_migration({})


@pytest.mark.asyncio
async def test_migrate_one_agent_calls_update_with_issue_strategy():
    admin_client = MagicMock()
    admin_client.patch_agent = AsyncMock()
    admin_cookie = "cookie"

    agent = {
        "id": "f47ac10b-58cc-4372-a567-0e02b2c3d479",
        "companyId": "co_xyz",
        "adapterConfig": {
            "url": "wss://ws.isol8.co",
            "authToken": "t" * 100,
            "sessionKeyStrategy": "fixed",
            "sessionKey": "user_abc",
        },
    }

    await migrate_one_agent(
        admin_client=admin_client,
        admin_cookie=admin_cookie,
        agent=agent,
    )

    admin_client.patch_agent.assert_awaited_once()
    kwargs = admin_client.patch_agent.await_args.kwargs
    assert kwargs["agent_id"] == agent["id"]
    new_cfg = kwargs["body"]["adapterConfig"]
    assert new_cfg["sessionKeyStrategy"] == "issue"
    # Other fields preserved verbatim.
    assert new_cfg["sessionKey"] == "user_abc"
    assert new_cfg["url"] == "wss://ws.isol8.co"
    assert new_cfg["authToken"] == "t" * 100
    # Paperclip's PATCH /api/agents/:id merges adapterConfig by default;
    # we set replaceAdapterConfig:true so the strategy flip is correct
    # by construction even if Paperclip later populates extra fields.
    assert kwargs["body"]["replaceAdapterConfig"] is True
