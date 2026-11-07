"""The super-agent switch, retired (Phase 1 task 4): every workspace is on, nothing reads a row for it, and
LiveConfig still sends `features.superAgent: true` because old app builds read it."""

import uuid
from unittest.mock import AsyncMock, patch

from core.config import Settings
from core.services.super_agent_flags import flags_for, list_on

GET_ROW = "core.repositories.container_repo.get_by_owner_id"
SCAN = "core.repositories.container_repo.scan_all"


def test_the_env_master_is_gone():
    assert "SUPER_AGENT_ENV_ENABLED" not in Settings.model_fields


async def test_flags_for_is_on_without_a_read():
    with patch(GET_ROW, AsyncMock(return_value={"super_agent": False})) as get_row:
        assert await flags_for("org_x") is True
    get_row.assert_not_awaited()


async def test_list_on_is_every_workspace():
    rows = [
        {"owner_id": "org_a", "super_agent": True},
        {"owner_id": "org_b", "super_agent": False},
        {"owner_id": "org_c"},
    ]
    with patch(SCAN, AsyncMock(return_value=rows)):
        assert await list_on() == ["org_a", "org_b", "org_c"]


def test_live_config_always_carries_super_agent(client):
    with (
        patch("core.repositories.messaging_repo.ensure_user_stream", AsyncMock(return_value=uuid.uuid4())),
        patch("core.repositories.messaging_repo.enroll_sessions_member", AsyncMock(return_value=uuid.uuid4())),
        patch(GET_ROW, AsyncMock(return_value={"super_agent": False})) as get_row,
    ):
        response = client.get("/api/v1/messaging/live-config")
    assert response.status_code == 200
    assert response.json()["features"] == {"superAgent": True}
    get_row.assert_not_awaited()
