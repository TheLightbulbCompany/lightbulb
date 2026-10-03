"""The bot page's ticket exchange: room- and booking-bound, dead with the meeting."""

import os
import time
from unittest.mock import AsyncMock, patch

import jwt
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from main import app  # noqa: E402
from routers import meet_bot as mod  # noqa: E402

lk = mod.livekit_huddles
ROOM = "11111111-1111-4111-8111-111111111111"
AGENTS = [{"id": "agent-ceo-1", "name": "Ada"}]


@pytest.fixture
def env():
    meeting = {"status": "bot_created", "recallBotId": "bot-1", "ticketNonce": "nonce-1"}
    with (
        patch.object(lk.settings, "LIVEKIT_URL", "wss://lk.test"),
        patch.object(lk.settings, "LIVEKIT_API_KEY", "k"),
        patch.object(lk.settings, "LIVEKIT_API_SECRET", "s" * 32),
        patch.object(lk.settings, "ENVIRONMENT", "dev"),
        patch.object(
            mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: {"meeting": meeting} if r == ROOM else None)
        ),
        patch.object(mod.voice_calls, "room_agents", AsyncMock(return_value=AGENTS)),
    ):
        yield TestClient(app), meeting


def _exchange(client, ticket):
    return client.post("/api/v1/meet-bot/session", json={"ticket": ticket})


def _ticket(nonce="nonce-1", exp_in=3600, room=ROOM):
    return lk.meet_ticket(room, nonce, int(time.time()) + exp_in)


def test_a_valid_ticket_returns_a_token_scoped_to_the_meet_room(env):
    client, meeting = env
    resp = _exchange(client, _ticket())
    assert resp.status_code == 200
    out = resp.json()
    assert out["serverUrl"] == "wss://lk.test" and out["roomName"] == f"meet-dev-{ROOM}"
    assert out["agents"] == AGENTS
    claims = jwt.decode(out["token"], "s" * 32, algorithms=["HS256"])
    assert claims["sub"] == f"meet:{ROOM}"
    assert claims["video"]["room"] == f"meet-dev-{ROOM}" and claims["video"]["roomJoin"] is True
    assert claims["video"]["canPublishSources"] == ["microphone"] and claims["video"]["canPublishData"] is True
    assert claims["exp"] - claims["nbf"] == 6 * 3600

    meeting["status"] = "in_call"  # Recall reloads the page on reconnect
    assert _exchange(client, _ticket()).status_code == 200


def test_ticket_is_room_and_bot_bound(env):
    client, meeting = env
    good = _ticket()
    body, _, sig = good.partition(".")
    assert _exchange(client, _ticket(nonce="nonce-other")).status_code == 401  # another booking's
    assert _exchange(client, f"{body}.{'0' * len(sig)}").status_code == 401  # tampered
    assert _exchange(client, _ticket(exp_in=-1)).status_code == 401  # expired
    assert _exchange(client, _ticket(room="22222222-2222-4222-8222-222222222222")).status_code == 401
    assert _exchange(client, "garbage").status_code == 401
    meeting["status"] = "ended"
    assert _exchange(client, good).status_code == 401
