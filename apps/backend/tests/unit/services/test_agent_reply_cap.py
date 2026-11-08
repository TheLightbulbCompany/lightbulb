"""Agent-reply cap: at most AGENT_REPLY_CAP agent messages after a human one wake anyone.

A human message is hop 0; a room turn's reply is its trigger's hop + 1; a post from outside a
room turn (heartbeat, routine, lightbulb-post.sh) is hop 1. At the cap the message still posts,
but wakes nobody -- @mentions included -- and one system line says the chain paused.
"""

import uuid
from unittest.mock import AsyncMock

import pytest

from core.services import messaging
from tests.unit.services.test_agent_message_attachments import AGENT, OWNER, ROOM, _request, created  # noqa: F401

OTHER = str(uuid.UUID(int=2))
RUN = str(uuid.UUID(int=3))
MEMBERS = [
    {
        "kind": "agent",
        "id": AGENT,
        "displayName": "Engineering",
        "mentionHandle": "engineering",
        "listenMode": "active",
    },
    {"kind": "agent", "id": OTHER, "displayName": "Finance", "mentionHandle": "finance", "listenMode": "active"},
]


@pytest.fixture
def room(monkeypatch, created):  # noqa: F811
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS))

    def with_trigger(trigger: dict | None):
        monkeypatch.setattr(
            messaging.messaging_repo,
            "get_run",
            AsyncMock(return_value={"roomId": ROOM, "agentId": AGENT, "triggerMessageId": "t1"}),
        )
        monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=trigger))

    return with_trigger


def _writes(create):
    """(message write, notice write or None) from the create_message mock."""
    calls = [c.kwargs for c in create.await_args_list]
    return calls[0], (calls[1] if len(calls) > 1 else None)


HUMAN = {"author": {"kind": "human", "id": "user_1", "displayName": "Aden"}}
HOP1 = {"author": {"kind": "agent", "id": OTHER, "displayName": "Finance"}, "agentHop": 1}


@pytest.mark.asyncio
async def test_a_reply_to_a_human_is_hop_one_and_still_wakes_listeners(room, created):  # noqa: F811
    room(HUMAN)
    await messaging.create_agent_message(OWNER, _request("numbers look fine", runId=RUN))
    message, notice = _writes(created)
    assert message["agent_hop"] == 1
    assert message["dispatch_targets"] == [{"agentId": OTHER, "eventKind": "room_event"}]
    assert notice is None


@pytest.mark.asyncio
@pytest.mark.parametrize("text", ["agreed", "@finance can you double-check?"])
async def test_the_second_agent_reply_wakes_nobody_mentions_included(room, created, text):  # noqa: F811
    room(HOP1)
    await messaging.create_agent_message(OWNER, _request(text, runId=RUN))
    message, notice = _writes(created)
    assert message["agent_hop"] == 2 and message["dispatch_targets"] == []
    assert notice["author"]["kind"] == "system" and notice["dispatch_targets"] == []
    assert notice["silent"] is True  # the capped reply above already notified; no second push
    assert notice["text"] == "Finance and Friday paused after 2 replies. Reply to keep them going."
    assert notice["idemp_sk"] == messaging.messaging_repo.agent_idemp_sk(
        f"chain-paused:{created.return_value[0]['messageId']}"
    )


@pytest.mark.asyncio
async def test_a_post_outside_a_turn_is_hop_one(room, created):  # noqa: F811
    """Heartbeat / routine / lightbulb-post.sh: no run, so hop 1 -- it may wake one teammate."""
    await messaging.create_agent_message(OWNER, _request("nightly summary"))
    message, notice = _writes(created)
    assert message["agent_hop"] == 1 and message["dispatch_targets"] and notice is None


@pytest.mark.asyncio
async def test_a_capped_message_with_nobody_to_wake_posts_no_notice(room, created, monkeypatch):  # noqa: F811
    room(HOP1)
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=MEMBERS[:1]))
    await messaging.create_agent_message(OWNER, _request("done", runId=RUN))
    message, notice = _writes(created)
    assert message["agent_hop"] == 2 and notice is None


@pytest.mark.asyncio
async def test_a_room_turn_reply_stores_its_trigger_as_in_reply_to(room, created):  # noqa: F811
    """Routing v2 §5: the agent line's `to` edge (who it answered) is the run's trigger."""
    room({**HUMAN, "messageId": "t1"})
    await messaging.create_agent_message(OWNER, _request("numbers look fine", runId=RUN))
    assert _writes(created)[0]["in_reply_to"] == "t1"
    await messaging.create_agent_message(OWNER, _request("heartbeat note"))  # no run: no trigger
    assert created.await_args_list[-1].kwargs["in_reply_to"] is None


