"""Tests for the live gateway-RPC push of desktop device pairings.

Since the OpenClaw 8.x image the gateway trust store is SQLite, seeded from
the backend-written paired.json files only at boot — so these hooks are what
make a pair against a RUNNING container take effect without a container roll.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.desktop_pair_live import (
    approve_pending_node_surface,
    heal_desktop_device_not_paired,
    push_desktop_pairing_live,
)


CONTAINER = {"gateway_token": "tok123"}
IP = "10.0.1.1"


@pytest.fixture
def mock_ecs():
    with patch("core.containers.get_ecs_manager") as m:
        ecs = MagicMock()
        ecs.resolve_running_container = AsyncMock(return_value=(CONTAINER, IP))
        m.return_value = ecs
        yield ecs


@pytest.fixture
def mock_pool():
    with patch("core.containers.get_gateway_pool") as m:
        pool = MagicMock()
        pool.send_rpc = AsyncMock(return_value={})
        m.return_value = pool
        yield pool


def rpc_calls(pool, method: str) -> list:
    return [c for c in pool.send_rpc.await_args_list if c.kwargs.get("method") == method]


# ---------------------------------------------------------------- push (pair endpoint)


@pytest.mark.asyncio
async def test_push_approves_only_matching_pending_devices(mock_ecs, mock_pool):
    """Pending requests for the freshly paired deviceIds are approved; a
    foreign device's pending request is left alone."""

    async def _rpc(**kwargs):
        if kwargs["method"] == "device.pair.list":
            return {
                "pending": [
                    {"requestId": "req-ours", "deviceId": "dev_mac"},
                    {"requestId": "req-foreign", "deviceId": "dev_other"},
                ]
            }
        return {}

    mock_pool.send_rpc = AsyncMock(side_effect=_rpc)
    with patch("core.services.desktop_pair_live._read_backend_pairing_file", return_value={}):
        ok = await push_desktop_pairing_live(owner_id="user_1", device_ids={"dev_mac", "dev_cli"})

    assert ok is True
    approves = rpc_calls(mock_pool, "device.pair.approve")
    assert [c.kwargs["params"] for c in approves] == [{"requestId": "req-ours"}]


@pytest.mark.asyncio
async def test_push_approves_pending_node_surface_within_grant(mock_ecs, mock_pool):
    """A pending node surface whose declared commands sit inside the owner's
    granted set is approved live."""

    async def _rpc(**kwargs):
        if kwargs["method"] == "device.pair.list":
            return {"pending": []}
        if kwargs["method"] == "node.pair.list":
            return {"pending": [{"requestId": "surf-1", "nodeId": "dev_cli", "commands": ["system.run"]}]}
        return {}

    mock_pool.send_rpc = AsyncMock(side_effect=_rpc)
    nodes_file = {"dev_cli": {"commands": ["system.run", "system.which"]}}
    with patch("core.services.desktop_pair_live._read_backend_pairing_file", return_value=nodes_file):
        ok = await push_desktop_pairing_live(owner_id="user_1", device_ids={"dev_cli"})

    assert ok is True
    approves = rpc_calls(mock_pool, "node.pair.approve")
    assert [c.kwargs["params"] for c in approves] == [{"requestId": "surf-1"}]


@pytest.mark.asyncio
async def test_push_no_rpc_when_container_not_running(mock_ecs, mock_pool):
    mock_ecs.resolve_running_container = AsyncMock(return_value=(None, None))

    ok = await push_desktop_pairing_live(owner_id="user_1", device_ids={"dev_mac"})

    assert ok is False
    mock_pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_push_swallows_rpc_failure(mock_ecs, mock_pool):
    """A gateway RPC failure never raises out of the push — the pair request
    must still succeed and the boot import remains the fallback."""
    mock_pool.send_rpc = AsyncMock(side_effect=RuntimeError("gateway exploded"))

    ok = await push_desktop_pairing_live(owner_id="user_1", device_ids={"dev_mac"})

    assert ok is False


