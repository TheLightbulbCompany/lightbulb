"""Tests for the server-side PostHog product-event bridge
(``core/observability/product_analytics.py``), including the trial-funnel
telemetry helpers added on top of it (Task 16):

- ``capture_product_event`` itself: no-op (never raises) when the write key
  is unset, the client is None, ``distinct_id`` is falsy, or the underlying
  ``client.capture`` call raises.
- ``capture_once_per_owner_per_day``: the bounded in-process FIFO dedup that
  ``trial_cap_reached`` / ``trial_expired_block`` ride — fires exactly once
  per (event, owner, UTC day), fires again the next day, and never raises
  even if the underlying capture blows up.
"""

import asyncio
import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.observability import product_analytics


# ---------------------------------------------------------------------------
# capture_product_event
# ---------------------------------------------------------------------------


def test_capture_no_op_when_client_is_none():
    """No PostHog write key configured (the default in tests) -> the module
    never even tries to build a client, and capture is a silent no-op."""
    with patch.object(product_analytics, "_client_or_none", return_value=None):
        # Must not raise.
        product_analytics.capture_product_event(distinct_id="user_1", event="trial_started")


def test_capture_no_op_when_distinct_id_missing():
    """A falsy distinct_id (no Clerk id resolved) is a no-op even with a
    live client -- capture() must never be called with distinct_id=None."""
    mock_client = MagicMock()
    with patch.object(product_analytics, "_client_or_none", return_value=mock_client):
        product_analytics.capture_product_event(distinct_id=None, event="trial_started")
    mock_client.capture.assert_not_called()


def test_capture_calls_client_with_merged_set_person():
    mock_client = MagicMock()
    with patch.object(product_analytics, "_client_or_none", return_value=mock_client):
        product_analytics.capture_product_event(
            distinct_id="user_1",
            event="trial_started",
            properties={"provider_choice": "bedrock_claude"},
            set_person={"subscription_status": "trial_local"},
        )
    mock_client.capture.assert_called_once_with(
        "user_1",
        "trial_started",
        {
            "provider_choice": "bedrock_claude",
            "environment": "local",
            "$set": {"subscription_status": "trial_local"},
        },
    )


def test_capture_stamps_environment(monkeypatch):
    """Every server-side event carries an ``environment`` super-property so dev
    and prod are separable in the shared PostHog project."""
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    mock_client = MagicMock()
    with patch.object(product_analytics, "_client_or_none", return_value=mock_client):
        product_analytics.capture_product_event(distinct_id="user_1", event="trial_started")
    assert mock_client.capture.call_args.args[2]["environment"] == "prod"


def test_capture_environment_defaults_to_local_when_unset(monkeypatch):
    """Empty ENVIRONMENT (local dev / CI) reads as ``local``."""
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "")
    mock_client = MagicMock()
    with patch.object(product_analytics, "_client_or_none", return_value=mock_client):
        product_analytics.capture_product_event(distinct_id="user_1", event="trial_started")
    assert mock_client.capture.call_args.args[2]["environment"] == "local"


def test_capture_does_not_clobber_caller_environment(monkeypatch):
    """A caller that supplies ``environment`` keeps its value (setdefault)."""
    from core.config import settings

    monkeypatch.setattr(settings, "ENVIRONMENT", "prod")
    mock_client = MagicMock()
    with patch.object(product_analytics, "_client_or_none", return_value=mock_client):
        product_analytics.capture_product_event(
            distinct_id="user_1", event="trial_started", properties={"environment": "custom"}
        )
    assert mock_client.capture.call_args.args[2]["environment"] == "custom"


def test_capture_swallows_client_exception():
    """A PostHog outage (capture() raises) must never propagate -- the
    module's whole reason for existing is to never break the billing path."""
    mock_client = MagicMock()
    mock_client.capture.side_effect = RuntimeError("posthog down")
    with patch.object(product_analytics, "_client_or_none", return_value=mock_client):
        # Must not raise.
        product_analytics.capture_product_event(distinct_id="user_1", event="trial_started")


# ---------------------------------------------------------------------------
# capture_once_per_owner_per_day
# ---------------------------------------------------------------------------


def _reset_dedup():
    product_analytics._daily_dedup_seen.clear()


