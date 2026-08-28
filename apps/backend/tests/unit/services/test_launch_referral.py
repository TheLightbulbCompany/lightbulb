from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services import launch_referral as ref

PAYER_ROW = {"email": "payer@x.com", "referral_code": "PAYER123", "referred_by": "REFER123", "free_months_banked": 0}
REFERRER_ROW = {"email": "ref@x.com", "referral_code": "REFER123", "user_id": "user_ref", "free_months_banked": 0}


def _invoice(**over):
    inv = {"id": "in_1", "customer": "cus_1", "amount_paid": 5000, "billing_reason": "subscription_create"}
    inv.update(over)
    return inv


@pytest.fixture
def deps():
    with (
        patch.object(ref.launch_phase, "schedule", return_value=object()),
        patch.object(ref, "billing_repo") as billing,
        patch.object(ref, "user_repo") as users,
        patch.object(ref, "launch_waitlist_repo") as wl,
        patch.object(ref, "stripe") as stripe_mod,
        patch.object(ref, "capture_product_event") as capture,
        patch.object(ref.settings, "STRIPE_FREE_MONTH_COUPON_ID", "coupon_free"),
    ):
        billing.list_by_stripe_customer_id = AsyncMock(
            return_value=[{"owner_id": "user_payer", "stripe_subscription_id": "sub_payer"}]
        )
        billing.get_by_owner_id = AsyncMock(
            return_value={"owner_id": "user_ref", "stripe_subscription_id": "sub_ref", "subscription_status": "active"}
        )
        users.get = AsyncMock(
            side_effect=lambda uid: {
                "user_id": uid,
                "email": {"user_payer": "payer@x.com", "user_ref": "ref@x.com"}[uid],
            }
        )
        wl.get = AsyncMock(
            side_effect=lambda e: {"payer@x.com": dict(PAYER_ROW), "ref@x.com": dict(REFERRER_ROW)}.get(e)
        )
        wl.get_by_referral_code = AsyncMock(return_value=dict(REFERRER_ROW))
        wl.mark_rewarded = AsyncMock(return_value=True)
        wl.add_counter = AsyncMock(return_value=1)
        stripe_mod.Subscription.retrieve = MagicMock(return_value={"id": "sub_ref", "discount": None, "discounts": []})
        stripe_mod.Subscription.modify = MagicMock()
        yield {"billing": billing, "wl": wl, "stripe": stripe_mod, "capture": capture}


async def test_rewards_referrer_once_on_first_paid_invoice(deps):
    await ref.on_invoice_paid(_invoice())
    deps["wl"].mark_rewarded.assert_awaited_once_with("payer@x.com")
    deps["wl"].add_counter.assert_any_await("ref@x.com", "referrals_paid", 1)
    deps["stripe"].Subscription.modify.assert_called_once_with(
        "sub_ref", discounts=[{"coupon": "coupon_free"}], idempotency_key="referral_reward:sub_ref:in_1"
    )
    deps["capture"].assert_called()


async def test_zero_amount_invoice_does_nothing(deps):
    await ref.on_invoice_paid(_invoice(amount_paid=0))
    deps["wl"].mark_rewarded.assert_not_awaited()
    deps["stripe"].Subscription.modify.assert_not_called()


async def test_cas_loss_means_no_double_reward(deps):
    deps["wl"].mark_rewarded = AsyncMock(return_value=False)
    await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    assert not any(c.args[1] == "referrals_paid" for c in deps["wl"].add_counter.await_args_list)


async def test_banks_when_referrer_has_no_subscription(deps):
    deps["billing"].get_by_owner_id = AsyncMock(
        return_value={"owner_id": "user_ref", "subscription_status": "trial_local"}
    )
    await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    deps["wl"].add_counter.assert_any_await("ref@x.com", "free_months_banked", 1)


async def test_banks_when_referrer_already_discounted(deps):
    deps["stripe"].Subscription.retrieve = MagicMock(
        return_value={"id": "sub_ref", "discount": {"coupon": {"id": "x"}}}
    )
    await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    deps["wl"].add_counter.assert_any_await("ref@x.com", "free_months_banked", 1)


async def test_banks_when_no_coupon_configured(deps):
    with patch.object(ref.settings, "STRIPE_FREE_MONTH_COUPON_ID", ""):
        await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    deps["wl"].add_counter.assert_any_await("ref@x.com", "free_months_banked", 1)


async def test_stripe_failure_after_cas_banks_the_month(deps):
    deps["stripe"].Subscription.modify.side_effect = RuntimeError("stripe down")
    await ref.on_invoice_paid(_invoice())  # no exception
    deps["wl"].mark_rewarded.assert_awaited_once_with("payer@x.com")
    deps["wl"].add_counter.assert_any_await("ref@x.com", "free_months_banked", 1)


async def test_drains_one_banked_month_for_the_payer(deps):
    payer = dict(PAYER_ROW, referred_by=None, free_months_banked=2)
    deps["wl"].get = AsyncMock(side_effect=lambda e: {"payer@x.com": payer}.get(e))
    deps["billing"].get_by_owner_id = AsyncMock(
        return_value={"owner_id": "user_payer", "stripe_subscription_id": "sub_payer", "subscription_status": "active"}
    )
    deps["stripe"].Subscription.retrieve = MagicMock(return_value={"id": "sub_payer", "discount": None})
    await ref.on_invoice_paid(_invoice())
    deps["wl"].add_counter.assert_any_await("payer@x.com", "free_months_banked", -1)
    deps["stripe"].Subscription.modify.assert_called_once_with(
        "sub_payer", discounts=[{"coupon": "coupon_free"}], idempotency_key="banked_month:sub_payer:in_1"
    )


