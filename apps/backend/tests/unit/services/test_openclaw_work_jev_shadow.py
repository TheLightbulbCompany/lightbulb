"""Jev routing for Listening traffic: decided once per message, enforced at dispatch (slice 2)."""

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone

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

    async def claim(room_id, message_id, *, attempts=0):
        current = rows.get((room_id, message_id))
        if current is not None and not (current.get("retry") and current["retryAt"] <= time.time()):
            return False
        rows[(room_id, message_id)] = {"pending": True, "claimedAt": 0, "attempts": attempts}
        return True

    async def finalize(room_id, message_id, decision, *, only_if_absent=False, only_if_retry=False):
        current = rows.get((room_id, message_id))
        if only_if_retry:
            writable = current is not None and bool(current.get("retry"))
        else:
            writable = current is None or ((current.get("pending") or current.get("retry")) and not only_if_absent)
        if writable:
            rows[(room_id, message_id)] = decision
        return rows[(room_id, message_id)]

    async def get(room_id, message_id):
        return rows.get((room_id, message_id))

    monkeypatch.setattr(jev.messaging_repo, "claim_decision", claim)
    monkeypatch.setattr(jev.messaging_repo, "finalize_decision", finalize)
    monkeypatch.setattr(jev.messaging_repo, "get_decision", get)
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value="k"))
    monkeypatch.setattr(jev, "agent_card", AsyncMock(side_effect=lambda o, a, name, rec="": f"{name} card. {rec}"))
    return rows


@pytest.fixture
def metrics(monkeypatch):
    out: list = []
    monkeypatch.setattr(jev, "put_metric", lambda name, **kw: out.append((name, kw)))
    return out


def _route_dims(metrics):
    return [kw["dimensions"] for name, kw in metrics if name == "messaging.jev.route"]


def _msg(i, *, author="u1", thread=None, text=None, seq=None):
    return {
        "messageId": f"h{i}",
        "messageSeq": seq if seq is not None else i,
        "author": {"kind": "human", "id": author, "displayName": "Aden"},
        "text": text if text is not None else f"t{i}",
        "threadRootMessageId": thread,
        "createdAt": "2026-09-22T11:00:00+00:00",
    }


@pytest.mark.asyncio
async def test_decision_computed_once_and_returned_to_every_candidate(monkeypatch, store, metrics):
    decide = AsyncMock(return_value=ANSWERS)
    monkeypatch.setattr(jev, "decide", decide)
    got = [
        await jev.route(META, agent, MESSAGE, latest=MESSAGE, members=MEMBERS, history=HISTORY)
        for agent in ("a1", "a2")
    ]
    assert decide.await_count == 1
    assert got[0] == got[1] and got[0]["who"] == "a1"
    state, questions = decide.await_args.args
    assert state["room"] == {"kind": "channel", "name": "general"}
    assert state["recent"] == [{"author": "Growth (agent)", "text": "x" * 1000}]
    assert "thread" not in state
    assert state["latest"]["author"] == "Aden (human)"
    who = questions["who"]
    assert who["instructions"].startswith("Which AI teammate, if any, should act on the latest message?")
    assert set(who["criteria"]) == {"a1", "a2", "none"}
    assert who["criteria"]["a2"] == "Growth card. Last spoke 3h ago."
    assert set(questions["placement"]["criteria"]) == {"main", "thread"}
    row = store[(ROOM, "m1")]
    assert row["who"] == "a1" and row["placement"] == "thread" and row["whoP"]["a1"] == 0.9
    assert row["candidates"] == ["a1", "a2"] and row["model"] == "typesafe-ai/jev" and row["quietChannel"] is True


@pytest.mark.asyncio
async def test_state_carries_thirty_recent_with_the_thread_first(monkeypatch, store, metrics):
    decide = AsyncMock(return_value=ANSWERS)
    monkeypatch.setattr(jev, "decide", decide)
    root = _msg(1, text="the root")
    history = [root, *(_msg(i) for i in range(2, 40)), _msg(40, thread="h1", text="in thread")]
    latest = {**MESSAGE, "threadRootMessageId": "h1", "messageSeq": 41}
    await jev.route(META, "a1", latest, latest=latest, members=MEMBERS, history=history)
    state = decide.await_args.args[0]
    assert [line["text"] for line in state["thread"]] == ["the root", "in thread"]
    assert len(state["recent"]) == 29  # the last 30, less the thread reply already shown
    assert state["recent"][-1]["text"] == "t39"
    assert list(state) == ["room", "thread", "recent", "latest"]


