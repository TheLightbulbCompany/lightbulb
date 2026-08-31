"""Unit tests for core.services.company_stripe_reads.day_money."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

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
    client = _fake_client(
        txns=[{"net": 250}, {"net": -100}],
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
    assert kwargs["params"]["type"] == "charge"
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
