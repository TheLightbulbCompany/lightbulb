"""Unit tests for core.services.company_stripe_reads."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe

from core.services import company_stripe_reads

OWNER = "user_test123"
ACCT = "acct_company1"
DAY = "2026-08-30"
DAY_GTE = int(datetime(2026, 8, 30, tzinfo=timezone.utc).timestamp())


class _FakeList:
    def __init__(self, items):
        self._items = items

    def auto_paging_iter(self):
        return iter(self._items)


def _fake_client(txns, customers):
    client = MagicMock()
    client.v1.balance_transactions.list.return_value = _FakeList(txns)
    client.v1.customers.list.return_value = _FakeList(customers)
    return client


async def test_happy_path_sums_nets_and_counts_customers():
    # No type filter: the refund's negative net has to land in the total, or
    # the number is gross rather than earned.
    client = _fake_client(
        txns=[{"net": 250, "type": "charge"}, {"net": -100, "type": "refund"}],
        customers=[{"id": "cus_1"}, {"id": "cus_2"}],
    )
    with (
        patch.object(
            company_stripe_reads.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"company_stripe_account_id": ACCT}),
        ),
        patch.object(company_stripe_reads, "_client", return_value=client),
    ):
        result = await company_stripe_reads.day_money(OWNER, DAY)

    assert result == {"revenue_microcents": 150 * 10_000, "signups": 2}

    _, kwargs = client.v1.balance_transactions.list.call_args
    assert kwargs["params"]["created"] == {"gte": DAY_GTE, "lt": DAY_GTE + 86400}
    assert "type" not in kwargs["params"]
    assert kwargs["options"] == {"stripe_account": ACCT}

    _, kwargs = client.v1.customers.list.call_args
    assert kwargs["params"]["created"] == {"gte": DAY_GTE, "lt": DAY_GTE + 86400}
    assert kwargs["options"] == {"stripe_account": ACCT}


async def test_unbound_owner_returns_none_without_sdk_call():
    fake = MagicMock()
    with (
        patch.object(
            company_stripe_reads.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": OWNER}),
        ),
        patch.object(company_stripe_reads, "_client", fake),
    ):
        assert await company_stripe_reads.day_money(OWNER, DAY) is None
    fake.assert_not_called()


async def test_missing_billing_row_returns_none():
    with patch.object(
        company_stripe_reads.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value=None),
    ):
        assert await company_stripe_reads.day_money(OWNER, DAY) is None


async def test_sdk_raise_returns_none():
    client = MagicMock()
    client.v1.balance_transactions.list.side_effect = RuntimeError("stripe down")
    with (
        patch.object(
            company_stripe_reads.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"company_stripe_account_id": ACCT}),
        ),
        patch.object(company_stripe_reads, "_client", return_value=client),
    ):
        assert await company_stripe_reads.day_money(OWNER, DAY) is None


async def test_signup_count_caps_at_1000():
    client = _fake_client(
        txns=[],
        customers=[{"id": f"cus_{i}"} for i in range(1500)],
    )
    with (
        patch.object(
            company_stripe_reads.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"company_stripe_account_id": ACCT}),
        ),
        patch.object(company_stripe_reads, "_client", return_value=client),
    ):
        result = await company_stripe_reads.day_money(OWNER, DAY)
    assert result == {"revenue_microcents": 0, "signups": 1000}


# --- stripe_page: the list reads behind the Money tabs ---------------------


def _bound_row():
    return patch.object(
        company_stripe_reads.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"company_stripe_account_id": ACCT}),
    )


async def test_stripe_page_propagates_stripe_errors_unlike_day_money():
    """day_money swallows to None — a stale tile is harmless. A list that
    renders "no payments" during an outage is a lie, so this one raises."""
    client = MagicMock()
    client.v1.charges.list.side_effect = stripe.error.APIConnectionError("stripe down")
    with _bound_row(), patch.object(company_stripe_reads, "_client", return_value=client):
        with pytest.raises(stripe.error.StripeError):
            await company_stripe_reads.stripe_page(OWNER, "payments")


async def test_stripe_page_caps_the_limit_and_never_walks_pages():
    client = MagicMock()
    client.v1.charges.list.return_value = {"data": [{"id": "ch_1"}], "has_more": True}
    with _bound_row(), patch.object(company_stripe_reads, "_client", return_value=client):
        page = await company_stripe_reads.stripe_page(OWNER, "payments", limit=5000)
    assert client.v1.charges.list.call_args.kwargs["params"]["limit"] == 100
    # One call, cursor handed back — no server-side walk to build a total.
    assert client.v1.charges.list.call_count == 1
    assert (page["has_more"], page["next_cursor"]) == (True, "ch_1")
