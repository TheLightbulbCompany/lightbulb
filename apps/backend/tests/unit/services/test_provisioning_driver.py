import asyncio
import os
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

    The lease is granted by default so each test exercises the step it names —
    the lease itself has its own tests below.
    """
    monkeypatch.setattr(mod, "poll_delay", lambda attempts: 0)
    monkeypatch.setattr(mod, "IDLE_RECONCILE_S", 0)
    monkeypatch.setattr(mod.repo, "claim", AsyncMock(side_effect=lambda wid, **kw: _row()))
    monkeypatch.setattr(mod.repo, "release", AsyncMock(return_value=None))
    monkeypatch.setattr(mod.repo, "transition", AsyncMock(return_value=None))
    mod._TASKS.clear()


@pytest.mark.asyncio
async def test_run_advances_until_terminal_then_stops():
    states = [_row("provisioning"), _row("binding"), _row("active")]
    advance = AsyncMock(side_effect=states[1:])
    with patch.object(mod, "advance", advance), patch.object(mod.repo, "get", AsyncMock(return_value=states[0])):
        await mod._run("pwf_1")
    assert advance.await_count == 2


@pytest.mark.asyncio
async def test_run_keeps_driving_an_owner_blocked_state(monkeypatch):
    """`awaiting_user_action` is DRIVEN, not abandoned — the guide says to keep
    polling the Account request and the Payment profile from the server. The
    old behaviour (return) stranded every workflow whose browser never came
    back."""
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    advance = AsyncMock(return_value=_row("awaiting_user_action"))
    with (
        patch.object(mod, "advance", advance),
        patch.object(mod.repo, "get", AsyncMock(return_value=_row("awaiting_user_action"))),
    ):
        await mod._run("pwf_1")
    assert advance.await_count == 3


@pytest.mark.asyncio
async def test_run_keeps_driving_a_stale_row(monkeypatch):
    """`stale` is NOT terminal: the ids are still good and the next owner click
    re-mints the link, so the driver keeps polling it on the slow clock and the
    state machine — not the driver — decides what stale means."""
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    advance = AsyncMock(return_value=_row("stale"))
    with patch.object(mod, "advance", advance), patch.object(mod.repo, "get", AsyncMock(return_value=_row("stale"))):
        await mod._run("pwf_1")
    assert advance.await_count == 3


@pytest.mark.asyncio
async def test_run_polls_an_unapproved_card_on_the_slow_clock(monkeypatch):
    """Approve-from-mobile must resume without a deploy."""
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    reconcile = AsyncMock(return_value=None)
    with (
        patch.object(mod, "_reconcile_approval", reconcile),
        patch.object(mod.repo, "get", AsyncMock(return_value=_row("awaiting_approval"))),
        patch.object(mod, "advance", AsyncMock()) as advance,
    ):
        await mod._run("pwf_1")
    assert reconcile.await_count == 3
    advance.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_stops_once_the_card_is_rejected():
    with (
        patch.object(mod, "_reconcile_approval", AsyncMock(return_value=_row("rejected"))),
        patch.object(mod.repo, "get", AsyncMock(return_value=_row("awaiting_approval"))),
        patch.object(mod, "advance", AsyncMock()) as advance,
    ):
        await mod._run("pwf_1")
    advance.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_survives_one_bad_step_and_stops_after_the_step_bound(monkeypatch):
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    advance = AsyncMock(side_effect=RuntimeError("boom"))
    with patch.object(mod, "advance", advance), patch.object(mod.repo, "get", AsyncMock(return_value=_row())):
        await mod._run("pwf_1")  # must not raise
    assert advance.await_count == 3


@pytest.mark.asyncio
async def test_running_out_of_steps_parks_the_row_for_review(monkeypatch):
    """A row still moving after MAX_STEPS is a logic loop, not patience."""
    monkeypatch.setattr(mod, "MAX_STEPS", 2)
    with (
        patch.object(mod, "advance", AsyncMock(return_value=_row("provisioning"))),
        patch.object(mod.repo, "get", AsyncMock(return_value=_row("provisioning"))),
    ):
        await mod._run("pwf_1")
    assert mod.repo.transition.await_args.kwargs["to_state"] == "requires_review"
    assert mod.repo.transition.await_args.kwargs["fields"] == {"error_code": "driver_steps_exceeded"}


@pytest.mark.asyncio
async def test_run_skips_a_row_another_instance_is_driving(monkeypatch):
    """The lease, not the conditional transition, is what stops two instances
    polling Stripe for one workflow during a rolling deploy's transient N=2."""
    monkeypatch.setattr(mod, "MAX_STEPS", 2)
    monkeypatch.setattr(mod.repo, "claim", AsyncMock(return_value=None))
    with (
        patch.object(mod, "advance", AsyncMock()) as advance,
        patch.object(mod.repo, "get", AsyncMock(return_value=_row())),
    ):
        await mod._run("pwf_1")
    advance.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_renews_the_lease_before_every_step_and_releases_it_at_the_end(monkeypatch):
    monkeypatch.setattr(mod, "MAX_STEPS", 3)
    with (
        patch.object(mod, "advance", AsyncMock(return_value=_row("provisioning"))),
        patch.object(mod.repo, "get", AsyncMock(return_value=_row("provisioning"))),
    ):
        await mod._run("pwf_1")
    assert mod.repo.claim.await_count == 3
    assert mod.repo.claim.await_args.kwargs["owner"] == mod.INSTANCE_ID
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
        task = mod._TASKS.get("pwf_1")
        await asyncio.gather(task, return_exceptions=True)
    assert "pwf_1" not in mod._TASKS


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
async def test_sweep_leaves_rows_another_instance_already_holds(monkeypatch):
    rows = [_row("provisioning", workflow_id="pwf_1"), _row("binding", workflow_id="pwf_2")]
    monkeypatch.setattr(mod.repo, "claim", AsyncMock(side_effect=[None, _row()]))
    with (
        patch.object(mod.repo, "list_open", AsyncMock(return_value=rows)),
        patch.object(mod, "kick", AsyncMock()) as kick,
    ):
        await mod.resume_open_workflows()
    assert [c.args[0] for c in kick.await_args_list] == ["pwf_2"]


