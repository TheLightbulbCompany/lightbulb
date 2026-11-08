"""Tests for the ``agent_run_completed`` PostHog emission
(``core/services/run_analytics.py``):

- sessionKey classification -> run_source / initiation_type for every session
  shape the gateway sees (webchat, channel, cron, unknown).
- outcome mapping from the lifecycle terminal (end / end+aborted / error).
- runId dedup FIFO, deterministic capture uuid, and the disabled short-circuit.
- internal-owner tagging via the platform-admin email rule + its TTL cache.
"""

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from core.services import run_analytics

OWNER = "user_owner1"


@pytest.fixture(autouse=True)
def _analytics_enabled(monkeypatch):
    """Enable the bridge + reset module state between tests."""
    run_analytics._seen_run_ids.clear()
    run_analytics._internal_cache.clear()
    monkeypatch.setattr(settings, "POSTHOG_PROJECT_WRITE_KEY", "phc_test")


def _payload(run_id="run-1", phase="end", **data_extra):
    return {"runId": run_id, "stream": "lifecycle", "data": {"phase": phase, **data_extra}}


async def _emit(parsed, payload=None, phase="end", owner_id=OWNER, is_internal=False):
    """Schedule + await one emission with capture mocked; returns the captured
    call kwargs, or None when nothing was captured."""
    with (
        patch.object(run_analytics, "capture_product_event") as mock_capture,
        patch.object(run_analytics, "_is_internal_owner", AsyncMock(return_value=is_internal)),
    ):
        task = run_analytics.schedule_run_completed(
            owner_id=owner_id,
            parsed=parsed,
            payload=payload if payload is not None else _payload(phase=phase),
            phase=phase,
        )
        if task is not None:
            await task
    if not mock_capture.called:
        return None
    return mock_capture.call_args.kwargs


# ---------------------------------------------------------------------------
# Synchronous classification: run_source / initiation_type matrix
# ---------------------------------------------------------------------------


async def test_session_key_classification_matrix():
    cases = [
        ({"agent_id": "a", "source": "webchat"}, "webchat", "human"),
        ({"agent_id": "a", "source": "webchat", "member_id": "user_b"}, "webchat", "human"),
        ({"agent_id": "a", "source": "dm", "channel": "telegram", "peer_id": "9"}, "channel", "human"),
        ({"agent_id": "a", "source": "group", "channel": "discord", "group_id": "g"}, "channel", "human"),
        ({"agent_id": "a", "source": "channel", "channel": "slack", "channel_id": "c"}, "channel", "human"),
        ({"agent_id": "a", "source": "cron", "cron_id": "cron-1"}, "cron", "autonomous"),
        # Malformed / keyless -> fail open into unknown, still counted.
        ({}, "unknown", "unknown"),
    ]
    for i, (parsed, want_source, want_initiation) in enumerate(cases):
        kwargs = await _emit(parsed, payload=_payload(run_id=f"run-{i}"))
        props = kwargs["properties"]
        assert props["run_source"] == want_source, parsed
        assert props["initiation_type"] == want_initiation, parsed
        assert kwargs["distinct_id"] == OWNER
        assert kwargs["event"] == "agent_run_completed"
        assert props["enrichment_failed"] is False


async def test_channel_and_cron_props_attached():
    kwargs = await _emit({"agent_id": "a", "source": "dm", "channel": "telegram", "peer_id": "9"})
    assert kwargs["properties"]["channel"] == "telegram"

    kwargs = await _emit({"agent_id": "a", "source": "cron", "cron_id": "cron-1"}, payload=_payload(run_id="run-2"))
    assert kwargs["properties"]["cron_id"] == "cron-1"
    assert kwargs["properties"]["channel"] is None


# ---------------------------------------------------------------------------
# Outcome mapping + duration
# ---------------------------------------------------------------------------


async def test_outcome_mapping():
    parsed = {"agent_id": "a", "source": "webchat"}
    kwargs = await _emit(parsed, payload=_payload(run_id="r-end"))
    assert kwargs["properties"]["outcome"] == "success"

    kwargs = await _emit(parsed, payload=_payload(run_id="r-abort", aborted=True))
    assert kwargs["properties"]["outcome"] == "aborted"

    kwargs = await _emit(parsed, payload=_payload(run_id="r-err", phase="error", error="boom"), phase="error")
    assert kwargs["properties"]["outcome"] == "failure"


async def test_duration_ms_from_lifecycle_timestamps():
    kwargs = await _emit(
        {"agent_id": "a", "source": "webchat"},
        payload=_payload(startedAt=1_000, endedAt=4_500),
    )
    assert kwargs["properties"]["duration_ms"] == 3_500

    kwargs = await _emit({"agent_id": "a", "source": "webchat"}, payload=_payload(run_id="r-2"))
    assert "duration_ms" not in kwargs["properties"]


async def test_duration_ms_from_explicit_field_when_no_timestamps():
    """The usage-callback path (cron/heartbeat/Paperclip) has no lifecycle
    startedAt/endedAt -- it reports durationMs directly."""
    kwargs = await _emit(
        {"agent_id": "a", "source": "cron"},
        payload=_payload(run_id="r-cron", durationMs=1200),
    )
    assert kwargs["properties"]["duration_ms"] == 1200


