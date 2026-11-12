import os
from unittest.mock import AsyncMock, MagicMock

import pytest

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

from core.services import photon, text_lines  # noqa: E402
from core.services.billing_service import BillingServiceError  # noqa: E402

pytestmark = pytest.mark.asyncio

PENDING = {"address": "pending:org_1", "kind": "text_pending", "agent_id": "agent_co", "operation_id": "op_1"}
DONE = {"operationId": "op_1", "state": "succeeded", "resourceId": "res_1"}
RUNNING = {"operationId": "op_1", "state": "running"}
LINE_ROW = {"address": "+15551230000", "agent_id": "agent_co", "photon_resource_id": "res_1"}


@pytest.fixture
def m(monkeypatch):
    """One parent mock so call order is assertable across the collaborators."""
    o = MagicMock()
    o.line = AsyncMock(return_value=None)
    o.pending = AsyncMock(return_value=None)
    o.put_pending = AsyncMock(return_value=True)
    o.del_pending = AsyncMock(return_value=True)
    o.claim = AsyncMock(return_value=0)  # the generation claimed under
    o.set_fd = AsyncMock()
    o.record = AsyncMock(return_value=True)
    o.delete = AsyncMock()
    o.request = AsyncMock(return_value={"operationId": "op_1", "state": "pending"})
    o.get_op = AsyncMock(return_value=DONE)
    o.find_user = AsyncMock(return_value=({"id": "usr_1", "handle": "+15551230000"}, None))
    o.set_page = AsyncMock(return_value=True)
    o.cancel = AsyncMock()
    o.release = AsyncMock(return_value=None)  # released now; a 202 still running comes back as the operation
    o.soul = AsyncMock()
    o.mail = AsyncMock()
    o.metric = MagicMock()
    r = text_lines.agent_email_repo
    monkeypatch.setattr(r, "text_line_for_owner", o.line)
    monkeypatch.setattr(r, "pending_line_for_owner", o.pending)
    monkeypatch.setattr(r, "put_pending_line", o.put_pending)
    monkeypatch.setattr(r, "delete_pending_line", o.del_pending)
    monkeypatch.setattr(r, "record_text_line", o.record)
    monkeypatch.setattr(r, "set_pending_page_token", o.set_page)
    monkeypatch.setattr(r, "delete_text_line", o.delete)
    monkeypatch.setattr(text_lines.billing_repo, "claim_front_desk", o.claim)
    monkeypatch.setattr(text_lines.billing_repo, "release_front_desk_claim", o.set_fd)
    monkeypatch.setattr(photon, "request_dedicated_line", o.request)
    monkeypatch.setattr(photon, "get_operation", o.get_op)
    monkeypatch.setattr(photon, "find_line_user", o.find_user)
    monkeypatch.setattr(photon, "cancel_operation", o.cancel)
    monkeypatch.setattr(photon, "release_line", o.release)
    monkeypatch.setattr(text_lines, "refresh_agent_soul", o.soul)
    monkeypatch.setattr(text_lines, "mail_changed", o.mail)
    monkeypatch.setattr(text_lines, "put_metric", o.metric)
    o.co_agent = AsyncMock(return_value="agent_co")
    monkeypatch.setattr(text_lines, "customer_ops_agent_id", o.co_agent)

    # A pending row exists once it has been put.
    async def _put_pending(**kw):
        o.pending.return_value = {**PENDING, "agent_id": kw["agent_id"], "operation_id": kw["operation_id"]}
        return True

    o.put_pending.side_effect = _put_pending

    # The delete is fenced on the operation, as the repo's condition is.
    async def _del_pending(owner_id, *, operation_id):
        current = o.pending.return_value
        if not current or current["operation_id"] != operation_id:
            return False
        o.pending.return_value = None
        return True

    o.del_pending.side_effect = _del_pending

    # The record transaction settles the pending row in the same breath, fenced on the operation too.
    async def _record(**kw):
        current = o.pending.return_value
        if not current or current["operation_id"] != kw["operation_id"]:
            return False
        o.pending.return_value = None
        return True

    o.record.side_effect = _record
    o.account = AsyncMock(return_value={"stripe_subscription_id": "sub_1"})
    monkeypatch.setattr(text_lines.billing_repo, "get_by_owner_id", o.account)
    o.build = AsyncMock(return_value=False)
    monkeypatch.setattr(text_lines.onboarding_brief, "start_front_desk_build_for_line", o.build)
    return o


