"""Hook-token gate + wiring for Task 28's node-invoke route (mirrors test_messaging_run_events.py)."""

import json
from unittest.mock import AsyncMock, patch

from core.services.messaging import ApiProblem

OWNER, TOKEN = "org_1", "hook-token-for-org_1"
H = {"Authorization": f"Bearer {TOKEN}"}
BODY = {
    "command": "system.run",
    "paramsJSON": json.dumps({"command": ["bash", "-lc", "ls"]}),
    "timeoutMs": 120_000,
}


def _container():
    return patch(
        "routers.messaging.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}),
    )


def test_node_invoke_missing_bearer_is_401(client):
    assert client.post(f"/api/v1/internal/node/invoke/{OWNER}", json=BODY).status_code == 401


def test_node_invoke_wrong_token_is_401(client):
    with _container():
        r = client.post(f"/api/v1/internal/node/invoke/{OWNER}", json=BODY, headers={"Authorization": "Bearer WRONG"})
    assert r.status_code == 401


def test_node_invoke_valid_token_reaches_the_service_with_forwarded_false(client):
    with (
        _container(),
        patch(
            "routers.messaging.service.invoke_node",
            AsyncMock(return_value={"ok": True, "payload": {"stdout": "README.md"}}),
        ) as invoke,
    ):
        r = client.post(f"/api/v1/internal/node/invoke/{OWNER}", json=BODY, headers=H)
    assert r.status_code == 200
    assert r.json() == {"ok": True, "payload": {"stdout": "README.md"}, "error": None}
    args, kwargs = invoke.call_args
    assert args[0] == OWNER
    assert args[1]["command"] == "system.run" and args[1]["paramsJSON"] == BODY["paramsJSON"]
    assert kwargs["forwarded"] is False


def test_node_invoke_forwarded_header_sets_forwarded_true(client):
    with (
        _container(),
        patch(
            "routers.messaging.service.invoke_node",
            AsyncMock(return_value={"ok": True, "payload": None}),
        ) as invoke,
    ):
        r = client.post(
            f"/api/v1/internal/node/invoke/{OWNER}",
            json=BODY,
            headers={**H, "X-Lightbulb-Node-Forwarded": "1"},
        )
    assert r.status_code == 200
    assert invoke.call_args.kwargs["forwarded"] is True


def test_node_invoke_missing_params_json_is_422(client):
    with _container():
        r = client.post(f"/api/v1/internal/node/invoke/{OWNER}", json={"command": "system.run"}, headers=H)
    assert r.status_code in (400, 422)


def test_node_invoke_api_problem_status_propagates(client):
    with (
        _container(),
        patch(
            "routers.messaging.service.invoke_node",
            AsyncMock(side_effect=ApiProblem("NODE_NOT_ATTACHED", "No desktop node is attached.")),
        ),
    ):
        r = client.post(f"/api/v1/internal/node/invoke/{OWNER}", json=BODY, headers=H)
    assert r.status_code == 409
    assert r.json()["code"] == "NODE_NOT_ATTACHED"


def test_node_invoke_command_not_allowed_is_403(client):
    with (
        _container(),
        patch(
            "routers.messaging.service.invoke_node",
            AsyncMock(side_effect=ApiProblem("COMMAND_NOT_ALLOWED", "not declared")),
        ),
    ):
        r = client.post(f"/api/v1/internal/node/invoke/{OWNER}", json=BODY, headers=H)
    assert r.status_code == 403


def test_node_invoke_timeout_is_504(client):
    with (
        _container(),
        patch(
            "routers.messaging.service.invoke_node",
            AsyncMock(side_effect=ApiProblem("NODE_TIMEOUT", "no answer", retryable=True)),
        ),
    ):
        r = client.post(f"/api/v1/internal/node/invoke/{OWNER}", json=BODY, headers=H)
    assert r.status_code == 504
