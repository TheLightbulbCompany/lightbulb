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
RUN = "33333333-3333-4333-8333-333333333333"


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
):
    repo = mod.meeting_invites.container_repo
    with (
        patch.object(repo, "try_claim_owner_lifecycle", AsyncMock(return_value=lease)) as claim,
        patch.object(repo, "release_owner_lifecycle_claim", AsyncMock()) as release,
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value=fresh_meta)),
        patch.object(vc, "prewake_owner", MagicMock()) as wake,
        patch.object(vc, "prewarm_cards", MagicMock()) as warm,
        patch.object(mod.container_repo, "try_claim_pass", AsyncMock(return_value=fresh)),
        patch.object(vc, "_room_team", room_team or AsyncMock(return_value=({"ownerId": "org_a"}, TEAM))),
        patch.object(mod.messaging, "human_display_name", AsyncMock(return_value="Pat")),
        patch.object(lk, "mint_token", MagicMock(return_value="jwt")) as mint,
        patch.object(lk, "has_ceo", has_ceo if isinstance(has_ceo, AsyncMock) else AsyncMock(return_value=has_ceo)),
        patch.object(lk, "dispatch_ceo", AsyncMock(side_effect=dispatch_error)) as dispatch,
    ):
        resp = client.post(f"/api/v1/huddles/{path_room}/join")
        _join.wake, _join.warm, _join.claim, _join.release = wake, warm, claim, release
        return resp, mint, dispatch


def test_the_calls_orb_route_is_gone():
    # Not a 404 probe: unknown paths fall through to the hosted-MCP root mount.
    assert not [r for r in app.routes if getattr(r, "path", "").startswith("/api/v1/calls")]


def test_join_wakes_the_workspace_and_warms_the_cards(client):
    resp, _, _ = _join(client)
    assert resp.status_code == 200
    # health, then profile.warm per agent, which also compacts that agent's session for this room
    _join.wake.assert_called_once_with("org_a", TEAM, ROOM)
    _join.warm.assert_called_once_with("org_a", TEAM)  # F4: the first /route fits its 3 s budget


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
    resp, _, dispatch = _join(client, room_team=AsyncMock(return_value=(scheduled, TEAM)))
    assert resp.status_code == 200
    dispatch.assert_awaited_once()
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


@pytest.mark.parametrize("kind", ["dm", "group-dm"])
def test_join_works_in_a_dm_and_group_dm(client, kind):
    """The real _room_team: any room kind joins; a DM's agent is a one-agent team that gets Lightbulb."""
    meta = {"ownerId": "org_a", "kind": kind}
    with (
        patch.object(vc.messaging, "require_room_member", AsyncMock(return_value=(meta, {}))),
        patch.object(vc, "room_agents", AsyncMock(return_value=[{"id": "agent-eng", "name": "Engineering"}])),
    ):
        resp, _, dispatch = _join(client, room_team=AsyncMock(wraps=vc._room_team))
    assert resp.status_code == 200 and resp.json()["lightbulb"] is True
    dispatch.assert_awaited_once()
    if kind == "group-dm":  # a DM is owner mode: nothing to wake (test_join_of_a_dm_dispatches_the_owner_router...)
        _join.wake.assert_called_once_with("org_a", [{"id": "agent-eng", "name": "Engineering"}], ROOM)


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
    (f"/api/v1/internal/huddles/{ROOM}/summon", {"ownerId": "org_a", "speakerUserId": "user_a", "roomToken": RT}),
    (
        "/api/v1/internal/huddles/calls/c1/turns",
        {
            "ownerId": "org_a",
            "speakerUserId": "user_a",
            "agentId": "agent-fin",
            "runId": RUN,
            "utterance": "hi",
            "roomToken": RT,
        },
    ),
    ("/api/v1/internal/huddles/calls/c1/end", {"ownerId": "org_a", "usageSeconds": 5, "roomToken": RT}),
    (f"/api/v1/internal/huddles/{ROOM}/presence", {"ownerId": "org_a", "userIds": [], "roomToken": RT}),
    (f"/api/v1/internal/huddles/{ROOM}/team", {"ownerId": "org_a", "roomToken": RT}),
    (f"/api/v1/internal/huddles/{ROOM}/agents/a1/session", {"ownerId": "org_a", "roomToken": RT}),
    (
        f"/api/v1/internal/huddles/{ROOM}/route",
        {"ownerId": "org_a", "speakerUserId": "user_a", "utterance": "hi", "roomToken": RT},
    ),
    (f"/api/v1/internal/huddles/{ROOM}/runs/{RUN}/abort", {"ownerId": "org_a", "roomToken": RT}),
    (f"/api/v1/internal/huddles/{ROOM}/runs/{RUN}/no-post", {"ownerId": "org_a", "roomToken": RT}),
]


