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

from unittest.mock import MagicMock, patch

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
        {"provider_choice": "bedrock_claude", "$set": {"subscription_status": "trial_local"}},
    )


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

    mock_capture.assert_called_once_with(distinct_id="user_1", event="trial_cap_reached", properties=None)


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
    an unbounded per-process leak) -- same idiom as connection_pool's
    ``_side_run_ids``."""
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
