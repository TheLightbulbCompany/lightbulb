import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest

from core.services import chat_gate, messaging
from core.services.messaging import ApiProblem, RunEventRequest, RunUsage

RUN = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
ROOM = "22222222-2222-4222-8222-222222222222"
AGENT_UUID = uuid.UUID(int=1)
USAGE = RunUsage(
    model="claude-sonnet-4-6",
    input_tokens=1200,
    output_tokens=300,
    cache_read_tokens=5000,
    cache_write_tokens=100,
    estimated_cost_usd=0.0123,
)
ROW = {
    "runId": RUN,
    "roomId": ROOM,
    "agentId": "a1",
    "triggerMessageId": "m1",
    "eventKind": "user_request",
    "dispatchedAt": "2026-09-12T03:15:00+00:00",
    "startedAt": "2026-09-12T03:15:01+00:00",
    "endedAt": "2026-09-12T03:15:09+00:00",
    "outcome": "completed",
    "status": "ended",
}


STARTED_ROW = {**ROW, "status": "started", "endedAt": None, "outcome": None}


def _event(**kw):
    return RunEventRequest(run_id=RUN, at="2026-09-12T03:15:00Z", **kw)


# The real announce_run, captured before the autouse fixture below replaces it.
_REAL_ANNOUNCE_RUN = messaging.announce_run


@pytest.fixture(autouse=True)
def _no_activity_hint(monkeypatch):
    """Keep run callbacks off AppSync/DynamoDB; tests asserting on the hint re-patch."""
    monkeypatch.setattr(messaging, "announce_run", AsyncMock())


@pytest.fixture
def metrics(monkeypatch):
    seen = []
    monkeypatch.setattr(messaging, "put_metric", lambda name, **kw: seen.append((name, kw.get("dimensions"))))
    return seen


@pytest.mark.asyncio
async def test_started_and_heartbeat_map_to_conditional_transitions(metrics):
    with patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=STARTED_ROW)) as start:
        assert (await messaging.handle_run_event("o", _event(type="run.started", session_key="k"))).status == "applied"
    start.assert_awaited_once_with("o", RUN, session_key="k")
    with (
        patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=None)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=ROW)),
    ):
        assert (await messaging.handle_run_event("o", _event(type="run.heartbeat"))).status == "replayed"
    with (
        patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=None)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=None)),
    ):
        with pytest.raises(ApiProblem) as exc:
            await messaging.handle_run_event("o", _event(type="run.heartbeat"))
    assert exc.value.error.code == "RUN_NOT_FOUND"


@pytest.mark.asyncio
async def test_a_late_started_for_an_already_ended_run_replays_not_5xx(metrics):
    """A container retry of run.started arriving after run.ended already closed the row
    must answer 200 replayed, never a 5xx or RUN_NOT_FOUND."""
    with (
        patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=None)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=ROW)),
    ):
        result = await messaging.handle_run_event("o", _event(type="run.started", session_key="k"))
    assert result.status == "replayed"


@pytest.mark.asyncio
async def test_ended_records_usage_and_pushes_only_on_error(metrics):
    record = AsyncMock()
    push = AsyncMock()
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=ROW)) as end,
        patch.object(
            messaging.messaging_repo,
            "get_message_by_id",
            AsyncMock(return_value={"author": {"kind": "human", "id": "user_bob"}}),
        ),
        patch.object(messaging, "record_usage", record),
        patch.object(messaging, "send_push_to_user", push),
        patch.object(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "name": "Friday"}])),
        patch.object(
            messaging.messaging_repo, "get_room", AsyncMock(return_value={"kind": "channel", "name": "GTM Strategy"})
        ),
    ):
        result = await messaging.handle_run_event("o", _event(type="run.ended", outcome="completed", usage=USAGE))
        assert result.status == "applied"
        assert end.await_args.kwargs["usage"]["inputTokens"] == 1200
        record.assert_awaited_once_with(
            owner_id="o",
            user_id="user_bob",
            model="claude-sonnet-4-6",
            input_tokens=1200,
            output_tokens=300,
            cache_read=5000,
            cache_write=100,
            room_turn=True,
            event_kind="user_request",
        )
        push.assert_not_awaited()
        await messaging.handle_run_event(
            "o", _event(type="run.ended", outcome="error", error_kind="timeout", usage=USAGE)
        )
        push.assert_awaited_once()
        # Two lines: who · where, then why (the ledger row here carries no errorKind).
        assert push.call_args.args[:3] == (
            "user_bob",
            "Friday · #GTM Strategy",
            "Couldn't finish: the run hit an error",
        )
    assert ("messaging.run.ended", {"outcome": "completed", "event_kind": "user_request"}) in metrics
    assert any(name == "messaging.run.duration" for name, _ in metrics)