@pytest.mark.parametrize("path,body", INTERNAL)
def test_internal_routes_need_the_worker_secret(client, path, body):
    with (
        patch.object(vc, "open_voice_window", AsyncMock()) as summon,
        patch.object(vc, "dispatch_voice_run", AsyncMock()) as turn,
        patch.object(vc, "route_live_line", AsyncMock()) as route,
        patch.object(vc, "abort_voice_run", AsyncMock()) as abort,
        patch.object(vc, "get_call", AsyncMock()) as get_call,
        patch.object(mod.messaging_repo, "get_room", AsyncMock()) as get_room,
    ):
        assert client.post(path, json=body).status_code == 401
        assert client.post(path, json=body, headers={"X-Lightbulb-Huddle-Secret": "wrong"}).status_code == 401
        with patch.object(mod.settings, "HUDDLE_WORKER_SECRET", ""):
            assert client.post(path, json=body, headers={"X-Lightbulb-Huddle-Secret": ""}).status_code == 401
            assert client.post(path, json=body, headers=W).status_code == 401
    for m in (summon, turn, route, abort, get_call, get_room):
        m.assert_not_awaited()


def test_summon_passes_through_voice_busy(client):
    with patch.object(vc, "open_voice_window", AsyncMock(side_effect=vc._problem(409, code="voice_busy"))):
        resp = client.post(INTERNAL[0][0], json=INTERNAL[0][1], headers=W)
    assert resp.status_code == 409 and resp.json()["detail"] == {"code": "voice_busy"}


def test_summon_tells_a_meet_about_a_voice_limit_and_nothing_else(client):
    said = MagicMock(return_value=None)
    with (
        patch.object(mod.meeting_invites, "say_voice_limit", said),
        patch.object(vc, "_fire") as fire,
        patch.object(vc, "open_voice_window", AsyncMock()) as start,
    ):
        start.side_effect = vc._problem(402, code="voice_limit", reason="free_minutes_used")
        assert client.post(INTERNAL[0][0], json=INTERNAL[0][1], headers=W).status_code == 402
        said.assert_called_once_with(ROOM, "free_minutes_used")
        fire.assert_called_once()
        start.side_effect = vc._problem(409, code="voice_busy")
        assert client.post(INTERNAL[0][0], json=INTERNAL[0][1], headers=W).status_code == 409
        said.assert_called_once()


def test_summon_returns_the_call(client):
    with patch.object(vc, "open_voice_window", AsyncMock(return_value={"callId": "c1"})) as start:
        resp = client.post(INTERNAL[0][0], json=INTERNAL[0][1], headers=W)
    assert resp.json() == {"callId": "c1"}
    assert start.await_args.kwargs == {"owner_id": "org_a", "room_id": ROOM, "speaker_user_id": "user_a"}


