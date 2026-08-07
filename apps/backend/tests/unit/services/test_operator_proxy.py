"""Tests for the Paperclip operator relay (operator_proxy).

The operator relay is a stripped-down sibling of node_proxy: a transparent
protocol relay between a Paperclip adapter and the owner's container, with
NONE of the node-host machinery (cli/mac buckets, exec pinning, node_status
broadcasts, ref-counted config patching).
"""

import asyncio

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.services.operator_proxy import (
    handle_operator_connect,
    handle_operator_message,
    handle_operator_disconnect,
    is_operator_connection,
    submit_operator_signed_connect,
    _operator_upstreams,
)


@pytest.fixture(autouse=True)
def clear_module_state():
    _operator_upstreams.clear()
    yield
    _operator_upstreams.clear()


@pytest.fixture
def mock_ecs():
    with patch("core.services.operator_proxy.get_ecs_manager") as m:
        ecs = AsyncMock()
        ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "tok123"}, "10.0.1.7"))
        m.return_value = ecs
        yield ecs


@pytest.fixture
def mock_upstream():
    with patch("core.services.operator_proxy.NodeUpstreamConnection") as cls:
        upstream = AsyncMock()
        upstream.connect = AsyncMock(return_value={"type": "res", "ok": True, "payload": {"protocol": 4}})
        upstream.start_reader = AsyncMock()
        upstream.close = AsyncMock()
        upstream.relay_to_upstream = AsyncMock()
        upstream.submit_signed_connect = MagicMock()
        upstream.set_client_push_callback = MagicMock()
        upstream.set_on_upstream_closed = MagicMock()
        upstream.device_id = "dev_operator_hash"
        upstream.signed_req_id = "adapter-req-1"
        cls.return_value = upstream
        yield upstream


@pytest.mark.asyncio
async def test_connect_registers_upstream_and_starts_reader(mock_ecs, mock_upstream):
    """A successful connect registers the relay and starts the verbatim reader."""
    mgmt = MagicMock()

    hello = await handle_operator_connect(
        owner_id="org_123",
        user_id="org_123",
        connection_id="conn_op_1",
        connect_params={"role": "operator", "client": {"id": "paperclip-operator"}},
        management_api=mgmt,
    )

    assert is_operator_connection("conn_op_1") is True
    mock_upstream.connect.assert_awaited_once()
    mock_upstream.start_reader.assert_awaited_once()
    assert hello["payload"]["protocol"] == 4


@pytest.mark.asyncio
async def test_connect_resolves_owner_container(mock_ecs, mock_upstream):
    """The relay opens the upstream against the OWNER's container."""
    mgmt = MagicMock()
    await handle_operator_connect(
        owner_id="org_owner",
        user_id="org_owner",
        connection_id="conn_op_2",
        connect_params={},
        management_api=mgmt,
    )
    mock_ecs.resolve_running_container.assert_awaited_once_with("org_owner")


@pytest.mark.asyncio
async def test_connect_forwards_hello_keyed_to_adapter_req_id(mock_ecs, mock_upstream):
    """hello-ok is forwarded DOWN rewritten to the adapter's own connect req id
    so the adapter's pending-request map matches (else the handshake times out)."""
    mgmt = MagicMock()
    mock_upstream.signed_req_id = "adapter-uuid-xyz"
    mock_upstream.connect = AsyncMock(
        return_value={"type": "res", "id": "container-internal-id", "ok": True, "payload": {"protocol": 4}}
    )

    await handle_operator_connect(
        owner_id="o",
        user_id="o",
        connection_id="conn_op_3",
        connect_params={},
        management_api=mgmt,
    )

    # The hello pushed to the adapter must carry the adapter's req id, not the
    # container's internal id.
    sent = [c.args for c in mgmt.send_message.call_args_list]
    hello_sends = [args for args in sent if args[0] == "conn_op_3" and args[1].get("ok")]
    assert hello_sends, "expected a hello-ok send to the adapter"
    assert hello_sends[-1][1]["id"] == "adapter-uuid-xyz"


@pytest.mark.asyncio
async def test_connect_rolls_back_on_handshake_failure(mock_ecs, mock_upstream):
    """If connect() raises, the upstream is rolled back (not left stale) and the
    error propagates to the caller."""
    mgmt = MagicMock()
    mock_upstream.connect = AsyncMock(side_effect=RuntimeError("handshake rejected"))

    with pytest.raises(RuntimeError, match="handshake rejected"):
        await handle_operator_connect(
            owner_id="o",
            user_id="o",
            connection_id="conn_fail",
            connect_params={},
            management_api=mgmt,
        )

    assert is_operator_connection("conn_fail") is False
    mock_upstream.close.assert_awaited()


