"""A rejected hook token is counted, so a stale token pages instead of failing silently."""

from unittest.mock import patch

from fastapi.testclient import TestClient

from main import app


def test_rejected_hook_token_is_counted():
    with patch("routers.messaging.put_metric") as metric:
        res = TestClient(app).get(
            "/api/v1/internal/messaging/attachments/org_1/11111111-1111-1111-1111-111111111111/22222222-2222-2222-2222-222222222222"
        )
    assert res.status_code == 401
    metric.assert_called_once_with("internal_hook.auth_failed", dimensions={"route": "other"})