@pytest.mark.parametrize(
    ("room", "trigger_root"),
    [
        ({"kind": "dm"}, None),
        ({"kind": "group-dm", "name": "Ops"}, None),
        ({"kind": "channel", "name": "GTM"}, "root-1"),
    ],
)
@pytest.mark.asyncio
async def test_a_failure_the_note_already_announces_is_not_pushed_twice(metrics, room, trigger_root):
    """The failure note lands in the DM / group chat / trigger's thread, where the projector
    pushes it to the trigger's author; a second "Couldn't finish" push would double it."""
    push = AsyncMock()
    trigger = {"author": {"kind": "human", "id": "user_bob"}, "threadRootMessageId": trigger_root}
    with (
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=trigger)),
        patch.object(messaging, "send_push_to_user", push),
        patch.object(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "name": "Friday"}])),
        patch.object(messaging.messaging_repo, "get_room", AsyncMock(return_value=room)),
    ):
        await messaging._after_run_ended("o", ROW, "error", None)
    push.assert_not_awaited()


@pytest.mark.asyncio
async def test_zero_usage_is_not_recorded(metrics):
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=ROW)),
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None)),
        patch.object(messaging, "record_usage", AsyncMock()) as record,
    ):
        empty = RunUsage(
            model="unknown",
            input_tokens=0,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
            estimated_cost_usd=None,
        )
        await messaging.handle_run_event("o", _event(type="run.ended", outcome="completed", usage=empty))
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_agent_message_with_a_run_id_must_match_the_ledger_row(metrics):
    from core.services.messaging import AgentMessageRequest, create_agent_message

    request = AgentMessageRequest(
        event_id="f0f0f0f0-f0f0-4f0f-8f0f-f0f0f0f0f0f0",
        room_id=ROOM,
        agent_id=AGENT_UUID,
        text="hi",
        reply_to_message_id=None,
        thread_root_message_id=None,
        run_id=RUN,
    )
    with (
        patch.object(messaging.messaging_repo, "get_room", AsyncMock(return_value={"ownerId": "o", "kind": "channel"})),
        patch.object(messaging.messaging_repo, "get_member", AsyncMock(return_value={"id": AGENT_UUID})),
        patch.object(
            messaging, "load_agent_roster", AsyncMock(return_value=[{"id": str(AGENT_UUID), "name": "Friday"}])
        ),
        patch.object(messaging.messaging_repo, "list_members", AsyncMock(return_value=[])),
        patch.object(
            messaging.messaging_repo,
            "get_run",
            AsyncMock(
                return_value={
                    "roomId": "33333333-3333-4333-8333-333333333333",
                    "agentId": str(AGENT_UUID),
                    "status": "ended",
                }
            ),
        ),
    ):
        with pytest.raises(ApiProblem) as exc:
            await create_agent_message("o", request)
    assert exc.value.error.code == "RUN_NOT_FOUND"


