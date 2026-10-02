import hashlib
import hmac
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
from core.auth import AuthContext, get_current_user  # noqa: E402
from main import app  # noqa: E402
from routers import huddles as mod  # noqa: E402

lk = mod.livekit_huddles
vc = mod.voice_calls
ROOM = "11111111-1111-4111-8111-111111111111"
SECRET = "worker-secret"
W = {"X-Lightbulb-Huddle-Secret": SECRET}
TOKEN = "hook-token-org-a"
ROOM2 = "22222222-2222-4222-8222-222222222222"


def _rt(room, owner):
    return hmac.new(SECRET.encode(), f"{room}:{owner}".encode(), hashlib.sha256).hexdigest()


RT = _rt(ROOM, "org_a")
TEAM = [{"id": "agent-fin", "name": "Finance"}, {"id": "agent-ceo-1", "name": "Ada"}]


@pytest.fixture
def client():
    app.dependency_overrides[get_current_user] = lambda: AuthContext(
        user_id="user_a", org_id="org_a", org_role="org:member"
    )
    with (
        patch.object(mod.settings, "HUDDLE_WORKER_SECRET", SECRET),
        patch.object(mod.settings, "LIVEKIT_URL", "wss://lk.test"),
        patch.object(mod.settings, "LIVEKIT_API_KEY", "k"),
        patch.object(mod.settings, "LIVEKIT_API_SECRET", "s"),
        patch.object(vc, "listening_agents", AsyncMock(return_value=TEAM)),
    ):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _problem(status, code):
    return AsyncMock(side_effect=vc._problem(status, code=code))


def _join(client, *, has_ceo=False, room_team=None, dispatch_error=None, path_room=ROOM, fresh=True):
    with (
        patch.object(vc, "prewake_owner", MagicMock()) as wake,
        patch.object(mod.container_repo, "try_claim_pass", AsyncMock(return_value=fresh)),
        patch.object(vc, "_room_team", room_team or AsyncMock(return_value=({"ownerId": "org_a"}, TEAM))),
        patch.object(mod.messaging, "human_display_name", AsyncMock(return_value="Pat")),
        patch.object(lk, "mint_token", MagicMock(return_value="jwt")) as mint,
        patch.object(lk, "has_ceo", has_ceo if isinstance(has_ceo, AsyncMock) else AsyncMock(return_value=has_ceo)),
        patch.object(lk, "dispatch_ceo", AsyncMock(side_effect=dispatch_error)) as dispatch,
    ):
        resp = client.post(f"/api/v1/huddles/{path_room}/join")
        _join.wake = wake
        return resp, mint, dispatch


def test_join_wakes_the_workspace(client):
    resp, _, _ = _join(client)
    assert resp.status_code == 200
    _join.wake.assert_called_once_with("org_a")


def test_join_does_not_dispatch_twice_while_the_worker_is_joining(client):
    resp, _, dispatch = _join(client, fresh=False)
    assert resp.status_code == 200
    dispatch.assert_not_awaited()


def test_join_returns_token_and_dispatches_the_ceo(client):
    resp, mint, dispatch = _join(client)
    assert resp.status_code == 200
    assert resp.json() == {
        "serverUrl": "wss://lk.test",
        "roomName": lk.room_name(ROOM),
        "token": "jwt",
        "lightbulb": True,
    }
    assert mint.call_args.kwargs == {"room_id": ROOM, "user_id": "user_a", "name": "Pat"}
    room, meta = dispatch.await_args.args
    assert room == ROOM
    assert meta == {"roomId": ROOM, "ownerId": "org_a", "roomToken": RT}


def test_join_skips_dispatch_when_the_ceo_is_there(client):
    resp, _, dispatch = _join(client, has_ceo=True)
    assert resp.status_code == 200
    dispatch.assert_not_awaited()


def test_join_non_member_is_404(client):
    resp, mint, _ = _join(client, room_team=AsyncMock(side_effect=mod.messaging.ApiProblem("ROOM_NOT_FOUND", "x")))
    assert resp.status_code == 404
    mint.assert_not_called()


def test_join_guest_is_404(client):
    resp, mint, _ = _join(client, room_team=_problem(404, "room_not_found"))
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "room_not_found"
    mint.assert_not_called()


def test_join_archived_is_409(client):
    resp, mint, _ = _join(client, room_team=_problem(409, "room_archived"))
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "room_archived"
    mint.assert_not_called()