async def test_ensure_line_fast_path_records_the_line(m):
    assert await text_lines.ensure_line("org_1") == "provisioned"
    m.claim.assert_awaited_once_with("org_1")
    m.request.assert_awaited_once()
    assert m.request.await_args.kwargs["idempotency_key"] == "front-desk:org_1:0"
    m.put_pending.assert_awaited_once_with(owner_id="org_1", agent_id="agent_co", operation_id="op_1")
    m.record.assert_awaited_once_with(
        number="+15551230000",
        owner_id="org_1",
        agent_id="agent_co",
        photon_resource_id="res_1",
        photon_user_id="usr_1",
        operation_id="op_1",
    )
    m.del_pending.assert_not_awaited()  # the record transaction settled it
    assert m.pending.return_value is None
    m.soul.assert_awaited_once_with("org_1", "agent_co")
    m.mail.assert_awaited_once_with("org_1")
    m.metric.assert_called_once_with("text_line.provisioned")


async def test_ensure_line_slow_path_returns_pending_and_keeps_the_row(m):
    m.get_op.return_value = RUNNING
    assert await text_lines.ensure_line("org_1") == "pending"
    m.get_op.assert_awaited_once_with("op_1")  # one look, no poll: the panel's read completes it
    m.record.assert_not_awaited()
    m.del_pending.assert_not_awaited()
    m.set_fd.assert_not_awaited()


async def test_second_delivery_during_pending_does_not_request_again(m):
    m.pending.return_value = PENDING
    m.get_op.return_value = RUNNING
    assert await text_lines.ensure_line("org_1") == "pending"
    m.request.assert_not_awaited()
    m.claim.assert_not_awaited()


async def test_complete_on_read_records_once_across_two_readers(m):
    m.pending.return_value = PENDING
    first = await text_lines.complete_pending("org_1")
    # The second reader read the pending row before the first settled it, then finds the first reader's
    # row for the SAME Photon resource before writing.
    m.pending.return_value = PENDING
    m.line.return_value = LINE_ROW
    second = await text_lines.complete_pending("org_1")
    assert (first, second) == ("provisioned", "provisioned")
    m.record.assert_awaited_once()  # the second reader never writes a second row
    m.release.assert_not_awaited()  # never frees a line another reader recorded


async def test_two_readers_cannot_record_two_lines(m):
    """Reader B saw op_1 fail, but reader A has already replaced the row with a newer request (op_2).
    B's delete is fenced on op_1, so it fails and leaves A's row: no orphaned op_2, no second request."""
    m.pending.return_value = PENDING

    async def _a_moves_on(op_id):
        m.pending.return_value = {**PENDING, "operation_id": "op_2"}
        return {"operationId": op_id, "state": "failed"}

    m.get_op.side_effect = _a_moves_on
    assert await text_lines.complete_pending("org_1") == "pending"
    m.del_pending.assert_awaited_once_with("org_1", operation_id="op_1")
    assert m.pending.return_value["operation_id"] == "op_2"
    m.set_fd.assert_not_awaited()  # A's claim stands
    m.request.assert_not_awaited()
    m.record.assert_not_awaited()