# ---------------------------------------------------------------------------
# run.ended with outcome error on a row that never reached started: the container
# published nothing (never started, or the sweeper closed it), so the callback
# itself must publish the terminal error + run_ended frame pair.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ended_error_without_started_publishes_terminal_frames(metrics):
    row = {**ROW, "outcome": "error"}
    row.pop("startedAt")
    publisher = AsyncMock()
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=row)),
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None)),
        patch.object(messaging, "record_usage", AsyncMock()),
        patch.object(messaging, "send_push_to_user", AsyncMock()),
        patch.object(messaging, "get_live_event_publisher", lambda: publisher),
    ):
        await messaging.handle_run_event(
            "o", _event(type="run.ended", outcome="error", error_kind="agent_did_not_start", usage=USAGE)
        )
    assert publisher.publish_room.await_count == 2
    frame_types = [call.args[1]["payload"]["frame"]["type"] for call in publisher.publish_room.await_args_list]
    assert frame_types == ["error", "run_ended"]


@pytest.mark.asyncio
async def test_ended_error_with_started_publishes_nothing(metrics):
    row = {**ROW, "outcome": "error"}
    publisher = AsyncMock()
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=row)),
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None)),
        patch.object(messaging, "record_usage", AsyncMock()),
        patch.object(messaging, "send_push_to_user", AsyncMock()),
        patch.object(messaging, "get_live_event_publisher", lambda: publisher),
    ):
        await messaging.handle_run_event(
            "o", _event(type="run.ended", outcome="error", error_kind="agent_did_not_start", usage=USAGE)
        )
    publisher.publish_room.assert_not_awaited()


@pytest.mark.asyncio
async def test_post_processing_failure_never_5xxes_an_applied_ended_callback(metrics):
    """A transient failure looking up the trigger message (get_message_by_id, called by
    _after_run_ended before either of its own guarded blocks) must not propagate out of
    handle_run_event: the ledger row is already closed, so the callback must still report
    applied, and the error+run_ended frames (published before post-processing runs) must
    still go out for a never-started error row."""
    row = {**ROW, "outcome": "error"}
    row.pop("startedAt")
    publisher = AsyncMock()
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=row)),
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(side_effect=RuntimeError("ddb down"))),
        patch.object(messaging, "get_live_event_publisher", lambda: publisher),
    ):
        result = await messaging.handle_run_event(
            "o", _event(type="run.ended", outcome="error", error_kind="agent_did_not_start", usage=USAGE)
        )
    assert result.status == "applied"
    assert publisher.publish_room.await_count == 2
    frame_types = [call.args[1]["payload"]["frame"]["type"] for call in publisher.publish_room.await_args_list]
    assert frame_types == ["error", "run_ended"]


# ---------------------------------------------------------------------------
# A failed run leaves one durable agent-authored message in its room (frames are
# ephemeral; a user who was not watching must still see what happened).
# ---------------------------------------------------------------------------


# The real helper, captured before the autouse fixture below replaces it.
_real_budget_block_reason = chat_gate.budget_block_reason


@pytest.fixture(autouse=True)
def _not_blocked():
    """Default every test to an owner with budget; the helper itself is tested in
    test_budget_block_reason below."""
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value={})),
        patch("core.services.chat_gate.budget_block_reason", AsyncMock(return_value=None)) as reason,
    ):
        yield reason


