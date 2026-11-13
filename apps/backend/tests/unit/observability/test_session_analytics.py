from unittest.mock import patch


from core.observability.session_analytics import capture_session_event


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