@pytest.mark.asyncio
async def test_state_trims_oldest_recent_first_to_the_char_budget(monkeypatch, store, metrics):
    decide = AsyncMock(return_value=ANSWERS)
    monkeypatch.setattr(jev, "decide", decide)
    history = [_msg(1, text="r" * 5000), *(_msg(i, text="y" * 5000) for i in range(2, 40))]
    history.append(_msg(40, thread="h1", text="z" * 5000))
    latest = {**MESSAGE, "threadRootMessageId": "h1", "messageSeq": 41}
    await jev.route(META, "a1", latest, latest=latest, members=MEMBERS, history=history)
    state = decide.await_args.args[0]
    assert len(json.dumps(state, ensure_ascii=False)) <= jev._STATE_MAX_CHARS
    assert len(state["thread"]) == 2  # the thread survives; general history is trimmed first
    assert 0 < len(state["recent"]) < 29
    assert state["recent"][-1]["text"] == "y" * 1000  # newest kept, capped at 1,000


@pytest.mark.asyncio
async def test_claim_loser_waits_for_the_winner_and_follows_it(monkeypatch, store, metrics):
    decide = AsyncMock(return_value=ANSWERS)
    monkeypatch.setattr(jev, "decide", decide)
    monkeypatch.setattr(jev, "_LOSER_POLL_S", 0.01)
    store[(ROOM, "m1")] = {"pending": True, "claimedAt": 0}  # another consumer is mid-decision

    async def winner_finishes():
        await asyncio.sleep(0.03)
        store[(ROOM, "m1")] = {"who": "a2", "whoP": {"a2": 0.9}, "placement": "main"}

    finisher = asyncio.create_task(winner_finishes())
    got = await jev.route(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    await finisher
    decide.assert_not_awaited()
    jev.agent_card.assert_not_awaited()
    assert got["who"] == "a2"


@pytest.mark.asyncio
async def test_claim_loser_times_out_to_a_stored_fallback(monkeypatch, store, metrics):
    monkeypatch.setattr(jev, "decide", AsyncMock(return_value=ANSWERS))
    monkeypatch.setattr(jev, "_LOSER_POLL_S", 0.01)
    monkeypatch.setattr(jev, "_LOSER_WAIT_S", 0.05)
    store[(ROOM, "m1")] = {"pending": True, "claimedAt": 0}
    result = await jev.route(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    assert result == {"fallback": True, "reason": "loser_timeout"} == store[(ROOM, "m1")]
    jev.decide.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_key_does_no_jev_work_and_stores_one_shared_fallback(monkeypatch, store, metrics):
    # Seen on dev 2026-09-23: ONE backend task had no key (a failed read) and fell back to
    # dispatch-all while another task routed -> two responders. A key-less consumer now joins
    # the shared decision row instead of acting alone.
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value=None))
    claim = AsyncMock()
    monkeypatch.setattr(jev.messaging_repo, "claim_decision", claim)
    decide = AsyncMock()
    monkeypatch.setattr(jev, "decide", decide)
    result = await jev.route(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    assert result == {"fallback": True, "reason": "disabled"} == store[(ROOM, "m1")]
    claim.assert_not_awaited()
    decide.assert_not_awaited()
    jev.agent_card.assert_not_awaited()
    assert ("messaging.jev.decision", {"dimensions": {"outcome": "disabled"}}) in metrics


@pytest.mark.asyncio
async def test_missing_key_follows_a_decision_another_task_already_made(monkeypatch, store, metrics):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value=None))
    routed = {"who": "a1", "whoP": {"a1": 0.97}, "placement": "thread"}
    store[(ROOM, "m1")] = routed
    assert await jev.route(META, "a2", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[]) == routed


