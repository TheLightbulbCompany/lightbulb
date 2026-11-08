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


def _rt(room, owner):
    return hmac.new(SECRET.encode(), f"{room}:{owner}".encode(), hashlib.sha256).hexdigest()


RT = _rt(ROOM, "org_a")
DM = {"ownerId": "org_a", "kind": "dm"}
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
        patch.object(vc, "room_agents", AsyncMock(return_value=TEAM)),
    ):
        yield TestClient(app)
    app.dependency_overrides.clear()


def _problem(status, code):
    return AsyncMock(side_effect=vc._problem(status, code=code))


def _join(
    client,
    *,
    has_ceo=False,
    room_team=None,
    dispatch_error=None,
    path_room=ROOM,
    fresh=True,
    lease="tok",
    fresh_meta=None,
    gate=None,
):
    repo = mod.meeting_invites.container_repo
    with (
        patch.object(repo, "try_claim_owner_lifecycle", AsyncMock(return_value=lease)) as claim,
        patch.object(repo, "release_owner_lifecycle_claim", AsyncMock()) as release,
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value=fresh_meta)),
        patch.object(vc, "prewake_owner", MagicMock()) as wake,
        patch.object(mod.container_repo, "try_claim_pass", AsyncMock(return_value=fresh)),
        patch.object(vc, "_room_team", room_team or AsyncMock(return_value=(DM, TEAM[:1]))),
        patch.object(vc, "_gate", gate or AsyncMock(return_value=("paid", 0))),
        patch.object(mod.messaging_repo, "get_voice_agent", AsyncMock(return_value={"name": "Nova"})),
        patch.object(mod.messaging, "human_display_name", AsyncMock(return_value="Pat")),
        patch.object(lk, "mint_token", MagicMock(return_value="jwt")) as mint,
        patch.object(lk, "has_ceo", has_ceo if isinstance(has_ceo, AsyncMock) else AsyncMock(return_value=has_ceo)),
        patch.object(lk, "dispatch_ceo", AsyncMock(side_effect=dispatch_error)) as dispatch,
    ):
        resp = client.post(f"/api/v1/huddles/{path_room}/join")
        _join.wake, _join.claim, _join.release = wake, claim, release
        return resp, mint, dispatch


def test_the_calls_orb_route_is_gone():
    # Not a 404 probe: unknown paths fall through to the hosted-MCP root mount.
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/calls")]


def test_agent_voice_outside_the_dm_is_409_with_no_room_or_token(client):
    """The multi-voice huddle engine is gone (task 9a): agent voice lives only in the DM with the assistant."""
    resp, mint, dispatch = _join(
        client, room_team=AsyncMock(return_value=({"ownerId": "org_a", "kind": "channel"}, TEAM))
    )
    assert resp.status_code == 409
    assert resp.json()["detail"] == {"code": "voice_in_dm_only", "message": "Voice lives in your room with Nova."}
    mint.assert_not_called()
    dispatch.assert_not_awaited()
    _join.wake.assert_not_called()


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
    assert meta == {"roomId": ROOM, "ownerId": "org_a", "roomToken": RT, "mode": "owner"}


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


def test_join_is_409_while_the_rooms_meeting_is_in_call(client):
    meta = {"ownerId": "org_a", "meeting": {"status": "in_call"}}
    resp, mint, dispatch = _join(client, room_team=AsyncMock(return_value=(meta, TEAM)))
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "meeting_in_call",
        "message": "This room's meeting is live. Join it in Google Meet.",
    }
    mint.assert_not_called()
    dispatch.assert_not_awaited()
    _join.wake.assert_not_called()


def test_a_live_room_join_takes_no_meeting_lease(client):
    assert _join(client)[0].status_code == 200
    _join.claim.assert_not_awaited()


def test_a_meeting_room_join_runs_under_the_meeting_lease(client):
    scheduled = {"ownerId": "org_a", "meeting": {"status": "bot_created"}}
    resp, mint, _ = _join(client, room_team=AsyncMock(return_value=(scheduled, [])))  # people-only: agents 409
    assert resp.status_code == 200
    mint.assert_called_once()
    _join.claim.assert_awaited_once()
    assert _join.claim.await_args.args == (f"meeting#{ROOM}",)
    _join.release.assert_awaited_once_with(f"meeting#{ROOM}", token="tok")


def test_join_is_409_when_the_meeting_went_in_call_since_the_first_read(client):
    scheduled = {"ownerId": "org_a", "meeting": {"status": "bot_created"}}
    resp, mint, dispatch = _join(
        client,
        room_team=AsyncMock(return_value=(scheduled, TEAM)),
        fresh_meta={"ownerId": "org_a", "meeting": {"status": "in_call"}},
    )
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "meeting_in_call"
    mint.assert_not_called()
    dispatch.assert_not_awaited()


