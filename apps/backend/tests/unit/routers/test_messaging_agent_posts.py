"""Hook-token gate for the out-of-turn agent post (mirrors test_messaging_run_events.py)."""

OWNER = "org_a"
AGENT = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
URL = f"/api/v1/internal/messaging/agent-posts/{OWNER}"
BODY = {"eventId": "post:a:1", "agentId": AGENT, "text": "Morning brief"}


def test_missing_bearer_is_401(client):
    assert client.post(URL, json=BODY).status_code == 401