def _turn(client, body, call=None, steered=None):
    with (
        patch.object(vc, "get_call", AsyncMock(return_value=call or _huddle_call())),
        patch.object(vc, "steer_voice_run", AsyncMock(return_value=steered)) as steer,
        patch.object(
            vc, "dispatch_voice_run", AsyncMock(return_value={"runId": RUN, "agentId": "agent-fin", "steered": False})
        ) as dispatch,
    ):
        return client.post(INTERNAL[1][0], json=body, headers=W), steer, dispatch


def test_turn_dispatches_the_workers_run_for_the_named_agent(client):
    resp, steer, dispatch = _turn(client, INTERNAL[1][1])
    assert resp.json() == {"runId": RUN, "agentId": "agent-fin", "steered": False}
    kw = dispatch.await_args.kwargs
    assert kw["run_id"] == RUN and kw["agent_id"] == "agent-fin" and kw["speaker_id"] == "user_a"
    assert kw["call"]["callId"] == "c1"
    steer.assert_not_awaited()


def test_turn_passes_the_speaker_label_through_and_bounds_it(client):
    resp, steer, dispatch = _turn(client, {**INTERNAL[1][1], "speakerName": "Priya + Dan", "steerRunId": RUN})
    assert resp.status_code == 200
    assert steer.await_args.kwargs["speaker_name"] == dispatch.await_args.kwargs["speaker_name"] == "Priya + Dan"
    assert dispatch.await_args.kwargs["speaker_id"] == "user_a"  # the id still decides who acts
    assert _turn(client, {**INTERNAL[1][1], "speakerName": "x" * 121})[0].status_code in (400, 422)
    assert client.post(INTERNAL[0][0], json={**INTERNAL[0][1], "speakerName": "x" * 121}, headers=W).status_code in (
        400,
        422,
    )


def test_turn_steers_first_and_skips_the_dispatch(client):
    steered = {"runId": "r-old", "agentId": "agent-fin", "steered": True}
    resp, steer, dispatch = _turn(client, {**INTERNAL[1][1], "steerRunId": RUN}, steered=steered)
    assert resp.json() == steered
    assert steer.await_args.kwargs["run_id"] == RUN
    dispatch.assert_not_awaited()


def test_turn_dispatches_when_the_steer_is_refused(client):
    resp, _, dispatch = _turn(client, {**INTERNAL[1][1], "steerRunId": RUN}, steered=None)
    assert resp.json()["steered"] is False
    dispatch.assert_awaited_once()


def test_turn_steer_only_never_dispatches(client):
    # routing v2 §4.3: lines said in the room steer the floor-holder's run, or nothing
    resp, _, dispatch = _turn(client, {**INTERNAL[1][1], "steerRunId": RUN, "steerOnly": True}, steered=None)
    assert resp.json() == {"runId": RUN, "agentId": "agent-fin", "steered": False}
    dispatch.assert_not_awaited()


def _huddle_call(**over):
    return {"callId": "c1", "ownerId": "org_a", "kind": "huddle", "roomId": ROOM, "status": vc.OPEN, **over}


@pytest.mark.parametrize(
    "call", [_huddle_call(status="call_closing"), _huddle_call(kind="dm"), _huddle_call(ownerId="org_b")]
)
def test_turn_needs_the_owners_open_window(client, call):
    with patch.object(mod.livekit_huddles, "room_token", return_value=RT):  # isolate the window check
        resp, _, dispatch = _turn(client, INTERNAL[1][1], call=call)
    assert resp.status_code == 404 and resp.json()["detail"] == {"code": "call_not_found"}
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("missing", ["agentId", "runId"])
def test_turn_without_agent_or_run_id_is_refused(client, missing):
    body = {k: v for k, v in INTERNAL[1][1].items() if k != missing}
    resp, _, dispatch = _turn(client, body)
    assert resp.status_code == 400  # the internal-huddles validation handler: no input echo
    dispatch.assert_not_awaited()


