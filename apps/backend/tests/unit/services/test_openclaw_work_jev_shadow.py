"""Slice-1 SHADOW routing: Jev's pick is recorded once per message and metered, dispatch is unchanged."""

import pytest
from unittest.mock import AsyncMock

from core.services import jev
from core.services import openclaw_work as work
from core.services.openclaw_work import OpenClawWorkWorker

ROOM = "11111111-1111-4111-8111-111111111111"
META = {
    "ownerId": "org_1",
    "roomId": ROOM,
    "kind": "channel",
    "name": "general",
    "revision": 1,
    "workspaceId": "ws",
    "deliveryKey": "dk",
}
MEMBERS = [
    {"kind": "agent", "id": "a1", "displayName": "Finance", "listenMode": "active"},
    {"kind": "agent", "id": "a2", "displayName": "Growth", "listenMode": "active"},
    {"kind": "human", "id": "u1", "displayName": "Aden"},
]
MESSAGE = {
    "messageId": "m1",
    "messageSeq": 5,
    "author": {"kind": "human", "id": "u1", "displayName": "Aden"},
    "text": "what did we spend on cloud last month?",
    "createdAt": "2026-09-22T12:00:00+00:00",
    "dispatchTargets": [{"agentId": "a1", "eventKind": "room_event"}, {"agentId": "a2", "eventKind": "room_event"}],
}
HISTORY = [
    {
        "author": {"kind": "agent", "id": "a2", "displayName": "Growth"},
        "text": "x" * 5000,
        "createdAt": "2026-09-22T09:00:00+00:00",
    },
]
ANSWERS = {
    "who": {"type": "choice", "choice": "a1", "probabilities": {"a1": 0.9, "a2": 0.05, "none": 0.05}},
    "placement": {"type": "choice", "choice": "thread", "probabilities": {"main": 0.2, "thread": 0.8}},
}


@pytest.fixture
def store(monkeypatch):
    rows: dict = {}

    async def claim(room_id, message_id):
        if (room_id, message_id) in rows:
            return False
        rows[(room_id, message_id)] = {"pending": True, "claimedAt": 0}
        return True

    async def put(room_id, message_id, decision):
        rows[(room_id, message_id)] = decision

    async def get(room_id, message_id):
        return rows.get((room_id, message_id))

    monkeypatch.setattr(jev.messaging_repo, "claim_decision", claim)
    monkeypatch.setattr(jev.messaging_repo, "put_decision", put)
    monkeypatch.setattr(jev.messaging_repo, "get_decision", get)
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value="k"))
    monkeypatch.setattr(jev, "agent_card", AsyncMock(side_effect=lambda o, a, name, rec="": f"{name} card. {rec}"))
    return rows


@pytest.fixture
def metrics(monkeypatch):
    out: list = []
    monkeypatch.setattr(jev, "put_metric", lambda name, **kw: out.append((name, kw)))
    return out


def _shadow_dims(metrics):
    return [kw["dimensions"] for name, kw in metrics if name == "messaging.jev.shadow"]


@pytest.mark.asyncio
async def test_decision_computed_once_across_two_candidates(monkeypatch, store, metrics):
    decide = AsyncMock(return_value=ANSWERS)
    monkeypatch.setattr(jev, "decide", decide)
    for agent in ("a1", "a2"):
        await jev.shadow(META, agent, MESSAGE, latest=MESSAGE, members=MEMBERS, history=HISTORY)
    assert decide.await_count == 1
    state, questions = decide.await_args.args
    assert state["room"] == {"kind": "channel", "name": "general"}
    assert state["recent"] == [{"author": "Growth (agent)", "text": "x" * 1000}]
    assert state["latest"]["author"] == "Aden (human)"
    who = questions["who"]
    assert who["instructions"].startswith("Which AI teammate, if any, should act on the latest message?")
    assert set(who["criteria"]) == {"a1", "a2", "none"}
    assert who["criteria"]["a2"] == "Growth card. Last spoke 3h ago."
    assert set(questions["placement"]["criteria"]) == {"main", "thread"}
    row = store[(ROOM, "m1")]
    assert row["who"] == "a1" and row["placement"] == "thread" and row["whoP"]["a1"] == 0.9
    assert row["candidates"] == ["a1", "a2"] and row["model"] == "typesafe-ai/jev" and row["quietChannel"] is True
    assert _shadow_dims(metrics) == [{"picked": "self", "quiet": "true"}, {"picked": "other", "quiet": "true"}]


