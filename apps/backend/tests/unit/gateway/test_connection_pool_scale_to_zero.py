"""Tests for trial-container event-driven scale-to-zero (Task 12) and the
ping-deadline lost-disconnect recovery (Task 13).

Covers three surfaces on ``GatewayConnectionPool``:

- ``_maybe_stop_trial_container`` — the veto ladder. Stops (desiredCount=0)
  a ``trial_local`` container ONLY when every liveness signal says nobody is
  using it: no in-process frontend, still trial at fire time, no active/parked
  webchat run, zero cross-task live chat tabs. Any doubt = don't stop
  (over-preserve, never over-kill). Also invoked by Tasks 13/14 with a
  different ``trigger`` metric dimension.

- ``_grace_close`` — the trial-stop arm bolted onto the existing 30s idle
  WS-close grace. For trial owners a second ``_TRIAL_STOP_GRACE_S`` grace
  follows the WS close; a run in flight AT CLOSE TIME aborts the arm, and a
  reconnect during the extended grace cancels it cleanly.

- ``note_client_activity`` / the per-owner client-liveness deadline (Task
  13, Layer 3) — recovers a LOST API Gateway ``$disconnect``. Armed on
  ``add_frontend_connection``, reset on every ping, cancelled the moment a
  normal disconnect empties the owner's frontend set. On expiry it
  force-clears the stale set and routes through ``remove_frontend_connection``
  — the exact same tail a real disconnect takes — so Task 12's veto ladder
  owns the actual stop decision; Layer 3 never duplicates it.
"""

import asyncio
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.gateway import connection_pool  # noqa: E402
from core.gateway.connection_pool import GatewayConnectionPool  # noqa: E402

OWNER = "owner_1"


def _pool(conn_svc=None) -> GatewayConnectionPool:
    """Pool with a mock ConnectionService so count_live_chat_connections
    never touches DynamoDB. Defaults to 'nobody home' (count == 0)."""
    if conn_svc is None:
        conn_svc = MagicMock()
        conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
        # No Paperclip run in flight (the operator veto — see
        # test_stop_is_vetoed_by_an_in_flight_paperclip_run).
        conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
        # No live gateway-liveness row anywhere (the cross-worker
        # replacement veto): our own row is deleted at close, and no other
        # worker has dialed a replacement.
        conn_svc.get_live_gateway_conn_token = AsyncMock(return_value=None)
    return GatewayConnectionPool(management_api=None, conn_svc=conn_svc)


def _fake_connection(**overrides) -> SimpleNamespace:
    """A GatewayConnection stand-in with sensible no-op defaults for the
    incidental hooks add_frontend_connection/remove_frontend_connection call
    when a connection is present (invalidate_recipients_cache,
    reset_run_state_dedup) — on top of the has_active_runs/_parked veto
    fields the grace/trial-stop path reads. Override any field via kwargs
    (e.g. close=AsyncMock())."""
    defaults = dict(
        has_active_runs=False,
        _parked=False,
        invalidate_recipients_cache=lambda: None,
        reset_run_state_dedup=lambda: None,
        # close_user drains the connection's last liveness write after close().
        drain_liveness_write=AsyncMock(),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _trial_account():
    return {"subscription_status": "trial_local"}


def _patch_billing(account):
    return patch(
        "core.repositories.billing_repo.get_by_owner_id",
        new=AsyncMock(return_value=account),
    )


def _patch_ecs(stop_mock):
    mgr = SimpleNamespace(stop_user_service=stop_mock)
    return patch("core.containers.get_ecs_manager", return_value=mgr)


# --------------------------------------------------------------------------
# _maybe_stop_trial_container — the veto ladder
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stops_when_all_vetoes_pass():
    """trial_local + no frontends + no active/parked run + zero cross-task
    live tabs → container is stopped and the grace-trigger metric fires."""
    pool = _pool()
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
        patch.object(connection_pool, "put_metric") as metric,
    ):
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_awaited_once_with(OWNER, reason="trial_idle")
    metric.assert_any_call("gateway.trial_scale_to_zero", dimensions={"trigger": "grace"})