# ---------------------------------------------------------------------------
# Dedup, deterministic uuid, disabled short-circuit
# ---------------------------------------------------------------------------


async def test_run_id_dedup_emits_once_and_is_owner_scoped():
    parsed = {"agent_id": "a", "source": "webchat"}
    assert await _emit(parsed, payload=_payload(run_id="dup-1")) is not None
    assert await _emit(parsed, payload=_payload(run_id="dup-1")) is None
    # runId comes from the owner's own container — another owner reusing the
    # same id must still emit.
    assert await _emit(parsed, payload=_payload(run_id="dup-1"), owner_id="user_other") is not None


async def test_missing_run_id_still_emits_with_no_capture_uuid():
    parsed = {"agent_id": "a", "source": "webchat"}
    payload = {"stream": "lifecycle", "data": {"phase": "end"}}
    kwargs = await _emit(parsed, payload=dict(payload))
    assert kwargs["event_uuid"] is None
    # No dedup key -> a second delivery emits again (at-least-once floor).
    assert await _emit(parsed, payload=dict(payload)) is not None


async def test_capture_uuid_is_deterministic_and_owner_scoped():
    kwargs = await _emit({"agent_id": "a", "source": "webchat"}, payload=_payload(run_id="run-x"))
    run_analytics._seen_run_ids.clear()
    kwargs2 = await _emit({"agent_id": "a", "source": "webchat"}, payload=_payload(run_id="run-x"))
    assert kwargs["event_uuid"] == kwargs2["event_uuid"]
    assert kwargs["event_uuid"] == str(uuid.uuid5(run_analytics._CAPTURE_UUID_NS, f"{OWNER}:run-x"))
    # Same runId under a different owner -> a DIFFERENT uuid, so PostHog's
    # exact-duplicate drop can never eat a second owner's event.
    kwargs3 = await _emit(
        {"agent_id": "a", "source": "webchat"}, payload=_payload(run_id="run-x"), owner_id="user_other"
    )
    assert kwargs3["event_uuid"] != kwargs["event_uuid"]


async def test_disabled_when_write_key_unset(monkeypatch):
    monkeypatch.setattr(settings, "POSTHOG_PROJECT_WRITE_KEY", "")
    task = run_analytics.schedule_run_completed(
        owner_id=OWNER, parsed={"agent_id": "a", "source": "webchat"}, payload=_payload(), phase="end"
    )
    assert task is None


# ---------------------------------------------------------------------------
# Internal-owner tagging
# ---------------------------------------------------------------------------


async def test_is_internal_personal_owner_uses_clerk_primary_email_and_caches():
    with patch.object(
        run_analytics.clerk_admin, "get_primary_email", AsyncMock(return_value="eng@thelightbulb.company")
    ) as mock_email:
        assert await run_analytics._is_internal_owner("user_a") is True
        assert await run_analytics._is_internal_owner("user_a") is True
    mock_email.assert_awaited_once_with("user_a")

    with patch.object(run_analytics.clerk_admin, "get_primary_email", AsyncMock(return_value="jane@gmail.com")):
        assert await run_analytics._is_internal_owner("user_b") is False


async def test_is_internal_ttl_expiry_refetches(monkeypatch):
    monkeypatch.setattr(run_analytics, "_INTERNAL_TTL_S", 0.0)
    with patch.object(
        run_analytics.clerk_admin, "get_primary_email", AsyncMock(return_value="eng@thelightbulb.company")
    ) as mock_email:
        await run_analytics._is_internal_owner("user_a")
        await run_analytics._is_internal_owner("user_a")
    assert mock_email.await_count == 2


async def test_is_internal_clerk_lookup_failure_is_false_and_not_cached():
    """get_primary_email raises the same way for can't-reach-Clerk and a
    genuinely email-less user — both stay uncached so the next call re-asks."""
    with patch.object(
        run_analytics.clerk_admin,
        "get_primary_email",
        AsyncMock(side_effect=RuntimeError("clerk has no user with id user_a")),
    ) as mock_email:
        assert await run_analytics._is_internal_owner("user_a") is False
        assert "user_a" not in run_analytics._internal_cache
        assert await run_analytics._is_internal_owner("user_a") is False
    assert mock_email.await_count == 2


async def test_is_internal_org_owner_resolves_via_clerk_created_by():
    """Org owners resolve through the org's Clerk created_by user — that
    user's primary email decides internal vs customer."""
    with (
        patch.object(
            run_analytics.clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_founder"})
        ) as mock_org,
        patch.object(
            run_analytics.clerk_admin, "get_primary_email", AsyncMock(return_value="founder@thelightbulb.company")
        ) as mock_email,
    ):
        assert await run_analytics._is_internal_owner("org_abc") is True
    mock_org.assert_awaited_once_with("org_abc")
    mock_email.assert_awaited_once_with("user_founder")

    with (
        patch.object(
            run_analytics.clerk_admin, "get_organization", AsyncMock(return_value={"created_by": "user_cust"})
        ),
        patch.object(run_analytics.clerk_admin, "get_primary_email", AsyncMock(return_value="cust@acme.com")),
    ):
        assert await run_analytics._is_internal_owner("org_customer") is False


