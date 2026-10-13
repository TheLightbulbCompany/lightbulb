"""Admin billing actions on the money path: each one checks what it is about to
touch before it writes, and says so when nothing happened."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import billing_service


def _owner(monkeypatch, row):
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))


def _invoice(monkeypatch, invoice):
    pay = MagicMock(return_value={"id": "in_1", "status": "paid"})
    monkeypatch.setattr(billing_service.stripe.Invoice, "retrieve", MagicMock(return_value=invoice))
    monkeypatch.setattr(billing_service.stripe.Invoice, "pay", pay)
    return pay


@pytest.mark.asyncio
async def test_mark_invoice_resolved_refuses_another_customers_invoice(monkeypatch):
    _owner(monkeypatch, {"owner_id": "org_1", "stripe_customer_id": "cus_1"})
    pay = _invoice(monkeypatch, {"id": "in_1", "customer": "cus_OTHER", "status": "open"})

    result = await billing_service.mark_invoice_resolved("org_1", "in_1")

    assert result == {"status": "invoice_customer_mismatch"}
    pay.assert_not_called()


@pytest.mark.asyncio
async def test_mark_invoice_resolved_refuses_an_owner_with_no_stripe_customer(monkeypatch):
    _owner(monkeypatch, {"owner_id": "org_1"})
    pay = _invoice(monkeypatch, {"id": "in_1", "customer": "cus_OTHER", "status": "open"})

    assert await billing_service.mark_invoice_resolved("org_1", "in_1") == {"status": "no_customer"}
    pay.assert_not_called()


@pytest.mark.asyncio
async def test_mark_invoice_resolved_says_so_when_the_invoice_is_already_paid(monkeypatch):
    _owner(monkeypatch, {"owner_id": "org_1", "stripe_customer_id": "cus_1"})
    pay = _invoice(monkeypatch, {"id": "in_1", "customer": "cus_1", "status": "paid"})

    assert await billing_service.mark_invoice_resolved("org_1", "in_1") == {"status": "already_paid"}
    pay.assert_not_called()


@pytest.mark.asyncio
async def test_mark_invoice_resolved_pays_the_owners_own_open_invoice(monkeypatch):
    _owner(monkeypatch, {"owner_id": "org_1", "stripe_customer_id": "cus_1"})
    pay = _invoice(monkeypatch, {"id": "in_1", "customer": "cus_1", "status": "open"})

    result = await billing_service.mark_invoice_resolved("org_1", "in_1")

    assert result == {"status": "resolved", "invoice_id": "in_1", "stripe_status": "paid"}
    assert pay.call_args.kwargs["paid_out_of_band"] is True


def _credit(monkeypatch, *, replayed=False):
    txn = MagicMock()
    txn.__getitem__.side_effect = {"id": "cbtxn_1"}.__getitem__
    txn.last_response.headers = {"Idempotent-Replayed": "true"} if replayed else {}
    create = MagicMock(return_value=txn)
    monkeypatch.setattr(billing_service.stripe.Customer, "create_balance_transaction", create)
    return create


@pytest.mark.asyncio
async def test_a_second_deliberate_credit_is_a_new_stripe_write(monkeypatch):
    """Same amount, same reason, two submissions: two idempotency keys. One
    submission retried (a double click) keeps one key."""
    _owner(monkeypatch, {"owner_id": "org_1", "stripe_customer_id": "cus_1"})
    create = _credit(monkeypatch)

    for request_id in ("submit-aaaa", "submit-aaaa", "submit-bbbb"):
        await billing_service.issue_credit_for_owner(
            "org_1", amount_cents=500, reason="goodwill", request_id=request_id
        )

    first, retry, second = (c.kwargs["idempotency_key"] for c in create.call_args_list)
    assert first == retry
    assert first != second


@pytest.mark.asyncio
async def test_a_deduplicated_credit_is_reported_as_a_duplicate(monkeypatch):
    _owner(monkeypatch, {"owner_id": "org_1", "stripe_customer_id": "cus_1"})
    _credit(monkeypatch, replayed=True)

    result = await billing_service.issue_credit_for_owner(
        "org_1", amount_cents=500, reason="goodwill", request_id="submit-aaaa"
    )

    assert result["status"] == "duplicate"
    assert result["transaction_id"] == "cbtxn_1"


def _subscription(monkeypatch, pause_collection):
    modify = MagicMock()
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "retrieve",
        MagicMock(return_value={"id": "sub_1", "pause_collection": pause_collection}),
    )
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)
    return modify


@pytest.mark.asyncio
async def test_pausing_a_paused_subscription_says_so_and_writes_nothing(monkeypatch):
    _owner(monkeypatch, {"owner_id": "org_1", "stripe_subscription_id": "sub_1"})
    modify = _subscription(monkeypatch, {"behavior": "mark_uncollectible"})

    result = await billing_service.pause_subscription_for_owner("org_1")

    assert result == {"status": "already_paused", "subscription_id": "sub_1"}
    modify.assert_not_called()


@pytest.mark.asyncio
async def test_pause_then_resume_flips_pause_collection(monkeypatch):
    _owner(monkeypatch, {"owner_id": "org_1", "stripe_subscription_id": "sub_1"})
    modify = _subscription(monkeypatch, None)
    assert (await billing_service.pause_subscription_for_owner("org_1"))["status"] == "paused"
    assert modify.call_args.kwargs == {"pause_collection": {"behavior": "mark_uncollectible"}}

    modify = _subscription(monkeypatch, {"behavior": "mark_uncollectible"})
    assert (await billing_service.resume_subscription_for_owner("org_1"))["status"] == "resumed"
    assert modify.call_args.kwargs == {"pause_collection": ""}


@pytest.mark.asyncio
async def test_resuming_a_running_subscription_says_so_and_writes_nothing(monkeypatch):
    _owner(monkeypatch, {"owner_id": "org_1", "stripe_subscription_id": "sub_1"})
    modify = _subscription(monkeypatch, None)

    assert (await billing_service.resume_subscription_for_owner("org_1"))["status"] == "not_paused"
    modify.assert_not_called()