def _failure_patches(create, trigger=None):
    return (
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=trigger)),
        patch.object(messaging.messaging_repo, "create_message", create),
        patch.object(messaging, "load_agent_roster", AsyncMock(return_value=[{"id": "a1", "name": "Friday"}])),
        patch.object(messaging, "record_usage", AsyncMock()),
        patch.object(messaging, "send_push_to_user", AsyncMock()),
        patch.object(messaging, "get_live_event_publisher", lambda: AsyncMock()),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,error_kind,usage,expected",
    [
        (
            "error",
            "agent_stopped_responding",
            USAGE,
            "Friday couldn't reply. The agent stopped responding. "
            "Try again, or check Settings → Model & Credits if this keeps happening.",
        ),
        (
            "error",
            "https://x.example/key=sk-live-abc",
            USAGE,
            "Friday couldn't reply. The run failed. "
            "Try again, or check Settings → Model & Credits if this keeps happening.",
        ),
        (
            "error",
            None,
            USAGE,
            "Friday couldn't reply. The run failed. "
            "Try again, or check Settings → Model & Credits if this keeps happening.",
        ),
    ],
)
async def test_failed_run_posts_one_silent_agent_message(metrics, outcome, error_kind, usage, expected):
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create, trigger={"author": {"kind": "human", "id": "user_bob"}})
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": outcome})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o", _event(type="run.ended", outcome=outcome, error_kind=error_kind, usage=usage)
            )
    create.assert_awaited_once()
    kw = create.await_args.kwargs
    assert kw["text"] == expected
    assert kw["author"] == {"kind": "agent", "id": "a1", "displayName": "Friday"}
    assert kw["mentions"] == [] and kw["dispatch_targets"] == []
    assert kw["room_id"] == ROOM and kw["run_id"] == RUN and kw["owner_id"] == "o"
    assert kw["thread_root_message_id"] is None
    # Keyed on the run, so a retried run.ended or a sweeper race replays instead of doubling.
    assert kw["idemp_sk"] == messaging.messaging_repo.agent_idemp_sk(f"run-failed:{RUN}")


@pytest.mark.asyncio
async def test_ingress_refusal_note_is_not_an_agent_read_receipt(metrics):
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create)
    with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
        await messaging.post_run_failure_message("o", ROW, "error", messaging.INGRESS_REFUSED)
    assert create.await_args.kwargs["author"]["kind"] == "system"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account,provider",
    [
        ({"provider_choice": "chatgpt_oauth"}, "ChatGPT"),
        ({"provider_choice": "byo_key", "byo_provider": "anthropic"}, "Anthropic"),
        ({"provider_choice": "byo_key"}, "Your AI provider"),
        ({"provider_choice": "openrouter_managed"}, "The AI model"),
    ],
)
async def test_provider_failure_names_the_provider_and_no_code(metrics, account, provider):
    """The 2026-09-25 Codex outage: the owner saw 'Run didn't finish (error: model_unavailable)'
    and read it as silence. A provider failure is a plain system notice naming the provider."""
    from core.constants import SYSTEM_ACTOR_ID

    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create)
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", AsyncMock(return_value=account)),
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})),
    ):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o", _event(type="run.ended", outcome="error", error_kind="model_unavailable", usage=USAGE)
            )
    kw = create.await_args.kwargs
    assert kw["text"] == f"Friday couldn't reply: {provider} is having problems right now. Resend in a bit."
    assert kw["author"] == {"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"}
    assert kw["dispatch_targets"] == []  # never auto-retried


@pytest.mark.asyncio
@pytest.mark.parametrize("steered", [False, True])
async def test_a_silent_completion_posts_nothing_records_usage_and_counts(metrics, steered):
    create, record = AsyncMock(), AsyncMock()
    patches = _failure_patches(create)
    row = {**ROW, "eventKind": "room_event", **({"steeredMessageIds": {"m2"}} if steered else {})}
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=row)):
        with (
            patches[0],
            patches[1],
            patches[2],
            patch.object(messaging, "record_usage", record),
            patches[4],
            patches[5],
        ):
            await messaging.handle_run_event(
                "o",
                RunEventRequest.model_validate(
                    {
                        "runId": RUN,
                        "type": "run.ended",
                        "at": "2026-09-12T03:15:00Z",
                        "outcome": "completed",
                        "silent": True,
                        "usage": USAGE.model_dump(by_alias=True),
                    }
                ),
            )
    create.assert_not_awaited()
    record.assert_awaited_once()
    assert ("messaging.agent.silent", {"event_kind": "room_event"}) in metrics
    # Routing v2 §4.1: the run stood down after a human line was steered into it.
    assert (("messaging.run.silent_after_steer", {"event_kind": "room_event"}) in metrics) is steered


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome,usage", [("completed", USAGE), ("aborted", USAGE), ("ignored", None), ("gate_error", None)]
)
async def test_non_failure_outcomes_post_nothing(metrics, outcome, usage):
    create = AsyncMock()
    patches = _failure_patches(create)
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": outcome})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event("o", _event(type="run.ended", outcome=outcome, usage=usage))
    create.assert_not_awaited()