@pytest.mark.asyncio
async def test_veto_in_process_frontend_prevents_stop():
    """A frontend WS reconnected on THIS task → never stop."""
    pool = _pool()
    pool._frontend_connections[OWNER] = {"conn_a"}
    stop = AsyncMock()
    # Billing must not even be consulted once a live frontend is seen.
    billing = AsyncMock(return_value=_trial_account())
    with (
        patch("core.repositories.billing_repo.get_by_owner_id", new=billing),
        _patch_ecs(stop),
    ):
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_not_called()
    billing.assert_not_called()


@pytest.mark.asyncio
async def test_veto_paid_owner_never_stopped():
    """A paid (active) owner's container is never stopped."""
    pool = _pool()
    stop = AsyncMock()
    with (
        _patch_billing({"subscription_status": "active"}),
        _patch_ecs(stop),
    ):
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_not_called()


@pytest.mark.asyncio
async def test_veto_missing_billing_row_prevents_stop():
    """No billing row at all → don't stop (can't prove trial-ness)."""
    pool = _pool()
    stop = AsyncMock()
    with (
        _patch_billing(None),
        _patch_ecs(stop),
    ):
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_not_called()


@pytest.mark.asyncio
async def test_veto_active_run_prevents_stop():
    """An in-flight webchat run (has_active_runs) → never kill mid-turn."""
    pool = _pool()
    pool._connections[OWNER] = SimpleNamespace(has_active_runs=True, _parked=False)
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_not_called()


@pytest.mark.asyncio
async def test_veto_parked_connection_prevents_stop():
    """A parked connection (WS dropped mid-run, presumed alive) → don't stop."""
    pool = _pool()
    pool._connections[OWNER] = SimpleNamespace(has_active_runs=False, _parked=True)
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_not_called()


@pytest.mark.asyncio
async def test_veto_cross_task_live_chat_prevents_stop():
    """Another backend task holds a live chat tab (DDB truth) → don't stop."""
    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=1)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    conn_svc.get_live_gateway_conn_token = AsyncMock(return_value=None)
    pool = _pool(conn_svc=conn_svc)
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_not_called()
    conn_svc.count_live_chat_connections.assert_awaited_once_with(OWNER)


@pytest.mark.asyncio
async def test_conversion_during_grace_not_stopped():
    """Owner converts (trial_local → active) between arm and fire; the
    fire-time re-read of the billing row must abort the stop."""
    pool = _pool()
    stop = AsyncMock()
    with (
        _patch_billing({"subscription_status": "active"}),
        _patch_ecs(stop),
    ):
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_not_called()


@pytest.mark.asyncio
async def test_stop_user_service_exception_swallowed_and_logged():
    """stop_user_service raising must NOT propagate (it runs in a detached
    grace task) and must be logged for observability."""
    pool = _pool()
    stop = AsyncMock(side_effect=RuntimeError("ecs boom"))
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
        patch.object(connection_pool.logger, "exception") as log_exc,
    ):
        # Must not raise.
        await pool._maybe_stop_trial_container(OWNER)

    stop.assert_awaited_once_with(OWNER, reason="trial_idle")
    log_exc.assert_called_once()


@pytest.mark.asyncio
async def test_trigger_dimension_passthrough_for_task_13_14():
    """Tasks 13/14 reuse this method with a different trigger dimension."""
    pool = _pool()
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
        patch.object(connection_pool, "put_metric") as metric,
    ):
        await pool._maybe_stop_trial_container(OWNER, trigger="idle_sweep")

    stop.assert_awaited_once_with(OWNER, reason="trial_idle")
    metric.assert_any_call("gateway.trial_scale_to_zero", dimensions={"trigger": "idle_sweep"})


# --------------------------------------------------------------------------
# pool-level accessors (thin delegates over the owner's GatewayConnection)
# --------------------------------------------------------------------------


