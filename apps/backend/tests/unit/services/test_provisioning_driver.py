import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import provisioning_driver as mod  # noqa: E402


def _row(state="provisioning", **over):
    base = {"workflow_id": "pwf_1", "owner_id": "org_a", "state": state, "attempts": 0}
    base.update(over)
    return base


@pytest.fixture(autouse=True)
def _fast(monkeypatch):
    """No real sleeping in tests; the delay policy is tested in the workflow.

    `claim` is granted by default and echoes the row `repo.get` would return —
    the real one re-reads the row (ReturnValues=ALL_NEW), and the loop drives
    what the claim hands back. The lease itself has its own tests below.
    """

    async def _claim(workflow_id, **_kw):
        return await mod.repo.get(workflow_id)

    monkeypatch.setattr(mod, "poll_delay", lambda attempts: 0)
    monkeypatch.setattr(mod, "IDLE_RECONCILE_S", 0)
    monkeypatch.setattr(mod.repo, "claim", AsyncMock(side_effect=_claim))
    monkeypatch.setattr(mod.repo, "release", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.repo, "transition", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.repo, "get", AsyncMock(return_value=_row()))
    mod._TASKS.clear()


@pytest.mark.asyncio
async def test_run_advances_until_terminal_then_stops():
    advance = AsyncMock(side_effect=[_row("binding"), _row("active")])
    with patch.object(mod, "advance", advance):
        await mod._run("pwf_1")
    assert advance.await_count == 2


@pytest.mark.asyncio
async def test_run_keeps_driving_an_owner_blocked_state():
    """`awaiting_user_action` is DRIVEN, not abandoned — the guide says to keep
    polling the Account request and the Payment profile from the server. The
    old behaviour (return) stranded every workflow whose browser never came
    back."""
    mod.repo.get.return_value = _row("awaiting_user_action")
    advance = AsyncMock(side_effect=[_row("awaiting_user_action")] * 3 + [_row("active")])
    with patch.object(mod, "advance", advance):
        await mod._run("pwf_1")
    assert advance.await_count == 4
    mod.repo.transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_keeps_driving_a_stale_row():
    """`stale` is NOT terminal: the ids are still good and the next owner click
    re-mints the link, so the driver keeps polling it on the slow clock and the
    state machine — not the driver — decides what stale means."""
    mod.repo.get.return_value = _row("stale")
    advance = AsyncMock(side_effect=[_row("stale")] * 3 + [_row("active")])
    with patch.object(mod, "advance", advance):
        await mod._run("pwf_1")
    assert advance.await_count == 4


@pytest.mark.asyncio
async def test_run_polls_an_unapproved_card_on_the_slow_clock():
    """Approve-from-mobile must resume without a deploy."""
    mod.repo.get.return_value = _row("awaiting_approval")
    reconcile = AsyncMock(side_effect=[None, None, _row("rejected")])
    with patch.object(mod, "_reconcile_approval", reconcile), patch.object(mod, "advance", AsyncMock()) as advance:
        await mod._run("pwf_1")
    assert reconcile.await_count == 3
    advance.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_stops_once_the_card_is_rejected():
    mod.repo.get.return_value = _row("awaiting_approval")
    with (
        patch.object(mod, "_reconcile_approval", AsyncMock(return_value=_row("rejected"))),
        patch.object(mod, "advance", AsyncMock()) as advance,
    ):
        await mod._run("pwf_1")
    advance.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_step_backs_off_instead_of_burning_the_budget(monkeypatch):
    """A blip that looped at full speed would spend all 400 steps in
    milliseconds and park a workflow whose only problem was a 500."""
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    advance = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(mod, "advance", advance), patch("asyncio.sleep", AsyncMock()) as slept:
        await mod._run("pwf_1")  # must not raise
    assert advance.await_count == 3
    assert slept.await_count == 3