async def test_failed_operation_releases_the_claim_and_raises_from_webhook(m):
    m.pending.return_value = PENDING
    m.get_op.return_value = {"operationId": "op_1", "state": "failed", "error": {"code": "no_inventory"}}
    assert await text_lines.complete_pending("org_1") == "failed"
    m.del_pending.assert_awaited_once_with("org_1", operation_id="op_1")
    m.set_fd.assert_awaited_once_with("org_1")
    m.metric.assert_called_once_with("text_line.provision_failed", dimensions={"code": "no_inventory"})
    m.record.assert_not_awaited()
    m.pending.return_value = PENDING
    with pytest.raises(BillingServiceError, match="line_provision_failed"):
        await text_lines.ensure_line("org_1")  # the webhook caller
    m.request.assert_not_awaited()


async def test_no_pending_row_is_absent(m):
    assert await text_lines.complete_pending("org_1") == "absent"
    m.get_op.assert_not_awaited()


async def test_read_during_the_claim_window_is_pending(m):
    # Claimed, Photon request in flight, no pending row yet: a reader keeps polling.
    m.account.return_value = {"front_desk": True}
    assert await text_lines.complete_pending("org_1") == "pending"
    m.get_op.assert_not_awaited()


async def test_old_claim_is_repaired(m):
    m.claim.side_effect = [None, 3]
    assert await text_lines.ensure_line("org_1") == "provisioned"
    m.set_fd.assert_not_awaited()  # no release, so no generation bump
    assert m.claim.await_args_list[0].kwargs == {}
    assert m.claim.await_args_list[1].kwargs["stale_before"]
    m.request.assert_awaited_once()


async def test_crash_retry_reuses_the_request_key(m):
    m.claim.side_effect = [None, 3]  # the stale retake returns the generation it kept
    await text_lines.ensure_line("org_1")
    m.request.assert_awaited_once()
    assert m.request.await_args.kwargs["idempotency_key"] == "front-desk:org_1:3"


async def test_fresh_claim_is_not_repaired(m):
    m.claim.return_value = None
    assert await text_lines.ensure_line("org_1") == "unchanged"
    m.set_fd.assert_not_awaited()
    m.request.assert_not_awaited()


async def test_request_key_is_stable_until_a_release(m):
    m.claim.return_value = 3
    m.put_pending.side_effect = ValueError("crash after the request")
    for _ in range(2):
        with pytest.raises(ValueError):
            await text_lines.ensure_line("org_1")
    keys = [c.kwargs["idempotency_key"] for c in m.request.await_args_list]
    assert keys == ["front-desk:org_1:3", "front-desk:org_1:3"]


async def test_claim_lost_with_a_row_present_is_unchanged(m):
    m.claim.return_value = None
    m.line.side_effect = [None, {"address": "+1555"}]
    assert await text_lines.ensure_line("org_1") == "unchanged"
    m.request.assert_not_awaited()
    m.set_fd.assert_not_awaited()


async def test_stale_flag_that_stays_claimed_is_unchanged(m):
    m.claim.return_value = None
    assert await text_lines.ensure_line("org_1") == "unchanged"
    assert m.claim.await_count == 2
    m.request.assert_not_awaited()


async def test_request_failure_releases_the_claim_and_raises(m):
    m.request.side_effect = photon.PhotonError(429, "rate_limited")
    with pytest.raises(photon.PhotonError):
        await text_lines.ensure_line("org_1")
    m.set_fd.assert_awaited_once_with("org_1")
    m.metric.assert_called_once_with("text_line.provision_failed", dimensions={"code": "rate_limited"})
    m.put_pending.assert_not_awaited()
    m.cancel.assert_not_awaited()


async def test_pending_row_write_failure_cancels_the_operation(m):
    m.put_pending.side_effect = ValueError("ddb down")
    with pytest.raises(ValueError):
        await text_lines.ensure_line("org_1")
    m.cancel.assert_awaited_once_with("op_1")
    m.release.assert_awaited_once_with("res_1")  # the cancel came too late (the operation is DONE): released
    m.set_fd.assert_awaited_once_with("org_1")


