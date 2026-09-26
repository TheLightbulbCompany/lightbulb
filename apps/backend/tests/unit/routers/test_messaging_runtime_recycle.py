"""Bridge -> backend 7 h recycle: hook-token gate, the stop, the warm-row clear."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError

OWNER, TOKEN, ARN = "org_a", "hook-token-for-a", "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r-1"
URL = f"/api/v1/internal/messaging/runtime/{OWNER}/recycle"
H = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def aws():
    table = MagicMock()
    with (
        patch(
            "routers.messaging.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN, "agentcore_runtime_arn": ARN}),
        ) as get_row,
        patch("routers.messaging.container_repo._get_table", return_value=table),
        patch("core.services.hermes_provisioning._stop_runtime_session") as stop,
    ):
        yield {"get_row": get_row, "table": table, "stop": stop}


def test_missing_or_wrong_bearer_is_401(client, aws):
    assert client.post(URL).status_code == 401
    assert client.post(URL, headers={"Authorization": "Bearer WRONG"}).status_code == 401
    aws["stop"].assert_not_called()


def test_stops_the_owners_session_and_clears_the_warm_row(client, aws):
    from core.services.runtime_transport import session_id_for

    r = client.post(URL, headers=H)
    assert r.status_code == 202 and r.json() == {"status": "stopped"}
    aws["stop"].assert_called_once_with(ARN, session_id_for(OWNER))
    aws["table"].delete_item.assert_called_with(Key={"owner_id": f"__session__#{OWNER}"})


def test_no_session_to_stop_is_a_stop(client, aws):
    aws["stop"].side_effect = ClientError({"Error": {"Code": "ResourceNotFoundException"}}, "StopRuntimeSession")
    assert client.post(URL, headers=H).status_code == 202


def test_no_runtime_is_409(client, aws):
    aws["get_row"].return_value = {"owner_id": OWNER, "hook_token": TOKEN}
    assert client.post(URL, headers=H).status_code == 409
    aws["stop"].assert_not_called()