@pytest.mark.asyncio
async def test_running_out_of_fast_steps_parks_the_row_for_review(monkeypatch):
    """A row still polling after MAX_STEPS is a logic loop, not patience."""
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    with patch.object(mod, "advance", AsyncMock(return_value=_row("provisioning"))) as advance:
        await mod._run("pwf_1")
    assert advance.await_count == 3
    assert mod.repo.transition.await_args.kwargs["to_state"] == "requires_review"
    assert mod.repo.transition.await_args.kwargs["fields"] == {"error_code": "driver_steps_exceeded"}


@pytest.mark.asyncio
async def test_slow_ticks_do_not_count_toward_the_step_bound(monkeypatch):
    """An owner is allowed a week to answer; MAX_STEPS bounds POLLING, and
    counting a card the owner has not opened would park healthy workflows."""
    monkeypatch.setattr(mod, "MAX_STEPS", 1)
    mod.repo.get.return_value = _row("awaiting_approval")
    reconcile = AsyncMock(side_effect=[None] * 5 + [_row("rejected")])
    with patch.object(mod, "_reconcile_approval", reconcile):
        await mod._run("pwf_1")
    assert reconcile.await_count == 6
    mod.repo.transition.assert_not_awaited()


def _aged(days: float, state="awaiting_approval"):
    created_at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    return _row(state, created_at=created_at)


@pytest.mark.asyncio
async def test_the_wall_clock_parks_a_row_nobody_ever_came_back_to():
    """The bound that covers waiting: just past the state machine's own 7-day
    stale deadline, so an owner-blocked row cannot poll forever. Measured from
    the row, so it survives the restart that produced this task — a
    task-anchored bound would reset on every deploy and never fire."""
    mod.repo.get.return_value = _aged(9)
    with patch.object(mod, "_reconcile_approval", AsyncMock(return_value=None)) as reconcile:
        await mod._run("pwf_1")
    reconcile.assert_not_awaited()  # parked on the FIRST tick after the restart
    assert mod.repo.transition.await_args.kwargs["fields"] == {"error_code": "driver_wall_exceeded"}


@pytest.mark.asyncio
async def test_a_row_still_inside_the_wall_clock_keeps_being_driven():
    mod.repo.get.return_value = _aged(1)
    with patch.object(mod, "_reconcile_approval", AsyncMock(side_effect=[None, _row("rejected")])) as reconcile:
        await mod._run("pwf_1")
    assert reconcile.await_count == 2
    mod.repo.transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_waits_out_a_lease_another_instance_holds(monkeypatch):
    """The lease, not the conditional transition, is what stops two instances
    polling Stripe for one workflow during a rolling deploy's transient N=2 —
    and a task that only ever waited must not park a row it never drove."""
    monkeypatch.setattr(mod, "MAX_STEPS", 1)
    monkeypatch.setattr(mod.repo, "claim", AsyncMock(side_effect=[None, None, _row("active")]))
    with patch.object(mod, "advance", AsyncMock()) as advance:
        await mod._run("pwf_1")
    advance.assert_not_awaited()
    mod.repo.transition.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_drives_the_row_the_claim_returns(monkeypatch):
    """The claim re-reads the row, so a move the owner route made while we were
    sleeping is seen on this tick instead of being driven from a stale copy."""
    monkeypatch.setattr(mod.repo, "claim", AsyncMock(return_value=_row("binding")))
    mod.repo.get.return_value = _row("provisioning")
    with patch.object(mod, "advance", AsyncMock(return_value=_row("active"))) as advance:
        await mod._run("pwf_1")
    assert advance.await_args.args[0]["state"] == "binding"


