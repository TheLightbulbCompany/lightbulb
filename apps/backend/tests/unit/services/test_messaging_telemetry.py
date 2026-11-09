"""Room telemetry: CloudWatch metrics and PostHog product events.

The gap these close: a human message that names an agent but dispatches to
nobody used to leave no trace at all — no metric, no event, no log line.
"""

import uuid
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.auth import AuthContext
from core.services import messaging

OWNER = "org_1"
USER = "user_owner"
AGENT = str(uuid.UUID(int=1))
OTHER_AGENT = str(uuid.UUID(int=2))
ROOM = str(uuid.UUID(int=9))


def _agent_member(agent_id: str = AGENT, *, listen_mode: str = "mentions", handle: str = "friday") -> dict:
    return {
        "kind": "agent",
        "id": agent_id,
        "displayName": "Friday",
        "mentionHandle": handle,
        "role": "member",
        "listenMode": listen_mode,
        "joinedSeq": 1,
    }


def _human_member(user_id: str = USER, role: str = "owner") -> dict:
    return {
        "kind": "human",
        "id": user_id,
        "displayName": "Owner",
        "mentionHandle": None,
        "role": role,
        "listenMode": None,
        "joinedSeq": 1,
    }


def _message(seq: int, author: dict, *, targets: list[dict] | None = None, created_at: str | None = None) -> dict:
    return {
        "roomId": ROOM,
        "messageId": str(uuid.uuid4()),
        "messageSeq": seq,
        "roomRevision": 1,
        "author": author,
        "text": "hi",
        "mentions": [],
        "replyToMessageId": None,
        "threadRootMessageId": None,
        "createdAt": created_at or messaging.utc_now().isoformat(),
        "runId": None,
        "dispatchTargets": targets or [],
        "attachments": [],
    }


def _metrics(mock: MagicMock, name: str) -> list[dict]:
    """Value, unit and dimensions of every emission of one metric.

    ``put_metric(name, value, unit, dimensions)`` is positional-friendly, so
    read both forms — a caller switching to positional args must not silently
    empty these assertions.
    """
    out = []
    for call in mock.call_args_list:
        if not call.args or call.args[0] != name:
            continue
        args = call.args
        out.append(
            {
                "value": args[1] if len(args) > 1 else call.kwargs.get("value", 1.0),
                "unit": args[2] if len(args) > 2 else call.kwargs.get("unit", "Count"),
                "dimensions": args[3] if len(args) > 3 else call.kwargs.get("dimensions"),
            }
        )
    return out


def _events(mock: MagicMock, name: str) -> list[dict]:
    return [call.kwargs for call in mock.call_args_list if call.kwargs.get("event") == name]


@pytest.fixture
def telemetry(monkeypatch):
    """Capture both telemetry sinks without emitting anything."""
    put_metric, capture = MagicMock(), MagicMock()
    monkeypatch.setattr(messaging, "put_metric", put_metric)
    monkeypatch.setattr(messaging, "capture_product_event", capture)
    monkeypatch.setattr(messaging, "schedule_trial_first_agent_response", MagicMock(return_value=None))
    return put_metric, capture


def _wire_human_message(
    monkeypatch, members: list[dict], created: dict, room_kind: str = "channel", *, replayed: bool = False
) -> MagicMock:
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=({"kind": room_kind, "ownerId": OWNER}, _human_member())),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(side_effect=lambda *a, **k: members[-1]))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    create = AsyncMock(return_value=(created, replayed))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", create)
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging.container_repo, "touch_last_interactive", AsyncMock())
    monkeypatch.setattr("core.services.onboarding_brief.capture_first_workspace_message", AsyncMock())
    return create


async def _send_human(text: str = "hey @friday", mentions: list[dict] | None = None):
    return await messaging.create_human_message(
        AuthContext(user_id=USER, org_id=OWNER),
        ROOM,
        messaging.CreateMessageRequest(
            clientMessageId=uuid.uuid4(),
            text=text,
            mentions=mentions if mentions is not None else [{"kind": "agent", "id": AGENT}],
            replyToMessageId=None,
            threadRootMessageId=None,
        ),
    )