async def test_stripe_failure_during_drain_puts_the_month_back(deps):
    payer = dict(PAYER_ROW, referred_by=None, free_months_banked=1)
    deps["wl"].get = AsyncMock(side_effect=lambda e: {"payer@x.com": payer}.get(e))
    deps["billing"].get_by_owner_id = AsyncMock(
        return_value={"owner_id": "user_payer", "stripe_subscription_id": "sub_payer", "subscription_status": "active"}
    )
    deps["stripe"].Subscription.retrieve = MagicMock(return_value={"id": "sub_payer", "discount": None})
    deps["stripe"].Subscription.modify.side_effect = RuntimeError("stripe down")
    await ref.on_invoice_paid(_invoice())  # no exception
    deps["wl"].add_counter.assert_any_await("payer@x.com", "free_months_banked", -1)
    deps["wl"].add_counter.assert_any_await("payer@x.com", "free_months_banked", 1)


async def test_org_owner_is_skipped(deps):
    deps["billing"].list_by_stripe_customer_id = AsyncMock(
        return_value=[{"owner_id": "org_1", "stripe_subscription_id": "sub_o"}]
    )
    await ref.on_invoice_paid(_invoice())
    deps["wl"].get.assert_not_awaited()


async def test_never_raises(deps):
    deps["billing"].list_by_stripe_customer_id = AsyncMock(side_effect=RuntimeError("ddb down"))
    await ref.on_invoice_paid(_invoice())  # no exception


def test_subscription_has_discount_handles_both_shapes():
    assert ref.subscription_has_discount({"discount": {"id": "di_1"}}) is True
    assert ref.subscription_has_discount({"discounts": ["di_1"]}) is True
    assert ref.subscription_has_discount({"discount": None, "discounts": []}) is False


async def test_refused_decrement_neither_grants_nor_puts_a_month_back(deps):
    """A lost decrement race must not mint a month: add_counter returns None when
    the conditional write is refused, and the drain has to stop there."""
    payer = dict(PAYER_ROW, referred_by=None, free_months_banked=1)
    deps["wl"].get = AsyncMock(side_effect=lambda e: {"payer@x.com": payer}.get(e))
    deps["wl"].add_counter = AsyncMock(return_value=None)
    deps["billing"].get_by_owner_id = AsyncMock(
        return_value={"owner_id": "user_payer", "stripe_subscription_id": "sub_payer", "subscription_status": "active"}
    )
    await ref.on_invoice_paid(_invoice())
    deps["stripe"].Subscription.modify.assert_not_called()
    assert deps["wl"].add_counter.await_args_list == [
        (("payer@x.com", "free_months_banked", -1), {}),
    ]


# --- Nothing may raise past the bank decision once mark_rewarded has won ------


async def test_referrer_lookup_failure_after_the_cas_does_not_escape(deps):
    deps["wl"].get_by_referral_code = AsyncMock(side_effect=RuntimeError("gsi down"))
    await ref._reward_referrer("payer@x.com", dict(PAYER_ROW), "in_1")  # no exception
    deps["stripe"].Subscription.modify.assert_not_called()


async def test_billing_read_failure_after_the_cas_banks_the_month(deps):
    deps["billing"].get_by_owner_id = AsyncMock(side_effect=RuntimeError("ddb down"))
    await ref._reward_referrer("payer@x.com", dict(PAYER_ROW), "in_1")  # no exception
    deps["wl"].add_counter.assert_any_await("ref@x.com", "free_months_banked", 1)


async def test_bank_write_failing_twice_logs_an_error_without_the_email(deps, caplog):
    deps["billing"].get_by_owner_id = AsyncMock(return_value={"subscription_status": "trial_local"})
    deps["wl"].add_counter = AsyncMock(side_effect=RuntimeError("ddb down"))
    with caplog.at_level("ERROR", logger=ref.logger.name):
        await ref._reward_referrer("payer@x.com", dict(PAYER_ROW), "in_1")  # no exception
    assert deps["wl"].add_counter.await_count == 4  # bank retried once, then referrals_paid the same
    errors = [r.getMessage() for r in caplog.records if r.levelname == "ERROR"]
    assert any("REFER123" in m and "user_ref" in m for m in errors)
    assert not any("ref@x.com" in m or "payer@x.com" in m for m in errors)


async def test_analytics_never_carries_an_email_as_the_distinct_id(deps):
    deps["wl"].get_by_referral_code = AsyncMock(return_value={"email": "ref@x.com", "referral_code": "REFER123"})
    await ref._reward_referrer("payer@x.com", dict(PAYER_ROW), "in_1")
    assert deps["capture"].call_args.kwargs["distinct_id"] is None


async def test_unconfigured_launch_reads_nothing(deps):
    with patch.object(ref.launch_phase, "schedule", return_value=None):
        await ref.on_invoice_paid(_invoice())
    deps["billing"].list_by_stripe_customer_id.assert_not_awaited()
