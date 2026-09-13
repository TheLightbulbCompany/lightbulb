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
    _tap_run_completion_telemetry,
)


def _lifecycle_frame(*, phase="end", stream="lifecycle", session_key="agent:friday:cron:c1:run:r1", run_id="r1"):
    return {
        "type": "event",
        "event": "agent",
        "payload": {"stream": stream, "sessionKey": session_key, "runId": run_id, "data": {"phase": phase}},
    }


def test_tap_emits_run_completed_on_lifecycle_terminal():
    """An autonomous run's lifecycle end frame relayed through the operator
    adapter is tapped into run_analytics with the event's own payload + phase."""
    frame = _lifecycle_frame(phase="end")
    with patch("core.services.run_analytics.schedule_run_completed", return_value=None) as sched:
        _tap_run_completion_telemetry("owner_1", frame)
    sched.assert_called_once()
    kw = sched.call_args.kwargs
    assert kw["owner_id"] == "owner_1"
    assert kw["phase"] == "end"
    assert kw["payload"] is frame["payload"]


def test_tap_emits_on_error_phase():
    with patch("core.services.run_analytics.schedule_run_completed", return_value=None) as sched:
        _tap_run_completion_telemetry("o", _lifecycle_frame(phase="error"))
    assert sched.call_args.kwargs["phase"] == "error"


def test_tap_ignores_non_lifecycle_non_terminal_and_non_event():
    non_agent = _lifecycle_frame()
    non_agent["event"] = "chat"  # lifecycle-shaped payload but not an agent event
    with patch("core.services.run_analytics.schedule_run_completed", return_value=None) as sched:
        _tap_run_completion_telemetry("o", _lifecycle_frame(stream="assistant"))  # not lifecycle
        _tap_run_completion_telemetry("o", _lifecycle_frame(phase="start"))  # not a terminal
        _tap_run_completion_telemetry("o", {"type": "res", "payload": {}})  # not an event frame
        _tap_run_completion_telemetry("o", non_agent)  # event, but not "agent"
    sched.assert_not_called()


def test_tap_never_raises():
    # schedule_run_completed blowing up must not propagate into the relay
    with patch("core.services.run_analytics.schedule_run_completed", side_effect=RuntimeError("boom")):
        _tap_run_completion_telemetry("o", _lifecycle_frame())
    # malformed frames also must not raise
    _tap_run_completion_telemetry("o", {"type": "event", "payload": "not-a-dict"})
    _tap_run_completion_telemetry("o", None)


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
    assert kwargs["device_id"] == "dev_stranded"
    assert kwargs["request_id"] == "req-99"
    # The original failure still propagates and the upstream is still rolled
    # back -- self-healing accelerates the NEXT connect, it does not turn
    # this one into a success.
    assert is_operator_connection("conn_notpaired") is False


@pytest.mark.asyncio
async def test_self_heal_cancellation_still_closes_upstream_and_propagates(mock_ecs, mock_upstream):
    """asyncio.CancelledError from the self-heal await (e.g. task cancelled
    during service shutdown) derives from BaseException, so it bypasses
    approve_pending_device_from_rejection's own `except Exception`. The
    upstream must still be closed (finally), and the CancelledError must
    propagate rather than being swallowed or replaced by the trailing raise."""
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
        heal.side_effect = asyncio.CancelledError()
        with pytest.raises(asyncio.CancelledError):
            await handle_operator_connect(
                owner_id="owner_z",
                user_id="owner_z",
                connection_id="conn_cancelled",
                connect_params={},
                management_api=mgmt,
            )

    mock_upstream.close.assert_awaited()
    assert is_operator_connection("conn_cancelled") is False


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


# ---------------------------------------------------------------------------
# Wake-a-sleeping-container on operator connect.
#
# resolve_running_container returns (None, None) for any row NOT in
# provisioning/running — i.e. a scaled-to-zero / stopped box. A Paperclip
# adapter connecting against such a container (a scheduled/triggered run, e.g.
# the nightly CEO routine) should fire a best-effort wake so the box boots and
# the adapter's own reconnect loop completes the run.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stopped_container_fires_wake(mock_ecs, mock_upstream):
    """A connect against a stopped container (resolve -> (None, None)) fires
    request_wake_silent with the routine_operator door and returns None without
    ever opening an upstream."""
    mock_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    mgmt = MagicMock()

    with (
        patch("core.services.container_wake.request_wake_silent", new_callable=AsyncMock) as wake,
        patch("core.services.operator_proxy.put_metric") as metric,
    ):
        wake.return_value = "dispatched"
        result = await handle_operator_connect(
            owner_id="org_sleepy",
            user_id="org_sleepy",
            connection_id="conn_sleepy",
            connect_params={},
            management_api=mgmt,
        )

    assert result is None
    wake.assert_awaited_once_with("org_sleepy", door="routine_operator")
    metric.assert_called_once_with("operator.connect.stopped_container_wake", dimensions={"outcome": "dispatched"})
    # No upstream was built or registered for a stopped container.
    mock_upstream.connect.assert_not_awaited()
    assert is_operator_connection("conn_sleepy") is False