def _wire_agent_message(
    monkeypatch,
    members: list[dict],
    created: dict,
    *,
    before: list[dict] | None = None,
    links: dict | None = None,
    replayed: bool = False,
) -> MagicMock:
    monkeypatch.setattr(
        messaging.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": OWNER, "kind": "channel"})
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=_agent_member()))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}]))
    monkeypatch.setattr(
        messaging.messaging_repo,
        "get_message_by_id",
        AsyncMock(side_effect=lambda _room, message_id: (links or {}).get(message_id)),
    )
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=members))
    # Email/handle lookup is unrelated to reply telemetry; this fixture has no saved user rows.
    monkeypatch.setattr("core.repositories.user_repo.get", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", AsyncMock(return_value=(created, replayed)))
    scan = AsyncMock(return_value=before or [])
    monkeypatch.setattr(messaging.messaging_repo, "list_messages_before", scan)
    return scan


async def _send_agent(text: str = "done", reply_to: str | None = None, run_id: str | None = None):
    return await messaging.create_agent_message(
        OWNER,
        messaging.AgentMessageRequest(
            eventId=f"evt-{uuid.uuid4()}",
            roomId=ROOM,
            agentId=AGENT,
            text=text,
            replyToMessageId=reply_to,
            threadRootMessageId=None,
            runId=run_id,
        ),
    )


# ---------------------------------------------------------------------------
# human messages
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wake_false_stores_the_message_without_dispatching_the_agent(monkeypatch, telemetry):
    put_metric, _ = telemetry
    members = [_human_member(), _agent_member()]
    created = _message(1, {"kind": "human", "id": USER, "displayName": "Owner"})
    create = _wire_human_message(monkeypatch, members, created, room_kind="dm")
    request = messaging.CreateMessageRequest(
        clientMessageId=uuid.uuid4(), text="record", mentions=[], replyToMessageId=None, threadRootMessageId=None
    )
    auth = AuthContext(user_id=USER, org_id=OWNER)

    await messaging.create_human_message(auth, ROOM, request, wake=False)
    quiet_dims = [m["dimensions"] for m in _metrics(put_metric, "messaging.message.created")]
    quiet_dispatch = [m["dimensions"] for m in _metrics(put_metric, "messaging.dispatch.targets")]
    await messaging.create_human_message(auth, ROOM, request)

    quiet, woken = (call.kwargs["dispatch_targets"] for call in create.await_args_list)
    assert quiet == [] and [t["agentId"] for t in woken] == [AGENT]
    # Telemetry follows what was stored: no targets for the quiet message, one for the default.
    assert [d["has_targets"] for d in quiet_dims] == ["false"]
    assert [d["count_bucket"] for d in quiet_dispatch] == ["0"]
    assert [m["dimensions"]["has_targets"] for m in _metrics(put_metric, "messaging.message.created")][1:] == ["true"]


@pytest.mark.asyncio
async def test_human_message_with_a_dispatch_target_reports_created_and_dispatch(monkeypatch, telemetry):
    put_metric, capture = telemetry
    members = [_human_member(), _agent_member()]
    created = _message(1, {"kind": "human", "id": USER, "displayName": "Owner"})
    _wire_human_message(monkeypatch, members, created)

    await _send_human()

    assert _metrics(put_metric, "messaging.message.created") == [
        {
            "value": 1.0,
            "unit": "Count",
            "dimensions": {"author_kind": "human", "room_kind": "channel", "has_targets": "true"},
        }
    ]
    assert _metrics(put_metric, "messaging.dispatch.targets") == [
        {"value": 1.0, "unit": "Count", "dimensions": {"count_bucket": "1", "room_kind": "channel", "source": "human"}}
    ]
    sent = _events(capture, "room_message_sent")
    assert len(sent) == 1 and sent[0]["distinct_id"] == USER
    assert sent[0]["properties"] == {
        "workspace_id": OWNER,
        "room_kind": "channel",
        "mentions_agents": 1,
        "mentions_sessions": False,
        "is_thread_reply": False,
        "has_attachment": False,
        "first_in_room": True,
        "sender_is_owner": True,
    }
    assert _events(capture, "agent_dispatch_missed") == []


@pytest.mark.asyncio
async def test_a_mention_that_dispatches_to_nobody_is_reported(monkeypatch, telemetry):
    put_metric, capture = telemetry
    # An observe member never runs, so mentioning it addresses nobody.
    members = [_human_member(), _agent_member(listen_mode="observe")]
    created = _message(4, {"kind": "human", "id": USER, "displayName": "Owner"})
    create = _wire_human_message(monkeypatch, members, created)

    await _send_human()

    assert create.await_args.kwargs["dispatch_targets"] == []
    assert _metrics(put_metric, "messaging.message.created")[0]["dimensions"]["has_targets"] == "false"
    assert _metrics(put_metric, "messaging.dispatch.targets")[0]["dimensions"]["count_bucket"] == "0"
    missed = _events(capture, "agent_dispatch_missed")
    assert len(missed) == 1 and missed[0]["distinct_id"] == USER
    assert missed[0]["properties"] == {"workspace_id": OWNER, "reason": "no_targets", "room_kind": "channel"}


@pytest.mark.asyncio
async def test_an_unmentioned_human_message_reports_no_miss(monkeypatch, telemetry):
    _, capture = telemetry
    members = [_human_member(), _agent_member(listen_mode="observe")]
    _wire_human_message(monkeypatch, members, _message(4, {"kind": "human", "id": USER, "displayName": "Owner"}))

    await _send_human(text="just thinking out loud", mentions=[])

    assert _events(capture, "agent_dispatch_missed") == []
    assert _events(capture, "room_message_sent")[0]["properties"]["mentions_agents"] == 0


# ---------------------------------------------------------------------------
# mention resolution
# ---------------------------------------------------------------------------


def test_unresolved_mentions_split_no_match_from_ambiguous():
    members = [
        _agent_member(AGENT, handle="dup"),
        _agent_member(OTHER_AGENT, handle="dup"),
        _agent_member(str(uuid.UUID(int=3)), handle="friday"),
    ]

    mentions, reasons = messaging.parse_agent_mentions("@friday @dup @ghost", members)

    assert mentions == [{"kind": "agent", "id": str(uuid.UUID(int=3))}]
    assert sorted(reasons) == ["ambiguous", "no_match"]


def test_a_fully_resolved_mention_reports_nothing():
    assert messaging.parse_agent_mentions("@friday ship it", [_agent_member()]) == (
        [{"kind": "agent", "id": AGENT}],
        [],
    )


def test_handles_inside_code_are_not_counted_as_unresolved():
    # A pasted decorator or traceback is not someone failing to address a
    # teammate; an ordinary email address already never matched.
    text = "see `@pytest.mark.asyncio` and\n```\n@ghost_helper()\n```\nmail ops@example.com"
    assert messaging.parse_agent_mentions(text, [_agent_member()]) == ([], [])
    # …but the same handle in prose still counts.
    assert messaging.parse_agent_mentions("@ghost are you there", [_agent_member()]) == ([], ["no_match"])


# ---------------------------------------------------------------------------
# agent replies
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reply_latency_prefers_the_explicit_reply_link(monkeypatch, telemetry):
    put_metric, capture = telemetry
    asked_at = messaging.utc_now() - timedelta(seconds=4)
    answered = _message(
        3,
        {"kind": "human", "id": USER, "displayName": "Owner"},
        targets=[{"agentId": AGENT, "eventKind": "user_request"}],
        created_at=asked_at.isoformat(),
    )
    created = _message(4, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    created["replyToMessageId"] = answered["messageId"]
    scan = _wire_agent_message(
        monkeypatch, [_human_member(), _agent_member()], created, links={answered["messageId"]: answered}
    )

    await _send_agent(reply_to=answered["messageId"])

    scan.assert_not_awaited()  # the link already answers it
    latency = _metrics(put_metric, "messaging.reply.latency")
    assert len(latency) == 1 and latency[0]["unit"] == "Milliseconds"
    assert latency[0]["dimensions"] == {"room_kind": "channel", "event_kind": "user_request", "matched": "reply_to"}
    assert 3_500 < latency[0]["value"] < 6_000
    assert _metrics(put_metric, "messaging.reply.unmatched") == []

    delivered = _events(capture, "agent_reply_delivered")
    assert len(delivered) == 1 and delivered[0]["distinct_id"] == USER
    assert delivered[0]["properties"]["event_kind"] == "user_request"
    assert delivered[0]["properties"]["agent_id"] == AGENT
    assert 3_500 < delivered[0]["properties"]["latency_ms"] < 6_000


@pytest.mark.asyncio
async def test_reply_latency_scans_back_a_bounded_window_for_the_answered_message(monkeypatch, telemetry):
    put_metric, capture = telemetry
    asked_at = messaging.utc_now() - timedelta(seconds=2)
    history = [
        # Ignored: dispatched to a different agent.
        _message(
            8,
            {"kind": "human", "id": USER, "displayName": "Owner"},
            targets=[{"agentId": OTHER_AGENT, "eventKind": "user_request"}],
        ),
        _message(
            9,
            {"kind": "human", "id": USER, "displayName": "Owner"},
            targets=[{"agentId": AGENT, "eventKind": "room_event"}],
            created_at=asked_at.isoformat(),
        ),
        # Ignored: another agent's chatter after the question.
        _message(10, {"kind": "agent", "id": OTHER_AGENT, "displayName": "Atlas"}),
    ]
    created = _message(11, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    scan = _wire_agent_message(monkeypatch, [_human_member(), _agent_member()], created, before=history)

    await _send_agent()

    assert scan.await_args.args == (ROOM, 11, 20)
    latency = _metrics(put_metric, "messaging.reply.latency")
    assert latency[0]["dimensions"] == {"room_kind": "channel", "event_kind": "room_event", "matched": "scan"}
    assert _events(capture, "agent_reply_delivered")[0]["distinct_id"] == USER
    # The once-ever trial signal rides the same person as the reply event.
    messaging.schedule_trial_first_agent_response.assert_called_once_with(OWNER, distinct_id=USER)


@pytest.mark.asyncio
async def test_an_unprompted_agent_message_counts_as_unmatched(monkeypatch, telemetry):
    put_metric, capture = telemetry
    # A human message that was never dispatched to this agent answers nothing.
    history = [_message(3, {"kind": "human", "id": USER, "displayName": "Owner"})]
    created = _message(4, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    _wire_agent_message(monkeypatch, [_human_member(), _agent_member()], created, before=history)

    await _send_agent()

    assert _metrics(put_metric, "messaging.reply.latency") == []
    assert _metrics(put_metric, "messaging.reply.unmatched") == [{"value": 1.0, "unit": "Count", "dimensions": None}]
    # No human to attribute it to, and the owner id is a Clerk ORG — keying a
    # person event on it would mint org-shaped people in PostHog.
    assert _events(capture, "agent_reply_delivered") == []


@pytest.mark.asyncio
async def test_an_agent_message_reports_created_dispatch_and_the_trial_gate(monkeypatch, telemetry):
    put_metric, capture = telemetry
    trial = MagicMock(return_value=None)
    monkeypatch.setattr(messaging, "schedule_trial_first_agent_response", trial)
    created = _message(4, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    _wire_agent_message(monkeypatch, [_human_member(), _agent_member(OTHER_AGENT, listen_mode="active")], created)

    await _send_agent(text="@ghost take a look")

    assert _metrics(put_metric, "messaging.message.created")[0]["dimensions"] == {
        "author_kind": "agent",
        "room_kind": "channel",
        "has_targets": "true",
    }
    assert _metrics(put_metric, "messaging.dispatch.targets")[0]["dimensions"] == {
        "count_bucket": "1",
        "room_kind": "channel",
        "source": "agent",
    }
    # No human message was answered: the once-ever trial signal is NOT spent on
    # this post, so the first real reply to a human can still fire it.
    trial.assert_not_called()
    missed = _events(capture, "agent_dispatch_missed")
    assert missed[0]["properties"] == {"workspace_id": OWNER, "reason": "unresolved_mention", "room_kind": "channel"}


@pytest.mark.asyncio
async def test_a_failing_reply_lookup_still_returns_the_receipt(monkeypatch, telemetry):
    put_metric, _ = telemetry
    created = _message(4, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    _wire_agent_message(monkeypatch, [_human_member(), _agent_member()], created)
    monkeypatch.setattr(
        messaging.messaging_repo, "list_messages_before", AsyncMock(side_effect=RuntimeError("dynamo down"))
    )

    receipt = await _send_agent()

    assert str(receipt.message_id) == created["messageId"]
    assert _metrics(put_metric, "messaging.reply.latency") == []


# ---------------------------------------------------------------------------
# agent-message run stamping (the doubles fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_agent_message_carries_a_verified_run_id(monkeypatch, telemetry):
    put_metric, _ = telemetry
    created = _message(4, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    _wire_agent_message(monkeypatch, [_human_member(), _agent_member()], created)
    run_id = str(uuid.UUID(int=7))
    monkeypatch.setattr(messaging.messaging_repo, "get_run", AsyncMock(return_value={"roomId": ROOM, "agentId": AGENT}))

    await _send_agent(run_id=run_id)

    assert messaging.messaging_repo.create_message.call_args.kwargs["run_id"] == run_id
    assert _metrics(put_metric, "messaging.agent_message.run_id") == [
        {"value": 1.0, "unit": "Count", "dimensions": {"result": "stamped", "room_kind": "channel"}}
    ]


@pytest.mark.asyncio
async def test_an_agent_message_without_a_run_id_stays_unstamped(monkeypatch, telemetry):
    put_metric, _ = telemetry
    created = _message(4, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    _wire_agent_message(monkeypatch, [_human_member(), _agent_member()], created)

    await _send_agent()

    assert messaging.messaging_repo.create_message.call_args.kwargs["run_id"] is None
    assert _metrics(put_metric, "messaging.agent_message.run_id") == [
        {"value": 1.0, "unit": "Count", "dimensions": {"result": "unset", "room_kind": "channel"}}
    ]


# ---------------------------------------------------------------------------
# idempotent replays
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_replayed_human_message_reports_nothing(monkeypatch, telemetry):
    put_metric, capture = telemetry
    created = _message(1, {"kind": "human", "id": USER, "displayName": "Owner"})
    _wire_human_message(monkeypatch, [_human_member(), _agent_member(listen_mode="observe")], created, replayed=True)

    message = await _send_human()

    assert str(message.message_id) == created["messageId"]  # the receipt still replays
    assert put_metric.call_args_list == []
    assert capture.call_args_list == []


@pytest.mark.asyncio
async def test_a_replayed_agent_message_reports_nothing(monkeypatch, telemetry):
    put_metric, capture = telemetry
    # Old enough that re-measuring would report the retry delay as reply latency.
    answered = _message(
        3,
        {"kind": "human", "id": USER, "displayName": "Owner"},
        targets=[{"agentId": AGENT, "eventKind": "user_request"}],
        created_at=(messaging.utc_now() - timedelta(hours=2)).isoformat(),
    )
    created = _message(4, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    scan = _wire_agent_message(
        monkeypatch, [_human_member(), _agent_member()], created, before=[answered], replayed=True
    )

    receipt = await _send_agent(text="@ghost done")

    assert str(receipt.message_id) == created["messageId"]
    scan.assert_not_awaited()
    assert put_metric.call_args_list == []
    assert capture.call_args_list == []


# ---------------------------------------------------------------------------
# rooms, members, reactions — and the blast radius of an analytics outage
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_creating_a_room_reports_the_room_and_each_member(monkeypatch, telemetry):
    _, capture = telemetry
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}]))
    monkeypatch.setattr(messaging, "_resolved_member", AsyncMock(side_effect=[_agent_member(), _human_member("u2")]))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock())
    monkeypatch.setattr(messaging.messaging_repo, "create_room", AsyncMock(return_value={"kind": "group-dm"}))
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock())

    await messaging.create_room(
        AuthContext(user_id=USER, org_id=OWNER),
        messaging.CreateRoomRequest(
            clientRoomId=uuid.uuid4(),
            name="planning",
            kind="group-dm",
            members=[
                messaging.CreateRoomMember(kind="agent", id=AGENT, role="member", listenMode="mentions"),
                messaging.CreateRoomMember(kind="human", id="u2", role="member", listenMode=None),
            ],
        ),
    )

    created = _events(capture, "room_created")
    assert len(created) == 1 and created[0]["distinct_id"] == USER
    assert created[0]["properties"] == {"workspace_id": OWNER, "kind": "group-dm"}
    # The creator is not "added"; the two invitees are, by the room owner.
    assert [event["properties"] for event in _events(capture, "room_member_added")] == [
        {"workspace_id": OWNER, "member_kind": "agent", "by": "owner"},
        {"workspace_id": OWNER, "member_kind": "human", "by": "owner"},
    ]


@pytest.mark.asyncio
async def test_the_seeded_starter_room_reports_system_membership(monkeypatch, telemetry):
    _, capture = telemetry
    starter_ids = {key: str(uuid.UUID(int=index + 20)) for index, key in enumerate(messaging._STARTER_KEYS)}
    company = MagicMock(status="active", starter_agent_ids=starter_ids)
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=company)
    monkeypatch.setattr("core.repositories.workspace_repo.WorkspaceRepo", MagicMock(return_value=repo))
    monkeypatch.setattr(messaging.messaging_repo, "get_room", AsyncMock(return_value=None))
    monkeypatch.setattr(
        messaging,
        "load_agent_roster",
        AsyncMock(return_value=[{"id": agent_id, "name": key} for key, agent_id in starter_ids.items()]),
    )
    monkeypatch.setattr(messaging, "human_display_name", AsyncMock(return_value="Owner"))
    monkeypatch.setattr(messaging.messaging_repo, "ensure_user_stream", AsyncMock())
    monkeypatch.setattr(messaging.messaging_repo, "create_room", AsyncMock(return_value={"kind": "channel"}))
    monkeypatch.setattr(messaging.messaging_repo, "create_message", AsyncMock(return_value=({}, False)))

    assert await messaging.ensure_default_room(OWNER, USER) is not None

    assert [event["properties"] for event in _events(capture, "room_created")] == [
        {"workspace_id": OWNER, "kind": "channel"}
    ]
    added = _events(capture, "room_member_added")
    assert len(added) == len(messaging._STARTER_KEYS)  # the owner is not "added"
    assert {(event["properties"]["member_kind"], event["properties"]["by"]) for event in added} == {("agent", "system")}


@pytest.mark.asyncio
async def test_adding_a_member_reports_the_adders_role(monkeypatch, telemetry):
    _, capture = telemetry
    monkeypatch.setattr(
        messaging,
        "require_room_member",
        AsyncMock(return_value=({"kind": "channel", "ownerId": OWNER}, _human_member(role="admin"))),
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value=None))
    monkeypatch.setattr(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": AGENT, "name": "Friday"}]))
    monkeypatch.setattr(messaging, "_resolved_member", AsyncMock(return_value=_agent_member()))
    monkeypatch.setattr(messaging.messaging_repo, "list_members", AsyncMock(return_value=[]))
    monkeypatch.setattr(messaging.messaging_repo, "upsert_member", AsyncMock(return_value={"kind": "channel"}))
    monkeypatch.setattr(messaging, "_publish_room_metadata", AsyncMock())
    monkeypatch.setattr(messaging, "_room_detail", AsyncMock())

    await messaging.upsert_member(
        AuthContext(user_id=USER, org_id=OWNER),
        ROOM,
        messaging.UpsertRoomMemberRequest(kind="agent", id=AGENT, role="member", listenMode="mentions"),
    )

    added = _events(capture, "room_member_added")
    assert added[0]["properties"] == {"workspace_id": OWNER, "member_kind": "agent", "by": "member"}


@pytest.mark.asyncio
async def test_a_reaction_is_reported_only_when_added(monkeypatch, telemetry):
    _, capture = telemetry
    item = _message(3, {"kind": "human", "id": USER, "displayName": "Owner"})
    monkeypatch.setattr(
        messaging, "require_room_member", AsyncMock(return_value=({"kind": "dm", "ownerId": OWNER}, _human_member()))
    )
    monkeypatch.setattr(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=item))
    monkeypatch.setattr(messaging.messaging_repo, "set_reaction", AsyncMock(return_value=item))
    publisher = MagicMock()
    publisher.publish_room = AsyncMock()
    monkeypatch.setattr("core.services.live_events.get_live_event_publisher", lambda: publisher)

    for active in (True, False):
        await messaging.set_reaction(
            AuthContext(user_id=USER, org_id=OWNER),
            ROOM,
            item["messageId"],
            messaging.SetReactionRequest(emoji="👍", active=active),
        )

    assert [event["properties"] for event in _events(capture, "reaction_added")] == [
        {"workspace_id": OWNER, "room_kind": "dm"}
    ]


@pytest.mark.asyncio
async def test_a_posthog_outage_never_fails_a_room_write(monkeypatch, telemetry):
    _, capture = telemetry
    capture.side_effect = RuntimeError("posthog down")
    created = _message(1, {"kind": "human", "id": USER, "displayName": "Owner"})
    _wire_human_message(monkeypatch, [_human_member(), _agent_member(listen_mode="observe")], created)

    message = await _send_human()
    assert str(message.message_id) == created["messageId"]

    agent_created = _message(2, {"kind": "agent", "id": AGENT, "displayName": "Friday"})
    _wire_agent_message(monkeypatch, [_human_member(), _agent_member()], agent_created)
    receipt = await _send_agent()
    assert str(receipt.message_id) == agent_created["messageId"]
    assert capture.call_count > 0  # it really was tried
