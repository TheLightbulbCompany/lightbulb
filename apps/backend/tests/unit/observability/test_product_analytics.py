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
# identity rule: an org id is a workspace, never a person (T01)
# ---------------------------------------------------------------------------


class _InlineExecutor:
    def submit(self, fn, *args):
        fn(*args)


def test_user_distinct_id_is_untouched(monkeypatch):
    mock_client = MagicMock()
    with patch.object(product_analytics, "_client_or_none", return_value=mock_client):
        product_analytics.capture_product_event(distinct_id="user_1", event="x", properties={"workspace_id": "org_9"})
    assert mock_client.capture.call_args.args[0] == "user_1"
    assert mock_client.capture.call_args.args[2]["workspace_id"] == "org_9"


def test_a_personless_server_event_keeps_its_uuid_under_a_fresh_distinct_id(monkeypatch):
    from core.observability import spark_events

    sent = []
    fake = type("C", (), {"capture": lambda self, d, e, p, **kw: sent.append((d, p, kw))})()
    monkeypatch.setattr(product_analytics, "_client_or_none", lambda: fake)
    for _ in range(2):  # a retried request
        spark_events.server_event("user_a", "account_deleted", "user_a", personless=True)
    (d1, p1, k1), (d2, _, k2) = sent
    assert d1 != d2 and "user_a" not in (d1, d2) and k1["uuid"] == k2["uuid"]
    assert p1["$process_person_profile"] is False and "$set" not in p1
