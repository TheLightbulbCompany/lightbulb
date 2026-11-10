"""The super-agent switch, retired (Phase 1 task 4): every workspace is on, nothing reads a row for it, and
LiveConfig still sends `features.superAgent: true` because old app builds read it."""

import uuid
from unittest.mock import AsyncMock, patch

from core.config import Settings

GET_ROW = "core.repositories.container_repo.get_by_owner_id"


def test_the_env_master_is_gone():
    assert "SUPER_AGENT_ENV_ENABLED" not in Settings.model_fields


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