@pytest.mark.asyncio
async def test_failure_message_lands_in_the_triggers_thread(metrics):
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create, trigger={"author": {"kind": "human", "id": "u"}, "threadRootMessageId": "root"})
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event("o", _event(type="run.ended", outcome="error", usage=USAGE))
    assert create.await_args.kwargs["thread_root_message_id"] == "root"


@pytest.mark.asyncio
async def test_failure_message_write_error_never_5xxes_the_callback(metrics):
    patches = _failure_patches(AsyncMock(side_effect=RuntimeError("ddb down")))
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            result = await messaging.handle_run_event("o", _event(type="run.ended", outcome="error", usage=USAGE))
    assert result.status == "applied"


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["trial_daily_cap", "included_daily_cap", "trial_expired", "out_of_credits"])
async def test_failed_run_of_a_blocked_owner_is_a_system_notice(metrics, _not_blocked, reason):
    """Out of budget is the platform talking, not the agent: a system-authored
    notice with the reason and no purchase link (the iPhone app renders room
    notes as-is), keyed on the run like the agent note."""
    from core.constants import SYSTEM_ACTOR_ID
    from core.services.chat_gate import BUDGET_BLOCK_MESSAGES

    _not_blocked.return_value = reason
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create)
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o", _event(type="run.ended", outcome="error", error_kind="model_unavailable", usage=USAGE)
            )
    kw = create.await_args.kwargs
    assert kw["text"] == BUDGET_BLOCK_MESSAGES[reason]
    assert "](" not in kw["text"]
    assert kw["author"] == {"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"}
    assert kw["idemp_sk"] == messaging.messaging_repo.agent_idemp_sk(f"run-failed:{RUN}")


@pytest.mark.asyncio
async def test_a_capped_owners_infra_failure_keeps_its_own_reason(metrics, _not_blocked):
    _not_blocked.return_value = "trial_daily_cap"
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create)
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o", _event(type="run.ended", outcome="error", error_kind="agent_did_not_start", usage=USAGE)
            )
    assert "The agent did not start this run." in create.await_args.kwargs["text"]
    assert create.await_args.kwargs["author"]["kind"] == "agent"


# The real ChatGPT OAuth 429 body the bridge classifies as provider_quota_exhausted.
CHATGPT_429 = {
    "error": {
        "type": "usage_limit_reached",
        "plan_type": "prolite",
        "resets_at": 1790565911,
        "resets_in_seconds": 311059,
    }
}  # verbatim prod body; resets 2026-09-28 03:25:11 UTC


@pytest.mark.parametrize(
    "raw,expected",
    [
        (
            {"provider": "chatgpt", "resets_at": CHATGPT_429["error"]["resets_at"]},
            {"provider": "chatgpt", "resets_at": 1790565911},
        ),
        ({"provider": "ChatGPT", "resetsAt": "2026-09-28T03:25:00Z"}, {"provider": "chatgpt", "resets_at": 1790565900}),
        ({"provider": "openai", "resets_at": "1790565900"}, {"provider": "openai", "resets_at": 1790565900}),
        ({"provider": "https://evil.example", "resets_at": "tomorrow", "x": 1}, None),
        ({"resets_at": True}, None),
        ({"resets_at": float("inf")}, None),
        ({"resets_at": 1790565900000}, None),  # milliseconds: implausible, dropped
        ("chatgpt", None),
        (None, None),
    ],
)
def test_error_detail_is_normalized_never_rejected(raw, expected):
    event = _event(
        type="run.ended", outcome="error", error_kind="provider_quota_exhausted", usage=USAGE, error_detail=raw
    )
    assert event.error_detail == expected