async def test_an_abandoned_request_releases_nothing_when_the_cancel_won(m):
    m.put_pending.side_effect = ValueError("ddb down")
    m.get_op.return_value = {"operationId": "op_1", "state": "cancelled"}
    with pytest.raises(ValueError):
        await text_lines.ensure_line("org_1")
    m.release.assert_not_awaited()
    m.set_fd.assert_awaited_once_with("org_1")


async def test_an_abandoned_line_that_will_not_release_is_counted_and_the_claim_still_clears(m):
    m.put_pending.side_effect = ValueError("ddb down")
    m.release.return_value = {"operationId": "op_r", "state": "failed"}
    with pytest.raises(ValueError):
        await text_lines.ensure_line("org_1")
    m.metric.assert_any_call("text_line.orphan_line")
    m.set_fd.assert_awaited_once_with("org_1")


async def test_transient_record_failure_keeps_the_pending_row(m):
    m.pending.return_value = PENDING
    m.record.side_effect = ValueError("ddb down")
    assert await text_lines.complete_pending("org_1") == "pending"
    assert await text_lines.ensure_line("org_1") == "pending"  # the webhook keeps the row too; a read records it
    m.release.assert_not_awaited()
    m.del_pending.assert_not_awaited()
    m.set_fd.assert_not_awaited()
    m.request.assert_not_awaited()


async def test_release_that_removed_the_pending_row_makes_a_late_record_fail(m):
    m.pending.return_value = PENDING

    async def _release_won(**kw):
        m.pending.return_value = None  # the release deleted the pending row during the slow find_line_user
        return False

    m.record.side_effect = _release_won
    assert await text_lines.complete_pending("org_1") == "pending"  # deferred, nothing written
    m.release.assert_not_awaited()
    m.del_pending.assert_not_awaited()
    assert await text_lines.complete_pending("org_1") == "absent"  # the claim flag is clear


async def test_unknown_line_user_keeps_the_pending_row(m):
    m.pending.return_value = PENDING
    m.find_user.return_value = (None, None)
    assert await text_lines.complete_pending("org_1") == "pending"
    m.release.assert_not_awaited()
    m.del_pending.assert_not_awaited()
    m.record.assert_not_awaited()
    m.set_page.assert_not_awaited()  # nothing to keep: the walk ended, and the row held no place


async def test_a_long_user_list_is_walked_across_looks_not_restarted(m):
    """Past the page cap the walk keeps its place on the pending row, and the next look continues from it."""
    m.pending.return_value = PENDING
    m.find_user.return_value = (None, "t.11")
    assert await text_lines.complete_pending("org_1") == "pending"
    m.set_page.assert_awaited_once_with("org_1", operation_id="op_1", page_token="t.11")
    m.pending.return_value = {**PENDING, "page_token": "t.11"}
    m.find_user.return_value = ({"id": "usr_1", "handle": "+15551230000"}, None)
    assert await text_lines.complete_pending("org_1") == "provisioned"
    assert m.find_user.await_args.kwargs["page_token"] == "t.11"
    m.record.assert_awaited_once()
    # The list ended without it: the place is cleared, so the next look starts over (the user may be new).
    m.set_page.reset_mock()
    m.pending.return_value = {**PENDING, "page_token": "t.11"}  # a fresh look at a row still holding a place
    m.find_user.return_value = (None, None)
    assert await text_lines.complete_pending("org_1") == "pending"
    m.set_page.assert_awaited_once_with("org_1", operation_id="op_1", page_token=None)


async def test_line_user_without_a_handle_is_deferred_not_released(m):
    m.pending.return_value = PENDING
    m.find_user.return_value = ({"id": "usr_1", "handle": None}, None)
    assert await text_lines.complete_pending("org_1") == "pending"
    assert await text_lines.ensure_line("org_1") == "pending"
    m.record.assert_not_awaited()
    m.release.assert_not_awaited()
    m.del_pending.assert_not_awaited()