@pytest.mark.asyncio
async def test_a_post_with_no_run_stores_the_message_it_answers(room, created):  # noqa: F811
    """The Router's reply and a thread's answer line name no run: the caller passes the asking message, and the voice
    matches a spoken ask's answer by it (voice_meet.agent_lines `inReplyTo`)."""
    await messaging.create_agent_message(OWNER, _request("Three PRs merged."), in_reply_to="voice_turn_1")
    assert created.await_args_list[-1].kwargs["in_reply_to"] == "voice_turn_1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "event_kind,between,counted",
    [
        ("room_event", [{**HUMAN, "threadRootMessageId": None}], True),  # Bob answered first
        ("room_event", [{**HUMAN, "threadRootMessageId": "t1"}], True),  # ... in the trigger's thread
        ("room_event", [{**HUMAN, "threadRootMessageId": "elsewhere"}], False),  # another conversation
        ("room_event", [HOP1], False),  # only a human answer counts
        ("user_request", [{**HUMAN, "threadRootMessageId": None}], False),  # addressed: never a stand-down
    ],
)
async def test_an_ambient_reply_after_a_human_answer_is_counted(
    room,
    created,  # noqa: F811
    monkeypatch,
    event_kind,
    between,
    counted,
):
    """Routing v2 §10: messaging.reply.after_human_answer is R1's residual rate."""
    room({**HUMAN, "messageId": "t1", "messageSeq": 0, "threadRootMessageId": None})
    run = {"roomId": ROOM, "agentId": AGENT, "triggerMessageId": "t1", "eventKind": event_kind}
    monkeypatch.setattr(messaging.messaging_repo, "get_run", AsyncMock(return_value=run))
    listed = AsyncMock(return_value=between)
    monkeypatch.setattr(messaging.messaging_repo, "list_messages_between", listed)
    await messaging.create_agent_message(OWNER, _request("MRR is $42k", runId=RUN))
    hit = any(c.args[0] == "messaging.reply.after_human_answer" for c in messaging.put_metric.call_args_list)
    assert hit is counted
    if event_kind == "room_event":
        listed.assert_awaited_once_with(ROOM, 1, 1)  # strictly between trigger (seq 0) and reply (seq 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", [HUMAN, HOP1])
async def test_a_progress_line_wakes_nobody_and_is_never_the_capped_reply(room, created, trigger):  # noqa: F811
    """A mid-run progress line: stored with its run, but no dispatch targets (so no WORK rows and
    no Jev), no push (silent), no chain-paused notice even at the cap, and no reply telemetry."""
    room(trigger)
    await messaging.create_agent_message(
        OWNER, _request("Pulled the invoices; @finance totals next.", runId=RUN, progress=True)
    )
    message, notice = _writes(created)
    assert message["dispatch_targets"] == [] and notice is None
    assert message["silent"] is True and message["progress"] is True
    assert message["run_id"] == RUN
    messaging._emit_reply_telemetry.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_progress_line_stores_no_mentions(room, created, monkeypatch):  # noqa: F811
    """create_message writes one MENTION# row per human in `mentions` (their mention badge) and
    the projector's mentionedUserIds reads the same list: a progress line passes none."""
    human = {"kind": "human", "id": "user_1", "displayName": "Aden", "mentionHandle": "aden"}
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[*MEMBERS, human]))
    monkeypatch.setattr(messaging, "with_human_handles", lambda members, _emails: members)
    room(HUMAN)
    await messaging.create_agent_message(OWNER, _request("@aden invoices are in.", runId=RUN))
    assert created.await_args.kwargs["mentions"] == [{"kind": "human", "id": "user_1"}]  # the control
    await messaging.create_agent_message(OWNER, _request("@aden invoices pulled.", runId=RUN, progress=True))
    assert created.await_args.kwargs["mentions"] == []


@pytest.mark.asyncio
async def test_the_reply_after_progress_lines_hops_and_dispatches_as_before(room, created):  # noqa: F811
    room(HUMAN)
    await messaging.create_agent_message(OWNER, _request("Pulled the invoices.", runId=RUN, progress=True))
    await messaging.create_agent_message(OWNER, _request("numbers look fine", runId=RUN))
    reply = created.await_args_list[-1].kwargs
    assert reply["agent_hop"] == 1 and reply["silent"] is False and reply["progress"] is False
    assert reply["dispatch_targets"] == [{"agentId": OTHER, "eventKind": "room_event"}]


def test_the_public_message_says_progress_only_when_it_is_one():
    row = {
        "messageId": str(uuid.uuid4()),
        "roomId": ROOM,
        "messageSeq": 2,
        "roomRevision": 1,
        "author": {"kind": "agent", "id": AGENT, "displayName": "Friday"},
        "text": "Pulled the invoices.",
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": messaging.utc_now().isoformat(),
        "runId": RUN,
    }
    wire = messaging.canonical_message({**row, "progress": True}).model_dump(mode="json", by_alias=True)
    assert wire["progress"] is True
    assert "progress" not in messaging.canonical_message(row).model_dump(mode="json", by_alias=True)