@pytest.mark.asyncio
async def test_run_renews_the_lease_every_iteration_and_releases_it_at_the_end(monkeypatch):
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    with patch.object(mod, "advance", AsyncMock(return_value=_row("provisioning"))):
        await mod._run("pwf_1")
    # Three worked steps plus the tick that found the budget spent.
    assert mod.repo.claim.await_count == 4
    assert mod.repo.claim.await_args.kwargs["owner"] == mod.INSTANCE_ID
    assert mod.repo.claim.await_args.kwargs["ttl_s"] == mod.LEASE_S
    mod.repo.release.assert_awaited_once_with("pwf_1", owner=mod.INSTANCE_ID)


@pytest.mark.asyncio
async def test_kick_is_idempotent_for_one_workflow():
    started = asyncio.Event()

    async def _slow(_wid):
        started.set()
        await asyncio.sleep(0.05)

    with patch.object(mod, "_run", _slow):
        await mod.kick("pwf_1")
        await started.wait()
        await mod.kick("pwf_1")
        assert len(mod._TASKS) == 1
        await asyncio.gather(*list(mod._TASKS.values()))


@pytest.mark.asyncio
async def test_kick_survives_a_task_that_raises():
    """A dead task must leave no entry behind, or the workflow could never be
    kicked again for the life of the process."""

    async def _boom(_wid):
        raise RuntimeError("boom")

    with patch.object(mod, "_run", _boom):
        await mod.kick("pwf_1")
        await asyncio.gather(mod._TASKS.get("pwf_1"), return_exceptions=True)
    assert "pwf_1" not in mod._TASKS


@pytest.mark.asyncio
async def test_a_step_that_keeps_failing_still_releases_and_deregisters(monkeypatch):
    """The lease must not outlive the task that took it, whatever went wrong."""
    monkeypatch.setattr(mod, "MAX_STEPS", 1)
    with patch.object(mod, "advance", AsyncMock(side_effect=RuntimeError("boom"))):
        await mod.kick("pwf_1")
        await asyncio.gather(*list(mod._TASKS.values()), return_exceptions=True)
    mod.repo.release.assert_awaited_once_with("pwf_1", owner=mod.INSTANCE_ID)
    assert "pwf_1" not in mod._TASKS


@pytest.mark.asyncio
async def test_shutdown_cancels_every_driver_and_hands_its_lease_back(monkeypatch):
    """The outgoing instance must release, not expire: otherwise the incoming
    one is locked out of the rows it just swept in to take over."""
    monkeypatch.setattr(mod, "IDLE_RECONCILE_S", 0.05)
    mod.repo.get.return_value = _row("awaiting_approval")
    with patch.object(mod, "_reconcile_approval", AsyncMock(return_value=None)):
        await mod.kick("pwf_1")
        await asyncio.sleep(0)  # let the task reach its first sleep
        await mod.cancel_all()
    mod.repo.release.assert_awaited_once_with("pwf_1", owner=mod.INSTANCE_ID)
    assert mod._TASKS == {}


@pytest.mark.asyncio
async def test_sweep_kicks_every_open_row():
    rows = [_row("provisioning", workflow_id="pwf_1"), _row("binding", workflow_id="pwf_2")]
    with (
        patch.object(mod.repo, "list_open", AsyncMock(return_value=rows)),
        patch.object(mod, "kick", AsyncMock()) as kick,
    ):
        await mod.resume_open_workflows()
    assert [c.args[0] for c in kick.await_args_list] == ["pwf_1", "pwf_2"]


@pytest.mark.asyncio
async def test_sweep_kicks_rows_another_instance_still_holds(monkeypatch):
    """The outgoing instance is still alive and renewing when the incoming one
    sweeps — a sweep that skipped held rows would skip every row, and it only
    runs once. Waiting for the lease is the task's job."""
    rows = [_row("provisioning", workflow_id="pwf_1"), _row("binding", workflow_id="pwf_2")]
    monkeypatch.setattr(mod.repo, "claim", AsyncMock(return_value=None))
    with (
        patch.object(mod.repo, "list_open", AsyncMock(return_value=rows)),
        patch.object(mod, "kick", AsyncMock()) as kick,
    ):
        await mod.resume_open_workflows()
    assert [c.args[0] for c in kick.await_args_list] == ["pwf_1", "pwf_2"]