async def test_failure_but_row_already_recorded_is_provisioned(m):
    m.pending.return_value = PENDING
    m.record.side_effect = ValueError("timeout after the write landed")
    m.line.side_effect = [None, LINE_ROW]
    assert await text_lines.complete_pending("org_1") == "provisioned"
    m.release.assert_not_awaited()


async def test_conflicting_existing_line_releases_the_extra_one(m):
    m.pending.return_value = PENDING
    m.line.return_value = {**LINE_ROW, "photon_resource_id": "res_other"}
    assert await text_lines.complete_pending("org_1") == "provisioned"
    m.record.assert_not_awaited()  # re-read before the write: the existing line stands
    m.release.assert_awaited_once_with("res_1")
    m.del_pending.assert_awaited_once_with("org_1", operation_id="op_1")
    m.set_fd.assert_not_awaited()


async def test_a_surplus_release_not_done_keeps_the_pending_row(m):
    """The pending row is the only record of the surplus line; it stays until Photon has released it."""
    m.pending.return_value = PENDING
    m.line.return_value = {**LINE_ROW, "photon_resource_id": "res_other"}
    m.release.return_value = {"operationId": "op_r", "state": "running"}  # a 202 release still running
    assert await text_lines.complete_pending("org_1") == "provisioned"
    m.release.assert_awaited_once_with("res_1")
    m.del_pending.assert_not_awaited()
    m.metric.assert_any_call("text_line.surplus_release_deferred")
    m.release.return_value = None
    m.release.side_effect = photon.PhotonError(500, "http_500")
    assert await text_lines.complete_pending("org_1") == "provisioned"
    m.del_pending.assert_not_awaited()


async def test_no_customer_operations_is_no_agent_without_a_request(m):
    m.co_agent.return_value = None
    assert await text_lines.ensure_line("org_1") == "no_agent"
    m.metric.assert_called_once_with("text_line.no_front_desk_agent")
    m.claim.assert_not_awaited()
    m.request.assert_not_awaited()


async def test_ensure_line_unchanged_when_row_exists(m):
    m.line.return_value = {"address": "+1555"}
    assert await text_lines.ensure_line("org_1") == "unchanged"
    m.claim.assert_not_awaited()
    m.request.assert_not_awaited()


async def test_release_cancels_a_pending_operation(m):
    m.pending.return_value = PENDING
    m.account.return_value = {"front_desk": True}  # a pending row stands on a claim
    m.get_op.return_value = {"operationId": "op_1", "state": "cancelled"}
    assert await text_lines.release_line("org_1") == "released"
    m.cancel.assert_awaited_once_with("op_1")
    m.del_pending.assert_awaited_once_with("org_1", operation_id="op_1")
    m.set_fd.assert_awaited_once_with("org_1")
    m.release.assert_not_awaited()


async def test_release_keeps_the_row_while_the_operation_still_runs(m):
    m.pending.return_value = PENDING
    m.get_op.return_value = RUNNING
    with pytest.raises(BillingServiceError, match="line_release_pending"):
        await text_lines.release_line("org_1")
    m.del_pending.assert_not_awaited()
    m.set_fd.assert_not_awaited()


async def test_release_of_a_finished_operation_goes_by_resource(m):
    """The cancel lost the race: the line is released by its resource, never recorded first."""
    m.pending.return_value = PENDING
    m.account.return_value = {"front_desk": True}
    m.get_op.return_value = DONE
    assert await text_lines.release_line("org_1") == "released"
    m.release.assert_awaited_once_with("res_1")
    m.find_user.assert_not_awaited()
    m.record.assert_not_awaited()
    m.del_pending.assert_awaited_once_with("org_1", operation_id="op_1")
    m.set_fd.assert_awaited_once_with("org_1")


