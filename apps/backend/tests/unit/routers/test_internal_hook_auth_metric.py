"""A rejected hook token is counted, so a stale Paperclip adapter snapshot (every nightly CEO /
issue run 401ing while room chat keeps working) pages instead of failing silently."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from main import app


def test_stale_token_on_the_paperclip_run_door_is_counted():
    with (
        patch("routers.messaging.container_repo.get_by_owner_id", AsyncMock(return_value={"hook_token": "current"})),
        patch("routers.messaging.put_metric") as metric,
    ):
        res = TestClient(app).post(
            "/api/v1/internal/paperclip/hermes/org_1/v1/runs", json={}, headers={"Authorization": "Bearer stale"}
        )
    assert res.status_code == 401
    metric.assert_called_once_with("internal_hook.auth_failed", dimensions={"route": "paperclip_run"})


def test_other_internal_doors_are_counted_under_their_own_route():
    with patch("routers.messaging.put_metric") as metric:
        res = TestClient(app).get(
            "/api/v1/internal/messaging/attachments/org_1/11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222"
        )
    assert res.status_code == 401
    metric.assert_called_once_with("internal_hook.auth_failed", dimensions={"route": "other"})