def test_has_active_runs_and_is_parked_no_connection():
    """No connection at all → neither active-run nor parked."""
    pool = _pool()
    assert pool.has_active_runs(OWNER) is False
    assert pool.is_parked(OWNER) is False


def test_has_active_runs_and_is_parked_delegate_to_connection():
    pool = _pool()
    pool._connections[OWNER] = SimpleNamespace(has_active_runs=True, _parked=True)
    assert pool.has_active_runs(OWNER) is True
    assert pool.is_parked(OWNER) is True


# --------------------------------------------------------------------------
# _grace_close — the trial-stop arm
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_grace_close_stops_trial_after_extended_grace(monkeypatch):
    """Idle trial owner with no run in flight: WS closes, then after the
    extended grace the container is stopped."""
    monkeypatch.setattr(connection_pool, "_GRACE_PERIOD", 0)
    monkeypatch.setattr(connection_pool, "_TRIAL_STOP_GRACE_S", 0)

    pool = _pool()
    close_mock = AsyncMock()
    pool._connections[OWNER] = SimpleNamespace(
        has_active_runs=False, _parked=False, close=close_mock, drain_liveness_write=AsyncMock()
    )
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await pool._grace_close(OWNER)

    close_mock.assert_awaited_once()  # WS closed
    stop.assert_awaited_once_with(OWNER, reason="trial_idle")  # container stopped
    assert OWNER not in pool._connections


@pytest.mark.asyncio
async def test_grace_close_run_in_flight_at_close_aborts_trial_stop(monkeypatch):
    """A run in flight AT CLOSE TIME (before close_user tears the connection
    down) aborts the trial-stop arm — the WS still closes, the container
    keeps running. This guards the ordering blind spot: after close_user the
    pool can no longer see the run, so the decision must be taken here."""
    monkeypatch.setattr(connection_pool, "_GRACE_PERIOD", 0)
    monkeypatch.setattr(connection_pool, "_TRIAL_STOP_GRACE_S", 0)
    # Ceiling lapses immediately with the run still active (the scenario this
    # test names) — otherwise the active-run hold spins to the real 300s ceiling.
    monkeypatch.setattr(connection_pool, "_PUSH_GRACE_CEILING_S", 0)

    pool = _pool()
    close_mock = AsyncMock()
    pool._connections[OWNER] = SimpleNamespace(
        has_active_runs=True, _parked=False, close=close_mock, drain_liveness_write=AsyncMock()
    )
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await pool._grace_close(OWNER)

    close_mock.assert_awaited_once()  # WS still closed (existing behavior)
    stop.assert_not_called()  # but trial-stop arm aborted