async def test_release_of_a_finished_operation_raises_and_keeps_the_row_when_photon_fails(m):
    m.pending.return_value = PENDING
    m.get_op.return_value = DONE
    m.release.side_effect = photon.PhotonError(500, "http_500")
    with pytest.raises(photon.PhotonError):
        await text_lines.release_line("org_1")
    m.del_pending.assert_not_awaited()
    m.set_fd.assert_not_awaited()


async def test_release_line_row_calls_photon_and_clears(m):
    m.line.return_value = LINE_ROW
    assert await text_lines.release_line("org_1") == "released"
    m.release.assert_awaited_once_with("res_1")
    m.delete.assert_awaited_once_with("+15551230000")
    m.set_fd.assert_awaited_once_with("org_1")
    m.soul.assert_awaited_once_with("org_1", "agent_co")


async def test_release_failure_keeps_the_row_and_raises(m):
    m.line.return_value = LINE_ROW
    m.release.side_effect = photon.PhotonError(500, "http_500")
    with pytest.raises(photon.PhotonError):
        await text_lines.release_line("org_1")
    m.delete.assert_not_awaited()
    m.set_fd.assert_not_awaited()


async def test_a_release_still_running_keeps_the_row(m):
    """A 202 release is an operation; the row and the claim stay until it has succeeded."""
    m.line.return_value = LINE_ROW
    m.release.return_value = {"operationId": "op_r", "state": "running"}
    with pytest.raises(BillingServiceError, match="line_release_pending"):
        await text_lines.release_line("org_1")
    m.delete.assert_not_awaited()
    m.set_fd.assert_not_awaited()


async def test_a_failed_release_keeps_the_row_and_is_counted(m):
    m.line.return_value = LINE_ROW
    m.release.return_value = {"operationId": "op_r", "state": "failed", "error": {"code": "carrier"}}
    with pytest.raises(BillingServiceError, match="line_release_failed"):
        await text_lines.release_line("org_1")
    m.delete.assert_not_awaited()
    m.set_fd.assert_not_awaited()
    m.metric.assert_any_call("text_line.release_failed", dimensions={"code": "carrier"})


async def test_release_absent_clears_flag(m):
    m.account.return_value = {"front_desk": True}
    assert await text_lines.release_line("org_1") == "absent"
    m.set_fd.assert_awaited_once_with("org_1")


async def test_release_absent_without_a_flag_writes_nothing(m):
    assert await text_lines.release_line("org_1") == "absent"
    m.set_fd.assert_not_awaited()
    m.delete.assert_not_awaited()
    m.release.assert_not_awaited()


def _stub_retrieve(monkeypatch, status, items, others: dict[str, str] | None = None):
    """``sub_1`` answers ``status``; an id in ``others`` answers its own status; any other id is unknown to Stripe."""

    def retrieve(sid):
        if sid == "sub_1":
            return {"id": sid, "status": status, "items": {"data": items}}
        if others and sid in others:
            return {"id": sid, "status": others[sid], "items": {"data": []}}
        raise text_lines.stripe.error.InvalidRequestError(f"No such subscription: {sid}", "id")

    monkeypatch.setattr(text_lines.stripe.Subscription, "retrieve", retrieve)


async def _sync(monkeypatch, status, items):
    _stub_retrieve(monkeypatch, status, items)
    return await text_lines.sync_front_desk("org_1", subscription_id="sub_1")


async def test_sync_with_item_on_live_provisions(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: i.get("fd", False))
    assert await _sync(monkeypatch, "active", [{"fd": True}]) == "provisioned"


async def test_sync_failed_operation_raises_for_stripe_retry(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: True)
    m.pending.return_value = PENDING
    m.get_op.return_value = {"operationId": "op_1", "state": "failed"}
    with pytest.raises(BillingServiceError, match="line_provision_failed"):
        await _sync(monkeypatch, "active", [{}])


