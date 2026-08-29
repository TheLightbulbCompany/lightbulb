from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
import stripe

from core.services import key_service
from core.services import launch_revenue as rev

START = datetime(2026, 9, 21, 16, tzinfo=timezone.utc)
END = datetime(2026, 10, 21, 16, tzinfo=timezone.utc)


def _page(items, has_more=False):
    page = MagicMock()
    page.data = [MagicMock(**i) for i in items]
    page.has_more = has_more
    return page


@pytest.mark.parametrize(
    "env,key,ok",
    [
        ("prod", "rk_live_abc123", True),
        ("prod", "rk_test_abc123", False),
        ("prod", "sk_live_abc123", False),
        ("dev", "rk_test_abc123", True),
        ("dev", "sk_test_abc123", False),
        ("dev", "rk_live_abc 123", False),
    ],
)
async def test_validate_key_prefix(env, key, ok):
    with (
        patch.object(rev.settings, "ENVIRONMENT", env),
        patch.object(rev.stripe.BalanceTransaction, "list", return_value=_page([])) as probe,
    ):
        if ok:
            await rev.validate_key(key)
            probe.assert_called_once_with(limit=1, api_key=key)
        else:
            with pytest.raises(rev.KeyRejected) as e:
                await rev.validate_key(key)
            assert e.value.reason == "bad_prefix"
            probe.assert_not_called()


async def test_validate_key_maps_stripe_errors():
    with patch.object(rev.settings, "ENVIRONMENT", "dev"):
        with patch.object(rev.stripe.BalanceTransaction, "list", side_effect=stripe.AuthenticationError("nope")):
            with pytest.raises(rev.KeyRejected) as e:
                await rev.validate_key("rk_test_abc")
            assert e.value.reason == "stripe_auth"
        with patch.object(rev.stripe.BalanceTransaction, "list", side_effect=stripe.PermissionError("no scope")):
            with pytest.raises(rev.KeyRejected) as e:
                await rev.validate_key("rk_test_abc")
            assert e.value.reason == "stripe_permission"


async def test_sum_net_revenue_charges_minus_refunds_paginated():
    pages = [
        _page(
            [
                {"type": "charge", "amount": 5000, "currency": "usd", "id": "txn_1"},
                {"type": "refund", "amount": -1000, "currency": "usd", "id": "txn_2"},
                {"type": "payout", "amount": -4000, "currency": "usd", "id": "txn_3"},
            ],
            has_more=True,
        ),
        _page(
            [
                {"type": "payment", "amount": 2500, "currency": "usd", "id": "txn_4"},
                {"type": "stripe_fee", "amount": -30, "currency": "usd", "id": "txn_5"},
            ]
        ),
    ]
    with patch.object(rev.stripe.BalanceTransaction, "list", side_effect=pages) as lst:
        cents, currency = await rev.sum_net_revenue("rk_test_x", START, END)
    assert (cents, currency) == (6500, "usd")
    first_kwargs = lst.call_args_list[0].kwargs
    assert first_kwargs["created"] == {"gte": int(START.timestamp()), "lt": int(END.timestamp())}
    assert first_kwargs["limit"] == 100 and first_kwargs["api_key"] == "rk_test_x"
    assert lst.call_args_list[1].kwargs["starting_after"] == "txn_3"


async def test_sum_net_revenue_empty_is_zero_usd():
    with patch.object(rev.stripe.BalanceTransaction, "list", return_value=_page([])):
        assert await rev.sum_net_revenue("rk_test_x", START, END) == (0, "usd")


async def test_sum_net_revenue_mixed_currency_raises():
    page = _page(
        [
            {"type": "charge", "amount": 100, "currency": "usd", "id": "a"},
            {"type": "charge", "amount": 100, "currency": "eur", "id": "b"},
        ]
    )
    with patch.object(rev.stripe.BalanceTransaction, "list", return_value=page):
        with pytest.raises(rev.MixedCurrency):
            await rev.sum_net_revenue("rk_test_x", START, END)


def test_secret_name_is_on_the_user_keys_rail():
    with patch.object(rev.settings, "ENVIRONMENT", "prod"):
        assert rev.secret_name("user_1") == key_service._secret_name("user_1", "stripe-launch")


async def test_store_and_load_key_round_trip_via_secrets_manager():
    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": "rk_test_x"}
    with patch.object(rev, "_sm", return_value=sm), patch.object(rev, "create_or_update_secret") as put:
        await rev.store_key("user_1", "rk_test_x")
        put.assert_called_once_with(sm, rev.secret_name("user_1"), "rk_test_x")
        assert await rev.load_key("user_1") == "rk_test_x"
        sm.get_secret_value.side_effect = sm.exceptions.ResourceNotFoundException = type("RNF", (Exception,), {})
        assert await rev.load_key("user_1") is None