def test_join_is_409_while_the_meeting_lease_is_busy(client):
    scheduled = {"ownerId": "org_a", "meeting": {"status": "bot_created"}}
    resp, mint, dispatch = _join(client, room_team=AsyncMock(return_value=(scheduled, TEAM)), lease=None)
    assert resp.status_code == 409
    assert resp.json()["detail"] == {
        "code": "meeting_in_call",
        "message": "This room's meeting is live. Join it in Google Meet.",
    }
    mint.assert_not_called()
    dispatch.assert_not_awaited()


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


@pytest.mark.parametrize("kind,status", [("dm", 200), ("group-dm", 409)])
def test_agent_voice_joins_only_in_a_dm(client, kind, status):
    """The real _room_team: a DM's agent is a one-agent team that gets Lightbulb; a group DM's agents 409."""
    meta = {"ownerId": "org_a", "kind": kind}
    with (
        patch.object(vc.messaging, "require_room_member", AsyncMock(return_value=(meta, {}))),
        patch.object(vc, "room_agents", AsyncMock(return_value=[{"id": "agent-eng", "name": "Engineering"}])),
    ):
        resp, _, dispatch = _join(client, room_team=AsyncMock(wraps=vc._room_team))
    assert resp.status_code == status
    assert dispatch.await_count == (status == 200)


def test_people_only_room_joins_without_dispatch_or_wake(client):
    meta = {"ownerId": "org_a", "kind": "group-dm"}
    with (
        patch.object(vc.messaging, "require_room_member", AsyncMock(return_value=(meta, {}))),
        patch.object(vc, "room_agents", AsyncMock(return_value=[])),
    ):
        resp, mint, dispatch = _join(client, room_team=AsyncMock(wraps=vc._room_team))
    assert resp.status_code == 200 and resp.json()["lightbulb"] is False and resp.json()["token"] == "jwt"
    mint.assert_called_once()
    dispatch.assert_not_awaited()
    _join.wake.assert_not_called()


def test_get_flags_a_people_only_room(client):
    with (
        patch.object(mod.messaging, "require_room_member", AsyncMock(return_value=({"ownerId": "org_a"}, {}))),
        patch.object(vc, "room_agents", AsyncMock(return_value=[])),
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
    (
        f"/api/v1/internal/huddles/{ROOM}/transcript",
        {"ownerId": "org_a", "roomToken": RT, "huddleId": "h1", "lines": []},
    ),
]


@pytest.mark.parametrize("path,body", INTERNAL)
def test_internal_routes_need_the_worker_secret(client, path, body):
    with patch.object(mod.messaging_repo, "get_room", AsyncMock()) as get_room:
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers={"X-Lightbulb-Huddle-Secret": "wrong"}).status_code == 401
        with patch.object(mod.settings, "HUDDLE_WORKER_SECRET", ""):
            assert client.post(path, json=body, headers={"X-Lightbulb-Huddle-Secret": ""}).status_code == 401
            assert client.post(path, json=body, headers=W).status_code == 401
    get_room.assert_not_awaited()


def test_the_huddle_engine_worker_routes_are_gone():
    """Task 9b: the multi-voice huddle engine's worker routes left with it (#5057). Only /transcript stays."""
    paths = {r.path for r in mod.router.routes}
    assert {p for p in paths if p.startswith("/internal/huddles")} == {"/internal/huddles/{room_id}/transcript"}


def test_internal_validation_error_does_not_echo_the_transcript(client):
    line = {"mark": 0, "kind": "person", "name": "Pat", "text": "PRIVATE-WORDS " + "x" * 4_000}
    body = {**INTERNAL[0][1], "lines": [line]}
    with patch.object(mod.meeting_notes, "on_transcript", AsyncMock()) as notes:
        resp = client.post(INTERNAL[0][0], json=body, headers=W)
    assert resp.status_code == 400
    assert "PRIVATE-WORDS" not in resp.text
    notes.assert_not_awaited()


TRANSCRIPT = f"/api/v1/internal/huddles/{ROOM}/transcript"


def test_transcript_batches_are_logged_for_debugging(client, caplog):
    lines = [
        {"mark": 0, "kind": "person", "name": "Prasiddha", "text": "Hey guys, how's it going?", "at": 1790972595.0},
        {"mark": 1, "kind": "agent", "name": "Pulse", "text": "Going well.", "at": 1790972630.0, "to": "Prasiddha"},
    ]
    with (
        caplog.at_level("INFO", logger="routers.huddles"),
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"})),
        patch.object(mod.meeting_notes, "on_transcript", AsyncMock()) as notes,
    ):
        resp = client.post(
            TRANSCRIPT, json={"ownerId": "org_a", "roomToken": RT, "huddleId": "h1", "lines": lines}, headers=W
        )
    assert resp.status_code == 200
    notes.assert_not_awaited()  # not a meeting room: nothing reaches the room
    record = next(r for r in caplog.records if "live transcript" in r.getMessage())
    assert "huddle=h1" in record.getMessage()
    assert "[0 @1790972595] Prasiddha: Hey guys, how's it going?" in record.getMessage()
    assert "[1 @1790972630] Pulse → Prasiddha: Going well." in record.getMessage()