def test_camel_case_error_detail_is_accepted():
    event = RunEventRequest.model_validate(
        {
            "runId": RUN,
            "type": "run.ended",
            "at": "2026-09-12T03:15:00Z",
            "outcome": "error",
            "errorKind": "provider_quota_exhausted",
            "errorDetail": {"provider": "chatgpt", "resets_at": CHATGPT_429["error"]["resets_at"]},
            "usage": USAGE.model_dump(by_alias=True),
        }
    )
    assert event.error_detail == {"provider": "chatgpt", "resets_at": 1790565911}


@pytest.mark.parametrize(
    "detail,expected",
    [
        (
            {"provider": "chatgpt", "resets_at": 1790565900},
            "Your ChatGPT plan has hit its usage limit — it resets Sep 28, 03:25 UTC. Upgrade your ChatGPT plan, "
            "or switch this workspace's model in Settings → Model & Credits.",
        ),
        (
            {"provider": "somecorp"},
            "Your AI provider has hit its usage limit — it resets soon. Upgrade your plan with them, "
            "or switch this workspace's model in Settings → Model & Credits.",
        ),
    ],
)
def test_provider_quota_message(detail, expected):
    assert messaging.provider_quota_message(detail) == expected


@pytest.mark.asyncio
async def test_provider_quota_failure_is_a_system_notice_and_stamps_billing(metrics, _not_blocked):
    from core.config import settings
    from core.constants import SYSTEM_ACTOR_ID

    create = AsyncMock(return_value=({"messageId": "x"}, False))
    stamp = AsyncMock()
    patches = _failure_patches(create)
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})),
        patch.object(messaging.billing_repo, "set_provider_quota_until", stamp),
    ):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o",
                _event(
                    type="run.ended",
                    outcome="error",
                    error_kind="provider_quota_exhausted",
                    error_detail={"provider": "chatgpt", "resets_at": CHATGPT_429["error"]["resets_at"]},
                    usage=USAGE,
                ),
            )
    kw = create.await_args.kwargs
    assert kw["text"].startswith("Your ChatGPT plan has hit its usage limit — it resets Sep 28, 03:25 UTC.")
    assert kw["text"].endswith(f"[Model & Credits]({settings.FRONTEND_URL.rstrip('/')}/settings?panel=providers)")
    assert "hiccup" not in kw["text"]
    assert kw["author"] == {"kind": "system", "id": SYSTEM_ACTOR_ID, "displayName": "Lightbulb"}
    assert kw["idemp_sk"] == messaging.messaging_repo.agent_idemp_sk(f"run-failed:{RUN}")
    stamp.assert_awaited_once_with("o", 1790565911)
    _not_blocked.assert_not_awaited()  # a quota failure is never re-read as a budget block


@pytest.mark.asyncio
async def test_old_bridge_quota_failure_without_detail_still_reads_well(metrics):
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    stamp = AsyncMock()
    patches = _failure_patches(create)
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})),
        patch.object(messaging.billing_repo, "set_provider_quota_until", stamp),
    ):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o", _event(type="run.ended", outcome="error", error_kind="provider_quota_exhausted", usage=USAGE)
            )
    assert "it resets soon" in create.await_args.kwargs["text"]
    stamp.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account,cap_reason,credits_blocked,expected",
    [
        ({"subscription_status": "trial_local"}, "trial_expired", True, "trial_expired"),
        # A capped owner never falls through to the credit check, even carrying openrouter_managed.
        ({"subscription_status": "trial_local", "provider_choice": "openrouter_managed"}, None, True, None),
        (
            {"subscription_status": "active", "plan": "standard", "provider_choice": "openrouter_managed"},
            None,
            True,
            "out_of_credits",
        ),
        (
            {"subscription_status": "active", "plan": "standard", "provider_choice": "openrouter_managed"},
            None,
            False,
            None,
        ),
        ({"subscription_status": "active", "plan": "standard", "provider_choice": "chatgpt_oauth"}, None, True, None),
        (None, None, True, None),
    ],
)
async def test_budget_block_reason(account, cap_reason, credits_blocked, expected):
    with (
        patch("core.services.trial_cap.reason_for_account", AsyncMock(return_value=cap_reason)),
        patch("core.services.metronome_gate.is_owner_blocked", AsyncMock(return_value=credits_blocked)),
    ):
        assert await _real_budget_block_reason("o", account) == expected