@pytest.mark.asyncio
async def test_missing_key_waits_for_a_pending_decision_like_a_loser(monkeypatch, store, metrics):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value=None))
    monkeypatch.setattr(jev, "_LOSER_POLL_S", 0.01)
    store[(ROOM, "m1")] = {"pending": True, "claimedAt": 0}

    async def winner_finishes():
        await asyncio.sleep(0.03)
        store[(ROOM, "m1")] = {"who": "a1", "whoP": {"a1": 0.9}, "placement": "main"}

    task = asyncio.create_task(winner_finishes())
    result = await jev.route(META, "a2", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    await task
    assert result["who"] == "a1"


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
    got = await jev.route(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    row = store[(ROOM, "m1")]
    assert row["fallback"] is True and row["reason"] == "invalid_choice" and "who" not in row
    assert got == row


@pytest.mark.asyncio
async def test_jev_unavailable_stores_fallback(monkeypatch, store, metrics):
    monkeypatch.setattr(jev, "decide", AsyncMock(return_value=None))
    recent = [{"author": {"kind": "human", "id": "u1"}, "text": "hi", "createdAt": "2026-09-22T11:55:00+00:00"}]
    got = await jev.route(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=recent)
    assert got["fallback"] is True and store[(ROOM, "m1")]["quietChannel"] is False


@pytest.mark.asyncio
async def test_winner_failure_stores_a_fallback_so_losers_stop_waiting(monkeypatch, store, metrics):
    monkeypatch.setattr(jev, "decide", AsyncMock(side_effect=RuntimeError("boom")))
    result = await jev.route(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    assert result["fallback"] is True and store[(ROOM, "m1")]["fallback"] is True


@pytest.mark.asyncio
async def test_route_errors_are_swallowed(monkeypatch, metrics):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value="k"))
    monkeypatch.setattr(jev.messaging_repo, "claim_decision", AsyncMock(side_effect=RuntimeError("ddb down")))
    assert await jev.route(META, "a1", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[]) is None


# --------------------------------------------------------------------------- enforcement in _dispatch_message

THREE = {
    **MESSAGE,
    "dispatchTargets": [{"agentId": a, "eventKind": "room_event"} for a in ("a1", "a2", "a3")],
}
MEMBERS3 = [*MEMBERS, {"kind": "agent", "id": "a3", "displayName": "Analytics", "listenMode": "active"}]


async def _dispatch(
    monkeypatch, event_kind, *, agent="a1", message=MESSAGE, listening=True, route=None, follow_up=None
):
    posted: list = []
    created: list = []
    metrics: list = []
    monkeypatch.setattr(work, "put_metric", lambda name, **kw: metrics.append((name, kw.get("dimensions"))))
    monkeypatch.setattr(
        work.container_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"gateway_token": "gw", "agentcore_runtime_arn": "arn:r1"}),
    )
    monkeypatch.setattr(work, "listening_available", AsyncMock(return_value=listening))
    monkeypatch.setattr(work.messaging_repo, "get_member", AsyncMock(return_value={"kind": "agent", "id": agent}))
    monkeypatch.setattr(work.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS3))
    monkeypatch.setattr(work, "human_emails", AsyncMock(return_value={}))
    monkeypatch.setattr(work, "with_human_handles", lambda members, _emails: members)
    monkeypatch.setattr(work.messaging_repo, "workspace_id_for_meta", AsyncMock(return_value="ws"))
    monkeypatch.setattr(
        work,
        "build_ingress_body",
        AsyncMock(side_effect=lambda **kw: {"version": 1, "message": message, "recentHistory": HISTORY}),
    )

    async def create_run(owner, run_id, **fields):
        created.append(run_id)
        return True

    monkeypatch.setattr(work.messaging_repo, "create_run", create_run)
    from core.services import local_session_grants, local_sessions

    monkeypatch.setattr(local_sessions, "live_sessions_for_room", AsyncMock(return_value=[]))
    monkeypatch.setattr(local_session_grants, "mint", lambda *a: None)
    if route is not None:
        monkeypatch.setattr(jev, "route", route)

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
    await worker._dispatch_message(META, {"kind": "agent", "id": agent}, message, event_kind, follow_up=follow_up)
    return posted, created, [dims for name, dims in metrics if name == "messaging.jev.route"]


