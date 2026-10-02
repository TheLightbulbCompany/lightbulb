from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import messaging
from core.services.messaging import RunUsage, UsageReportRequest

USAGE = RunUsage(
    model="claude-sonnet-4-6",
    input_tokens=10,
    output_tokens=2,
    cache_read_tokens=0,
    cache_write_tokens=0,
    estimated_cost_usd=0.01,
)
BODY = UsageReportRequest(
    session_key="agent:friday:cron:job-1",
    agent_id="friday",
    run_id="run-1",
    success=True,
    duration_ms=1200,
    usage=USAGE,
)


def _zero_usage() -> RunUsage:
    return RunUsage(
        model="unknown",
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
        estimated_cost_usd=None,
    )


def _patches(claimed=True):
    return (
        patch(
            "core.services.messaging.messaging_repo.claim_usage_receipt", new_callable=AsyncMock, return_value=claimed
        ),
        patch("core.services.messaging.record_usage", new_callable=AsyncMock),
        patch("core.services.messaging.run_analytics.schedule_run_completed", MagicMock(return_value=None)),
    )


@pytest.mark.asyncio
async def test_first_report_records_usage_and_analytics():
    claim, record, analytics = _patches()
    with claim as claim, record as record, analytics as analytics:
        response = await messaging.handle_usage_report("org_1", BODY)
    assert response.status == "applied"
    claim.assert_awaited_once_with("org_1", "run-1")
    record.assert_awaited_once_with(
        owner_id="org_1",
        user_id="org_1",
        model="claude-sonnet-4-6",
        input_tokens=10,
        output_tokens=2,
        cache_read=0,
        cache_write=0,
        room_turn=False,
        event_kind=None,
    )
    kwargs = analytics.call_args.kwargs
    assert (kwargs["owner_id"], kwargs["phase"], kwargs["payload"]["runId"], kwargs["parsed"]["source"]) == (
        "org_1",
        "end",
        "run-1",
        "cron",
    )
    assert kwargs["payload"]["data"]["durationMs"] == 1200


@pytest.mark.asyncio
async def test_replay_and_room_sessions_write_nothing():
    claim, record, analytics = _patches(claimed=False)
    with claim, record as record, analytics as analytics:
        assert (await messaging.handle_usage_report("org_1", BODY)).status == "replayed"
        room = BODY.model_copy(
            update={
                "session_key": "agent:11111111-1111-4111-8111-111111111111:lightbulb:channel:"
                "22222222-2222-4222-8222-222222222222"
            }
        )
        assert (await messaging.handle_usage_report("org_1", room)).status == "ignored"
    record.assert_not_awaited()
    analytics.assert_not_called()


@pytest.mark.asyncio
async def test_zero_usage_skips_counters_but_still_emits_analytics():
    claim, record, analytics = _patches()
    empty = BODY.model_copy(
        update={
            "usage": RunUsage(
                model="unknown",
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_write_tokens=0,
                estimated_cost_usd=None,
            ),
            "success": False,
        }
    )
    with claim, record as record, analytics as analytics:
        assert (await messaging.handle_usage_report("org_1", empty)).status == "applied"
    record.assert_not_awaited()
    assert analytics.call_args.kwargs["phase"] == "error"


@pytest.mark.asyncio
async def test_usage_report_emits_run_completed_once_across_instances(monkeypatch):
    """The durable receipt, not the per-process _seen_run_ids cache, is what keeps
    run.completed single-emit at desiredCount >= 2. Replay must not re-emit."""
    calls = []
    monkeypatch.setattr(
        "core.services.messaging.run_analytics.schedule_run_completed",
        lambda **kw: calls.append(kw["payload"]["runId"]),
    )
    # First writer wins; the SECOND call models the other instance.
    claims = iter([True, False])
    monkeypatch.setattr(
        "core.repositories.messaging_repo.claim_usage_receipt",
        AsyncMock(side_effect=lambda *a, **k: next(claims)),
    )
    body = UsageReportRequest(
        session_key="agent:friday:cron:o1", agent_id="friday", run_id="r1", success=True, usage=_zero_usage()
    )
    first = await messaging.handle_usage_report("o1", body)
    second = await messaging.handle_usage_report("o1", body)
    assert (first.status, second.status) == ("applied", "replayed")
    assert calls == ["r1"]


@pytest.mark.asyncio
async def test_a_hermes_paperclip_run_reaches_analytics_as_a_paperclip_issue_run():
    """A nightly CEO / issue run reports its hermes_gateway session id. It must arrive parsed as
    source=paperclip WITH the issue id, or the enrichment that tags nightly runs never runs."""
    body = BODY.model_copy(update={"session_key": "paperclip:company:c-1:agent:a-2:issue:i-3"})
    claim, record, analytics = _patches()
    with claim, record, analytics as analytics:
        await messaging.handle_usage_report("org_1", body)
    assert analytics.call_args.kwargs["parsed"] == {"agent_id": "a-2", "source": "paperclip", "issue_id": "i-3"}


ROOM_KEY = "agent:11111111-1111-4111-8111-111111111111:lightbulb:channel:22222222-2222-4222-8222-222222222222"


@pytest.mark.asyncio
async def test_room_key_without_family_is_ignored_and_claims_nothing():
    claim, record, analytics = _patches()
    with claim as claim, record as record, analytics:
        assert (
            await messaging.handle_usage_report("org_1", BODY.model_copy(update={"session_key": ROOM_KEY}))
        ).status == "ignored"
    claim.assert_not_awaited()
    record.assert_not_awaited()


@pytest.mark.asyncio
async def test_voice_run_on_the_room_key_applies_once_per_run():
    voice = BODY.model_copy(update={"session_key": ROOM_KEY, "family": "voice"})
    claims = iter([True, False])
    with (
        patch(
            "core.services.messaging.messaging_repo.claim_usage_receipt",
            new_callable=AsyncMock,
            side_effect=lambda *a, **k: next(claims),
        ) as claim,
        patch("core.services.messaging.record_usage", new_callable=AsyncMock) as record,
        patch("core.services.messaging.run_analytics.schedule_run_completed", MagicMock(return_value=None)),
    ):
        assert (await messaging.handle_usage_report("org_1", voice)).status == "applied"
        assert (await messaging.handle_usage_report("org_1", voice)).status == "replayed"
    assert claim.await_count == 2
    record.assert_awaited_once()
    assert record.call_args.kwargs["room_turn"] is False


@pytest.mark.asyncio
async def test_voice_run_on_a_non_room_key_applies():
    claim, record, analytics = _patches()
    with claim, record as record, analytics:
        assert (
            await messaging.handle_usage_report("org_1", BODY.model_copy(update={"family": "voice"}))
        ).status == "applied"
    record.assert_awaited_once()


def test_unknown_family_is_rejected():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        UsageReportRequest.model_validate({**BODY.model_dump(by_alias=True), "family": "chat"})