def test_an_answers_wait_to_be_heard_is_logged(client, caplog):
    """The worker's ready -> heard wait (10-09 Meet: ~19 s, unseen) reaches the log, never the room."""
    line = {
        "mark": 3,
        "kind": "agent",
        "name": "Friday",
        "text": "17 items.",
        "at": 1790972630.0,
        "ready": 1790972611.5,
    }
    with (
        caplog.at_level("INFO", logger="routers.huddles"),
        patch.object(
            mod.messaging_repo,
            "get_room",
            AsyncMock(return_value={"ownerId": "org_a", "meeting": {"status": "in_call"}}),
        ),
        patch.object(mod.meeting_notes, "on_transcript", AsyncMock()) as notes,
    ):
        body = {"ownerId": "org_a", "roomToken": RT, "huddleId": "h1", "lines": [line]}
        assert client.post(TRANSCRIPT, json=body, headers=W).status_code == 200
    record = next(r for r in caplog.records if "live transcript" in r.getMessage())
    assert "[3 @1790972630 waited 18.5s] Friday: 17 items." in record.getMessage()
    assert "ready" not in notes.await_args.args[1][0]  # the room's transcript.txt never shows it


def test_a_meeting_rooms_chunks_reach_the_notes_with_the_final_flag(client):
    line = {"mark": 7, "kind": "person", "name": "Pat", "text": "Ship it", "at": 1.0, "to": ""}
    body = {"ownerId": "org_a", "roomToken": RT, "huddleId": "h1", "lines": [line]}
    meta = {"ownerId": "org_a", "meeting": {"status": "in_call"}}
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value=meta)),
        patch.object(mod.meeting_notes, "on_transcript", AsyncMock()) as notes,
    ):
        assert client.post(TRANSCRIPT, json=body, headers=W).status_code == 200
        assert client.post(TRANSCRIPT, json={**body, "final": True}, headers=W).status_code == 200
    assert [c.args for c in notes.await_args_list] == [(ROOM, [line], False), (ROOM, [line], True)]


def test_only_a_final_transcript_may_carry_more_than_500_lines(client):
    line = {"mark": 0, "kind": "person", "name": "Pat", "text": "x", "at": 1.0, "to": ""}
    body = {"ownerId": "org_a", "roomToken": RT, "huddleId": "h1", "lines": [line] * 501}
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"})),
        patch.object(mod, "put_metric"),
    ):
        assert client.post(TRANSCRIPT, json=body, headers=W).status_code == 400
        assert client.post(TRANSCRIPT, json={**body, "final": True}, headers=W).status_code == 200
        too_many = {**body, "final": True, "lines": [line] * 5_001}
        assert client.post(TRANSCRIPT, json=too_many, headers=W).status_code == 400


def test_transcript_refuses_a_bad_room_token(client):
    body = {"ownerId": "org_a", "roomToken": "nope", "huddleId": "h1", "lines": []}
    assert client.post(TRANSCRIPT, json=body, headers=W).status_code == 401


def test_join_of_a_dm_dispatches_the_owner_router_and_wakes_nothing(client):
    """Owner mode (voice spec §4.5, §5 Home): Live in a DM with the assistant is the Router session, whose reads and
    start_work go through the backend, so no workspace runtime is woken."""
    meta = {"ownerId": "org_a", "kind": "dm"}
    gate = AsyncMock(return_value=("paid", 0))
    resp, _, dispatch = _join(client, room_team=AsyncMock(return_value=(meta, TEAM[:1])), gate=gate)
    assert resp.status_code == 200
    gate.assert_awaited_once_with("org_a")
    assert dispatch.await_args.args[1] == {"roomId": ROOM, "ownerId": "org_a", "roomToken": RT, "mode": "owner"}
    _join.wake.assert_not_called()


def test_owner_mode_join_is_402_without_plan_or_free_minutes(client):
    """The /summon gate, at the DM's join: no session (and no realtime bill) without a plan or free minutes."""
    meta = {"ownerId": "org_a", "kind": "dm"}
    limit = vc._problem(402, code="voice_limit", reason="free_minutes_used")
    resp, mint, dispatch = _join(
        client, room_team=AsyncMock(return_value=(meta, TEAM[:1])), gate=AsyncMock(side_effect=limit)
    )
    assert resp.status_code == 402
    assert resp.json()["detail"] == {"code": "voice_limit", "reason": "free_minutes_used"}
    dispatch.assert_not_awaited()