def test_dedup_fires_once_per_owner_per_event_per_day():
    _reset_dedup()
    with (
        patch.object(product_analytics, "utc_today", return_value="2026-07-11"),
        patch.object(product_analytics, "capture_product_event") as mock_capture,
    ):
        product_analytics.capture_once_per_owner_per_day(owner_id="user_1", event="trial_cap_reached")
        product_analytics.capture_once_per_owner_per_day(owner_id="user_1", event="trial_cap_reached")
        product_analytics.capture_once_per_owner_per_day(owner_id="user_1", event="trial_cap_reached")

    mock_capture.assert_called_once_with(
        distinct_id="user_1", event="trial_cap_reached", properties={"workspace_id": "user_1"}
    )


def test_dedup_keys_the_person_separately_from_the_workspace():
    """The dedup is per workspace; the PostHog person is the human who hit the
    door, and the workspace rides along as a property."""
    _reset_dedup()
    with (
        patch.object(product_analytics, "utc_today", return_value="2026-07-11"),
        patch.object(product_analytics, "capture_product_event") as mock_capture,
    ):
        product_analytics.capture_once_per_owner_per_day(
            owner_id="org_1", event="trial_cap_reached", properties={"x": 1}, distinct_id="user_a"
        )
        product_analytics.capture_once_per_owner_per_day(
            owner_id="org_1", event="trial_cap_reached", properties={"x": 1}, distinct_id="user_b"
        )
    mock_capture.assert_called_once_with(
        distinct_id="user_a", event="trial_cap_reached", properties={"workspace_id": "org_1", "x": 1}
    )


def test_dedup_fires_again_on_a_new_utc_day():
    _reset_dedup()
    with patch.object(product_analytics, "capture_product_event") as mock_capture:
        with patch.object(product_analytics, "utc_today", return_value="2026-07-11"):
            product_analytics.capture_once_per_owner_per_day(owner_id="user_1", event="trial_cap_reached")
        with patch.object(product_analytics, "utc_today", return_value="2026-07-12"):
            product_analytics.capture_once_per_owner_per_day(owner_id="user_1", event="trial_cap_reached")

    assert mock_capture.call_count == 2


def test_dedup_is_independent_per_owner_and_per_event():
    _reset_dedup()
    with (
        patch.object(product_analytics, "utc_today", return_value="2026-07-11"),
        patch.object(product_analytics, "capture_product_event") as mock_capture,
    ):
        product_analytics.capture_once_per_owner_per_day(owner_id="user_1", event="trial_cap_reached")
        product_analytics.capture_once_per_owner_per_day(owner_id="user_2", event="trial_cap_reached")
        product_analytics.capture_once_per_owner_per_day(owner_id="user_1", event="trial_expired_block")

    assert mock_capture.call_count == 3


def test_dedup_set_is_bounded_fifo():
    """Insert past the cap and confirm the oldest entries are evicted (not
    an unbounded per-process leak) -- same idiom as run_analytics's
    ``_seen_run_ids``."""
    _reset_dedup()
    with (
        patch.object(product_analytics, "utc_today", return_value="2026-07-11"),
        patch.object(product_analytics, "capture_product_event"),
        patch.object(product_analytics, "_DAILY_DEDUP_CAP", 3),
    ):
        for i in range(5):
            product_analytics.capture_once_per_owner_per_day(owner_id=f"user_{i}", event="trial_cap_reached")
        assert len(product_analytics._daily_dedup_seen) == 3
        # The earliest owners were evicted, so they fire again.
        with patch.object(product_analytics, "capture_product_event") as mock_capture:
            product_analytics.capture_once_per_owner_per_day(owner_id="user_0", event="trial_cap_reached")
        mock_capture.assert_called_once()
    _reset_dedup()


def test_dedup_delegates_to_real_capture_product_event_never_raising():
    """End-to-end with the REAL ``capture_product_event`` (not mocked) --
    proves ``capture_once_per_owner_per_day`` inherits the module's
    never-raises contract rather than needing its own try/except, even
    when the underlying PostHog client blows up."""
    _reset_dedup()
    mock_client = MagicMock()
    mock_client.capture.side_effect = RuntimeError("posthog down")
    with (
        patch.object(product_analytics, "utc_today", return_value="2026-07-11"),
        patch.object(product_analytics, "_client_or_none", return_value=mock_client),
    ):
        # Must not raise.
        product_analytics.capture_once_per_owner_per_day(owner_id="user_1", event="trial_cap_reached")
    mock_client.capture.assert_called_once()
    _reset_dedup()


# ---------------------------------------------------------------------------
# trial_first_agent_response — once ever per owner, gated by a billing-row CAS
# ---------------------------------------------------------------------------