def _decision(who, p=0.9, placement="thread"):
    return {"who": who, "whoP": {who: p}, "placement": placement}


@pytest.mark.asyncio
async def test_winner_routes_and_the_other_candidates_skip(monkeypatch, store):
    decide = AsyncMock(return_value={**ANSWERS, "who": {"choice": "a2", "probabilities": {"a2": 0.8}}})
    monkeypatch.setattr(jev, "decide", decide)
    monkeypatch.setattr(jev, "put_metric", lambda *a, **k: None)
    posts, runs, outcomes = [], [], []
    for agent in ("a1", "a2", "a3"):
        posted, created, dims = await _dispatch(monkeypatch, "room_event", agent=agent, message=THREE)
        posts += posted
        runs += created
        outcomes += dims
    assert decide.await_count == 1
    assert len(posts) == 1 and len(runs) == 1
    assert posts[0]["routing"] == {"decidedBy": "jev", "placement": "thread"}
    assert outcomes == [{"outcome": "skipped"}, {"outcome": "routed"}, {"outcome": "skipped"}]


@pytest.mark.asyncio
async def test_none_invokes_nobody(monkeypatch):
    for agent in ("a1", "a2"):
        posted, created, dims = await _dispatch(
            monkeypatch, "room_event", agent=agent, route=AsyncMock(return_value=_decision("none"))
        )
        assert posted == [] and created == [] and dims == [{"outcome": "skipped"}]


@pytest.mark.asyncio
async def test_below_threshold_invokes_nobody(monkeypatch):
    posted, created, dims = await _dispatch(
        monkeypatch, "room_event", route=AsyncMock(return_value=_decision("a1", 0.49))
    )
    assert posted == [] and created == [] and dims == [{"outcome": "skipped"}]


ONE = {**MESSAGE, "dispatchTargets": [{"agentId": "a1", "eventKind": "room_event"}]}


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [None, {"fallback": True}])
async def test_fallback_with_one_candidate_still_dispatches_it(monkeypatch, decision):
    """Jev down/undecided with exactly one Listening candidate: unchanged, it just runs."""
    posted, created, dims = await _dispatch(
        monkeypatch, "room_event", agent="a1", message=ONE, route=AsyncMock(return_value=decision)
    )
    assert len(posted) == 1 and len(created) == 1 and dims == [{"outcome": "fallback"}]
    assert "routing" not in posted[0] and "replyThreadRootId" not in posted[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", [{"fallback": True}])
async def test_fallback_with_two_candidates_dispatches_exactly_one(monkeypatch, decision):
    """Jev down/undecided with >=2 Listening candidates (spec #2942): dispatch exactly one
    deterministic responder -- the CEO if it's a candidate, else the lowest agent id -- instead
    of every candidate running independently."""
    monkeypatch.setattr(work, "_fallback_ceo_id", AsyncMock(return_value=None))
    outcomes = {}
    for agent in ("a1", "a2"):
        outcomes[agent] = await _dispatch(
            monkeypatch, "room_event", agent=agent, route=AsyncMock(return_value=decision)
        )
    posted_a1, created_a1, dims_a1 = outcomes["a1"]
    assert len(posted_a1) == 1 and len(created_a1) == 1 and dims_a1 == [{"outcome": "fallback_single"}]
    assert "routing" not in posted_a1[0] and "replyThreadRootId" not in posted_a1[0]
    posted_a2, created_a2, dims_a2 = outcomes["a2"]
    assert posted_a2 == [] and created_a2 == [] and dims_a2 == [{"outcome": "skipped"}]


@pytest.mark.asyncio
async def test_fallback_dispatches_the_ceo_when_it_is_a_candidate(monkeypatch):
    monkeypatch.setattr(work, "_fallback_ceo_id", AsyncMock(return_value="a2"))
    outcomes = {}
    for agent in ("a1", "a2", "a3"):
        outcomes[agent] = await _dispatch(
            monkeypatch, "room_event", agent=agent, message=THREE, route=AsyncMock(return_value={"fallback": True})
        )
    posted_a2, created_a2, dims_a2 = outcomes["a2"]
    assert len(posted_a2) == 1 and len(created_a2) == 1 and dims_a2 == [{"outcome": "fallback_single"}]
    for other in ("a1", "a3"):
        posted, created, dims = outcomes[other]
        assert posted == [] and created == [] and dims == [{"outcome": "skipped"}]


@pytest.mark.asyncio
async def test_real_route_failing_still_dispatches(monkeypatch):
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value="k"))
    monkeypatch.setattr(jev.messaging_repo, "claim_decision", AsyncMock(side_effect=RuntimeError("ddb down")))
    monkeypatch.setattr(jev, "put_metric", lambda *a, **k: None)
    posted, created, dims = await _dispatch(monkeypatch, "room_event", message=ONE)
    assert len(posted) == 1 and len(created) == 1 and dims == [{"outcome": "fallback"}]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("placement", "thread_root", "expected"),
    [
        ("main", None, None),
        ("thread", None, "m1"),
        ("main", "root-1", "root-1"),  # already inside a thread: it stays there
        ("thread", "root-1", "root-1"),
    ],
)
async def test_placement_maps_to_reply_thread_root(monkeypatch, placement, thread_root, expected):
    message = {**MESSAGE, "threadRootMessageId": thread_root}
    posted, _, _ = await _dispatch(
        monkeypatch, "room_event", message=message, route=AsyncMock(return_value=_decision("a1", placement=placement))
    )
    assert posted[0]["routing"] == {"decidedBy": "jev", "placement": placement}
    assert "replyThreadRootId" in posted[0] and posted[0]["replyThreadRootId"] == expected