@pytest.mark.asyncio
async def test_sweep_never_raises_when_the_table_is_unreachable():
    with patch.object(mod.repo, "list_open", AsyncMock(side_effect=RuntimeError("ddb down"))):
        await mod.resume_open_workflows()  # must not raise — it runs in the lifespan


@pytest.mark.asyncio
async def test_sweep_keeps_going_when_one_row_blows_up(monkeypatch):
    rows = [_row("provisioning", workflow_id="pwf_1"), _row("binding", workflow_id="pwf_2")]
    monkeypatch.setattr(mod.repo, "claim", AsyncMock(side_effect=[RuntimeError("throttled"), _row()]))
    with (
        patch.object(mod.repo, "list_open", AsyncMock(return_value=rows)),
        patch.object(mod, "kick", AsyncMock()) as kick,
    ):
        await mod.resume_open_workflows()
    assert [c.args[0] for c in kick.await_args_list] == ["pwf_2"]


def _admin(approval):
    admin = MagicMock()
    admin._http = object()
    admin.get_approval = AsyncMock(return_value=approval)
    return admin


@pytest.mark.asyncio
async def test_reconcile_resumes_a_card_the_owner_approved_elsewhere():
    admin = _admin({"id": "apr_1", "status": "approved", "payload": {"usage_limit": {"max_amount": 5000}}})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row("connecting_provider"))) as resumed,
    ):
        out = await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1"))
    assert out["state"] == "connecting_provider"
    assert resumed.await_args.kwargs["usage_limit"] == {"max_amount": 5000}


@pytest.mark.asyncio
async def test_reconcile_falls_back_to_the_limit_the_card_was_filed_with():
    """The owner approved without editing the amount."""
    admin = _admin({"id": "apr_1", "status": "approved", "payload": {}})
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
@pytest.mark.parametrize(
    "status,expected",
    [("accepted", "approved"), ("APPROVED", "approved"), ("declined", "rejected"), ("denied", "rejected")],
)
async def test_reconcile_accepts_every_spelling_paperclip_might_use(status, expected):
    """The wire spelling is unverified (see the VERIFY in the plan), so every
    plausible one is honoured rather than one guessed and the rest ignored."""
    admin = _admin({"id": "apr_1", "status": status, "payload": {}})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row("connecting_provider"))) as resumed,
        patch.object(mod, "on_rejected", AsyncMock(return_value=_row("rejected"))) as declined,
    ):
        await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1"))
    assert (resumed.await_count == 1) == (expected == "approved")
    assert (declined.await_count == 1) == (expected == "rejected")


@pytest.mark.asyncio
async def test_reconcile_records_who_approved_when_it_came_from_paperclip():
    admin = _admin({"id": "apr_1", "status": "approved", "payload": {}})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock(return_value=_row("connecting_provider"))) as resumed,
    ):
        await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1"))
    assert resumed.await_args.kwargs["approved_by"] == "paperclip"


@pytest.mark.asyncio
async def test_reconcile_leaves_a_still_pending_card_alone():
    admin = _admin({"id": "apr_1", "status": "pending"})
    with (
        patch.object(mod, "shared_admin_client", return_value=admin),
        patch.object(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie")),
        patch.object(mod, "on_approved", AsyncMock()) as resumed,
    ):
        assert await mod._reconcile_approval(_row("awaiting_approval", approval_id="apr_1")) is None
    resumed.assert_not_awaited()


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