@pytest.mark.asyncio
async def test_a_run_that_errors_re_dispatches_the_follow_ups_steered_into_it(metrics):
    """A steered follow-up the failed run may never have answered gets its own turn; a
    completed run's steers were answered in it."""
    steered = {**ROW, "steeredMessageIds": {"m2"}}
    create = AsyncMock(return_value=True)
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=steered)),
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None)),
        patch.object(messaging.messaging_repo, "create_follow_up", create),
        patch.object(messaging, "post_run_failure_message", AsyncMock()),
    ):
        await messaging.handle_run_event("o", _event(type="run.ended", outcome="completed", usage=USAGE))
        create.assert_not_awaited()
        await messaging.handle_run_event(
            "o", _event(type="run.ended", outcome="error", error_kind="timeout", usage=USAGE)
        )
    create.assert_awaited_once()
    assert create.await_args.kwargs | {"follow_up_id": None} == {
        "owner_id": "o",
        "room_id": ROOM,
        "agent_id": "a1",
        "follow_up_id": None,
        "trigger_message_id": "m2",
        "event_kind": "user_request",
        "reason": messaging.STEER_LOST,
        "text": "",
    }


@pytest.mark.asyncio
async def test_a_lost_room_event_steer_is_re_dispatched_as_a_room_event(metrics):
    """Routing v2 §4.1: a room event steered in before Jev goes back through Jev, not to this
    agent as a forced answer; the addressed steer beside it keeps its kind."""
    steered = {**ROW, "steeredMessageIds": {"m2", "m3"}, "ambientSteerIds": {"m3"}}
    create = AsyncMock(return_value=True)
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=steered)),
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=None)),
        patch.object(messaging.messaging_repo, "create_follow_up", create),
        patch.object(messaging, "post_run_failure_message", AsyncMock()),
    ):
        await messaging.handle_run_event(
            "o", _event(type="run.ended", outcome="error", error_kind="timeout", usage=USAGE)
        )
    kinds = {c.kwargs["trigger_message_id"]: c.kwargs["event_kind"] for c in create.await_args_list}
    assert kinds == {"m2": "user_request", "m3": "room_event"}


@pytest.mark.asyncio
async def test_started_run_announces_working_and_a_replay_does_not(metrics, monkeypatch):
    announce = AsyncMock()
    monkeypatch.setattr(messaging, "announce_run", announce)
    with patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=STARTED_ROW)):
        await messaging.handle_run_event("o", _event(type="run.started", session_key="k"))
    announce.assert_awaited_once_with(STARTED_ROW, "working")
    announce.reset_mock()
    with (
        patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=None)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=ROW)),
    ):
        await messaging.handle_run_event("o", _event(type="run.started", session_key="k"))
    announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_applied_heartbeat_reannounces_working_and_a_replay_does_not(metrics, monkeypatch):
    announce = AsyncMock()
    monkeypatch.setattr(messaging, "announce_run", announce)
    with patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=STARTED_ROW)):
        await messaging.handle_run_event("o", _event(type="run.heartbeat"))
    announce.assert_awaited_once_with(STARTED_ROW, "working")
    announce.reset_mock()
    with (
        patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=None)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=ROW)),
    ):
        assert (await messaging.handle_run_event("o", _event(type="run.heartbeat"))).status == "replayed"
    announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_heartbeat_racing_run_ended_announces_the_row_it_applied_not_a_later_read(metrics, monkeypatch):
    """run.ended can close the row between the heartbeat's update and any re-read; the
    "working" hint must come from the row the heartbeat itself wrote, never an ended one."""
    announce = AsyncMock()
    monkeypatch.setattr(messaging, "announce_run", announce)
    with (
        patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=STARTED_ROW)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=ROW)),  # already ended
    ):
        await messaging.handle_run_event("o", _event(type="run.heartbeat"))
    announce.assert_awaited_once_with(STARTED_ROW, "working")


