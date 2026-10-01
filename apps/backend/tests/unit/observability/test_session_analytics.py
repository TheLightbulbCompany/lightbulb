import uuid
from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from core.observability.session_analytics import capture_session_event, capture_session_mention
from core.services import local_session_relay, local_sessions
from routers.messaging import SessionOpenRequest, record_local_session_open


def test_session_event_is_content_free_and_idempotent():
    row = {
        "ownerId": "org_1",
        "sessionId": "session-1",
        "humanUserId": "user_1",
        "provider": "codex",
        "title": "private title",
        "cwdName": "/private/path",
    }
    with patch("core.observability.session_analytics.capture_product_event") as capture:
        for _ in range(2):
            capture_session_event(
                row,
                "local_session_command_admitted",
                "request-1",
                request_id="request-1",
                command_kind="send",
                origin="direct",
                offline_queued=True,
            )
    first, second = [call.kwargs for call in capture.call_args_list]
    assert first == second
    assert first["distinct_id"] == "user_1"
    assert first["properties"] == {
        "workspace_id": "org_1",
        "session_id": "session-1",
        "provider": "codex",
        "request_id": "request-1",
        "command_kind": "send",
        "origin": "direct",
        "offline_queued": True,
    }
    assert "private" not in str(first)


def test_room_mention_result_joins_session_without_content():
    with patch("core.observability.session_analytics.capture_product_event") as capture:
        capture_session_mention("org_1", "user_2", "session-1", "message-1", "rejected")
    assert capture.call_args.kwargs["properties"] == {
        "workspace_id": "org_1",
        "session_id": "session-1",
        "origin": "room_mention",
        "state": "rejected",
    }


@pytest.mark.asyncio
async def test_iphone_open_requires_session_access_and_uses_visit_id():
    row = {"ownerId": "org_1", "sessionId": "session-1", "humanUserId": "user_1", "provider": "claude"}
    visit = uuid.uuid4()
    with (
        patch(
            "routers.messaging.local_sessions.authorize_session", new_callable=AsyncMock, return_value=(row, None)
        ) as authorize,
        patch("routers.messaging.capture_session_event") as capture,
    ):
        await record_local_session_open(
            "session-1", SessionOpenRequest(open_id=visit), AuthContext(user_id="user_2", org_id="org_1")
        )
    authorize.assert_awaited_once()
    assert capture.call_args.args == (row, "session_opened", str(visit))
    assert capture.call_args.kwargs == {"actor_user_id": "user_2", "origin": "ios", "own": False}


@pytest.mark.asyncio
async def test_offline_terminal_outcomes_keep_only_receipt_metadata():
    session = {"ownerId": "org_1", "sessionId": "session-1", "humanUserId": "user_1", "provider": "claude"}
    command = {"requestId": "request-1", "actorUserId": "user_2", "offlineIdentity": {"private": "data"}}
    with (
        patch("core.services.local_sessions.repo.get_session", new_callable=AsyncMock, return_value=session),
        patch("core.services.local_sessions.capture_session_event") as capture,
    ):
        await local_sessions._capture_offline_terminal("org_1", "session-1", command, "cancelled")
    assert capture.call_args.args == (session, "local_session_command_settled", "request-1:rejected:cancelled")
    assert capture.call_args.kwargs == {
        "actor_user_id": "user_2",
        "request_id": "request-1",
        "command_kind": "send",
        "origin": "direct",
        "state": "rejected",
        "offline_queued": True,
        "outcome_reason": "cancelled",
    }
    with patch("core.services.local_session_relay.capture_session_event") as capture:
        local_session_relay._capture_offline_rejection(session, command, "expired")
    assert capture.call_args.kwargs["outcome_reason"] == "expired"
    assert "private" not in str(capture.call_args)