@pytest.mark.asyncio
async def test_claim_loser_never_calls_jev_and_meters_pending(monkeypatch, store, metrics):
    decide = AsyncMock(return_value=ANSWERS)
    monkeypatch.setattr(jev, "decide", decide)
    store[(ROOM, "m1")] = {"pending": True, "claimedAt": 0}  # another consumer is mid-decision
    await jev.shadow(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    decide.assert_not_awaited()
    jev.agent_card.assert_not_awaited()
    assert _shadow_dims(metrics) == [{"picked": "pending", "quiet": "unknown"}]


@pytest.mark.asyncio
async def test_claim_loser_meters_against_a_completed_row(monkeypatch, store, metrics):
    decide = AsyncMock(return_value=ANSWERS)
    monkeypatch.setattr(jev, "decide", decide)
    store[(ROOM, "m1")] = {"who": "none", "quietChannel": False}
    await jev.shadow(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    decide.assert_not_awaited()
    assert _shadow_dims(metrics) == [{"picked": "none", "quiet": "false"}]


@pytest.mark.asyncio
async def test_missing_key_disables_before_any_io(monkeypatch, store, metrics):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value=None))
    claim = AsyncMock()
    monkeypatch.setattr(jev.messaging_repo, "claim_decision", claim)
    decide = AsyncMock()
    monkeypatch.setattr(jev, "decide", decide)
    await jev.shadow(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    claim.assert_not_awaited()
    decide.assert_not_awaited()
    jev.agent_card.assert_not_awaited()
    assert store == {}
    assert ("messaging.jev.decision", {"dimensions": {"outcome": "disabled"}}) in metrics
    assert _shadow_dims(metrics) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "answers",
    [
        {**ANSWERS, "who": {"choice": "a9"}},  # not a candidate
        {**ANSWERS, "placement": {"choice": "dm"}},
    ],
)
async def test_invalid_choice_is_stored_as_fallback(monkeypatch, store, metrics, answers):
    monkeypatch.setattr(jev, "decide", AsyncMock(return_value=answers))
    await jev.shadow(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    row = store[(ROOM, "m1")]
    assert row["fallback"] is True and row["reason"] == "invalid_choice" and "who" not in row
    assert _shadow_dims(metrics)[0]["picked"] == "fallback"


@pytest.mark.asyncio
async def test_jev_unavailable_stores_fallback(monkeypatch, store, metrics):
    monkeypatch.setattr(jev, "decide", AsyncMock(return_value=None))
    recent = [{"author": {"kind": "human", "id": "u1"}, "text": "hi", "createdAt": "2026-09-22T11:55:00+00:00"}]
    await jev.shadow(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=recent)
    assert store[(ROOM, "m1")]["fallback"] is True and store[(ROOM, "m1")]["quietChannel"] is False
    assert _shadow_dims(metrics) == [{"picked": "fallback", "quiet": "false"}]


@pytest.mark.asyncio
async def test_shadow_errors_are_swallowed(monkeypatch, metrics):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value="k"))
    monkeypatch.setattr(jev.messaging_repo, "claim_decision", AsyncMock(side_effect=RuntimeError("ddb down")))
    await jev.shadow(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    assert _shadow_dims(metrics) == [{"picked": "error", "quiet": "unknown"}]


# --------------------------------------------------------------------------- the hook in _dispatch_message


async def _dispatch(monkeypatch, event_kind, *, listening=True, shadow=None, follow_up=None):
    posted: list = []
    created: list = []
    monkeypatch.setattr(work, "put_metric", lambda *a, **k: None)
    monkeypatch.setattr(
        work.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr(work, "listening_available", AsyncMock(return_value=listening))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value={"kind": "agent", "id": "a1"}))
    monkeypatch.setattr(work.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS))
    monkeypatch.setattr(work, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(work, "with_human_handles", lambda members, _emails: members)
    monkeypatch.setattr(work.messaging_repo, "workspace_id_for_meta", AsyncMock(return_value="ws"))
    monkeypatch.setattr(
        work, "build_ingress_body", AsyncMock(return_value={"version": 1, "message": MESSAGE, "recentHistory": HISTORY})
    )

    async def create_run(owner, run_id, **fields):
        created.append(run_id)
        return True

    monkeypatch.setattr(work.messaging_repo, "create_run", create_run)
    from core.services import local_session_grants, local_sessions

    monkeypatch.setattr(local_sessions, "live_sessions_for_room", AsyncMock(return_value=[]))
    monkeypatch.setattr(local_session_grants, "mint", lambda *a: None)
    shadow = shadow or AsyncMock()
    monkeypatch.setattr(jev, "shadow", shadow)

    class Answer:
        status_code = 202
        text = ""

        def json(self):
            return {"status": "accepted"}

    worker = OpenClawWorkWorker(sqs_client=object())

    async def post(*args):
        posted.append(args[2])
        return Answer()

    monkeypatch.setattr(worker, "_post", post)
    await worker._dispatch_message(META, {"kind": "agent", "id": "a1"}, MESSAGE, event_kind, follow_up=follow_up)
    return shadow, posted, created


@pytest.mark.asyncio
async def test_hook_runs_for_room_event_and_dispatch_is_unchanged(monkeypatch):
    shadow, posted, created = await _dispatch(monkeypatch, "room_event")
    shadow.assert_awaited_once()
    assert shadow.await_args.args[1] == "a1"
    assert shadow.await_args.kwargs["history"] == HISTORY
    assert len(posted) == 1 and len(created) == 1


@pytest.mark.asyncio
async def test_hook_never_runs_for_user_request(monkeypatch):
    shadow, posted, _ = await _dispatch(monkeypatch, "user_request")
    shadow.assert_not_awaited()
    assert len(posted) == 1


@pytest.mark.asyncio
async def test_hook_never_runs_for_a_follow_up(monkeypatch):
    # A follow-up is the agent's own delegation result: deterministic, never routed.
    follow_up = {"id": "11111111-1111-1111-1111-111111111111", "reason": "delegation_completed", "text": "done"}
    shadow, posted, _ = await _dispatch(monkeypatch, "room_event", follow_up=follow_up)
    shadow.assert_not_awaited()
    assert len(posted) == 1


@pytest.mark.asyncio
async def test_hook_never_runs_when_plan_gate_drops(monkeypatch):
    shadow, posted, _ = await _dispatch(monkeypatch, "room_event", listening=False)
    shadow.assert_not_awaited()
    assert posted == []


@pytest.mark.asyncio
async def test_real_shadow_failing_still_dispatches(monkeypatch):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value="k"))
    monkeypatch.setattr(jev.messaging_repo, "claim_decision", AsyncMock(side_effect=RuntimeError("ddb down")))
    monkeypatch.setattr(jev, "put_metric", lambda *a, **k: None)
    _, posted, created = await _dispatch(monkeypatch, "room_event", shadow=jev.shadow)
    assert len(posted) == 1 and len(created) == 1