def test_route_a_wake_word_line_is_the_one_router_with_wake(client):
    """Routing v2 §6: no route_voice; "Lightbulb, ..." (no people count) is route_live_line(wake=True)."""
    assert not hasattr(vc, "route_voice")
    routed = {"agentId": "agent-fin", "agentIds": ["agent-fin", "agent-eng"]}
    with patch.object(vc, "route_live_line", AsyncMock(return_value=routed)) as route:
        resp = client.post(INTERNAL[6][0], json=INTERNAL[6][1], headers=W)
    assert resp.json() == routed  # agentId (the first) still there for the shipped worker
    assert route.await_args.kwargs == {
        "owner_id": "org_a", "room_id": ROOM, "speaker_id": "user_a", "utterance": "hi", "transcript": "",
        "humans": 0, "wake": True, "floor": None, "voice_runs": [], "at": [], "setting": "live",
        "unclaimed": False, "open_requests": [], "engagements": [],
    }  # fmt: skip


def test_route_with_a_people_count_may_be_nobody_or_hold(client):
    floor = {"agent": "agent-fin", "speaker": "Leo"}
    with patch.object(vc, "route_live_line", AsyncMock(return_value={"agentId": None, "hold": True})) as live:
        resp = client.post(INTERNAL[6][0], json={**INTERNAL[6][1], "humans": 2, "floor": floor}, headers=W)
    assert resp.json() == {"agentId": None, "hold": True}  # the shipped worker ignores "hold"
    assert live.await_args.kwargs["humans"] == 2 and live.await_args.kwargs["wake"] is False
    assert live.await_args.kwargs["floor"] == floor
    body = {**INTERNAL[6][1], "humans": 2, "setting": "meeting", "unclaimed": True}
    meeting_room = {"ownerId": "org_a", "meeting": {"status": "in_call"}}
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value=meeting_room)),
        patch.object(vc, "route_live_line", AsyncMock(return_value={"agentId": None})) as live,
    ):
        client.post(INTERNAL[6][0], json=body, headers=W)
    assert live.await_args.kwargs["setting"] == "meeting" and live.await_args.kwargs["unclaimed"] is True
    # the worker's evidence reaches the router whole: who the speaker is talking with, and how far each
    # open request's answer has been said
    talking = [
        {"agentId": "agent-fin", "obligation": "r1", "task": "our burn", "state": "active", "expects": "reply",
         "why": "", "last": "paused", "lastBy": "speaker", "seconds": 4}
    ]  # fmt: skip
    opened = [
        {
            "id": "r1",
            "agentId": "agent-fin",
            "task": "our burn",
            "status": "ready",
            "asker": "Priya",
            "delivery": "paused",
        }
    ]
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value=meeting_room)),
        patch.object(vc, "route_live_line", AsyncMock(return_value={"agentId": None})) as live,
    ):
        client.post(INTERNAL[6][0], json={**body, "engagements": talking, "open": opened}, headers=W)
    assert live.await_args.kwargs["engagements"] == talking and live.await_args.kwargs["open_requests"] == opened
    assert client.post(INTERNAL[6][0], json={**body, "engagements": talking * 9}, headers=W).status_code == 400
    assert client.post(INTERNAL[6][0], json={**body, "setting": "zoom"}, headers=W).status_code == 400


def test_route_meeting_setting_outside_a_meeting_room_is_400(client):
    body = {**INTERNAL[6][1], "humans": 2, "setting": "meeting"}
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "org_a"})),
        patch.object(vc, "route_live_line", AsyncMock()) as live,
    ):
        resp = client.post(INTERNAL[6][0], json=body, headers=W)
    assert resp.status_code == 400 and resp.json()["detail"]["code"] == "not_a_meeting"
    live.assert_not_awaited()


def test_abort_passes_the_room(client):
    with patch.object(vc, "abort_voice_run", AsyncMock(return_value=True)) as abort:
        resp = client.post(INTERNAL[7][0], json=INTERNAL[7][1], headers=W)
    assert resp.json() == {"aborted": True}
    abort.assert_awaited_once_with("org_a", ROOM, RUN)


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