@pytest.mark.asyncio
async def test_grace_close_arms_trial_stop_after_mixed_case_session_key_run_completes(monkeypatch):
    """End-to-end regression for the ``_active_runs`` key-casing bug: the
    dispatcher marks a run active under the MIXED-CASE ``build_chat_session_key``
    (which embeds the raw Clerk user_id), OpenClaw's chat-terminal event
    carries the LOWERCASED sessionKey, and — after the fix — the run-active
    window still clears. Drives a REAL ``GatewayConnection`` (not a
    ``SimpleNamespace`` stand-in like the other tests in this file) through
    ``mark_run_active`` + ``_handle_message`` so the casing mismatch would
    actually be exercised, then asserts ``_grace_close`` arms the trial-stop
    path — proving the fix reaches all the way to the scale-to-zero decision,
    not just the in-memory dict."""
    monkeypatch.setattr(connection_pool, "_GRACE_PERIOD", 0)
    monkeypatch.setattr(connection_pool, "_TRIAL_STOP_GRACE_S", 0)

    from core.gateway.connection_pool import GatewayConnection

    mixed_case_owner_id = "user_2abcDEFghiJKLmnoPQR"

    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = mixed_case_owner_id
    conn.owner_id = mixed_case_owner_id
    conn._active_runs = {}
    conn._analytics_tasks = set()
    conn._run_state = {}
    conn._side_run_ids = {}
    conn._run_text = {}
    conn._push_tasks = set()
    conn._parked = False
    # Run-completion observer registry (INV-5 bootstrap dispatcher). Not
    # exercised by this test, but _handle_message's TRUE-terminal branch
    # always pops from it now.
    conn._run_completion_callbacks = {}
    conn._run_completion_tasks = set()
    # close_user drains the last scheduled liveness write; __new__ skips __init__.
    conn._pending_liveness_write = None
    conn.close = AsyncMock()

    async def fake_forward(message, target_member_id=None):
        return None

    with patch.object(conn, "_forward_to_frontends", fake_forward):
        mixed_case_session_key = f"agent:agentA:{mixed_case_owner_id}"
        conn.mark_run_active(mixed_case_session_key, agent_id="agentA")

        terminal_event = {
            "type": "event",
            "event": "chat",
            "payload": {
                "sessionKey": mixed_case_session_key.lower(),
                "state": "final",
                "runId": "run-1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            },
        }
        await conn._handle_message(terminal_event)

    assert conn.has_active_runs is False, "run must have cleared before grace-close runs"

    pool = _pool()
    pool._connections[mixed_case_owner_id] = conn
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await pool._grace_close(mixed_case_owner_id)

    conn.close.assert_awaited_once()
    stop.assert_awaited_once_with(mixed_case_owner_id, reason="trial_idle")


@pytest.mark.asyncio
async def test_grace_close_paid_owner_never_stops(monkeypatch):
    monkeypatch.setattr(connection_pool, "_GRACE_PERIOD", 0)
    monkeypatch.setattr(connection_pool, "_TRIAL_STOP_GRACE_S", 0)

    pool = _pool()
    close_mock = AsyncMock()
    pool._connections[OWNER] = SimpleNamespace(
        has_active_runs=False, _parked=False, close=close_mock, drain_liveness_write=AsyncMock()
    )
    stop = AsyncMock()
    with (
        _patch_billing({"subscription_status": "active"}),
        _patch_ecs(stop),
    ):
        await pool._grace_close(OWNER)

    close_mock.assert_awaited_once()
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_grace_close_frontend_reconnect_before_ws_close_skips_everything(monkeypatch):
    """A frontend reconnecting during the FIRST (WS) grace short-circuits —
    the connection is not even closed."""
    monkeypatch.setattr(connection_pool, "_GRACE_PERIOD", 0)
    monkeypatch.setattr(connection_pool, "_TRIAL_STOP_GRACE_S", 0)

    pool = _pool()
    close_mock = AsyncMock()
    pool._connections[OWNER] = SimpleNamespace(
        has_active_runs=False, _parked=False, close=close_mock, drain_liveness_write=AsyncMock()
    )
    pool._frontend_connections[OWNER] = {"reconnected"}
    stop = AsyncMock()
    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await pool._grace_close(OWNER)

    close_mock.assert_not_called()
    stop.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_during_extended_grace_cancels_stop(monkeypatch):
    """A reconnect during the extended (trial-stop) grace cancels the grace
    task cleanly via add_frontend_connection, and the container is NOT
    stopped. Verifies the grace task is re-registered in _grace_tasks after
    close_user so the cancellation reaches the extended sleep."""
    monkeypatch.setattr(connection_pool, "_GRACE_PERIOD", 0)
    # Long extended grace so the task parks in the sleep under our control.
    monkeypatch.setattr(connection_pool, "_TRIAL_STOP_GRACE_S", 3600)

    pool = _pool()
    close_mock = AsyncMock()
    pool._connections[OWNER] = SimpleNamespace(
        has_active_runs=False, _parked=False, close=close_mock, drain_liveness_write=AsyncMock()
    )
    stop = AsyncMock()

    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        task = asyncio.create_task(pool._grace_close(OWNER))

        # Drive the task until it parks in the extended sleep: connection gone
        # (close_user ran) and the grace task re-registered under OWNER.
        for _ in range(50):
            await asyncio.sleep(0)
            if OWNER not in pool._connections and not task.done() and pool._grace_tasks.get(OWNER) is task:
                break
        else:
            task.cancel()
            pytest.fail("grace task never reached the extended-sleep state")

        close_mock.assert_awaited_once()  # WS was closed

        # Reconnect: cancels the re-registered grace task.
        pool.add_frontend_connection(OWNER, "reconnected")
        assert task.cancelled() or pool._grace_tasks.get(OWNER) is not task

        try:
            await task
        except asyncio.CancelledError:
            pass

    stop.assert_not_called()
    assert task.done()


# --------------------------------------------------------------------------
# note_client_activity — deadline arm/reset/cancel bookkeeping (Task 13)
#
# These use the REAL asyncio TimerHandle returned by loop.call_later and
# assert on `.cancelled()` — deterministic and immune to event-loop timing
# nondeterminism, unlike waiting out real elapsed time (never used here).
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_frontend_connection_arms_client_deadline():
    """Registering a frontend connection arms a live, uncancelled deadline
    timer for its owner."""
    pool = _pool()
    pool.add_frontend_connection(OWNER, "conn-1")

    handle = pool._client_deadline_handles.get(OWNER)
    assert handle is not None
    assert not handle.cancelled()


def test_add_frontend_connection_without_running_loop_is_safe():
    """No running event loop (e.g. a synchronous caller) must never raise —
    note_client_activity degrades to a no-op instead of crashing or minting
    an orphaned event loop."""
    pool = _pool()
    pool.add_frontend_connection(OWNER, "conn-1")  # must not raise

    assert "conn-1" in pool._frontend_connections[OWNER]
    assert OWNER not in pool._client_deadline_handles


@pytest.mark.asyncio
async def test_note_client_activity_noop_when_no_frontend_registered():
    """A ping (or any activity signal) for an owner with zero registered
    frontend connections must not arm a timer — the deadline only exists to
    protect a connection set that actually has members."""
    pool = _pool()
    pool.note_client_activity(OWNER)

    assert OWNER not in pool._client_deadline_handles


@pytest.mark.asyncio
async def test_note_client_activity_ping_resets_deadline_handle():
    """A ping within the window (note_client_activity) cancels the previous
    deadline timer and arms a fresh one — proven via TimerHandle identity +
    cancellation, not by waiting out real time."""
    pool = _pool()
    pool.add_frontend_connection(OWNER, "conn-1")
    first_handle = pool._client_deadline_handles[OWNER]
    assert not first_handle.cancelled()

    pool.note_client_activity(OWNER)  # simulated ping
    second_handle = pool._client_deadline_handles[OWNER]

    assert first_handle.cancelled()
    assert second_handle is not first_handle
    assert not second_handle.cancelled()


@pytest.mark.asyncio
async def test_sustained_pings_keep_cancelling_prior_deadline_and_never_clear():
    """A long-running streaming chat keeps Task 11's 30s ping heartbeat
    landing independent of chat traffic: each ping must cancel the previous
    deadline timer before rearming a fresh one, so the deadline can never
    fire and the frontend set is never touched mid-turn."""
    pool = _pool()
    pool.add_frontend_connection(OWNER, "conn-1")

    prev_handle = pool._client_deadline_handles[OWNER]
    for _ in range(10):
        pool.note_client_activity(OWNER)  # simulated ping
        new_handle = pool._client_deadline_handles[OWNER]
        assert prev_handle.cancelled()
        assert new_handle is not prev_handle
        assert not new_handle.cancelled()
        prev_handle = new_handle

    assert "conn-1" in pool._frontend_connections[OWNER]
    assert OWNER not in pool._grace_tasks


@pytest.mark.asyncio
async def test_remove_frontend_connection_cancels_deadline_on_last_removal():
    """A normal disconnect (the last frontend connection going away) cancels
    the deadline timer outright — normal disconnects supersede the
    recovery mechanism entirely."""
    pool = _pool()
    pool.add_frontend_connection(OWNER, "conn-1")
    handle = pool._client_deadline_handles[OWNER]

    pool.remove_frontend_connection(OWNER, "conn-1")

    assert handle.cancelled()
    assert OWNER not in pool._client_deadline_handles


@pytest.mark.asyncio
async def test_removing_one_of_two_connections_keeps_deadline_armed():
    """The timer tracks the SET, not a single connection: removing one of
    two registered tabs must not cancel the still-armed deadline."""
    pool = _pool()
    pool.add_frontend_connection(OWNER, "conn-1")
    pool.add_frontend_connection(OWNER, "conn-2")
    handle = pool._client_deadline_handles[OWNER]

    pool.remove_frontend_connection(OWNER, "conn-1")

    assert pool._client_deadline_handles.get(OWNER) is handle
    assert not handle.cancelled()


@pytest.mark.asyncio
async def test_close_user_cancels_deadline_timer():
    """close_user pops the whole frontend-connection entry regardless of
    whether remove_frontend_connection already ran — the deadline timer
    must never outlive that path either (defense in depth)."""
    pool = _pool()
    pool.add_frontend_connection(OWNER, "conn-1")
    handle = pool._client_deadline_handles[OWNER]
    close_mock = AsyncMock()
    pool._connections[OWNER] = SimpleNamespace(
        has_active_runs=False, _parked=False, close=close_mock, drain_liveness_write=AsyncMock()
    )

    await pool.close_user(OWNER)

    assert handle.cancelled()
    assert OWNER not in pool._client_deadline_handles


# --------------------------------------------------------------------------
# Client-deadline expiry — force-clear + flow into Task 12's grace path
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_client_deadline_expiry_force_clears_frontends_and_arms_grace():
    """Expiry with no pings force-clears the owner's frontend set and arms
    the standard grace task via remove_frontend_connection's own tail —
    Layer 3 recovers the missing disconnect signal without duplicating
    Task 12's stop logic."""
    pool = _pool()
    pool.add_frontend_connection(OWNER, "conn-1")
    pool.add_frontend_connection(OWNER, "conn-2")
    pool._connections[OWNER] = _fake_connection()

    with patch.object(pool, "_grace_close", AsyncMock()) as grace_mock:
        await pool._on_client_deadline_expired(OWNER)

    assert len(pool._frontend_connections.get(OWNER, set())) == 0
    assert OWNER in pool._grace_tasks
    grace_mock.assert_called_once_with(OWNER)


@pytest.mark.asyncio
async def test_client_deadline_expiry_noop_when_set_already_empty():
    """Expiry re-reads _frontend_connections at fire time rather than
    trusting captured state — if the set already emptied via a different
    path (e.g. a connection-level dead-conn prune) the expiry handler is a
    harmless no-op, never wrongly arming a second grace task."""
    pool = _pool()
    pool._connections[OWNER] = SimpleNamespace(has_active_runs=False, _parked=False)
    # No frontend ever registered for OWNER.

    with patch.object(pool, "_grace_close", AsyncMock()) as grace_mock:
        await pool._on_client_deadline_expired(OWNER)

    grace_mock.assert_not_called()
    assert OWNER not in pool._grace_tasks


@pytest.mark.asyncio
async def test_client_deadline_expiry_flows_into_trial_stop(monkeypatch):
    """End-to-end: a lost $disconnect (deadline expiry, no pings) recovers
    all the way through Task 12's WS-close grace + extended trial-stop grace
    when every veto in _maybe_stop_trial_container passes — proving Layer 3
    flows INTO Layer 2's checks rather than reimplementing the stop
    decision itself."""
    monkeypatch.setattr(connection_pool, "_GRACE_PERIOD", 0)
    monkeypatch.setattr(connection_pool, "_TRIAL_STOP_GRACE_S", 0)

    pool = _pool()
    close_mock = AsyncMock()
    pool.add_frontend_connection(OWNER, "conn-1")
    pool._connections[OWNER] = _fake_connection(close=close_mock)
    stop = AsyncMock()

    with (
        _patch_billing(_trial_account()),
        _patch_ecs(stop),
    ):
        await pool._on_client_deadline_expired(OWNER)

        for _ in range(200):
            await asyncio.sleep(0)
            if stop.await_count:
                break
        else:
            pytest.fail("client-deadline expiry never flowed through to trial stop")

    stop.assert_awaited_once_with(OWNER, reason="trial_idle")
    close_mock.assert_awaited_once()
    assert len(pool._frontend_connections.get(OWNER, set())) == 0


@pytest.mark.asyncio
async def test_client_deadline_real_timer_fires_end_to_end(monkeypatch):
    """Smoke test for the actual loop.call_later wiring (not a direct
    _on_client_deadline_expired call like the tests above): with the
    deadline collapsed to 0s and no pings, the REAL scheduled timer fires,
    spawns the expiry task, and force-clears the frontend set — proving
    note_client_activity's arm path is really wired to the recovery flow,
    not just callable in isolation."""
    monkeypatch.setattr(connection_pool, "_CLIENT_DEADLINE_S", 0)
    pool = _pool()

    with patch.object(pool, "_grace_close", AsyncMock()) as grace_mock:
        pool.add_frontend_connection(OWNER, "conn-1")
        pool._connections[OWNER] = _fake_connection()
        assert OWNER in pool._client_deadline_handles

        for _ in range(100):
            await asyncio.sleep(0)
            if OWNER in pool._grace_tasks:
                break
        else:
            pytest.fail("real client-deadline timer never fired")

    assert len(pool._frontend_connections.get(OWNER, set())) == 0
    assert OWNER not in pool._client_deadline_handles
    grace_mock.assert_called_once_with(OWNER)


@pytest.mark.asyncio
async def test_stop_is_vetoed_by_an_in_flight_paperclip_run(monkeypatch):
    """has_active_runs is WEBCHAT-ONLY. A trial owner who posts a Teams comment
    and closes their tab has a live Friday run the pool is completely blind to —
    and stopping the container under it kills a legitimate turn.

    The openclaw_gateway adapter opens its WS in execute() and closes it when the
    run ends, so a live `operator` connection IS a run in flight.
    """
    pool = GatewayConnectionPool(management_api=None)
    stop = AsyncMock()

    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=0)  # tab is closed
    conn_svc.has_live_operator_connection = AsyncMock(return_value=True)  # ...but Paperclip is running
    conn_svc.get_live_gateway_conn_token = AsyncMock(return_value=None)
    pool._conn_svc = conn_svc

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch("core.containers.get_ecs_manager", return_value=MagicMock(stop_user_service=stop)),
    ):
        await pool._maybe_stop_trial_container("user_x")

    stop.assert_not_called()


@pytest.mark.asyncio
async def test_stop_proceeds_once_the_paperclip_run_ends(monkeypatch):
    """With no tab AND no operator connection, nobody is using the container."""
    pool = GatewayConnectionPool(management_api=None)
    stop = AsyncMock()

    conn_svc = MagicMock()
    conn_svc.count_live_chat_connections = AsyncMock(return_value=0)
    conn_svc.has_live_operator_connection = AsyncMock(return_value=False)
    conn_svc.get_live_gateway_conn_token = AsyncMock(return_value=None)
    pool._conn_svc = conn_svc

    with (
        patch(
            "core.repositories.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value={"subscription_status": "trial_local"}),
        ),
        patch("core.containers.get_ecs_manager", return_value=MagicMock(stop_user_service=stop)),
    ):
        await pool._maybe_stop_trial_container("user_x")

    stop.assert_awaited_once_with("user_x", reason="trial_idle")