async def test_is_internal_org_lookup_miss_is_false_and_not_cached():
    """A None org (Clerk 404/outage) or missing created_by must not stick for
    an hour — the next call re-queries."""
    with patch.object(run_analytics.clerk_admin, "get_organization", AsyncMock(return_value=None)) as mock_org:
        assert await run_analytics._is_internal_owner("org_abc") is False
        assert "org_abc" not in run_analytics._internal_cache
        assert await run_analytics._is_internal_owner("org_abc") is False
    assert mock_org.await_count == 2

    with patch.object(run_analytics.clerk_admin, "get_organization", AsyncMock(return_value={"id": "org_abc"})):
        assert await run_analytics._is_internal_owner("org_abc") is False
    assert "org_abc" not in run_analytics._internal_cache


async def test_is_internal_lands_on_the_event():
    kwargs = await _emit({"agent_id": "a", "source": "webchat"}, is_internal=True)
    assert kwargs["properties"]["is_internal"] is True


async def test_resolve_internal_owner_raises_while_is_internal_owner_swallows():
    """ops/growth reads use the RAISING resolver directly (segmentation there
    is load-bearing, so a Clerk failure must surface as unavailable); the
    emit-path wrapper still swallows the same failure to False."""
    with patch.object(
        run_analytics.clerk_admin,
        "get_primary_email",
        AsyncMock(side_effect=RuntimeError("clerk down")),
    ):
        with pytest.raises(RuntimeError):
            await run_analytics.resolve_internal_owner("user_a")
        assert await run_analytics._is_internal_owner("user_a") is False


# ---------------------------------------------------------------------------
# CloudWatch run-outcome metric (run.completed) — the alarmable signal, emitted
# synchronously and deduped, covering both the chat path and the operator tap.
# ---------------------------------------------------------------------------


async def _emit_with_metric(parsed, *, run_id="rc-1", phase="end"):
    # Patch the async capture body away — we're testing the synchronous
    # CloudWatch metric, not the PostHog enrichment.
    with (
        patch.object(run_analytics, "put_metric") as metric,
        patch.object(run_analytics, "_capture", AsyncMock()),
    ):
        task = run_analytics.schedule_run_completed(
            owner_id=OWNER, parsed=parsed, payload=_payload(run_id=run_id, phase=phase), phase=phase
        )
        if task is not None:
            await task
    return metric


async def test_run_completed_metric_success_webchat():
    metric = await _emit_with_metric({"agent_id": "a", "source": "webchat"})
    metric.assert_any_call("run.completed", dimensions={"outcome": "success", "source": "webchat"})


async def test_run_completed_metric_fires_even_when_posthog_disabled(monkeypatch):
    # The reliability metric must NOT be gated by the product-analytics write key.
    monkeypatch.setattr(settings, "POSTHOG_PROJECT_WRITE_KEY", "")
    with patch.object(run_analytics, "put_metric") as metric:
        task = run_analytics.schedule_run_completed(
            owner_id=OWNER,
            parsed={"agent_id": "a", "source": "cron", "cron_id": "c1"},
            payload=_payload(run_id="rc-off", phase="end"),
            phase="end",
        )
    assert task is None  # no PostHog capture when disabled
    metric.assert_any_call("run.completed", dimensions={"outcome": "success", "source": "cron"})


async def test_run_completed_metric_deduped_on_owner_run_id():
    payload = _payload(run_id="rc-dup", phase="end")
    with (
        patch.object(run_analytics, "put_metric") as metric,
        patch.object(run_analytics, "_capture", AsyncMock()),
    ):
        for _ in range(2):
            task = run_analytics.schedule_run_completed(
                owner_id=OWNER, parsed={"agent_id": "a", "source": "webchat"}, payload=payload, phase="end"
            )
            if task is not None:
                await task
    run_completed = [c for c in metric.call_args_list if c.args and c.args[0] == "run.completed"]
    assert len(run_completed) == 1


@pytest.mark.asyncio
async def test_room_run_is_keyed_on_the_requester_with_the_workspace_as_a_property():
    with (
        patch.object(run_analytics, "capture_product_event") as mock_capture,
        patch.object(run_analytics, "_is_internal_owner", AsyncMock(return_value=False)),
    ):
        task = run_analytics.schedule_run_completed(
            owner_id="org_w",
            parsed={"source": "room", "agent_id": "a1", "room_id": "r1"},
            payload=_payload(run_id="room-run"),
            phase="end",
            person_id="user_bob",
        )
        await task
    kwargs = mock_capture.call_args.kwargs
    assert kwargs["distinct_id"] == "user_bob"
    assert kwargs["properties"]["workspace_id"] == "org_w"
    assert kwargs["properties"]["run_source"] == "room"
    assert kwargs["properties"]["initiation_type"] == "human"