def test_join_not_configured_is_503(client):
    with patch.object(mod.settings, "LIVEKIT_URL", ""):
        resp, mint, _ = _join(client)
    assert resp.status_code == 503 and resp.json()["detail"]["code"] == "huddles_unavailable"
    mint.assert_not_called()


@pytest.mark.parametrize(
    "has_ceo,dispatch_error",
    [(AsyncMock(side_effect=RuntimeError("twirp")), None), (False, TimeoutError())],
)
def test_join_livekit_failure_is_502(client, has_ceo, dispatch_error):
    metrics = []
    with patch.object(mod, "put_metric", lambda name, **kw: metrics.append(name)):
        resp, _, _ = _join(client, has_ceo=has_ceo, dispatch_error=dispatch_error)
    assert resp.status_code == 502 and resp.json()["detail"] == {"code": "huddles_upstream"}
    assert metrics == ["huddle.livekit.error"]


@pytest.mark.parametrize("kind", ["dm", "group-dm"])
def test_join_works_in_a_dm_and_group_dm(client, kind):
    """The real _room_team: any room kind joins; a DM's agent is a one-agent team that gets Lightbulb."""
    meta = {"ownerId": "org_a", "kind": kind}
    with (
        patch.object(vc.messaging, "require_room_member", AsyncMock(return_value=(meta, {}))),
        patch.object(vc, "listening_agents", AsyncMock(return_value=[{"id": "agent-eng", "name": "Engineering"}])),
    ):
        resp, _, dispatch = _join(client, room_team=AsyncMock(wraps=vc._room_team))
    assert resp.status_code == 200 and resp.json()["lightbulb"] is True
    dispatch.assert_awaited_once()
    _join.wake.assert_called_once_with("org_a")


def test_people_only_room_joins_without_dispatch_or_wake(client):
    meta = {"ownerId": "org_a", "kind": "group-dm"}
    with (
        patch.object(vc.messaging, "require_room_member", AsyncMock(return_value=(meta, {}))),
        patch.object(vc, "listening_agents", AsyncMock(return_value=[])),
    ):
        resp, mint, dispatch = _join(client, room_team=AsyncMock(wraps=vc._room_team))
    assert resp.status_code == 200 and resp.json()["lightbulb"] is False and resp.json()["token"] == "jwt"
    mint.assert_called_once()
    dispatch.assert_not_awaited()
    _join.wake.assert_not_called()