@pytest.mark.asyncio
async def test_sweep_never_raises_when_the_table_is_unreachable():
    with patch.object(mod.repo, "list_open", AsyncMock(side_effect=RuntimeError("ddb down"))):
        await mod.resume_open_workflows()  # must not raise — it runs in the lifespan


@pytest.mark.asyncio
async def test_sweep_keeps_going_when_one_row_blows_up():
    rows = [_row("provisioning", workflow_id="pwf_1"), _row("binding", workflow_id="pwf_2")]
    with (
        patch.object(mod.repo, "list_open", AsyncMock(return_value=rows)),
        patch.object(mod, "kick", AsyncMock(side_effect=[RuntimeError("boom"), None])) as kick,
    ):
        await mod.resume_open_workflows()
    assert [c.args[0] for c in kick.await_args_list] == ["pwf_1", "pwf_2"]


def _admin(approval):
    admin = MagicMock()
    admin._http = object()
    admin.get_approval = AsyncMock(return_value=approval)
    return admin


@pytest.mark.asyncio
async def test_reconcile_resumes_a_card_the_owner_approved_elsewhere():
    admin = _admin({"id": "apr_1", "status": "approved"})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row("connecting_provider"))),
    ):
        out = await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1"))
    assert out["state"] == "connecting_provider"


@pytest.mark.asyncio
async def test_reconcile_uses_the_limit_the_card_was_filed_with():
    """A Paperclip approver cannot edit a card's payload, so this path can only
    honour the filed limit — an edited one arrives through the approve route."""
    admin = _admin({"id": "apr_1", "status": "approved", "payload": {"usage_limit": {"max_amount": 5000}}})
    row = _row("awaiting_approval", approval_id="apr_1", usage_limit={"max_amount": 2000})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row("connecting_provider"))) as resumed,
    ):
        await mod._reconcile_approval(row)
    assert resumed.await_args.kwargs["usage_limit"] == {"max_amount": 2000}


@pytest.mark.asyncio
async def test_reconcile_records_a_rejection_as_a_terminal_state():
    """A declined card must stop the workflow, not poll for a week."""
    admin = _admin({"id": "apr_1", "status": "rejected"})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock()) as resumed,
        patch.object(mod, "on_rejected", AsyncMock(return_value=_row("rejected"))) as declined,
    ):
        out = await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1"))
    assert out["state"] == "rejected"
    declined.assert_awaited_once_with("pwf_1")
    resumed.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_records_who_approved_when_it_came_from_paperclip():
    admin = _admin({"id": "apr_1", "status": "approved"})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row("connecting_provider"))) as resumed,
    ):
        await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1"))
    assert resumed.await_args.kwargs["approved_by"] == "paperclip"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["pending", "revision_requested", None])
async def test_reconcile_leaves_an_undecided_card_alone(status):
    admin = _admin({"id": "apr_1", "status": status})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock()) as resumed,
        patch.object(mod, "on_rejected", AsyncMock()) as declined,
    ):
        assert await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1")) is None
    resumed.assert_not_awaited()
    declined.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_treats_a_paperclip_outage_as_transient():
    """A Paperclip 500 must leave the row alone and let the next tick retry."""
    from core.services.paperclip_admin_client import PaperclipApiError

    admin = MagicMock()
    admin._http = object()
    admin.get_approval = AsyncMock(side_effect=PaperclipApiError("GET /api/approvals/apr_1 -> 500", 500, "boom"))
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock()) as resumed,
    ):
        assert await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1")) is None
    resumed.assert_not_awaited()


@pytest.mark.asyncio
async def test_reconcile_does_nothing_without_a_card_to_read():
    with patch.object(mod, "shared_admin_client") as client:
        assert await mod._reconcile_approval(_row("awaiting_approval")) is None
    client.assert_not_called()
