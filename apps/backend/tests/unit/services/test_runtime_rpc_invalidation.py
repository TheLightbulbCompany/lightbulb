"""Every mutating RPC tells the owner's open tabs which resource went stale.

The per-connection ``*.changed`` broadcast is gone, so an invalidation event on the owner's user
channel is the only thing that makes a second tab (or the desktop app) re-read after a write.
Driven over a method that still FORWARDS to the runtime (``skills.update``) -- ``config.patch``
is served from the profile in S3 now, and its invalidation is covered where that path is
(test_runtime_rpc_hermes_config.py).
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.services import runtime_rpc as rpc


async def _run(method, params=None, *, call=None):
    with (
        patch(
            "core.services.runtime_rpc.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}
        ) as rpc_call,
        patch("core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock) as publish,
    ):
        if call is not None:
            rpc_call.side_effect = call
        result = await rpc.execute_sync(
            owner_id="org_1", user_id="user_1", request_id="r", method=method, params=params or {}
        )
    return result, rpc_call, publish


@pytest.mark.asyncio
async def test_mutating_call_publishes_invalidation():
    result, call, publish = await _run("skills.update", {"name": "x"})
    assert result == {"ok": True}
    call.assert_awaited_once_with("org_1", "skills.update", {"name": "x"}, timeout_s=rpc._RPC_TIMEOUT_S)
    event = publish.await_args.args[1]
    assert (publish.await_args.args[0], event["type"], event["payload"]) == (
        "org_1",
        "runtime.resource.invalidated",
        {"resource": "skills"},
    )


@pytest.mark.asyncio
async def test_read_call_publishes_nothing():
    """A read has nothing to invalidate, and publishing on one would make every poll in every
    tab trigger a refetch in every other tab."""
    _, _, publish = await _run("channels.status")
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_publish_failure_does_not_fail_the_rpc():
    """The write already landed. Failing the call because the notification did not would make
    the client retry a completed mutation."""
    with patch(
        "core.services.runtime_rpc.publish_owner_users", new_callable=AsyncMock, side_effect=RuntimeError("appsync")
    ):
        with patch("core.services.runtime_rpc.container_rpc.call", new_callable=AsyncMock, return_value=1):
            assert (
                await rpc.execute_sync(
                    owner_id="org_1", user_id="user_1", request_id="r", method="skills.update", params={"name": "x"}
                )
                == 1
            )


@pytest.mark.asyncio
async def test_every_mutating_method_names_a_resource():
    """The map is the whole mechanism: a mutating method missing from it writes and tells nobody,
    which looks like the write never happened until the tab is reloaded."""
    assert set(rpc._INVALIDATES) == {
        "agents.create",
        "agents.update",
        "agents.delete",
        "config.set",
        "config.patch",
        "skills.update",
    }