def test_get_flags_a_people_only_room(client):
    with (
        patch.object(mod.messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_a"}, {}))),
        patch.object(vc, "listening_agents", AsyncMock(return_value=[])),
        patch.object(lk, "participants", AsyncMock(return_value=["user_a"])),
    ):
        assert client.get(f"/api/v1/huddles/{ROOM}").json() == {
            "userIds": ["user_a"],
            "available": True,
            "lightbulb": False,
        }


def test_get_guest_is_404(client):
    with (
        patch.object(mod.messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_b"}, {}))),
        patch.object(lk, "participants", AsyncMock(side_effect=AssertionError("no LiveKit call"))),
    ):
        resp = client.get(f"/api/v1/huddles/{ROOM}")
    assert resp.status_code == 404 and resp.json()["detail"] == {"code": "room_not_found"}


def test_get_lists_participants(client):
    with (
        patch.object(mod.messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_a"}, {}))),
        patch.object(lk, "participants", AsyncMock(return_value=["user_a", "user_b"])),
    ):
        assert client.get(f"/api/v1/huddles/{ROOM}").json() == {
            "userIds": ["user_a", "user_b"],
            "available": True,
            "lightbulb": True,
        }


def test_get_is_empty_when_not_configured(client):
    with (
        patch.object(mod.settings, "LIVEKIT_URL", ""),
        patch.object(mod.messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_a"}, {}))),
        patch.object(lk, "participants", AsyncMock(side_effect=AssertionError("no LiveKit call"))),
    ):
        assert client.get(f"/api/v1/huddles/{ROOM}").json() == {"userIds": [], "available": False, "lightbulb": True}


def test_get_livekit_failure_is_502(client):
    metrics = []
    with (
        patch.object(mod, "put_metric", lambda name, **kw: metrics.append(name)),
        patch.object(mod.messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_a"}, {}))),
        patch.object(lk, "participants", AsyncMock(side_effect=RuntimeError("twirp"))),
    ):
        resp = client.get(f"/api/v1/huddles/{ROOM}")
    assert resp.status_code == 502 and resp.json()["detail"] == {"code": "huddles_upstream"}
    assert metrics == ["huddle.livekit.error"]


INTERNAL = [
    (f"/api/v1/internal/huddles/{ROOM}/summon", {"ownerId": "org_a", "speakerUserId": "user_a", "roomToken": RT}),
    (
        "/api/v1/internal/huddles/calls/c1/turns",
        {"ownerId": "org_a", "speakerUserId": "user_a", "seq": 1, "utterance": "hi", "roomToken": RT},
    ),
    ("/api/v1/internal/huddles/calls/c1/end", {"ownerId": "org_a", "usageSeconds": 5, "roomToken": RT}),
    (f"/api/v1/internal/huddles/{ROOM}/presence", {"ownerId": "org_a", "userIds": [], "roomToken": RT}),
    (f"/api/v1/internal/huddles/{ROOM}/gate", {"ownerId": "org_a", "roomToken": RT}),
]


@pytest.mark.parametrize("path,body", INTERNAL)
def test_internal_routes_need_the_worker_secret(client, path, body):
    with (
        patch.object(vc, "start_huddle_call", AsyncMock()) as summon,
        patch.object(vc, "start_huddle_turn", AsyncMock()) as turn,
        patch.object(vc, "get_call", AsyncMock()) as get_call,
        patch.object(mod.messaging_repo, "get_room", AsyncMock()) as get_room,
    ):
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers={"X-Lightbulb-Huddle-Secret": "wrong"}).status_code == 401
        with patch.object(mod.settings, "HUDDLE_WORKER_SECRET", ""):
            assert client.post(path, json=body, headers={"X-Lightbulb-Huddle-Secret": ""}).status_code == 401
            assert client.post(path, json=body, headers=W).status_code == 401
    for m in (summon, turn, get_call, get_room):
        m.assert_not_awaited()


def test_summon_passes_through_voice_busy(client):
    with patch.object(vc, "start_huddle_call", AsyncMock(side_effect=vc._problem(409, code="voice_busy"))):
        resp = client.post(INTERNAL[0][0], json=INTERNAL[0][1], headers=W)
    assert resp.status_code == 409 and resp.json()["detail"] == {"code": "voice_busy"}


def test_summon_returns_the_call(client):
    with patch.object(vc, "start_huddle_call", AsyncMock(return_value={"callId": "c1"})) as start:
        resp = client.post(INTERNAL[0][0], json=INTERNAL[0][1], headers=W)
    assert resp.json() == {"callId": "c1"}
    assert start.await_args.kwargs == {"owner_id": "org_a", "room_id": ROOM, "speaker_user_id": "user_a"}


def test_turn_returns_turn_and_agent(client):
    with (
        patch.object(vc, "get_call", AsyncMock(return_value=_huddle_call())),
        patch.object(vc, "start_huddle_turn", AsyncMock(return_value={"turnId": "t", "agentId": "a"})) as start,
    ):
        resp = client.post(INTERNAL[1][0], json=INTERNAL[1][1], headers=W)
    assert resp.json() == {"turnId": "t", "agentId": "a"}
    assert start.await_args.kwargs["owner_id"] == "org_a" and start.await_args.kwargs["call_id"] == "c1"


def _huddle_call(**over):
    return {"callId": "c1", "ownerId": "org_a", "kind": "huddle", "roomId": ROOM, "status": vc.OPEN, **over}


def test_end_closes_with_reported_usage(client):
    with (
        patch.object(vc, "get_call", AsyncMock(return_value=_huddle_call())),
        patch.object(vc, "close_call", AsyncMock(return_value=True)) as close,
    ):
        resp = client.post(INTERNAL[2][0], json=INTERNAL[2][1], headers=W)
    assert resp.json() == {"closed": True}
    close.assert_awaited_once_with("c1", usage_seconds=5)


@pytest.mark.parametrize(
    "call,body_owner,status",
    [
        (None, "org_a", 404),
        (_huddle_call(kind="room"), "org_a", 404),
        (_huddle_call(ownerId="org_b"), "org_a", 401),  # the token was minted for org_a's room
        (_huddle_call(), "org_b", 404),  # right token, wrong body owner
    ],
)
def test_end_refuses_a_foreign_or_non_huddle_call(client, call, body_owner, status):
    body = {**INTERNAL[2][1], "ownerId": body_owner}
    with (
        patch.object(vc, "get_call", AsyncMock(return_value=call)),
        patch.object(vc, "close_call", AsyncMock()) as close,
    ):
        assert client.post(INTERNAL[2][0], json=body, headers=W).status_code == status
    close.assert_not_awaited()


@pytest.mark.parametrize("usage", [-1, 1.5, "x"])
def test_end_rejects_bad_usage(client, usage):
    with patch.object(vc, "close_call", AsyncMock()) as close:
        resp = client.post(INTERNAL[2][0], json={**INTERNAL[2][1], "usageSeconds": usage}, headers=W)
    assert resp.status_code == 400  # the redacting validation handler
    close.assert_not_awaited()


def test_presence_publishes_huddle_changed(client):
    publisher = MagicMock(publish_room=AsyncMock())
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"})),
        patch.object(mod, "get_live_event_publisher", return_value=publisher),
    ):
        resp = client.post(INTERNAL[3][0], json={**INTERNAL[3][1], "userIds": ["user_a", "user_b"]}, headers=W)
    assert resp.status_code == 200
    room, event = publisher.publish_room.await_args.args
    assert room == ROOM
    assert event["type"] == "room.huddle.changed"
    assert event["payload"] == {"roomId": ROOM, "userIds": ["user_a", "user_b"]}


def test_presence_refuses_another_owners_room(client):
    publisher = MagicMock(publish_room=AsyncMock())
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_b"})),
        patch.object(mod, "get_live_event_publisher", return_value=publisher),
    ):
        assert client.post(INTERNAL[3][0], json=INTERNAL[3][1], headers=W).status_code == 404
    publisher.publish_room.assert_not_awaited()


FRAMES = "/api/v1/internal/calls/c1/frames"
EVENTS = {"events": [{"type": "chunk", "text": "hi"}]}
HOOK = {"Authorization": f"Bearer {TOKEN}"}


def _frames(client, call, headers=HOOK):
    with (
        patch.object(vc, "get_call", AsyncMock(return_value=call)),
        patch(
            "routers.messaging.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_a", "hook_token": TOKEN}),
        ) as container,
        patch.object(lk, "send_frames", AsyncMock()) as send,
    ):
        return client.post(FRAMES, json=EVENTS, headers=headers), send, container