@pytest.mark.asyncio
async def test_route_never_runs_for_user_request(monkeypatch):
    route = AsyncMock()
    posted, _, dims = await _dispatch(monkeypatch, "user_request", route=route)
    route.assert_not_awaited()
    assert len(posted) == 1 and "routing" not in posted[0] and dims == []


@pytest.mark.asyncio
async def test_route_never_runs_for_a_follow_up(monkeypatch):
    # A follow-up is the agent's own delegation result: deterministic, never routed.
    follow_up = {"id": "11111111-1111-1111-1111-111111111111", "reason": "delegation_completed", "text": "done"}
    route = AsyncMock()
    posted, _, _ = await _dispatch(monkeypatch, "room_event", route=route, follow_up=follow_up)
    route.assert_not_awaited()
    assert len(posted) == 1 and "routing" not in posted[0]


@pytest.mark.asyncio
async def test_route_never_runs_when_plan_gate_drops(monkeypatch):
    route = AsyncMock()
    posted, _, _ = await _dispatch(monkeypatch, "room_event", listening=False, route=route)
    route.assert_not_awaited()
    assert posted == []


# --------------------------------------------------------------------------- the agent's history window


def _row(seq, *, thread=None, mid=None):
    return {
        "messageId": mid or f"00000000-0000-4000-8000-{seq:012d}",
        "roomId": ROOM,
        "messageSeq": seq,
        "roomRevision": 1,
        "author": {"kind": "human", "id": "u1", "displayName": "U"},
        "text": f"t{seq}",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": thread,
        "createdAt": "2026-09-22T00:00:00Z",
        "runId": None,
        "attachments": [],
    }


ROOT = "00000000-0000-4000-8000-000000000003"


@pytest.mark.asyncio
async def test_ingress_history_merges_a_thread_older_than_the_window(monkeypatch):
    window = [_row(s) for s in range(60, 100)] + [_row(100, thread=ROOT)]
    thread = [_row(3, mid=ROOT), _row(10, thread=ROOT), _row(100, thread=ROOT)]
    before = AsyncMock(return_value=window)
    list_thread = AsyncMock(return_value=thread)
    monkeypatch.setattr(work.messaging_repo, "list_messages_before", before)
    monkeypatch.setattr(work.messaging_repo, "list_thread", list_thread)
    trigger = _row(101, thread=ROOT)
    body = await work.build_ingress_body(
        meta={**META, "revision": 1}, target_agent_id="a1", event_kind="room_event", message=trigger, members=[]
    )
    before.assert_awaited_once_with(ROOM, 101, 50)
    list_thread.assert_awaited_once_with(ROOM, ROOT, 101)
    seqs = [m["messageSeq"] for m in body["recentHistory"]]
    assert seqs == [3, 10, *range(60, 101)]  # deduped, strictly increasing, below the trigger


