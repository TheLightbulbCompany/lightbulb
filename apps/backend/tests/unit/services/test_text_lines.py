import os
from types import SimpleNamespace
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


async def _ret(v):
    return v


@pytest.fixture
def m(monkeypatch):
    """One parent mock so call order is assertable across the collaborators."""
    o = MagicMock()
    o.line = AsyncMock(return_value=None)
    o.pending = AsyncMock(return_value=None)
    o.put_pending = AsyncMock(return_value=True)
    o.del_pending = AsyncMock(return_value=True)
    o.claim = AsyncMock(return_value=True)
    o.set_fd = AsyncMock()
    o.put = AsyncMock(return_value={"address": "+15551230000"})
    o.delete = AsyncMock()
    o.request = AsyncMock(return_value={"operationId": "op_1", "state": "pending"})
    o.get_op = AsyncMock(return_value=DONE)
    o.find_user = AsyncMock(return_value={"id": "usr_1", "handle": "+15551230000"})
    o.cancel = AsyncMock()
    o.release = AsyncMock()
    o.soul = AsyncMock()
    o.mail = AsyncMock()
    o.metric = MagicMock()
    o.sleep = AsyncMock()
    r = text_lines.agent_email_repo
    monkeypatch.setattr(r, "text_line_for_owner", o.line)
    monkeypatch.setattr(r, "pending_line_for_owner", o.pending)
    monkeypatch.setattr(r, "put_pending_line", o.put_pending)
    monkeypatch.setattr(r, "delete_pending_line", o.del_pending)
    monkeypatch.setattr(r, "put_text_line", o.put)
    monkeypatch.setattr(r, "delete_text_line", o.delete)
    monkeypatch.setattr(text_lines.billing_repo, "claim_front_desk", o.claim)
    monkeypatch.setattr(text_lines.billing_repo, "set_front_desk", o.set_fd)
    monkeypatch.setattr(photon, "request_dedicated_line", o.request)
    monkeypatch.setattr(photon, "get_operation", o.get_op)
    monkeypatch.setattr(photon, "find_line_user", o.find_user)
    monkeypatch.setattr(photon, "cancel_operation", o.cancel)
    monkeypatch.setattr(photon, "release_line", o.release)
    monkeypatch.setattr(text_lines, "refresh_agent_soul", o.soul)
    monkeypatch.setattr(text_lines, "mail_changed", o.mail)
    monkeypatch.setattr(text_lines, "put_metric", o.metric)
    monkeypatch.setattr(text_lines.asyncio, "sleep", o.sleep)
    row = SimpleNamespace(starter_agent_ids={"customer_operations": "agent_co"})
    repo = MagicMock()
    repo.resolve_owner_row = AsyncMock(return_value=row)
    o.roster_row = row
    monkeypatch.setattr(text_lines, "PaperclipRepo", lambda **kw: repo)

    # A pending row exists once it has been put.
    async def _put_pending(**kw):
        o.pending.return_value = {**PENDING, "agent_id": kw["agent_id"], "operation_id": kw["operation_id"]}
        return True

    o.put_pending.side_effect = _put_pending
    return o


async def test_ensure_line_fast_path_records_the_line(m):
    assert await text_lines.ensure_line("org_1") == "provisioned"
    m.claim.assert_awaited_once_with("org_1")
    m.request.assert_awaited_once()
    assert m.request.await_args.kwargs["idempotency_key"].startswith("front-desk:org_1:")
    m.put_pending.assert_awaited_once_with(owner_id="org_1", agent_id="agent_co", operation_id="op_1")
    m.put.assert_awaited_once_with(
        number="+15551230000",
        owner_id="org_1",
        agent_id="agent_co",
        photon_resource_id="res_1",
        photon_user_id="usr_1",
    )
    m.del_pending.assert_awaited_once_with("org_1")
    m.soul.assert_awaited_once_with("org_1", "agent_co")
    m.mail.assert_awaited_once_with("org_1")
    m.metric.assert_called_once_with("text_line.provisioned")


async def test_ensure_line_slow_path_returns_pending_and_keeps_the_row(m):
    m.get_op.return_value = RUNNING
    assert await text_lines.ensure_line("org_1") == "pending"
    assert m.sleep.await_count == 5  # polled for the 10 s budget, every 2 s
    m.put.assert_not_awaited()
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
    wins = iter([True, False])
    m.del_pending.side_effect = lambda owner: _ret(next(wins))
    first = await text_lines.complete_pending("org_1")
    # The second reader's put loses to the first reader's row for the SAME Photon resource.
    m.put.return_value = None
    m.line.return_value = LINE_ROW
    second = await text_lines.complete_pending("org_1")
    assert (first, second) == ("provisioned", "provisioned")
    m.release.assert_not_awaited()  # never frees a line another reader recorded


async def test_failed_operation_releases_the_claim_and_raises_from_webhook(m):
    m.pending.return_value = PENDING
    m.get_op.return_value = {"operationId": "op_1", "state": "failed", "error": {"code": "no_inventory"}}
    with pytest.raises(BillingServiceError, match="line_provision_failed"):
        await text_lines.complete_pending("org_1", from_webhook=True)
    m.del_pending.assert_awaited_once_with("org_1")
    m.set_fd.assert_awaited_once_with("org_1", False)
    m.metric.assert_called_once_with("text_line.provision_failed", dimensions={"code": "no_inventory"})
    m.put.assert_not_awaited()


