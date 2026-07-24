"""Tests for ``publish_status_changed`` (REALTIME, spec PR-7).

Pins the contract every durable write site relies on: best-effort,
fire-and-forget, never raises, no-ops on a falsy owner_id, and pushes the
content-free ``onboarding_status_changed`` event through
``GatewayConnectionPool.broadcast_to_owner`` (INV-1: no computed state rides
along).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.services.status_events import STATUS_CHANGED_EVENT, publish_status_changed


def test_publish_status_changed_broadcasts_via_the_gateway_pool():
    pool = MagicMock()
    with patch("core.containers.get_gateway_pool", return_value=pool):
        publish_status_changed("owner-1")
    pool.broadcast_to_owner.assert_called_once_with("owner-1", STATUS_CHANGED_EVENT, {})


def test_publish_status_changed_carries_no_payload_state():
    """INV-1: this is a pure invalidation nudge -- compute_status stays the
    sole composer. The payload must never carry a phase/status string."""
    pool = MagicMock()
    with patch("core.containers.get_gateway_pool", return_value=pool):
        publish_status_changed("owner-1")
    _, _, payload = pool.broadcast_to_owner.call_args[0]
    assert payload == {}


def test_publish_status_changed_noop_for_falsy_owner_id():
    pool = MagicMock()
    with patch("core.containers.get_gateway_pool", return_value=pool):
        publish_status_changed(None)
        publish_status_changed("")
    pool.broadcast_to_owner.assert_not_called()


def test_publish_status_changed_swallows_pool_errors():
    """A push failure (pool not booted, transient error) must never raise --
    every call site treats this like a best-effort metric emission, not an
    operation whose failure should propagate into the write it rides along
    with."""
    with patch("core.containers.get_gateway_pool", side_effect=RuntimeError("boom")):
        publish_status_changed("owner-1")  # must not raise
