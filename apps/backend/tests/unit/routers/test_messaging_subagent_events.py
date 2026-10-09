from unittest.mock import AsyncMock, patch

OWNER, TOKEN = "user_a", "hook-token-for-a"
KEY = "agent:22222222-2222-4222-8222-222222222222:lightbulb:channel:11111111-1111-4111-8111-111111111111"
BODY = {"sessionKey": KEY, "events": [{"type": "subagent.start", "seq": 1, "payload": {"subagent_id": "sa-1"}}]}


def _container():
    return patch(
        "routers.messaging.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}),
    )


def test_valid_token_reaches_the_service(client):
    with (
        _container(),
        patch("routers.messaging.subagents.publish_events", AsyncMock(return_value={"published": 1})) as svc,
    ):
        r = client.post(
            f"/api/v1/internal/messaging/subagents/{OWNER}/events",
            json=BODY,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert r.status_code == 200 and r.json() == {"published": 1}
    assert svc.await_args.args[0] == OWNER


def test_wrong_token_is_401(client):
    with _container(), patch("routers.messaging.subagents.publish_events", AsyncMock()) as svc:
        r = client.post(
            f"/api/v1/internal/messaging/subagents/{OWNER}/events", json=BODY, headers={"Authorization": "Bearer nope"}
        )
    assert r.status_code == 401
    svc.assert_not_awaited()