FRAMES = f"/api/v1/internal/huddles/org_a/rooms/{ROOM}/frames"
EVENTS = {
    "events": [
        {
            "schemaVersion": 1,
            "eventId": "e1",
            "type": "voice.run.frame",
            "payload": {"roomId": ROOM, "runId": RUN, "agentId": "agent-fin", "frame": {"type": "chunk"}},
        }
    ]
}
HOOK = {"Authorization": f"Bearer {TOKEN}"}


def _frames(client, *, events=EVENTS, headers=HOOK, room=None, send_error=None):
    with (
        patch.object(mod.messaging_repo, "get_room", AsyncMock(return_value=room or {"ownerId": "org_a"})),
        patch(
            "routers.messaging.container_repo.get_by_owner_id",
            AsyncMock(return_value={"owner_id": "org_a", "hook_token": TOKEN}),
        ) as container,
        patch.object(lk, "send_frames", AsyncMock(side_effect=send_error)) as send,
        patch.object(mod, "put_metric") as metric,
        patch.object(vc, "get_call", AsyncMock(side_effect=AssertionError("frames never read the window"))),
    ):
        return client.post(FRAMES, json=events, headers=headers), send, container, metric


def test_frames_relay_by_room_even_after_the_window_closed(client):
    resp, send, container, _ = _frames(client)
    assert resp.status_code == 200 and resp.json() == {"ok": True, "delivered": True}
    send.assert_awaited_once_with(ROOM, EVENTS["events"])
    assert container.await_args.args[0] == "org_a"


def test_frames_need_the_owners_hook_token(client):
    resp, send, _, _ = _frames(client, headers={"Authorization": "Bearer other-owner-token"})
    assert resp.status_code == 401
    send.assert_not_awaited()


def test_frames_refuse_another_owners_room(client):
    resp, send, _, _ = _frames(client, room={"ownerId": "org_b"})
    assert resp.status_code == 404
    send.assert_not_awaited()


def test_frames_refuse_an_event_for_another_room(client):
    bad = {"events": [{**EVENTS["events"][0], "payload": {**EVENTS["events"][0]["payload"], "roomId": ROOM2}}]}
    resp, send, _, _ = _frames(client, events=bad)
    assert resp.status_code == 400 and resp.json()["detail"] == {"code": "room_mismatch"}
    send.assert_not_awaited()


def test_frames_with_no_worker_are_dropped_with_a_metric(client):
    resp, _, _, metric = _frames(client, send_error=RuntimeError("twirp"))
    assert resp.status_code == 202  # accepted, nobody heard it: the bridge posts a completed answer itself
    metric.assert_called_once_with("huddle.frame.no_worker")


def test_join_normalizes_an_uppercase_room_id(client):
    team = AsyncMock(return_value=({"ownerId": "org_a"}, TEAM))
    resp, mint, dispatch = _join(client, room_team=team, path_room=ROOM.upper())
    assert resp.status_code == 200
    assert resp.json()["roomName"] == lk.room_name(ROOM)
    assert team.await_args.args[2] == ROOM and mint.call_args.kwargs["room_id"] == ROOM
    meta = dispatch.await_args.args[1]
    assert meta["roomId"] == ROOM and meta["roomToken"] == RT


def test_summon_refuses_a_token_for_another_room(client):
    with patch.object(vc, "open_voice_window", AsyncMock()) as start:
        resp = client.post(f"/api/v1/internal/huddles/{ROOM2}/summon", json=INTERNAL[0][1], headers=W)
    assert resp.status_code == 401
    start.assert_not_awaited()


def test_summon_refuses_a_token_for_another_owner(client):
    with patch.object(vc, "open_voice_window", AsyncMock()) as start:
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
        patch.object(vc, "dispatch_voice_run", AsyncMock()) as start,
    ):
        assert client.post(INTERNAL[1][0], json=INTERNAL[1][1], headers=W).status_code == 401
    start.assert_not_awaited()


