"""Every mutating RPC tells the owner's open tabs which resource went stale.

The per-connection ``*.changed`` broadcast is gone, so an invalidation event on the owner's user
channel is the only thing that makes a second tab (or the desktop app) re-read after a write.
Every mutating method is served by the backend itself now (``config.patch``'s path is covered in
test_runtime_rpc_hermes_config.py); what still forwards to the runtime is read-only.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import runtime_rpc as rpc


@pytest.mark.asyncio
async def test_invalidate_publishes_the_resource_for_a_mutating_method():
    with patch("core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock) as publish:
        await rpc._invalidate("org_1", "agents.update")
    event = publish.await_args.args[1]
    assert (publish.await_args.args[0], event["type"], event["payload"]) == (
        "org_1",
        "runtime.resource.invalidated",
        {"resource": "agents"},
    )


@pytest.mark.asyncio
async def test_forwarded_read_publishes_nothing():
    """A read has nothing to invalidate, and publishing on one would make every poll in every
    tab trigger a refetch in every other tab."""
    with (
        patch("core.services.runtime_rpc.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}),
        patch("core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock) as publish,
    ):
        await rpc.execute_sync(owner_id="org_1", user_id="user_1", request_id="r", method="health", params={})
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failure_does_not_fail_the_write():
    """The write already landed. Failing the call because the notification did not would make
    the client retry a completed mutation."""
    with patch(
        "core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock, side_effect=RuntimeError("appsync")
    ):
        await rpc._invalidate("org_1", "config.patch")  # must not raise


@pytest.mark.asyncio
async def test_every_mutating_method_names_a_resource():
    """The map is the whole mechanism: a mutating method missing from it writes and tells nobody,
    which looks like the write never happened until the tab is reloaded."""
    assert set(rpc._INVALIDATES) == set(rpc._MUTATING_METHODS)