def test_frames_forward_for_a_huddle_call(client):
    resp, send, container = _frames(client, _huddle_call())
    assert resp.status_code == 200
    send.assert_awaited_once_with(ROOM, EVENTS["events"])
    assert container.await_args.args[0] == "org_a"  # the call's owner, not anything the caller named


@pytest.mark.parametrize("call", [None, _huddle_call(kind="dm"), _huddle_call(status="call_closing")])
def test_frames_404_for_missing_non_huddle_or_closed(client, call):
    resp, send, _ = _frames(client, call)
    assert resp.status_code == 404
    send.assert_not_awaited()


def test_frames_need_the_owners_hook_token(client):
    resp, send, _ = _frames(client, _huddle_call(), headers={"Authorization": "Bearer other-owner-token"})
    assert resp.status_code == 401
    send.assert_not_awaited()


def test_join_normalizes_an_uppercase_room_id(client):
    team = AsyncMock(return_value=({"ownerId": "org_a"}, TEAM))
    resp, mint, dispatch = _join(client, room_team=team, path_room=ROOM.upper())
    assert resp.status_code == 200
    assert resp.json()["roomName"] == lk.room_name(ROOM)
    assert team.await_args.args[2] == ROOM and mint.call_args.kwargs["room_id"] == ROOM
    meta = dispatch.await_args.args[1]
    assert meta["roomId"] == ROOM and meta["roomToken"] == RT


def test_summon_refuses_a_token_for_another_room(client):
    with patch.object(vc, "start_huddle_call", AsyncMock()) as start:
        resp = client.post(f"/api/v1/internal/huddles/{ROOM2}/summon", json=INTERNAL[0][1], headers=W)
    assert resp.status_code == 401
    start.assert_not_awaited()


def test_summon_refuses_a_token_for_another_owner(client):
    with patch.object(vc, "start_huddle_call", AsyncMock()) as start:
        resp = client.post(INTERNAL[0][0], json={**INTERNAL[0][1], "ownerId": "org_b"}, headers=W)
    assert resp.status_code == 401
    start.assert_not_awaited()


def test_presence_refuses_a_token_for_another_room(client):
    publisher = MagicMock(publish_room=AsyncMock())
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"})),
        patch.object(mod, "get_live_event_publisher", return_value=publisher),
    ):
        resp = client.post(f"/api/v1/internal/huddles/{ROOM2}/presence", json=INTERNAL[3][1], headers=W)
    assert resp.status_code == 401
    publisher.publish_room.assert_not_awaited()