def test_internal_validation_error_does_not_echo_the_transcript(client):
    body = {**INTERNAL[1][1], "transcript": "PRIVATE-WORDS " + "x" * 12_000}
    with patch.object(vc, "dispatch_voice_run", AsyncMock()) as start:
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


# ---- Option D: /team and /agents/{agentId}/session ------------------------------------------

STARTERS = {"ceo": "agent-ceo-1", "finance": "agent-fin"}
MEMBERS = [
    {"kind": "agent", "id": "agent-ceo-1", "displayName": "CEO", "listenMode": "active"},
    {"kind": "agent", "id": "agent-fin", "displayName": "Penny", "listenMode": "mentions"},
    {"kind": "agent", "id": "agent-obs", "displayName": "Quiet", "listenMode": "observe"},
    {"kind": "human", "id": "user_a", "displayName": "Aden"},
]


class _FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item, ConditionExpression=None):
        key = Item["owner_id"]
        if ConditionExpression and key in self.items:
            raise vc.ClientError({"Error": {"Code": "ConditionalCheckFailedException"}}, "PutItem")
        self.items[key] = Item


@pytest.fixture
def option_d():
    table = _FakeTable()
    state = {
        "room": {"ownerId": "org_a", "name": "general"},
        "roster": [{"id": "agent-ceo-1", "name": "CEO", "title": "CEO"}],
    }
    with (
        patch.object(vc, "_table", lambda: table),
        patch.object(vc.messaging_repo, "get_room", AsyncMock(side_effect=lambda _r: state["room"])),
        patch.object(vc.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS)),
        patch.object(vc, "_starter_ids", AsyncMock(return_value=STARTERS)),
        patch.object(vc.messaging, "load_agent_roster", AsyncMock(side_effect=lambda _o: state["roster"])),
        patch.object(
            vc.hermes_profiles, "read_soul_md", MagicMock(return_value="<!-- lightbulb:generated -->\nRuns it.")
        ),
        patch.object(vc.hermes_profiles, "read_voice_notes", MagicMock(return_value="Your name is CEO.\n\nBe blunt.")),
    ):
        yield state


def _session(client, agent="agent-ceo-1"):
    return client.post(
        f"/api/v1/internal/huddles/{ROOM}/agents/{agent}/session", json={"ownerId": "org_a", "roomToken": RT}, headers=W
    )


def test_team_lists_every_agent_any_mode_people_and_the_ceo(client, option_d):
    resp = client.post(f"/api/v1/internal/huddles/{ROOM}/team", json={"ownerId": "org_a", "roomToken": RT}, headers=W)
    assert resp.status_code == 200
    assert resp.json() == {
        "agents": [
            {"id": "agent-ceo-1", "name": "CEO", "listenMode": "active", "defaultName": True},
            {"id": "agent-fin", "name": "Penny", "listenMode": "mentions", "defaultName": False},
            {"id": "agent-obs", "name": "Quiet", "listenMode": "observe", "defaultName": False},
        ],
        "people": [{"userId": "user_a", "name": "Aden"}],
        "ceoId": "agent-ceo-1",
    }


def test_session_gives_the_agents_voice_and_persona_and_never_asks_for_a_name(client, option_d):
    body = _session(client).json()
    assert "askName" not in body and body["voice"] == "marin" and body["name"] == "CEO"
    assert "don't have a name yet" not in body["persona"] and "Be blunt." in body["persona"]
    assert "Runs it." in body["persona"] and "lightbulb:generated" not in body["persona"]
    assert "Penny" in body["persona"] and "Quiet" in body["persona"]


def test_session_uses_the_renamed_name(client, option_d):
    option_d["roster"] = [{"id": "agent-ceo-1", "name": "Nova", "title": "CEO"}]
    body = _session(client).json()
    assert body["name"] == "Nova" and body["persona"].startswith("You are Nova, CEO,")


