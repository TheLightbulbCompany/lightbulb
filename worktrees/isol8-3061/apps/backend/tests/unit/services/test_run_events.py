import uuid
from unittest.mock import AsyncMock, patch

import pytest

from core.services import messaging
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


def _event(**kw):
    return RunEventRequest(run_id=RUN, at="2026-09-12T03:15:00Z", **kw)


@pytest.fixture
def metrics(monkeypatch):
    seen = []
    monkeypatch.setattr(messaging, "put_metric", lambda name, **kw: seen.append((name, kw.get("dimensions"))))
    return seen


@pytest.mark.asyncio
async def test_started_and_heartbeat_map_to_conditional_transitions(metrics):
    with patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=True)) as start:
        assert (await messaging.handle_run_event("o", _event(type="run.started", session_key="k"))).status == "applied"
    start.assert_awaited_once_with("o", RUN, session_key="k")
    with (
        patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=False)),
        patch.object(messaging.messaging_repo, "get_run", AsyncMock(return_value=ROW)),
    ):
        assert (await messaging.handle_run_event("o", _event(type="run.heartbeat"))).status == "replayed"
    with (
        patch.object(messaging.messaging_repo, "heartbeat_run", AsyncMock(return_value=False)),
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
        patch.object(messaging.messaging_repo, "start_run", AsyncMock(return_value=False)),
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
        assert push.call_args.args[0] == "user_bob" and "Friday" in push.call_args.args[1]
    assert ("messaging.run.ended", {"outcome": "completed", "event_kind": "user_request"}) in metrics
    assert any(name == "messaging.run.duration" for name, _ in metrics)


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


@pytest.fixture(autouse=True)
def _not_capped():
    with patch("core.services.trial_cap.trial_block_reason", AsyncMock(return_value=None)) as reason:
        yield reason


@pytest.fixture(autouse=True)
def _not_credit_blocked():
    """Default every test to NOT blocked on Bedrock/OpenRouter credits (Layer
    3's shared predicate), the same way ``_not_capped`` defaults Layer 2's."""
    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            AsyncMock(return_value={"provider_choice": "openrouter_managed"}),
        ),
        patch("core.services.metronome_gate.is_owner_blocked", AsyncMock(return_value=False)) as blocked,
    ):
        yield blocked


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
            "I couldn't finish that (error: agent_stopped_responding). The agent stopped responding. "
            "Try again, or check Settings → Model & Credits if this keeps happening.",
        ),
        (
            "error",
            "https://x.example/key=sk-live-abc",
            USAGE,
            "I couldn't finish that (error). Agent run failed. "
            "Try again, or check Settings → Model & Credits if this keeps happening.",
        ),
        (
            "error",
            None,
            USAGE,
            "I couldn't finish that (error). Agent run failed. "
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
@pytest.mark.parametrize("reason", ["trial_daily_cap", "included_daily_cap", "trial_expired"])
async def test_failed_run_of_a_blocked_owner_says_why(metrics, _not_capped, reason):
    from core.config import settings
    from core.services.chat_gate import BUDGET_BLOCK_MESSAGES

    _not_capped.return_value = reason
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create)
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o", _event(type="run.ended", outcome="error", error_kind="model_unavailable", usage=USAGE)
            )
    path, label = messaging.BUDGET_BLOCK_LINKS[reason]
    expected = f"{BUDGET_BLOCK_MESSAGES[reason]} [{label}]({settings.FRONTEND_URL.rstrip('/')}{path})"
    assert create.await_args.kwargs["text"] == expected


@pytest.mark.asyncio
async def test_a_capped_owners_infra_failure_keeps_its_own_reason(metrics, _not_capped):
    _not_capped.return_value = "trial_daily_cap"
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create)
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o", _event(type="run.ended", outcome="error", error_kind="agent_did_not_start", usage=USAGE)
            )
    assert "The agent did not start this run." in create.await_args.kwargs["text"]


@pytest.mark.asyncio
async def test_failed_run_of_a_credit_blocked_owner_says_so(metrics, _not_credit_blocked):
    """A paid owner out of Bedrock/OpenRouter credits gets the out_of_credits
    note instead of the generic model_unavailable text — same gate as
    gate_chat/provision_gate Layer 3 (metronome_gate.is_owner_blocked)."""
    from core.config import settings
    from core.services.chat_gate import BUDGET_BLOCK_MESSAGES

    _not_credit_blocked.return_value = True
    create = AsyncMock(return_value=({"messageId": "x"}, False))
    patches = _failure_patches(create)
    with patch.object(messaging.messaging_repo, "end_run", AsyncMock(return_value={**ROW, "outcome": "error"})):
        with patches[0], patches[1], patches[2], patches[3], patches[4], patches[5]:
            await messaging.handle_run_event(
                "o", _event(type="run.ended", outcome="error", error_kind="model_unavailable", usage=USAGE)
            )
    path, label = messaging.BUDGET_BLOCK_LINKS["out_of_credits"]
    expected = f"{BUDGET_BLOCK_MESSAGES['out_of_credits']} [{label}]({settings.FRONTEND_URL.rstrip('/')}{path})"
    assert create.await_args.kwargs["text"] == expected
