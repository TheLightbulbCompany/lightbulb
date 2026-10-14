"""POST /setup-requests — the public /request-setup form: stores a row,
emails Aden, emits setup_requested, and refuses a bad body."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import setup_requests

BODY = {
    "name": "Mara Reyes",
    "email": "mara@acme.test",
    "company": "Acme",
    "team_size": "3-5",
    "agents": ["claude_code", "codex"],
    "first_handoff": "weekly investor update",
    "source": "hacker_news",
    "thread_link": "https://news.ycombinator.com/item?id=1",
    "attribution": {"first_touch_landing_path": "/?utm_source=hn", "first_touch_utm_source": "hn"},
    "source_param": "compare-slack",
    "anonymous_id": "anon-123",
}


@pytest.fixture(autouse=True)
def _fresh_rate():
    from routers import setup_requests as r

    r._RATE._buckets.clear()
    yield
    r._RATE._buckets.clear()


async def test_stores_emails_and_captures(unauthenticated_async_client):
    put = AsyncMock()
    send = AsyncMock(return_value="msg-1")
    capture = MagicMock()
    with (
        patch.object(setup_requests.setup_requests_repo, "put", put),
        patch.object(setup_requests.email_service, "send_email", send),
        patch.object(setup_requests, "capture_product_event", capture),
    ):
        r = await unauthenticated_async_client.post("/api/v1/setup-requests", json=BODY)

    assert r.status_code == 201, r.text
    item = put.await_args.args[0]
    assert item["id"] == r.json()["id"]
    assert item["source"] == "hacker_news" and item["source_param"] == "compare-slack"
    assert item["attribution"]["first_touch_utm_source"] == "hn"

    mail = send.await_args.kwargs
    assert mail["to"] == "aden@thelightbulb.company"
    assert mail["reply_to"] == "mara@acme.test"
    assert "weekly investor update" in mail["text"]

    ev = capture.call_args.kwargs
    assert ev["event"] == "setup_requested" and ev["distinct_id"] == "anon-123"
    assert ev["properties"]["source"] == "hacker_news"
    assert ev["properties"]["first_touch_utm_source"] == "hn"


@pytest.mark.parametrize(
    "over",
    [
        {"source": None},
        {"source": "tiktok"},
        {"email": "not-an-email"},
        {"email": "a@b.co\r\nBcc: x@y.z"},
        {"team_size": "7"},
        {"agents": []},
        {"name": "   "},
        {"company": "Acme\r\nBcc: x@y.z"},
        {"first_handoff": ""},
    ],
)
async def test_rejects_a_bad_body(unauthenticated_async_client, over):
    body = {k: v for k, v in {**BODY, **over}.items() if v is not None}
    with patch.object(setup_requests.setup_requests_repo, "put", AsyncMock()) as put:
        r = await unauthenticated_async_client.post("/api/v1/setup-requests", json=body)
    assert r.status_code == 422
    put.assert_not_awaited()


async def test_rate_limited_per_ip(unauthenticated_async_client):
    with (
        patch.object(setup_requests.setup_requests_repo, "put", AsyncMock()),
        patch.object(setup_requests.email_service, "send_email", AsyncMock()),
        patch.object(setup_requests, "capture_product_event", MagicMock()),
    ):
        codes = [
            (await unauthenticated_async_client.post("/api/v1/setup-requests", json=BODY)).status_code for _ in range(6)
        ]
    assert codes[:5] == [201] * 5 and codes[5] == 429
