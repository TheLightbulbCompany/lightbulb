"""Recall's status webhook drives the meeting room; realtime speaker events reach LiveKit; the
worker's idle leave pulls the bot."""

import base64
import hashlib
import hmac
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from main import app  # noqa: E402
from routers import meetings as mod  # noqa: E402

ROOM = "11111111-1111-4111-8111-111111111111"
SECRET = "whsec_" + base64.b64encode(b"k" * 24).decode()
WORKER = "worker-secret"


def _signed(payload: dict, secret: str = SECRET) -> tuple[bytes, dict]:
    body = json.dumps(payload).encode()
    stamp = str(int(time.time()))
    key = base64.b64decode(secret.removeprefix("whsec_"))
    sig = base64.b64encode(hmac.new(key, f"msg_1.{stamp}.".encode() + body, hashlib.sha256).digest()).decode()
    return body, {"webhook-id": "msg_1", "webhook-timestamp": stamp, "webhook-signature": f"v1,{sig}"}


def _status(code: str, bot_id: str = "bot-1", sub_code: str | None = None, room: str = ROOM) -> dict:
    return {
        "event": f"bot.{code}",
        "data": {
            "data": {"code": code, "sub_code": sub_code, "updated_at": "2026-10-03T00:00:00Z"},
            "bot": {"id": bot_id, "metadata": {"roomId": room}},
        },
    }


@pytest.fixture
def env():
    meta = {
        "roomId": ROOM,
        "ownerId": "org_a",
        "meeting": {"uid": "u1", "status": "bot_created", "recallBotId": "bot-1", "ticketNonce": "n"},
    }

    async def patch_meeting(room_id, fields, drop=()):
        meta["meeting"] = {**meta["meeting"], **fields}
        return meta["meeting"]

    publisher = MagicMock(publish_room=AsyncMock(return_value=True))
    lk = mod.livekit_huddles
    with (
        patch.object(mod.settings, "MEETINGS_ENABLED", True),
        patch.object(mod.meeting_bots.settings, "RECALL_WEBHOOK_SECRET", SECRET),
        patch.object(mod.settings, "HUDDLE_WORKER_SECRET", WORKER),
        patch.object(mod.messaging_repo, "get_room", AsyncMock(side_effect=lambda _r: meta)),
        patch.object(mod.meeting_invites, "_patch_meeting", AsyncMock(side_effect=patch_meeting)),
        patch.object(mod.meeting_invites, "_post_line", AsyncMock()) as line,
        patch.object(mod.container_repo, "try_claim_pass", AsyncMock(return_value=True)),
        patch.object(mod.clerk_admin, "get_organization", AsyncMock(return_value={"name": "Acme"})),
        patch.object(lk, "dispatch_meet", AsyncMock()) as dispatch,
        patch.object(lk, "close_room", AsyncMock()) as close,
        patch.object(lk, "send_meet_speaker", AsyncMock()) as speaker,
        patch.object(mod.voice_calls, "prewake_owner", MagicMock()) as wake,
        patch.object(mod, "get_live_event_publisher", return_value=publisher),
        patch.object(mod, "put_metric") as metric,
    ):
        yield {
            "client": TestClient(app),
            "meta": meta,
            "dispatch": dispatch,
            "close": close,
            "speaker": speaker,
            "wake": wake,
            "line": line,
            "publisher": publisher,
            "metric": metric,
        }


def _post(client, payload, path="/api/v1/webhooks/recall", secret=SECRET):
    body, headers = _signed(payload, secret)
    return client.post(path, content=body, headers={**headers, "content-type": "application/json"})


def test_in_call_dispatches_the_worker_once_and_prewakes(env):
    assert _post(env["client"], _status("in_call_recording")).status_code == 200
    env["dispatch"].assert_awaited_once()
    room_id, metadata = env["dispatch"].await_args.args
    assert room_id == ROOM
    assert metadata == {
        "roomId": ROOM,
        "ownerId": "org_a",
        "roomToken": mod.livekit_huddles.room_token(ROOM, "org_a"),
        "source": "meet",
        "company": "Acme",
    }
    env["wake"].assert_called_once_with("org_a")
    assert env["meta"]["meeting"]["status"] == "in_call"
    event = env["publisher"].publish_room.await_args.args[1]
    assert event["type"] == "room.meeting.changed" and event["payload"] == {"roomId": ROOM, "status": "in_call"}

    # A replay, and Meet's second in-call status, dispatch nothing more.
    assert _post(env["client"], _status("in_call_recording")).status_code == 200
    assert _post(env["client"], _status("in_call_not_recording")).status_code == 200
    env["dispatch"].assert_awaited_once()
    env["wake"].assert_called_once()