@pytest.mark.asyncio
async def test_not_paired_rejection_triggers_self_heal_approval(mock_ecs, mock_upstream):
    """A NOT_PAIRED rejection on connect() triggers a self-heal approval
    attempt against the requestId parsed from the container's rejection
    payload -- the highest-leverage fix in the plan (the adapter's own
    reconnect then lands paired)."""
    mgmt = MagicMock()
    mock_upstream.connect = AsyncMock(side_effect=RuntimeError("Node handshake rejected: NOT_PAIRED"))
    mock_upstream.device_id = "dev_stranded"
    mock_upstream.rejection_error = {
        "code": "NOT_PAIRED",
        "message": "device not paired",
        "details": {"requestId": "req-99", "deviceId": "dev_stranded"},
    }

    with patch(
        "core.services.operator_proxy.approve_pending_device_from_rejection",
        new_callable=AsyncMock,
    ) as heal:
        heal.return_value = True
        with pytest.raises(RuntimeError, match="NOT_PAIRED"):
            await handle_operator_connect(
                owner_id="owner_x",
                user_id="owner_x",
                connection_id="conn_notpaired",
                connect_params={},
                management_api=mgmt,
            )

    heal.assert_awaited_once()
    kwargs = heal.await_args.kwargs
    assert kwargs["owner_id"] == "owner_x"
    assert kwargs["ip"] == "10.0.1.7"  # from mock_ecs.resolve_running_container
    assert kwargs["gateway_token"] == "tok123"  # from mock_ecs's container row
    assert kwargs["device_id"] == "dev_stranded"
    assert kwargs["request_id"] == "req-99"
    # The original failure still propagates and the upstream is still rolled
    # back -- self-healing accelerates the NEXT connect, it does not turn
    # this one into a success.
    assert is_operator_connection("conn_notpaired") is False


@pytest.mark.asyncio
async def test_non_not_paired_rejection_does_not_trigger_self_heal(mock_ecs, mock_upstream):
    """A rejection that is NOT NOT_PAIRED (e.g. an auth failure / token
    mismatch) must NOT trigger an approval attempt -- this is the lockout
    guard: the gateway locks out after 10 failed auths in 60s, and blindly
    approving on every rejection would retry into that lockout."""
    mgmt = MagicMock()
    mock_upstream.connect = AsyncMock(side_effect=RuntimeError("Node handshake rejected: AUTH_FAILED"))
    mock_upstream.device_id = "dev_stranded"
    mock_upstream.rejection_error = {"code": "AUTH_FAILED", "message": "bad token"}

    with patch(
        "core.services.operator_proxy.approve_pending_device_from_rejection",
        new_callable=AsyncMock,
    ) as heal:
        with pytest.raises(RuntimeError, match="AUTH_FAILED"):
            await handle_operator_connect(
                owner_id="owner_y",
                user_id="owner_y",
                connection_id="conn_authfail",
                connect_params={},
                management_api=mgmt,
            )

    heal.assert_not_awaited()


@pytest.mark.asyncio
async def test_message_relays_to_upstream(mock_ecs, mock_upstream):
    """Steady-state adapter frames relay verbatim to the upstream container."""
    mgmt = MagicMock()
    await handle_operator_connect(
        owner_id="o", user_id="o", connection_id="conn_op_4", connect_params={}, management_api=mgmt
    )

    frame = {"type": "req", "id": "r1", "method": "agent", "params": {"message": "hi"}}
    await handle_operator_message("conn_op_4", frame)
    mock_upstream.relay_to_upstream.assert_awaited_once_with(frame)


@pytest.mark.asyncio
async def test_message_no_upstream_is_noop(mock_ecs, mock_upstream):
    """Relaying a frame for an unknown connection is a logged no-op, not a crash."""
    await handle_operator_message("conn_unknown", {"type": "req"})
    mock_upstream.relay_to_upstream.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_signed_connect_routes_to_pending_handshake(mock_ecs, mock_upstream):
    """The adapter's signed connect is routed into the pending connect() future."""
    # Register the upstream directly (connect() is blocking on the future in
    # real life; here we just need the registry populated).
    _operator_upstreams["conn_op_5"] = mock_upstream

    signed = {"type": "req", "id": "c1", "method": "connect", "params": {"device": {"id": "x"}}}
    routed = submit_operator_signed_connect("conn_op_5", signed)
    assert routed is True
    mock_upstream.submit_signed_connect.assert_called_once_with(signed)


