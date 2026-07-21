"""Tests for the authenticated outbound agent-mail reply relay.

This router is the auth boundary for a per-user OpenClaw container calling
back out to send an email reply. It deliberately does NOT use
``Depends(get_current_user)`` — the container has no Clerk token. Auth is:

1. the presented bearer token must timing-safe-match the OWNER's
   ``containers.hook_token`` row, where
2. the owner is named inside the (unforgeable, Fernet-encrypted)
   ``reply_token`` — so a container can only ever reply as its own owner.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from routers import agent_mail_relay


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Each test starts with an empty rate-limit bucket — shared in-process
    state would otherwise let the rate-limit test's 60+ POSTs pre-charge any
    other test hitting the same owner."""
    agent_mail_relay._RATE_BUCKETS_PER_OWNER.clear()
    yield
    agent_mail_relay._RATE_BUCKETS_PER_OWNER.clear()


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def fake_ctx():
    return {
        "owner_id": "o",
        "agent_id": "agent_1",
        "reply_to": "someone@example.com",
        "in_reply_to": "<msg-1@mail>",
        "subject": "Hello",
    }


@pytest.fixture
def fake_container():
    return {"owner_id": "o", "hook_token": "the-real-hook-token"}


def _body():
    return {"reply_token": "tok", "body_text": "hi there"}


def test_missing_authorization_returns_401(client):
    r = client.post("/api/v1/agent-mail/reply", json=_body())
    assert r.status_code == 401
    assert r.json()["detail"] == "missing_token"


def test_malformed_authorization_returns_401(client):
    r = client.post(
        "/api/v1/agent-mail/reply",
        json=_body(),
        headers={"Authorization": "the-real-hook-token"},
    )
    assert r.status_code == 401
    assert r.json()["detail"] == "missing_token"


def test_bad_token_returns_400_and_does_not_call_send_reply(client):
    with (
        patch("routers.agent_mail_relay.decode_reply_token", return_value=None),
        patch("routers.agent_mail_relay.send_reply", new=AsyncMock()) as mock_send,
    ):
        r = client.post(
            "/api/v1/agent-mail/reply",
            json=_body(),
            headers={"Authorization": "Bearer the-real-hook-token"},
        )
        assert r.status_code == 400
        assert r.json()["detail"] == {"status": "bad_token"}
        mock_send.assert_not_called()


def test_wrong_hook_token_returns_401_and_does_not_call_send_reply(client, fake_ctx, fake_container):
    with (
        patch("routers.agent_mail_relay.decode_reply_token", return_value=fake_ctx),
        patch(
            "routers.agent_mail_relay.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=fake_container),
        ),
        patch("routers.agent_mail_relay.send_reply", new=AsyncMock()) as mock_send,
    ):
        r = client.post(
            "/api/v1/agent-mail/reply",
            json=_body(),
            headers={"Authorization": "Bearer not-the-real-token"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == {"status": "invalid_token"}
        mock_send.assert_not_called()


def test_no_container_row_returns_401_and_does_not_call_send_reply(client, fake_ctx):
    with (
        patch("routers.agent_mail_relay.decode_reply_token", return_value=fake_ctx),
        patch(
            "routers.agent_mail_relay.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=None),
        ),
        patch("routers.agent_mail_relay.send_reply", new=AsyncMock()) as mock_send,
    ):
        r = client.post(
            "/api/v1/agent-mail/reply",
            json=_body(),
            headers={"Authorization": "Bearer the-real-hook-token"},
        )
        assert r.status_code == 401
        assert r.json()["detail"] == {"status": "invalid_token"}
        mock_send.assert_not_called()


def test_happy_path_returns_200_sent_and_calls_send_reply_once(client, fake_ctx, fake_container):
    with (
        patch("routers.agent_mail_relay.decode_reply_token", return_value=fake_ctx),
        patch(
            "routers.agent_mail_relay.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=fake_container),
        ),
        patch("routers.agent_mail_relay.send_reply", new=AsyncMock(return_value="sent")) as mock_send,
    ):
        r = client.post(
            "/api/v1/agent-mail/reply",
            json=_body(),
            headers={"Authorization": "Bearer the-real-hook-token"},
        )
        assert r.status_code == 200
        assert r.json() == {"status": "sent"}
        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs["ctx"] == fake_ctx
        assert kwargs["body_text"] == "hi there"


def test_rate_limit_exceeded_returns_429(client, fake_ctx, fake_container):
    with (
        patch("routers.agent_mail_relay.decode_reply_token", return_value=fake_ctx),
        patch(
            "routers.agent_mail_relay.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=fake_container),
        ),
        patch("routers.agent_mail_relay.send_reply", new=AsyncMock(return_value="sent")),
    ):
        last_status = None
        for _ in range(agent_mail_relay.RATE_LIMIT_PER_MINUTE + 1):
            resp = client.post(
                "/api/v1/agent-mail/reply",
                json=_body(),
                headers={"Authorization": "Bearer the-real-hook-token"},
            )
            last_status = resp.status_code
        assert last_status == 429
        assert resp.json()["detail"] == {"status": "rate_limited"}