@pytest.mark.asyncio
async def test_blocked_wake_disarms_an_expired_trial(mock_ecs, mock_upstream):
    """A routine's wake blocked for TRIAL_EXPIRED pauses the nightly routine —
    the one entitlement change no webhook catches (trial_end is a local stamp)."""
    from core.services import trial_cap

    mock_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    mgmt = MagicMock()

    with (
        patch("core.services.container_wake.request_wake_silent", new_callable=AsyncMock) as wake,
        patch("core.services.trial_cap.trial_block_reason", new_callable=AsyncMock) as reason,
        patch(
            "core.services.paperclip_agent_lifecycle.disarm_owner_nightly_automation",
            new_callable=AsyncMock,
        ) as disarm,
        patch("core.services.operator_proxy.put_metric") as metric,
    ):
        wake.return_value = "blocked"
        reason.return_value = trial_cap.TRIAL_EXPIRED
        disarm.return_value = {"routines_paused": 1, "issues_cancelled": 2}
        result = await handle_operator_connect(
            owner_id="user_expired",
            user_id="user_expired",
            connection_id="conn_expired",
            connect_params={},
            management_api=mgmt,
        )

    assert result is None
    disarm.assert_awaited_once_with("user_expired")
    metric.assert_any_call("nightly_ceo.disarm", dimensions={"trigger": "trial_expired", "outcome": "ran"})


@pytest.mark.asyncio
async def test_blocked_wake_does_not_disarm_a_capped_trial(mock_ecs, mock_upstream):
    """A DAILY_CAP block resets at UTC midnight — the routine must stay armed
    and fire again tomorrow, so a capped block never disarms."""
    from core.services import trial_cap

    mock_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    mgmt = MagicMock()

    with (
        patch("core.services.container_wake.request_wake_silent", new_callable=AsyncMock) as wake,
        patch("core.services.trial_cap.trial_block_reason", new_callable=AsyncMock) as reason,
        patch(
            "core.services.paperclip_agent_lifecycle.disarm_owner_nightly_automation",
            new_callable=AsyncMock,
        ) as disarm,
    ):
        wake.return_value = "blocked"
        reason.return_value = trial_cap.DAILY_CAP
        await handle_operator_connect(
            owner_id="user_capped",
            user_id="user_capped",
            connection_id="conn_capped",
            connect_params={},
            management_api=mgmt,
        )

    disarm.assert_not_awaited()


@pytest.mark.asyncio
async def test_blocked_wake_for_a_non_trial_gate_does_not_disarm(mock_ecs, mock_upstream):
    """A block from a non-trial gate (canceled / credits / OAuth) -> reason is
    None -> no disarm here. Those cancellations are teardown's job."""
    mock_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    mgmt = MagicMock()

    with (
        patch("core.services.container_wake.request_wake_silent", new_callable=AsyncMock) as wake,
        patch("core.services.trial_cap.trial_block_reason", new_callable=AsyncMock) as reason,
        patch(
            "core.services.paperclip_agent_lifecycle.disarm_owner_nightly_automation",
            new_callable=AsyncMock,
        ) as disarm,
    ):
        wake.return_value = "blocked"
        reason.return_value = None
        await handle_operator_connect(
            owner_id="user_canceled",
            user_id="user_canceled",
            connection_id="conn_canceled",
            connect_params={},
            management_api=mgmt,
        )

    disarm.assert_not_awaited()


@pytest.mark.asyncio
async def test_provisioning_or_zombie_row_falls_through_without_wake(mock_ecs, mock_upstream):
    """A (container, None) shape — provisioning mid-flight, or a "running" row whose
    Cloud Map lookup found nothing (transient zombie) — deliberately does NOT wake
    (request_wake_silent would no-op it: in_flight / already_running) and falls
    through to the existing upstream path. Documents the scope boundary; the dead
    "running"-row case is a separate force-reprovision follow-up."""
    mock_ecs.resolve_running_container = AsyncMock(return_value=({"gateway_token": "tok"}, None))
    mgmt = MagicMock()

    with patch("core.services.container_wake.request_wake_silent", new_callable=AsyncMock) as wake:
        await handle_operator_connect(
            owner_id="org_zombie",
            user_id="org_zombie",
            connection_id="conn_zombie",
            connect_params={},
            management_api=mgmt,
        )

    wake.assert_not_awaited()
    # Falls through to the existing path, which builds the upstream with ip=None.
    mock_upstream.connect.assert_awaited_once()


@pytest.mark.asyncio
async def test_running_container_does_not_fire_wake(mock_ecs, mock_upstream):
    """A connect against a reachable running container proceeds normally and
    does NOT fire a wake."""
    mgmt = MagicMock()

    with patch(
        "core.services.container_wake.request_wake_silent",
        new_callable=AsyncMock,
    ) as wake:
        hello = await handle_operator_connect(
            owner_id="org_awake",
            user_id="org_awake",
            connection_id="conn_awake",
            connect_params={},
            management_api=mgmt,
        )
        await asyncio.sleep(0)

    wake.assert_not_awaited()
    mock_upstream.connect.assert_awaited_once()
    assert hello["payload"]["protocol"] == 4


@pytest.mark.asyncio
async def test_wake_failure_is_swallowed(mock_ecs, mock_upstream):
    """A raising wake must never break the connect — it is a fire-and-forget
    side effect. handle_operator_connect still returns None cleanly."""
    mock_ecs.resolve_running_container = AsyncMock(return_value=(None, None))
    mgmt = MagicMock()

    with patch(
        "core.services.container_wake.request_wake_silent",
        new_callable=AsyncMock,
    ) as wake:
        wake.side_effect = RuntimeError("wake blew up")
        # Must NOT raise despite the wake exploding — handle_operator_connect
        # awaits the wake inside a try/except, so the failure is contained here.
        result = await handle_operator_connect(
            owner_id="org_boom",
            user_id="org_boom",
            connection_id="conn_boom",
            connect_params={},
            management_api=mgmt,
        )

    assert result is None
    wake.assert_awaited_once_with("org_boom", door="routine_operator")


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