def test_submit_signed_connect_unknown_connection_returns_false():
    assert submit_operator_signed_connect("nope", {"type": "req"}) is False


@pytest.mark.asyncio
async def test_disconnect_closes_and_deregisters(mock_ecs, mock_upstream):
    mgmt = MagicMock()
    await handle_operator_connect(
        owner_id="o", user_id="o", connection_id="conn_op_6", connect_params={}, management_api=mgmt
    )
    assert is_operator_connection("conn_op_6") is True

    await handle_operator_disconnect("conn_op_6")
    assert is_operator_connection("conn_op_6") is False
    mock_upstream.close.assert_awaited()


@pytest.mark.asyncio
async def test_disconnect_is_idempotent(mock_ecs, mock_upstream):
    """Both the adapter-disconnect path and the upstream-died callback can fire
    for the same connection; the second call must be a no-op."""
    mgmt = MagicMock()
    await handle_operator_connect(
        owner_id="o", user_id="o", connection_id="conn_op_7", connect_params={}, management_api=mgmt
    )

    await handle_operator_disconnect("conn_op_7")
    mock_upstream.close.reset_mock()
    # Second call: upstream already popped — no double close.
    await handle_operator_disconnect("conn_op_7")
    mock_upstream.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_downstream_push_reaps_orphaned_upstream(mock_ecs, mock_upstream):
    """If the Management API push to the adapter returns False (GoneException —
    the adapter WS is gone), the relay upstream is reaped. This is the
    load-bearing path for a cross-task $disconnect: the explicit teardown landed
    on a different backend task, so the next container->adapter push is what
    reveals the loss and tears the orphaned upstream down."""
    mgmt = MagicMock()
    mgmt.send_message = MagicMock(return_value=True)  # ok during the handshake
    await handle_operator_connect(
        owner_id="o", user_id="o", connection_id="conn_reap", connect_params={}, management_api=mgmt
    )
    assert is_operator_connection("conn_reap") is True

    # Capture the push callback the relay installed on the upstream.
    push_cb = mock_upstream.set_client_push_callback.call_args[0][0]
    # Adapter is now gone: the Management API push returns False, confirmed.
    mgmt.send_message = MagicMock(return_value=False)
    mgmt.gone_confirmed = MagicMock(return_value=True)
    await push_cb({"type": "event", "event": "agent"})
    # The reaper defers teardown via create_task — let it run.
    await asyncio.sleep(0)

    assert is_operator_connection("conn_reap") is False
    mock_upstream.close.assert_awaited()


@pytest.mark.asyncio
async def test_unconfirmed_gone_keeps_relay_alive(mock_ecs, mock_upstream):
    """A lone GoneException can be a spurious API Gateway blip on a live
    adapter WS — the frame is dropped but the relay must NOT be reaped."""
    mgmt = MagicMock()
    mgmt.send_message = MagicMock(return_value=True)
    await handle_operator_connect(
        owner_id="o", user_id="o", connection_id="conn_blip", connect_params={}, management_api=mgmt
    )
    push_cb = mock_upstream.set_client_push_callback.call_args[0][0]
    mgmt.send_message = MagicMock(return_value=False)
    mgmt.gone_confirmed = MagicMock(return_value=False)
    await push_cb({"type": "event", "event": "agent"})
    await asyncio.sleep(0)

    assert is_operator_connection("conn_blip") is True
    mock_upstream.close.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_push_does_not_reap(mock_ecs, mock_upstream):
    """A normal (truthy) push must NOT tear down the relay."""
    mgmt = MagicMock()
    mgmt.send_message = MagicMock(return_value=True)
    await handle_operator_connect(
        owner_id="o", user_id="o", connection_id="conn_ok", connect_params={}, management_api=mgmt
    )
    push_cb = mock_upstream.set_client_push_callback.call_args[0][0]
    await push_cb({"type": "event", "event": "agent"})
    await asyncio.sleep(0)
    assert is_operator_connection("conn_ok") is True


@pytest.mark.asyncio
async def test_upstream_closed_callback_deregisters(mock_ecs, mock_upstream):
    """The on_upstream_closed callback installed at connect tears down the
    registry entry when the upstream dies independently of the adapter."""
    mgmt = MagicMock()
    await handle_operator_connect(
        owner_id="o", user_id="o", connection_id="conn_op_8", connect_params={}, management_api=mgmt
    )
    cb = mock_upstream.set_on_upstream_closed.call_args[0][0]
    assert callable(cb)

    await cb()
    assert is_operator_connection("conn_op_8") is False