@pytest.mark.asyncio
async def test_every_close_path_announces_idle(metrics, monkeypatch):
    announce = AsyncMock()
    monkeypatch.setattr(messaging, "announce_run", announce)
    monkeypatch.setattr(messaging, "_after_run_ended", AsyncMock())
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=ROW)):
        await messaging.handle_run_event("o", _event(type="run.ended", outcome="completed", usage=USAGE))
    announce.assert_awaited_with(ROW, "idle")
    announce.reset_mock()
    monkeypatch.setattr(messaging, "get_live_event_publisher", lambda: type("P", (), {"publish_room": AsyncMock()})())
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=ROW)):
        await messaging.close_aborted_run("o", RUN)
    announce.assert_awaited_with(ROW, "idle")
    announce.reset_mock()
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=None)):
        await messaging.close_aborted_run("o", RUN)  # already closed: its idle was announced then
    announce.assert_not_awaited()


@pytest.mark.asyncio
async def test_working_expires_with_the_heartbeat_timeout_idle_does_not_and_neither_raises(monkeypatch):
    from core.services import live_events

    publish = AsyncMock(side_effect=RuntimeError("appsync down"))
    monkeypatch.setattr(live_events, "publish_room_activity", publish)
    monkeypatch.setattr(messaging.messaging_repo, "get_member", AsyncMock(return_value={"displayName": "CEO"}))
    before = datetime.now(timezone.utc)
    await _REAL_ANNOUNCE_RUN(ROW, "working")  # does not raise
    room, actor, state, expires_at = publish.await_args.args
    assert (room, actor, state) == (ROOM, {"kind": "agent", "id": "a1", "displayName": "CEO"}, "working")
    timeout = timedelta(seconds=messaging.messaging_repo.RUN_HEARTBEAT_TIMEOUT_S)
    assert before + timeout <= expires_at <= datetime.now(timezone.utc) + timeout
    publish.reset_mock()
    await _REAL_ANNOUNCE_RUN(ROW, "idle")
    publish.assert_awaited_once_with(ROOM, {"kind": "agent", "id": "a1", "displayName": "CEO"}, "idle", None)


@pytest.mark.asyncio
@pytest.mark.parametrize("closer", ["bridge", "stop"])
async def test_a_room_run_emits_agent_run_completed_on_its_requester(metrics, closer):
    """T01: every close (the bridge's run.ended, a Stop, the sweeper) reaches agent_run_completed,
    keyed on the human whose message started the run."""
    trigger = {"author": {"kind": "human", "id": "user_bob"}}
    aborted = {**ROW, "outcome": "aborted"}
    with (
        patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value=aborted)),
        patch.object(messaging.messaging_repo, "get_message_by_id", AsyncMock(return_value=trigger)),
        patch.object(messaging, "record_usage", AsyncMock()),
        patch.object(messaging, "get_live_event_publisher"),
        patch.object(messaging.run_analytics, "schedule_run_completed") as schedule,
    ):
        if closer == "bridge":
            await messaging.handle_run_event("org_1", _event(type="run.ended", outcome="aborted", usage=USAGE))
        else:
            await messaging.close_aborted_run("org_1", RUN, forced=True)
    schedule.assert_called_once()
    kwargs = schedule.call_args.kwargs
    assert kwargs["owner_id"] == "org_1"
    assert kwargs["person_id"] == "user_bob"
    assert kwargs["parsed"]["source"] == "room"
    assert kwargs["payload"] == {"runId": RUN, "data": {"aborted": True, "durationMs": 8000}}
    assert kwargs["phase"] == "end"