async def test_failed_operation_returns_absent_from_read(m):
    m.pending.return_value = PENDING
    m.get_op.return_value = {"operationId": "op_1", "state": "cancelled", "reason": "by_us"}
    assert await text_lines.complete_pending("org_1") == "absent"
    m.set_fd.assert_awaited_once_with("org_1", False)
    m.metric.assert_called_once_with("text_line.provision_failed", dimensions={"code": "by_us"})


async def test_no_pending_row_is_absent(m):
    assert await text_lines.complete_pending("org_1") == "absent"
    m.get_op.assert_not_awaited()


async def test_stale_flag_without_rows_is_repaired(m):
    m.claim.side_effect = [False, True]
    assert await text_lines.ensure_line("org_1") == "provisioned"
    m.set_fd.assert_any_await("org_1", False)
    assert m.claim.await_count == 2
    m.request.assert_awaited_once()


async def test_claim_lost_with_a_row_present_is_unchanged(m):
    m.claim.return_value = False
    m.line.side_effect = [None, {"address": "+1555"}]
    assert await text_lines.ensure_line("org_1") == "unchanged"
    m.request.assert_not_awaited()
    m.set_fd.assert_not_awaited()


async def test_stale_flag_that_stays_claimed_is_unchanged(m):
    m.claim.return_value = False
    assert await text_lines.ensure_line("org_1") == "unchanged"
    assert m.claim.await_count == 2
    m.request.assert_not_awaited()


async def test_request_failure_releases_the_claim_and_raises(m):
    m.request.side_effect = photon.PhotonError(429, "rate_limited")
    with pytest.raises(photon.PhotonError):
        await text_lines.ensure_line("org_1")
    m.set_fd.assert_awaited_once_with("org_1", False)
    m.metric.assert_called_once_with("text_line.provision_failed", dimensions={"code": "rate_limited"})
    m.put_pending.assert_not_awaited()
    m.cancel.assert_not_awaited()


async def test_pending_row_write_failure_cancels_the_operation(m):
    m.put_pending.side_effect = ValueError("ddb down")
    with pytest.raises(ValueError):
        await text_lines.ensure_line("org_1")
    m.cancel.assert_awaited_once_with("op_1")
    m.set_fd.assert_awaited_once_with("org_1", False)


async def test_record_failure_releases_the_bought_line(m):
    m.put.return_value = None
    with pytest.raises(Exception):
        await text_lines.ensure_line("org_1")
    m.release.assert_awaited_once_with("res_1")
    m.del_pending.assert_awaited_once_with("org_1")
    m.set_fd.assert_awaited_once_with("org_1", False)
    m.soul.assert_not_awaited()


async def test_unknown_line_user_releases_the_bought_line(m):
    m.find_user.return_value = None
    with pytest.raises(Exception):
        await text_lines.ensure_line("org_1")
    m.release.assert_awaited_once_with("res_1")
    m.put.assert_not_awaited()


async def test_no_customer_operations_is_no_agent_without_a_request(m):
    m.roster_row.starter_agent_ids = {"ceo": "x"}
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
    m.get_op.return_value = {"operationId": "op_1", "state": "cancelled"}
    assert await text_lines.release_line("org_1") == "released"
    m.cancel.assert_awaited_once_with("op_1")
    m.del_pending.assert_awaited_once_with("org_1")
    m.set_fd.assert_awaited_once_with("org_1", False)
    m.release.assert_not_awaited()


async def test_release_falls_through_when_cancel_loses_the_race(m):
    m.pending.return_value = PENDING
    m.get_op.return_value = DONE
    # complete_pending records the line; the line row then exists for the release.
    m.line.return_value = LINE_ROW
    assert await text_lines.release_line("org_1") == "released"
    m.put.assert_awaited_once()
    m.release.assert_awaited_once_with("res_1")
    m.delete.assert_awaited_once_with("+15551230000")
    m.set_fd.assert_awaited_once_with("org_1", False)


async def test_release_line_row_calls_photon_and_clears(m):
    m.line.return_value = LINE_ROW
    assert await text_lines.release_line("org_1") == "released"
    m.release.assert_awaited_once_with("res_1")
    m.delete.assert_awaited_once_with("+15551230000")
    m.set_fd.assert_awaited_once_with("org_1", False)
    m.soul.assert_awaited_once_with("org_1", "agent_co")


async def test_release_failure_keeps_the_row_and_raises(m):
    m.line.return_value = LINE_ROW
    m.release.side_effect = photon.PhotonError(500, "http_500")
    with pytest.raises(photon.PhotonError):
        await text_lines.release_line("org_1")
    m.delete.assert_not_awaited()
    m.set_fd.assert_not_awaited()


async def test_release_absent_clears_flag(m):
    assert await text_lines.release_line("org_1") == "absent"
    m.set_fd.assert_awaited_once_with("org_1", False)


def _stub_retrieve(monkeypatch, status, items):
    monkeypatch.setattr(
        text_lines.stripe.Subscription,
        "retrieve",
        lambda sid: {"id": sid, "status": status, "items": {"data": items}},
    )


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