@pytest.mark.asyncio
async def test_push_one_device_approve_failure_does_not_drop_the_other(mock_ecs, mock_pool):
    async def _rpc(**kwargs):
        if kwargs["method"] == "device.pair.list":
            return {
                "pending": [
                    {"requestId": "req-a", "deviceId": "dev_mac"},
                    {"requestId": "req-b", "deviceId": "dev_cli"},
                ]
            }
        if kwargs["method"] == "device.pair.approve" and kwargs["params"] == {"requestId": "req-a"}:
            raise RuntimeError("unknown requestId")
        return {}

    mock_pool.send_rpc = AsyncMock(side_effect=_rpc)
    with patch("core.services.desktop_pair_live._read_backend_pairing_file", return_value={}):
        ok = await push_desktop_pairing_live(owner_id="user_1", device_ids={"dev_mac", "dev_cli"})

    assert ok is True
    approves = rpc_calls(mock_pool, "device.pair.approve")
    assert {c.kwargs["params"]["requestId"] for c in approves} == {"req-a", "req-b"}


# ------------------------------------------------------------- node surface approval


@pytest.mark.asyncio
async def test_surface_approval_blocks_commands_beyond_grant(mock_pool):
    """A client declaring more commands than the owner's permission grant
    derived is left pending — never approved."""

    async def _rpc(**kwargs):
        if kwargs["method"] == "node.pair.list":
            return {
                "pending": [
                    {
                        "requestId": "surf-1",
                        "nodeId": "dev_cli",
                        "commands": ["system.run", "screen.snapshot"],
                    }
                ]
            }
        return {}

    mock_pool.send_rpc = AsyncMock(side_effect=_rpc)
    nodes_file = {"dev_cli": {"commands": ["system.run"]}}
    with patch("core.services.desktop_pair_live._read_backend_pairing_file", return_value=nodes_file):
        ok = await approve_pending_node_surface(owner_id="user_1", ip=IP, gateway_token="tok123", device_id="dev_cli")

    assert ok is False
    assert rpc_calls(mock_pool, "node.pair.approve") == []


@pytest.mark.asyncio
async def test_surface_approval_allows_the_81_sidecars_unconditional_declarations(mock_pool):
    """The 2026.8.1 sidecar declares system.execApprovals.get/set, fs.listDir,
    terminal.upload and browser.proxy.upload.v1 unconditionally, regardless of
    the owner's permission grant. A full-grant node's declared surface must
    stay a subset of the backend-written allowed set so the approval is not
    left pending forever."""
    from core.services.desktop_pair import DESKTOP_CLI_COMMANDS

    declared = [
        "system.run",
        "system.execApprovals.get",
        "system.execApprovals.set",
        "fs.listDir",
        "terminal.upload",
        "browser.proxy.upload.v1",
    ]

    async def _rpc(**kwargs):
        if kwargs["method"] == "node.pair.list":
            return {"pending": [{"requestId": "surf-1", "nodeId": "dev_cli", "commands": declared}]}
        return {}

    mock_pool.send_rpc = AsyncMock(side_effect=_rpc)
    nodes_file = {"dev_cli": {"commands": list(DESKTOP_CLI_COMMANDS)}}
    with patch("core.services.desktop_pair_live._read_backend_pairing_file", return_value=nodes_file):
        ok = await approve_pending_node_surface(owner_id="user_1", ip=IP, gateway_token="tok123", device_id="dev_cli")

    assert ok is True
    approves = rpc_calls(mock_pool, "node.pair.approve")
    assert [c.kwargs["params"] for c in approves] == [{"requestId": "surf-1"}]


@pytest.mark.asyncio
async def test_surface_approval_skips_device_not_in_backend_file(mock_pool):
    """A node the owner never paired through /desktop/pair gets no approval —
    and no RPC is even attempted (the gate fails closed before the list)."""
    with patch("core.services.desktop_pair_live._read_backend_pairing_file", return_value={}):
        ok = await approve_pending_node_surface(owner_id="user_1", ip=IP, gateway_token="tok123", device_id="dev_rogue")

    assert ok is False
    mock_pool.send_rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_surface_approval_noop_when_nothing_pending(mock_pool):
    """Idempotent: an already-approved (or boot-folded) surface has no pending
    request — the call is a clean no-op."""
    mock_pool.send_rpc = AsyncMock(return_value={"pending": []})
    with patch(
        "core.services.desktop_pair_live._read_backend_pairing_file",
        return_value={"dev_cli": {"commands": ["system.run"]}},
    ):
        ok = await approve_pending_node_surface(owner_id="user_1", ip=IP, gateway_token="tok123", device_id="dev_cli")

    assert ok is False
    assert rpc_calls(mock_pool, "node.pair.approve") == []