async def test_sync_without_item_releases_existing_line(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: False)
    m.line.return_value = LINE_ROW
    assert await _sync(monkeypatch, "active", [{}]) == "released"
    m.release.assert_awaited_once_with("res_1")


async def test_sync_without_item_and_no_line_is_absent(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: False)
    assert await _sync(monkeypatch, "active", []) == "absent"
    m.release.assert_not_awaited()


async def test_sync_canceled_releases(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: True)
    m.line.return_value = LINE_ROW
    assert await _sync(monkeypatch, "canceled", [{}]) == "released"


@pytest.mark.parametrize("status", ["incomplete_expired", "incomplete", "past_due"])
async def test_sync_other_statuses_are_skipped(m, monkeypatch, status):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: True)
    assert await _sync(monkeypatch, status, [{}]) == "skipped"
    m.request.assert_not_awaited()
    m.release.assert_not_awaited()


@pytest.mark.parametrize(
    ("stored_now", "previous"),
    [("sub_new", None), ("sub_1", "sub_new")],  # the current id as stored now, or before the event's own write
)
async def test_stale_cancel_of_an_old_subscription_does_not_release(m, monkeypatch, stored_now, previous):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: True)
    _stub_retrieve(monkeypatch, "canceled", [{}], others={"sub_new": "active"})
    m.account.return_value = {"stripe_subscription_id": stored_now}
    m.line.return_value = LINE_ROW
    got = await text_lines.sync_front_desk("org_1", subscription_id="sub_1", previous_subscription_id=previous)
    assert got == "skipped_superseded"
    m.release.assert_not_awaited()
    m.delete.assert_not_awaited()


@pytest.mark.parametrize("others", [{"sub_old": "canceled"}, {}])  # the stored id is dead too, or unknown to Stripe
async def test_a_cancel_is_not_superseded_by_a_stored_id_that_is_itself_dead(m, monkeypatch, others):
    # Reordered events: a stale cancel of OLD sub_old overwrote the row's id, then the live sub_1 is deleted.
    # The stored id names no live subscription, so nothing supersedes the cancel and the line is released.
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: True)
    _stub_retrieve(monkeypatch, "canceled", [{}], others=others)
    m.account.return_value = {"stripe_subscription_id": "sub_old"}
    m.line.return_value = LINE_ROW
    got = await text_lines.sync_front_desk("org_1", subscription_id="sub_1", previous_subscription_id="sub_old")
    assert got == "released"
    m.release.assert_awaited_once()


async def test_new_active_subscription_provisions_even_when_an_old_id_was_stored(m, monkeypatch):
    monkeypatch.setattr(text_lines, "_is_front_desk_item", lambda i: True)
    _stub_retrieve(monkeypatch, "active", [{}])
    m.account.return_value = {"stripe_subscription_id": "sub_old"}
    got = await text_lines.sync_front_desk("org_1", subscription_id="sub_1", previous_subscription_id="sub_old")
    assert got == "provisioned"
    m.request.assert_awaited_once()


async def test_unlink_member_drops_every_phone_the_member_holds(m, monkeypatch):
    drop = AsyncMock()
    monkeypatch.setattr(text_lines.agent_email_repo, "drop_member_phones", drop)
    await text_lines.unlink_member("org_1", "user_a")  # no line: nothing to drop
    drop.assert_not_awaited()
    m.line.return_value = LINE_ROW
    await text_lines.unlink_member("org_1", "user_a")
    drop.assert_awaited_once_with(LINE_ROW, "user_a")


async def test_provisioned_line_starts_the_build(m):
    assert await text_lines.ensure_line("org_1") == "provisioned"
    m.build.assert_awaited_once_with("org_1")


async def test_build_failure_does_not_fail_the_provision(m):
    m.build.side_effect = RuntimeError("messaging down")
    assert await text_lines.ensure_line("org_1") == "provisioned"