@pytest.fixture
def _first_response_state(monkeypatch):
    product_analytics._first_response_done.clear()
    monkeypatch.setattr(product_analytics.settings, "POSTHOG_PROJECT_WRITE_KEY", "phc_test")
    yield
    product_analytics._first_response_done.clear()


def test_first_response_not_scheduled_when_bridge_is_off(monkeypatch):
    """No key → no task, no DynamoDB — this is what keeps every unit test
    that drives a chat.final off the live billing table."""
    monkeypatch.setattr(product_analytics.settings, "POSTHOG_PROJECT_WRITE_KEY", "")
    assert product_analytics.schedule_trial_first_agent_response("user_x") is None


@pytest.mark.asyncio
async def test_first_response_fires_once_when_the_cas_is_won(_first_response_state):
    mock_client = MagicMock()
    cas = AsyncMock(return_value=True)
    with (
        patch.object(product_analytics, "_client_or_none", return_value=mock_client),
        patch("core.repositories.billing_repo.mark_trial_first_agent_response", cas),
    ):
        task = product_analytics.schedule_trial_first_agent_response("user_t")
        assert task is not None
        await task
        assert product_analytics.schedule_trial_first_agent_response("user_t") is None
    cas.assert_awaited_once_with("user_t")
    mock_client.capture.assert_called_once()
    args, kwargs = mock_client.capture.call_args
    assert args[0] == "user_t" and args[1] == "trial_first_agent_response"
    assert args[2]["workspace_id"] == "user_t"
    assert kwargs["uuid"] == str(
        uuid.uuid5(product_analytics._TRIAL_FIRST_RESPONSE_NS, f"user_t:{product_analytics.utc_today()}")
    )


@pytest.mark.asyncio
async def test_first_response_lands_on_the_human_who_asked(_first_response_state):
    """The CAS stays on the workspace row; the PostHog person is the human
    whose message the agent answered."""
    mock_client = MagicMock()
    cas = AsyncMock(return_value=True)
    with (
        patch.object(product_analytics, "_client_or_none", return_value=mock_client),
        patch("core.repositories.billing_repo.mark_trial_first_agent_response", cas),
    ):
        await product_analytics.schedule_trial_first_agent_response("org_w", distinct_id="user_asker")
    cas.assert_awaited_once_with("org_w")
    args, _ = mock_client.capture.call_args
    assert args[0] == "user_asker" and args[1] == "trial_first_agent_response"
    assert args[2]["workspace_id"] == "org_w"


@pytest.mark.asyncio
async def test_first_response_silent_when_the_cas_is_lost(_first_response_state):
    """Already stamped, or not a trial owner: the row refuses the write and
    nothing is captured — but the owner is memoised so we stop asking."""
    mock_client = MagicMock()
    cas = AsyncMock(return_value=False)
    with (
        patch.object(product_analytics, "_client_or_none", return_value=mock_client),
        patch("core.repositories.billing_repo.mark_trial_first_agent_response", cas),
    ):
        await product_analytics.schedule_trial_first_agent_response("user_p")
        assert product_analytics.schedule_trial_first_agent_response("user_p") is None
    cas.assert_awaited_once()
    mock_client.capture.assert_not_called()


@pytest.mark.asyncio
async def test_first_response_write_failure_is_swallowed_and_not_memoised(_first_response_state):
    mock_client = MagicMock()
    cas = AsyncMock(side_effect=[RuntimeError("ddb blip"), True])
    with (
        patch.object(product_analytics, "_client_or_none", return_value=mock_client),
        patch("core.repositories.billing_repo.mark_trial_first_agent_response", cas),
    ):
        await product_analytics.schedule_trial_first_agent_response("user_r")
        mock_client.capture.assert_not_called()
        t = product_analytics.schedule_trial_first_agent_response("user_r")
        assert t is not None
        await t
    assert cas.await_count == 2
    mock_client.capture.assert_called_once()


@pytest.mark.asyncio
async def test_first_response_cancellation_does_not_memoise(_first_response_state):
    """close() cancels analytics tasks. A cancel mid-CAS must leave the owner
    unresolved so the next reply tries again — not silently done forever."""
    started = asyncio.Event()

    async def slow_cas(owner_id):
        started.set()
        await asyncio.sleep(10)
        return True

    with patch("core.repositories.billing_repo.mark_trial_first_agent_response", slow_cas):
        task = product_analytics.schedule_trial_first_agent_response("user_c")
        assert task is not None
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert "user_c" not in product_analytics._first_response_done