# ------------------------------------------------------------------ NOT_PAIRED heal


@pytest.mark.asyncio
async def test_heal_skips_device_not_in_backend_file():
    """A device the owner never paired stays NOT_PAIRED — auto-approving it
    would let any member's raw WS connect mint its own trust entry."""
    with (
        patch("core.services.desktop_pair_live._read_backend_pairing_file", return_value={}),
        patch(
            "core.services.desktop_pair_live.approve_pending_device_from_rejection",
            AsyncMock(),
        ) as approve,
    ):
        healed = await heal_desktop_device_not_paired(
            owner_id="user_1",
            ip=IP,
            gateway_token="tok123",
            device_id="dev_rogue",
            request_id="req-1",
        )

    assert healed is False
    approve.assert_not_awaited()


@pytest.mark.asyncio
async def test_heal_approves_backend_paired_device():
    devices_file = {"dev_mac": {"publicKey": "pk", "userId": "user_1"}}
    with (
        patch(
            "core.services.desktop_pair_live._read_backend_pairing_file",
            return_value=devices_file,
        ),
        patch(
            "core.services.desktop_pair_live.approve_pending_device_from_rejection",
            AsyncMock(return_value=True),
        ) as approve,
    ):
        healed = await heal_desktop_device_not_paired(
            owner_id="user_1",
            ip=IP,
            gateway_token="tok123",
            device_id="dev_mac",
            request_id="req-1",
        )

    assert healed is True
    approve.assert_awaited_once_with(
        owner_id="user_1",
        ip=IP,
        gateway_token="tok123",
        device_id="dev_mac",
        request_id="req-1",
    )


@pytest.mark.asyncio
async def test_heal_without_device_id_is_a_noop():
    with patch(
        "core.services.desktop_pair_live.approve_pending_device_from_rejection",
        AsyncMock(),
    ) as approve:
        healed = await heal_desktop_device_not_paired(
            owner_id="user_1", ip=IP, gateway_token="tok123", device_id=None, request_id=None
        )

    assert healed is False
    approve.assert_not_awaited()


# ------------------------------------------------------------------- revoke eviction


@pytest.mark.asyncio
async def test_revoke_evicts_device_from_live_trust_store(mock_ecs, mock_pool):
    from core.services.desktop_pair_live import push_desktop_revoke_live

    ok = await push_desktop_revoke_live(owner_id="user_1", device_id="dev_mac")

    assert ok is True
    removes = rpc_calls(mock_pool, "device.pair.remove")
    assert [c.kwargs["params"] for c in removes] == [{"deviceId": "dev_mac"}]


@pytest.mark.asyncio
async def test_revoke_eviction_treats_unknown_device_as_done(mock_ecs, mock_pool):
    """An already-absent device is the desired end state, not a failure."""
    from core.services.desktop_pair_live import push_desktop_revoke_live

    mock_pool.send_rpc = AsyncMock(side_effect=RuntimeError("unknown deviceId"))

    ok = await push_desktop_revoke_live(owner_id="user_1", device_id="dev_gone")

    assert ok is True


@pytest.mark.asyncio
async def test_revoke_eviction_no_rpc_when_container_not_running(mock_ecs, mock_pool):
    from core.services.desktop_pair_live import push_desktop_revoke_live

    mock_ecs.resolve_running_container = AsyncMock(return_value=(None, None))

    ok = await push_desktop_revoke_live(owner_id="user_1", device_id="dev_mac")

    assert ok is False
    mock_pool.send_rpc.assert_not_awaited()