def test_session_falls_back_to_the_bare_persona_when_reads_fail(client, option_d):
    with patch.object(vc.messaging, "load_agent_roster", AsyncMock(side_effect=RuntimeError("down"))):
        body = _session(client, "agent-fin").json()
    assert body["name"] == "Penny" and body["voice"] == "stone"


def test_session_refuses_an_absent_agent_but_not_an_observe_one(client, option_d):
    resp = _session(client, "agent-nope")
    assert resp.status_code == 409 and resp.json()["detail"]["code"] == "agent_not_in_room"
    assert _session(client, "agent-obs").status_code == 200  # Live ignores listen modes


def test_session_and_team_404_on_a_guest_owned_room(client, option_d):
    option_d["room"] = {"ownerId": "org_other", "name": "shared"}
    assert _session(client).status_code == 404
    resp = client.post(f"/api/v1/internal/huddles/{ROOM}/team", json={"ownerId": "org_a", "roomToken": RT}, headers=W)
    assert resp.status_code == 404 and resp.json()["detail"]["code"] == "room_not_found"


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


LATE = f"/api/v1/internal/huddles/{ROOM}/late-answer"


def test_a_late_answer_is_posted_into_the_room_by_its_agent(client):
    with patch.object(mod.voice_calls, "post_late_answer", AsyncMock()) as post:
        body = {
            "ownerId": "org_a",
            "roomToken": RT,
            "agentId": "agent-fin",
            "runId": "run-9",
            "request": "my board?",
            "answer": "Three tasks open.",
        }
        assert client.post(LATE, json=body, headers=W).status_code == 200
    post.assert_awaited_once_with("org_a", ROOM, "agent-fin", "run-9", "my board?", "Three tasks open.")


def test_a_late_answer_refuses_a_bad_room_token(client):
    body = {"ownerId": "org_a", "roomToken": "nope", "agentId": "agent-fin", "runId": "run-9", "answer": "x"}
    assert client.post(LATE, json=body, headers=W).status_code == 401


def test_no_post_marks_the_rooms_run(client):
    with patch.object(vc, "mark_no_post", AsyncMock(return_value=True)) as mark:
        resp = client.post(INTERNAL[-1][0], json=INTERNAL[-1][1], headers=W)
    assert resp.json() == {"noPost": True}
    mark.assert_awaited_once_with("org_a", ROOM, RUN)


def test_turn_passes_the_turns_other_requests_to_a_new_run_only(client):
    lines = ['Ada is answering "the plan"']
    resp, steer, dispatch = _turn(client, {**INTERNAL[1][1], "requests": lines, "steerRunId": RUN}, steered=None)
    assert dispatch.await_args.kwargs["requests"] == lines
    assert "requests" not in steer.await_args.kwargs
    assert _turn(client, {**INTERNAL[1][1], "requests": ["x"] * 9})[0].status_code == 400
    assert _turn(client, {**INTERNAL[1][1], "requests": ["x" * 201]})[0].status_code == 400
    assert _turn(client, {**INTERNAL[1][1], "requests": [""]})[0].status_code == 400


def test_join_of_a_dm_dispatches_the_owner_router_and_wakes_nothing(client):
    """Owner mode (voice spec §4.5, §5 Home): Live in a DM with the assistant is the Router session, whose reads and
    start_work go through the backend, so no workspace runtime is woken and no routing cards are warmed."""
    meta = {"ownerId": "org_a", "kind": "dm"}
    resp, _, dispatch = _join(client, room_team=AsyncMock(return_value=(meta, TEAM[:1])))
    assert resp.status_code == 200
    assert dispatch.await_args.args[1] == {"roomId": ROOM, "ownerId": "org_a", "roomToken": RT, "mode": "owner"}
    _join.wake.assert_not_called()
    _join.warm.assert_not_called()