@pytest.mark.asyncio
async def test_ingress_history_skips_the_thread_read_when_the_root_is_in_the_window(monkeypatch):
    window = [_row(s) for s in range(1, 10)]
    monkeypatch.setattr(work.messaging_repo, "list_messages_before", AsyncMock(return_value=window))
    list_thread = AsyncMock()
    monkeypatch.setattr(work.messaging_repo, "list_thread", list_thread)
    trigger = _row(10, thread=ROOT)
    body = await work.build_ingress_body(
        meta={**META, "revision": 1}, target_agent_id="a1", event_kind="room_event", message=trigger, members=[]
    )
    list_thread.assert_not_awaited()
    assert [m["messageSeq"] for m in body["recentHistory"]] == list(range(1, 10))


@pytest.mark.asyncio
async def test_a_timed_out_loser_and_a_late_winner_agree_on_one_decision(monkeypatch, store, metrics):
    """Split-brain guard: the loser gives up and finalizes a fallback; the winner's real decision
    arrives later, loses the conditional write, and follows the stored fallback too. So both
    dispatch the SAME deterministic fallback agent, never two different ones."""
    monkeypatch.setattr(jev, "_LOSER_WAIT_S", 0.0)
    store[(META["roomId"], MESSAGE["messageId"])] = {"pending": True, "claimedAt": 0}
    loser = await jev.route(META, "a2", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    assert loser.get("fallback") is True and store[(META["roomId"], MESSAGE["messageId"])]["fallback"] is True
    final = await jev.messaging_repo.finalize_decision(META["roomId"], MESSAGE["messageId"], {"who": "a1"})
    assert final.get("fallback") is True


@pytest.mark.asyncio
async def test_a_routing_error_with_several_candidates_retries_instead_of_guessing(monkeypatch):
    """route() returns None only when routing itself errored (e.g. DynamoDB), so this consumer
    cannot see the shared decision: guessing could run beside Jev's pick, skipping could
    silence the chosen agent. It retries the delivery, which re-reads the decision."""
    monkeypatch.setattr(work, "_fallback_ceo_id", AsyncMock(return_value="a2"))
    for agent in ("a1", "a2", "a3"):
        with pytest.raises(work.WorkRetry):
            await _dispatch(monkeypatch, "room_event", agent=agent, message=THREE, route=AsyncMock(return_value=None))


@pytest.mark.asyncio
async def test_a_keyless_task_never_overwrites_a_live_claim(monkeypatch, store, metrics):
    """Review finding: the key-less task read no row, a keyed task then claimed it, and the
    key-less fallback overwrote the live claim (Jev silently off). It now writes only into an
    ABSENT row, else waits for the claimant like any loser."""
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value=None))
    monkeypatch.setattr(jev, "_LOSER_POLL_S", 0.01)
    real_get = jev.messaging_repo.get_decision
    calls = {"n": 0}

    async def get_then_claimed(room_id, message_id):
        calls["n"] += 1
        if calls["n"] == 1:
            store[(room_id, message_id)] = {"pending": True, "claimedAt": 0}  # keyed task claims now
            return None
        if calls["n"] == 3:
            store[(room_id, message_id)] = {"who": "a1", "whoP": {"a1": 0.9}, "placement": "thread"}
        return await real_get(room_id, message_id)

    monkeypatch.setattr(jev.messaging_repo, "get_decision", get_then_claimed)
    result = await jev.route(META, "a2", MESSAGE, latest=MESSAGE, members=MEMBERS, history=[])
    assert result["who"] == "a1"


