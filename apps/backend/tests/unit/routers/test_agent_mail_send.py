"""Tests for the agent-initiated owner-email endpoint.

The auth boundary here is narrower than the reply relay's: there is no
reply_token and no recipient parameter at all. The owner is named in the path,
the bearer must match THAT owner's hook_token, and the recipient is resolved
server-side — so a container can only ever mail its own owner, and nothing a
caller sends can redirect the message.
"""

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from routers import agent_mail_send

OWNER = "user_a"
TOKEN = "hook-token-for-a"
PATH = f"/api/v1/agent-mail/send-to-owner/u/{OWNER}"


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    agent_mail_send._RATE_BUCKETS_PER_OWNER.clear()
    yield
    agent_mail_send._RATE_BUCKETS_PER_OWNER.clear()


@pytest.fixture
def client():
    return TestClient(app)


def _body(purpose="first_contact"):
    return {"purpose": purpose, "subject": "Hello", "body_text": "hi"}


def _container(owner_id):
    return {"owner_id": owner_id, "hook_token": TOKEN}


def test_missing_bearer_is_401(client):
    r = client.post(PATH, json=_body())
    assert r.status_code == 401


def test_wrong_token_is_401(client):
    with patch.object(
        agent_mail_send.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value=_container(OWNER)),
    ):
        r = client.post(PATH, json=_body(), headers={"Authorization": "Bearer WRONG"})
    assert r.status_code == 401


def test_unknown_owner_is_401(client):
    with patch.object(agent_mail_send.container_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        r = client.post(PATH, json=_body(), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 401


def test_owner_as_token_holder_cannot_send_for_a_different_owner(client):
    """owner_b's row carries a DIFFERENT hook_token, so owner_a's token fails
    the compare — the whole point of naming the owner in the path."""
    with patch.object(
        agent_mail_send.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_b", "hook_token": "hook-token-for-b"}),
    ):
        r = client.post(
            "/api/v1/agent-mail/send-to-owner/u/user_b",
            json=_body(),
            headers={"Authorization": f"Bearer {TOKEN}"},
        )
    assert r.status_code == 401


@pytest.mark.asyncio
async def test_non_ascii_bearer_is_401_not_500():
    """hmac.compare_digest raises TypeError on a non-ASCII `str`, which would
    surface as a 500 instead of a 401.

    Driven against the route function directly: httpx refuses to transmit a
    non-ASCII header at all, but Starlette decodes real headers as latin-1, so
    a raw ASGI request genuinely can hand us one.
    """
    from fastapi import HTTPException

    class _Req:
        headers = {"Authorization": "Bearer ünïcode"}

    with patch.object(
        agent_mail_send.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value=_container(OWNER)),
    ):
        with pytest.raises(HTTPException) as exc:
            await agent_mail_send.send_owner_email(
                OWNER,
                agent_mail_send.OwnerSendRequest(**_body()),
                _Req(),
            )
    assert exc.value.status_code == 401


def test_unknown_purpose_is_400(client):
    with patch.object(
        agent_mail_send.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value=_container(OWNER)),
    ):
        r = client.post(PATH, json=_body(purpose="nonsense"), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 400


def test_valid_call_returns_the_service_status(client):
    with (
        patch.object(
            agent_mail_send.container_repo,
            "get_by_owner_id",
            AsyncMock(return_value=_container(OWNER)),
        ),
        patch.object(agent_mail_send, "send_to_owner", AsyncMock(return_value="sent")) as send,
    ):
        r = client.post(PATH, json=_body(), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert r.json() == {"status": "sent"}
    # No recipient is threaded through — the service resolves it from owner_id.
    assert "to" not in send.await_args.kwargs
    assert send.await_args.kwargs["owner_id"] == OWNER


def test_half_an_agent_identity_is_400(client):
    with patch.object(
        agent_mail_send.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value=_container(OWNER)),
    ):
        for half in ({"agent_id": "agent_x"}, {"agent_name": "X"}):
            r = client.post(PATH, json={**_body(), **half}, headers={"Authorization": f"Bearer {TOKEN}"})
            assert r.status_code == 400
            assert r.json()["detail"] == {"status": "bad_agent_identity"}


def test_agent_identity_is_threaded_through_to_the_service(client):
    with (
        patch.object(
            agent_mail_send.container_repo,
            "get_by_owner_id",
            AsyncMock(return_value=_container(OWNER)),
        ),
        patch.object(agent_mail_send, "send_to_owner", AsyncMock(return_value="sent")) as send,
    ):
        body = {**_body(purpose="routine"), "agent_id": "agent_support", "agent_name": "Support Triage"}
        r = client.post(PATH, json=body, headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200
    assert send.await_args.kwargs["agent_id"] == "agent_support"
    assert send.await_args.kwargs["agent_name"] == "Support Triage"


def test_already_sent_is_surfaced_verbatim(client):
    with (
        patch.object(
            agent_mail_send.container_repo,
            "get_by_owner_id",
            AsyncMock(return_value=_container(OWNER)),
        ),
        patch.object(agent_mail_send, "send_to_owner", AsyncMock(return_value="already_sent")),
    ):
        r = client.post(PATH, json=_body(), headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.json() == {"status": "already_sent"}


def test_rate_limit_returns_429(client):
    with (
        patch.object(
            agent_mail_send.container_repo,
            "get_by_owner_id",
            AsyncMock(return_value=_container(OWNER)),
        ),
        patch.object(agent_mail_send, "send_to_owner", AsyncMock(return_value="sent")),
    ):
        headers = {"Authorization": f"Bearer {TOKEN}"}
        for _ in range(agent_mail_send.RATE_LIMIT_PER_MINUTE):
            assert client.post(PATH, json=_body(), headers=headers).status_code == 200
        assert client.post(PATH, json=_body(), headers=headers).status_code == 429


def test_401s_do_not_charge_the_rate_limit_bucket(client):
    """Auth runs before the limiter, so an unauthenticated flood cannot lock
    the real owner out."""
    with patch.object(
        agent_mail_send.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value=_container(OWNER)),
    ):
        for _ in range(80):
            client.post(PATH, json=_body(), headers={"Authorization": "Bearer WRONG"})
    assert len(agent_mail_send._RATE_BUCKETS_PER_OWNER[OWNER]) == 0
