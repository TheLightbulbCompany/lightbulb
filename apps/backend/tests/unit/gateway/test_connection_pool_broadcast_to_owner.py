"""Tests for GatewayConnectionPool.broadcast_to_owner (REALTIME, spec PR-7).

The pool-level fan-out core.services.status_events.publish_status_changed
rides on: pushes a generic {type: "event"} frame to every frontend WS
registered for an owner, independent of whether an OpenClaw GatewayConnection
exists for them (a billing/paperclip write can land before the container
itself is provisioned).
"""

import os

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from unittest.mock import MagicMock

import pytest

from core.gateway.connection_pool import GatewayConnectionPool


@pytest.mark.asyncio
async def test_broadcast_to_owner_delivers_to_every_registered_frontend():
    pool = GatewayConnectionPool(management_api=MagicMock())
    pool._management_api.send_message = MagicMock(return_value=True)
    pool.add_frontend_connection("owner_1", "conn_a")
    pool.add_frontend_connection("owner_1", "conn_b")

    pool.broadcast_to_owner("owner_1", "onboarding_status_changed", {})
    await pool._sender.flush()

    sent = {c.args[0] for c in pool._management_api.send_message.call_args_list}
    assert sent == {"conn_a", "conn_b"}
    frames = [c.args[1] for c in pool._management_api.send_message.call_args_list]
    for frame in frames:
        assert frame == {
            "type": "event",
            "event": "onboarding_status_changed",
            "payload": {},
        }


@pytest.mark.asyncio
async def test_broadcast_to_owner_does_not_reach_other_owners():
    pool = GatewayConnectionPool(management_api=MagicMock())
    pool._management_api.send_message = MagicMock(return_value=True)
    pool.add_frontend_connection("owner_1", "conn_a")
    pool.add_frontend_connection("owner_2", "conn_c")

    pool.broadcast_to_owner("owner_1", "onboarding_status_changed", {})
    await pool._sender.flush()

    sent = {c.args[0] for c in pool._management_api.send_message.call_args_list}
    assert sent == {"conn_a"}


def test_broadcast_to_owner_no_registered_frontends_is_a_noop():
    pool = GatewayConnectionPool(management_api=MagicMock())
    # No frontend connections registered for this owner at all -- must not
    # raise (fire-and-forget from a repo write path).
    pool.broadcast_to_owner("owner_ghost", "onboarding_status_changed", {})