@pytest.mark.asyncio
async def test_rate_limited_decisions_retry_then_fall_back_after_the_cap(monkeypatch, store, metrics):
    """Dev 2026-09-23: a 44-message paste drew 429s, and falling straight back ran one agent on
    every chit-chat line. A 429 now stores a retry row every consumer backs off on; the first one
    back re-claims and asks again, and only after RATE_LIMIT_RETRIES does it fall back."""
    decide = AsyncMock(return_value=jev.RATE_LIMITED)
    monkeypatch.setattr(jev, "decide", decide)
    key = (META["roomId"], MESSAGE["messageId"])
    fresh = {**MESSAGE, "createdAt": datetime.now(timezone.utc).isoformat()}

    async def route(agent):
        return await jev.route(META, agent, fresh, latest=fresh, members=MEMBERS, history=HISTORY)

    for attempt in range(1, jev.RATE_LIMIT_RETRIES + 1):
        got = [await route(agent) for agent in ("a1", "a2")]
        # The second consumer arrives inside the backoff: it backs off too, no second Jev call.
        assert got[0] == got[1] and got[0]["retry"] is True and got[0]["attempts"] == attempt
        assert decide.await_count == attempt
        assert got[0]["retryAt"] - time.time() == pytest.approx(10 * attempt, abs=2)
        store[key]["retryAt"] = 0  # the backoff elapses
    final = await route("a1")
    assert final["fallback"] is True and final["reason"] == "rate_limited"
    assert decide.await_count == jev.RATE_LIMIT_RETRIES + 1
    # A retry that then succeeds routes normally, and every consumer follows it.
    store.clear()
    decide.side_effect = [jev.RATE_LIMITED, ANSWERS]
    assert (await route("a1"))["retry"]
    store[key]["retryAt"] = 0
    assert (await route("a2"))["who"] == "a1" and (await route("a1"))["who"] == "a1"


@pytest.mark.asyncio
async def test_a_retry_decision_backs_the_delivery_off_with_jitter(monkeypatch):
    for agent in ("a1", "a2"):
        with pytest.raises(work.WorkRetry) as caught:
            await _dispatch(
                monkeypatch,
                "room_event",
                agent=agent,
                route=AsyncMock(return_value={"retry": True, "attempts": 2, "retryAt": time.time() + 20}),
            )
        assert 18 <= caught.value.visibility <= 25


@pytest.mark.asyncio
async def test_a_rate_limited_message_older_than_the_window_falls_back_at_once(monkeypatch, store, metrics):
    """Review finding: a retrying message holds its room+agent FIFO group, so a paste would
    stall ~1 min per message. One queued long enough to be stale falls back immediately."""
    monkeypatch.setattr(jev, "decide", AsyncMock(return_value=jev.RATE_LIMITED))
    stale = {**MESSAGE, "createdAt": (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()}
    got = await jev.route(META, "a1", stale, latest=stale, members=MEMBERS, history=HISTORY)
    assert got["fallback"] is True and got["reason"] == "rate_limited"


@pytest.mark.asyncio
async def test_a_keyless_task_finalizes_only_a_still_retry_row(monkeypatch, store, metrics):
    """Review finding: a key-less task that read an elapsed retry row must not overwrite the claim
    a keyed task took meanwhile."""
    key = (META["roomId"], MESSAGE["messageId"])
    store[key] = {"retry": True, "attempts": 1, "retryAt": 0}
    monkeypatch.setattr(jev, "_gateway_key", AsyncMock(return_value=None))
    monkeypatch.setattr(jev, "_LOSER_POLL_S", 0.01)
    real_finalize = jev.messaging_repo.finalize_decision

    async def claimed_first(room_id, message_id, decision, **kw):
        store[key] = {"pending": True, "claimedAt": 0, "attempts": 1}  # a keyed task re-claims now
        got = await real_finalize(room_id, message_id, decision, **kw)
        store[key] = {"who": "a1", "whoP": {"a1": 0.9}, "placement": "main"}  # ...and decides
        return got

    monkeypatch.setattr(jev.messaging_repo, "finalize_decision", claimed_first)
    got = await jev.route(META, "a2", MESSAGE, latest=MESSAGE, members=MEMBERS, history=HISTORY)
    assert got["who"] == "a1"