def test_turn_refuses_a_token_for_another_rooms_call(client):
    with (
        patch.object(vc, "get_call", AsyncMock(return_value=_huddle_call(roomId=ROOM2))),
        patch.object(vc, "start_huddle_turn", AsyncMock()) as start,
    ):
        assert client.post(INTERNAL[1][0], json=INTERNAL[1][1], headers=W).status_code == 401
    start.assert_not_awaited()


def test_internal_validation_error_does_not_echo_the_transcript(client):
    body = {**INTERNAL[1][1], "transcript": "PRIVATE-WORDS " + "x" * 12_000}
    with patch.object(vc, "start_huddle_turn", AsyncMock()) as start:
        resp = client.post(INTERNAL[1][0], json=body, headers=W)
    assert resp.status_code == 400
    assert "PRIVATE-WORDS" not in resp.text
    start.assert_not_awaited()


def test_presence_drops_a_stale_snapshot(client):
    publisher = MagicMock(publish_room=AsyncMock())
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"})),
        patch.object(mod.container_repo, "try_advance_huddle_presence", AsyncMock(return_value=False)) as adv,
        patch.object(mod, "get_live_event_publisher", return_value=publisher),
    ):
        resp = client.post(INTERNAL[3][0], json={**INTERNAL[3][1], "version": 7}, headers=W)
    assert resp.status_code == 200
    adv.assert_awaited_once_with(ROOM, 7)
    publisher.publish_room.assert_not_awaited()


# -- the proactive-speech gate --

GATE = f"/api/v1/internal/huddles/{ROOM}/gate"
GATE_BODY = {
    "ownerId": "org_a",
    "roomToken": RT,
    "transcript": [{"author": "Aden", "text": "ok that settles it"}, {"author": "Leo", "text": "cool"}],
    "people": ["Aden", "Leo"],
    "lightbulbLastSaid": None,
    "spokeSecondsAgo": None,
    "pendingNews": ["Finance: the Q3 report is done"],
    "pauseMs": 900,
}


def _gate(client, body=GATE_BODY, answers=None, room=None):
    decide = AsyncMock(return_value=answers)
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value=room or {"ownerId": "org_a"})),
        patch.object(mod.speak_gate.jev, "decide", decide),
    ):
        return client.post(GATE, json=body, headers=W), decide


def test_gate_calls_jev_with_the_news_prompt(client):
    answers = {"turn": {"choice": "speak", "probabilities": {"speak": 0.93}}, "kind": {"choice": "N1"}}
    resp, decide = _gate(client, answers=answers)
    assert resp.status_code == 200 and resp.json() == {"pSpeak": 0.93, "kind": "N1"}
    state, questions = decide.await_args.args
    assert questions == mod.speak_gate.questions("news")
    assert questions["turn"]["instructions"] == mod.speak_gate.NEWS_INSTRUCTIONS
    assert mod.speak_gate.NEWS_INSTRUCTIONS != mod.speak_gate.SPEAK_INSTRUCTIONS  # the news clause replaced
    assert state == {
        "room": {"kind": "call", "people": ["Aden", "Leo"], "ai": "Lightbulb"},
        "recent": [{"author": "Aden", "text": "ok that settles it"}],
        "latest": {"author": "Leo", "text": "cool", "pauseMs": 900},
        "lightbulb": {"news": ["Finance: the Q3 report is done"]},
    }


def test_gate_is_silent_when_jev_fails(client):
    resp, _ = _gate(client, answers=None)
    assert resp.json() == {"pSpeak": 0.0, "kind": None}


def test_gate_is_silent_on_timeout(client, monkeypatch):
    import asyncio

    async def slow(state, questions):
        await asyncio.sleep(5)

    monkeypatch.setattr(mod.speak_gate, "TIMEOUT_S", 0.01)
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"})),
        patch.object(mod.speak_gate.jev, "decide", slow),
    ):
        resp = client.post(GATE, json=GATE_BODY, headers=W)
    assert resp.status_code == 200 and resp.json() == {"pSpeak": 0.0, "kind": None}


def test_gate_refuses_another_owners_room_and_a_foreign_token(client):
    resp, decide = _gate(client, room={"ownerId": "org_b"})
    assert resp.status_code == 404
    resp, decide2 = _gate(client, body={**GATE_BODY, "roomToken": _rt(ROOM2, "org_a")})
    assert resp.status_code == 401
    decide.assert_not_awaited()
    decide2.assert_not_awaited()


def test_gate_bounds_the_transcript(client):
    body = {**GATE_BODY, "transcript": [{"author": "A", "text": "x" * 1_000}] * 5}
    resp, decide = _gate(client, body=body)
    assert resp.status_code == 400
    decide.assert_not_awaited()
