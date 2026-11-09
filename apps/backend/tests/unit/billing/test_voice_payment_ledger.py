from core.repositories import billing_repo
from core.billing import voice_billing, voice_plan
from tests.unit.repositories.test_billing_repo import dynamodb_table  # noqa: F401


async def _record(invoice, state, *, contract="voice", end="2099-01-01T00:00:00Z"):
    return await billing_repo.record_voice_payment(
        "org",
        contract_id=contract,
        invoice_id=invoice,
        period_start="2026-11-01T00:00:00Z",
        paid_through=end,
        state=state,
        action_url="https://invoice.stripe.com/owned" if state == "failed" else None,
    )


async def test_paid_invoice_is_absorbing_and_other_contract_cannot_mutate_voice(dynamodb_table):  # noqa: F811
    dynamodb_table.put_item(
        Item={"owner_id": "org", "metronome_voice_contract_id": "voice", "voice_plan_status": "pending_payment"}
    )
    assert await _record("in_fee", "failed")
    assert not voice_billing.has_voice_plan(await billing_repo.get_by_owner_id("org"))
    assert await _record("in_fee", "paid")
    assert voice_billing.has_voice_plan(await billing_repo.get_by_owner_id("org"))
    assert not await _record("in_fee", "failed")  # delayed failure cannot undo collected payment
    assert not await _record("in_wrong", "failed", contract="credits")
    row = await billing_repo.get_by_owner_id("org")
    assert not row.get("voice_plan_unpaid_invoice_ids") and row["voice_plan_invoice_states"]["in_fee"] == "paid"


async def test_each_overage_debt_must_recover_and_older_payment_never_shortens_coverage(dynamodb_table):  # noqa: F811
    dynamodb_table.put_item(
        Item={"owner_id": "org", "metronome_voice_contract_id": "voice", "voice_plan_status": "active"}
    )
    await _record("in_fee", "paid")
    await _record("in_usage_a", "failed", end=None)
    await _record("in_usage_b", "failed", end=None)
    await _record("in_usage_a", "paid", end=None)
    row = await billing_repo.get_by_owner_id("org")
    assert row["voice_plan_unpaid_invoice_ids"] == {"in_usage_b"}
    assert not voice_billing.has_voice_plan(row)
    await _record("in_usage_b", "paid", end=None)
    await _record("in_old_fee", "paid", end="2098-01-01T00:00:00Z")
    row = await billing_repo.get_by_owner_id("org")
    assert row["voice_plan_paid_through"] == "2099-01-01T00:00:00Z"
    assert voice_billing.has_voice_plan(row)


async def test_cancel_retry_keeps_one_period_end_and_never_reenables_expired_access(dynamodb_table):  # noqa: F811
    dynamodb_table.put_item(
        Item={
            "owner_id": "org",
            "metronome_voice_contract_id": "voice",
            "voice_plan_status": "active",
            "voice_plan_paid_through": "2099-01-01T00:00:00Z",
            "voice_plan_paid_periods": {"fee": {"start": "2000-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"}},
        }
    )
    first = await billing_repo.begin_voice_plan_cancellation("org")
    second = await billing_repo.begin_voice_plan_cancellation("org")
    assert first == second and first.endswith("T00:00:00+00:00")
    pending = await billing_repo.get_by_owner_id("org")
    assert not pending.get("voice_plan_ending_at") and voice_plan.status(pending) == "cancellation_pending"
    assert voice_billing.has_voice_plan(pending)
    await billing_repo.finish_voice_plan_cancellation("org")
    row = await billing_repo.get_by_owner_id("org")
    assert row["voice_plan_status"] == "canceling"
    assert not voice_billing.has_voice_plan({**row, "voice_plan_ending_at": "2000-01-01T00:00:00Z"})


async def test_future_prepayment_does_not_unlock_now_or_hide_a_paid_current_period(dynamodb_table):  # noqa: F811
    dynamodb_table.put_item(Item={"owner_id": "org", "metronome_voice_contract_id": "voice"})
    await billing_repo.record_voice_payment(
        "org",
        contract_id="voice",
        invoice_id="future",
        period_start="2098-12-01T00:00:00Z",
        paid_through="2099-01-01T00:00:00Z",
        state="paid",
        action_url=None,
    )
    assert not voice_billing.has_voice_plan(await billing_repo.get_by_owner_id("org"))
    await _record("current", "paid", end="2098-01-01T00:00:00Z")
    assert voice_billing.has_voice_plan(await billing_repo.get_by_owner_id("org"))


def test_failed_unconfirmed_cancellation_never_claims_subscription_ended():
    row = {
        "metronome_voice_contract_id": "voice",
        "voice_plan_cancel_target": "2000-01-01T00:00:00Z",
        "voice_plan_paid_periods": {"fee": {"start": "2000-01-01T00:00:00Z", "end": "2099-01-01T00:00:00Z"}},
    }
    assert voice_plan.status(row) == "cancellation_pending" and voice_billing.has_voice_plan(row)
