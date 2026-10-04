"""Recall's audio socket: only an audio ticket for a live booking opens it, and frames reach the bridge."""

import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from main import app  # noqa: E402
from routers import meet_audio as mod  # noqa: E402

lk = mod.livekit_huddles
ROOM = "11111111-1111-4111-8111-111111111111"


class FakeBridge:
    built: list["FakeBridge"] = []

    def __init__(self, room_id):
        self.room_id, self.frames, self.closed = room_id, [], False
        FakeBridge.built.append(self)

    async def feed(self, frame):
        self.frames.append(frame)

    async def close(self):
        self.closed = True


@pytest.fixture
def env():
    FakeBridge.built = []
    meeting = {"status": "in_call", "recallBotId": "bot-1", "ticketNonce": "nonce-1", "audio": "separate"}
    with (
        patch.object(lk.settings, "LIVEKIT_API_SECRET", "s" * 32),
        patch.object(
            mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda r: {"meeting": meeting} if r == ROOM else None)
        ),
        patch.object(mod.meet_bridge, "MeetBridge", FakeBridge),
    ):
        yield TestClient(app), meeting


def _ticket(purpose=lk.AUDIO_TICKET, nonce="nonce-1", exp_in=3600):
    return lk.meet_ticket(ROOM, nonce, int(time.time()) + exp_in, purpose)


def _refused(client, ticket, room=ROOM):
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"/api/v1/gateway/meet/?room={room}&t={ticket}") as ws:
            ws.receive_text()
    return exc.value.code


def test_a_good_ticket_feeds_the_bridge_and_closes_it(env):
    client, _ = env
    with client.websocket_connect(f"/api/v1/gateway/meet/?room={ROOM}&t={_ticket()}") as ws:
        ws.send_text('{"event": "audio_separate_raw.data"}')
        ws.send_text("not json")  # the bridge drops it; the socket stays up
        ws.send_bytes(b'{"event": "participant_events.join"}')
    (bridge,) = FakeBridge.built
    assert bridge.room_id == ROOM and bridge.closed
    assert bridge.frames == ['{"event": "audio_separate_raw.data"}', "not json", '{"event": "participant_events.join"}']


def test_the_path_without_a_slash_also_opens(env):
    client, _ = env
    with client.websocket_connect(f"/api/v1/gateway/meet?room={ROOM}&t={_ticket()}") as ws:
        ws.send_text("{}")
    assert FakeBridge.built[0].frames == ["{}"]


def test_bad_tickets_are_refused_before_a_bridge(env):
    client, meeting = env
    assert _refused(client, "garbage") == 1008
    assert _refused(client, _ticket(purpose=lk.PAGE_TICKET)) == 1008  # the page's ticket opens no audio
    assert _refused(client, _ticket(exp_in=-1)) == 1008
    assert _refused(client, _ticket(nonce="nonce-old")) == 1008  # a rebooked bot
    assert _refused(client, _ticket(), room="22222222-2222-4222-8222-222222222222") == 1008
    meeting["status"] = "ended"
    assert _refused(client, _ticket()) == 1008
    meeting["status"], meeting["recallBotId"] = "bot_created", None
    assert _refused(client, _ticket()) == 1008
    assert FakeBridge.built == []