def test_call_ended_closes_the_room_once(env):
    env["meta"]["meeting"]["status"] = "in_call"
    _post(env["client"], _status("call_ended", sub_code="call_ended_by_host"))
    _post(env["client"], _status("done"))
    assert env["meta"]["meeting"]["status"] == "ended"
    env["close"].assert_awaited_once_with(ROOM, meet=True)
    env["metric"].assert_any_call("meeting.ended", dimensions={"sub_code": "call_ended_by_host"})


def test_fatal_knocking_disabled_posts_a_plain_line(env):
    _post(env["client"], _status("fatal", sub_code="google_meet_knocking_disabled"))
    meeting = env["meta"]["meeting"]
    assert meeting["status"] == "bot_failed" and meeting["botError"] == "google_meet_knocking_disabled"
    env["line"].assert_awaited_once()
    assert env["line"].await_args.args[1:] == (ROOM, "meeting:bot-1:fatal", "The host didn't let the agents in.")
    _post(env["client"], _status("done"))  # Recall follows a fatal with done: nothing more
    env["line"].assert_awaited_once()
    env["close"].assert_awaited_once()


def test_bad_signature_is_401(env):
    resp = _post(env["client"], _status("in_call_recording"), secret="whsec_" + base64.b64encode(b"x" * 24).decode())
    assert resp.status_code == 401
    env["dispatch"].assert_not_awaited()


@pytest.mark.parametrize(
    "payload", [_status("in_call_recording", bot_id="bot-other"), _status("in_call_recording", room="nope")]
)
def test_unknown_or_mismatched_bot_is_a_noop(env, payload):
    assert _post(env["client"], payload).status_code == 200
    env["dispatch"].assert_not_awaited()
    assert env["meta"]["meeting"]["status"] == "bot_created"


def test_meetings_off_is_a_noop(env):
    with patch.object(mod.settings, "MEETINGS_ENABLED", False):
        assert _post(env["client"], _status("in_call_recording")).status_code == 200
    env["dispatch"].assert_not_awaited()


def test_realtime_speech_on_is_one_speaker_packet(env):
    payload = {
        "event": "participant_events.speech_on",
        "data": {
            "data": {
                "participant": {"id": 7, "name": "Pat"},
                "timestamp": {"absolute": "2026-10-03T00:00:01Z", "relative": 1.0},
            },
            "bot": {"id": "bot-1", "metadata": {"roomId": ROOM}},
        },
    }
    assert _post(env["client"], payload, "/api/v1/webhooks/recall/realtime").status_code == 200
    env["speaker"].assert_awaited_once_with(
        ROOM, {"participantId": "7", "name": "Pat", "speaking": True, "at": 1790985601.0}
    )


def test_worker_idle_leave_pulls_the_bot_and_ends(env):
    with patch.object(mod.meeting_bots, "leave", AsyncMock()) as leave:
        resp = env["client"].request(
            "DELETE",
            f"/api/v1/internal/meetings/{ROOM}",
            json={"ownerId": "org_a", "roomToken": mod.livekit_huddles.room_token(ROOM, "org_a")},
            headers={"X-Lightbulb-Huddle-Secret": WORKER},
        )
    assert resp.status_code == 200
    leave.assert_awaited_once_with("bot-1")
    assert env["meta"]["meeting"]["status"] == "ended"
    env["close"].assert_awaited_once_with(ROOM, meet=True)
    # The call_ended that follows the leave is a replay.
    _post(env["client"], _status("call_ended"))
    env["close"].assert_awaited_once()


def test_worker_leave_needs_the_room_token(env):
    resp = env["client"].request(
        "DELETE",
        f"/api/v1/internal/meetings/{ROOM}",
        json={"ownerId": "org_a", "roomToken": "wrong"},
        headers={"X-Lightbulb-Huddle-Secret": WORKER},
    )
    assert resp.status_code == 401
