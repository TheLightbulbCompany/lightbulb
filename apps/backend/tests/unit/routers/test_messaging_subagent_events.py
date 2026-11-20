from unittest.mock import AsyncMock, patch

OWNER, TOKEN = "user_a", "hook-token-for-a"
KEY = "agent:22222222-2222-4222-8222-222222222222:lightbulb:channel:11111111-1111-4111-8111-111111111111"
BODY = {"sessionKey": KEY, "events": [{"type": "subagent.start", "seq": 1, "payload": {"subagent_id": "sa-1"}}]}


def _container():
    return patch(
        "routers.messaging.container_repo.get_by_owner_id",
        AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}),
    )


def test_valid_token_is_accepted_and_dropped(client):
    """The bridge still posts subagent events; nothing shows them, so they are accepted (200) and dropped."""
    with _container():
        r = client.post(
            f"/api/v1/internal/messaging/subagents/{OWNER}/events",
            json=BODY,
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert r.status_code == 200 and r.json() == {"published": 0}


def test_wrong_token_is_401(client):
    with _container():
        r = client.post(
            f"/api/v1/internal/messaging/subagents/{OWNER}/events", json=BODY, headers={"Authorization": "Bearer nope"}
        )
    assert r.status_code == 401
