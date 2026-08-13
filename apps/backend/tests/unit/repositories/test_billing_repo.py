"""Tests for billing account DynamoDB repository."""

import os
import time
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")


@pytest.fixture
def dynamodb_table():
    """Create a moto DynamoDB billing-accounts table with Stripe GSI."""
    with mock_aws():
        client = boto3.resource("dynamodb", region_name="us-east-1")
        table = client.create_table(
            TableName="test-billing-accounts",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "stripe_customer_id", "AttributeType": "S"},
                {"AttributeName": "apple_original_transaction_id", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "stripe-customer-index",
                    "KeySchema": [{"AttributeName": "stripe_customer_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
                {
                    "IndexName": "apple-original-transaction-index",
                    "KeySchema": [{"AttributeName": "apple_original_transaction_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        table.meta.client.get_waiter("table_exists").wait(TableName="test-billing-accounts")

        with (
            patch("core.dynamodb._table_prefix", "test-"),
            patch("core.dynamodb._dynamodb_resource", client),
        ):
            yield table


@pytest.mark.asyncio
async def test_create_and_get(dynamodb_table):
    from core.repositories import billing_repo

    result = await billing_repo.create_if_not_exists("user_1", "cus_abc")
    assert result["owner_id"] == "user_1"
    assert result["stripe_customer_id"] == "cus_abc"
    assert "id" in result

    item = await billing_repo.get_by_owner_id("user_1")
    assert item is not None
    assert item["stripe_customer_id"] == "cus_abc"


@pytest.mark.asyncio
async def test_get_by_owner_id_nonexistent(dynamodb_table):
    from core.repositories import billing_repo

    item = await billing_repo.get_by_owner_id("ghost")
    assert item is None


@pytest.mark.asyncio
async def test_get_by_stripe_customer_id(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_2", "cus_def")
    item = await billing_repo.get_by_stripe_customer_id("cus_def")
    assert item is not None
    assert item["owner_id"] == "user_2"


@pytest.mark.asyncio
async def test_get_by_stripe_customer_id_nonexistent(dynamodb_table):
    from core.repositories import billing_repo

    item = await billing_repo.get_by_stripe_customer_id("cus_nope")
    assert item is None


@pytest.mark.asyncio
async def test_create_if_not_exists_rejects_duplicate(dynamodb_table):
    from core.repositories import billing_repo
    from core.repositories.billing_repo import AlreadyExistsError

    await billing_repo.create_if_not_exists("user_3", "cus_ghi")
    with pytest.raises(AlreadyExistsError):
        await billing_repo.create_if_not_exists("user_3", "cus_ghi_dup")


@pytest.mark.asyncio
async def test_set_subscription_persists_status_and_trial_end(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_4", "cus_jkl")
    result = await billing_repo.set_subscription(
        owner_id="user_4",
        subscription_id="sub_xyz",
        status="trialing",
        trial_end=1700000000,
    )
    assert result is not None
    assert result["stripe_subscription_id"] == "sub_xyz"
    assert result["subscription_status"] == "trialing"
    assert result["trial_end"] == 1700000000


@pytest.mark.asyncio
async def test_set_subscription_nonexistent(dynamodb_table):
    from core.repositories import billing_repo

    result = await billing_repo.set_subscription(owner_id="ghost", subscription_id="sub_x", status="active")
    assert result is None


@pytest.mark.asyncio
async def test_set_subscription_clear_seat_fields_removes_stale_seat_data(dynamodb_table):
    """Regression: cancelling a per-seat org subscription must also clear
    subscription_item_id + seat_count, not just the subscription id/status —
    otherwise is_seat_subscription (routers/billing.py) stays True forever."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_seat_cancel", "cus_seat_cancel", owner_type="org")
    await billing_repo.set_team_subscription(
        "org_seat_cancel",
        subscription_id="sub_seat",
        subscription_item_id="si_seat",
        status="active",
        seat_count=4,
    )

    result = await billing_repo.set_subscription(
        owner_id="org_seat_cancel",
        subscription_id=None,
        status="canceled",
        trial_end=None,
        clear_seat_fields=True,
    )
    assert result is not None
    assert result["subscription_status"] == "canceled"
    assert result["stripe_subscription_id"] is None
    assert "subscription_item_id" not in result
    assert "seat_count" not in result

    stored = await billing_repo.get_by_owner_id("org_seat_cancel")
    assert "subscription_item_id" not in stored
    assert "seat_count" not in stored


@pytest.mark.asyncio
async def test_set_subscription_without_clear_seat_fields_preserves_seat_data(dynamodb_table):
    """Default behavior (clear_seat_fields=False) — used by the
    created/updated webhook path — must NOT touch seat fields, since that
    path relies on set_team_subscription running separately/afterward."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_seat_keep", "cus_seat_keep", owner_type="org")
    await billing_repo.set_team_subscription(
        "org_seat_keep",
        subscription_id="sub_seat",
        subscription_item_id="si_seat",
        status="active",
        seat_count=3,
    )

    result = await billing_repo.set_subscription(
        owner_id="org_seat_keep",
        subscription_id="sub_seat",
        status="active",
        trial_end=None,
    )
    assert result["subscription_item_id"] == "si_seat"
    assert result["seat_count"] == 3


@pytest.mark.asyncio
async def test_set_team_subscription_writes_item_id_and_seats(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_seat", "cus_seat", owner_type="org")
    result = await billing_repo.set_team_subscription(
        "org_seat",
        subscription_id="sub_seat",
        subscription_item_id="si_seat",
        status="active",
        seat_count=5,
    )
    assert result is not None
    assert result["stripe_subscription_id"] == "sub_seat"
    assert result["subscription_item_id"] == "si_seat"
    assert result["subscription_status"] == "active"
    assert result["seat_count"] == 5

    stored = await billing_repo.get_by_owner_id("org_seat")
    assert stored["subscription_item_id"] == "si_seat"
    assert stored["seat_count"] == 5


@pytest.mark.asyncio
async def test_set_team_subscription_noops_without_row(dynamodb_table):
    from core.repositories import billing_repo

    # Never upsert a phantom row for an owner with no billing account.
    result = await billing_repo.set_team_subscription(
        "org_missing",
        subscription_id="sub_x",
        subscription_item_id="si_x",
        status="active",
        seat_count=2,
    )
    assert result is None
    assert await billing_repo.get_by_owner_id("org_missing") is None


@pytest.mark.asyncio
async def test_delete(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_5", "cus_mno")
    await billing_repo.delete("user_5")
    item = await billing_repo.get_by_owner_id("user_5")
    assert item is None


@pytest.mark.asyncio
async def test_set_and_get_by_metronome_customer_id(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_m", "cus_m")
    await billing_repo.set_metronome_ids(
        "org_m", metronome_customer_id="cust-uuid-1", metronome_contract_id="contract-1"
    )

    item = await billing_repo.get_by_owner_id("org_m")
    assert item["metronome_customer_id"] == "cust-uuid-1"
    assert item["metronome_contract_id"] == "contract-1"

    # The webhook resolves owner via the Metronome UUID.
    resolved = await billing_repo.get_by_metronome_customer_id("cust-uuid-1")
    assert resolved is not None
    assert resolved["owner_id"] == "org_m"

    assert await billing_repo.get_by_metronome_customer_id("unknown-uuid") is None


@pytest.mark.asyncio
async def test_create_trial_local_if_absent_writes_trial_row(dynamodb_table):
    from core.repositories import billing_repo

    before = int(time.time())
    created = await billing_repo.create_trial_local_if_absent("user_trial_1", "personal")
    after = int(time.time())

    assert created is True, "the call that actually writes the row must return True"
    item = await billing_repo.get_by_owner_id("user_trial_1")
    assert item is not None
    assert item["subscription_status"] == "trial_local"
    assert item["owner_type"] == "personal"
    assert "trial_end" in item
    three_days = 3 * 24 * 60 * 60
    assert before + three_days <= item["trial_end"] <= after + three_days
    assert not any(k.startswith("stripe_") for k in item)


@pytest.mark.asyncio
async def test_create_trial_local_if_absent_is_noop_when_row_exists(dynamodb_table):
    from core.repositories import billing_repo

    first_created = await billing_repo.create_trial_local_if_absent("user_trial_2", "personal")
    first = await billing_repo.get_by_owner_id("user_trial_2")
    assert first_created is True

    # Second call must not overwrite the existing row.
    second_created = await billing_repo.create_trial_local_if_absent("user_trial_2", "org")
    second = await billing_repo.get_by_owner_id("user_trial_2")
    assert second == first
    assert second_created is False, "the no-op branch must return False"


@pytest.mark.asyncio
async def test_create_trial_local_if_absent_noop_on_existing_active_row(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_trial_3", "cus_active")
    await billing_repo.set_subscription(
        owner_id="user_trial_3",
        subscription_id="sub_active",
        status="active",
    )
    before = await billing_repo.get_by_owner_id("user_trial_3")

    created = await billing_repo.create_trial_local_if_absent("user_trial_3", "personal")
    after = await billing_repo.get_by_owner_id("user_trial_3")

    assert after == before
    assert after["subscription_status"] == "active"
    assert created is False


@pytest.mark.asyncio
async def test_set_metronome_ids_requires_existing_row(dynamodb_table):
    """The conditional write must not upsert a phantom billing row (same trap
    documented on clear_provider_choice)."""
    from botocore.exceptions import ClientError

    from core.repositories import billing_repo

    with pytest.raises(ClientError):
        await billing_repo.set_metronome_ids(
            "org_ghost", metronome_customer_id="cust-uuid-1", metronome_contract_id="contract-1"
        )
    assert await billing_repo.get_by_owner_id("org_ghost") is None


@pytest.mark.asyncio
async def test_add_trial_daily_spend_accumulates_per_date(dynamodb_table):
    """First add creates the nested map + leaf; second add accumulates the
    same day; a different date is an independent leaf."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_x", "cus_meta")

    t1 = await billing_repo.add_trial_daily_spend("user_x", "2026-07-10", 1000)
    t2 = await billing_repo.add_trial_daily_spend("user_x", "2026-07-10", 500)
    assert t1 == 1000
    assert t2 == 1500

    t3 = await billing_repo.add_trial_daily_spend("user_x", "2026-07-11", 200)
    assert t3 == 200

    assert await billing_repo.get_trial_daily_spend("user_x", "2026-07-10") == 1500
    assert await billing_repo.get_trial_daily_spend("user_x", "2026-07-11") == 200


@pytest.mark.asyncio
async def test_add_trial_daily_spend_supports_negative_delta_rollback(dynamodb_table):
    """The ADD verb is signed: a negative delta subtracts. No caller writes
    negatives today (the meter Lambda only ever adds priced invocations), but
    the primitive's arithmetic must stay correct — a correction/backfill is the
    obvious future caller, and a silently-clamped ADD would be a money bug."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_y", "cus_meta2")
    await billing_repo.add_trial_daily_spend("user_y", "2026-07-10", 1000)
    total = await billing_repo.add_trial_daily_spend("user_y", "2026-07-10", -400)
    assert total == 600


@pytest.mark.asyncio
async def test_get_trial_daily_spend_defaults_to_zero(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_z", "cus_meta3")
    # row exists but no spend recorded yet for this date
    assert await billing_repo.get_trial_daily_spend("user_z", "2026-07-10") == 0
    # no billing row at all for this owner
    assert await billing_repo.get_trial_daily_spend("ghost_owner", "2026-07-10") == 0


@pytest.mark.asyncio
async def test_add_trial_daily_spend_requires_existing_row(dynamodb_table):
    """No phantom billing rows — same invariant as set_metronome_ids /
    clear_provider_choice."""
    from botocore.exceptions import ClientError

    from core.repositories import billing_repo

    with pytest.raises(ClientError):
        await billing_repo.add_trial_daily_spend("ghost_owner", "2026-07-10", 1000)
    assert await billing_repo.get_by_owner_id("ghost_owner") is None


@pytest.mark.asyncio
async def test_set_stripe_customer_id_backfills_existing_trial_local_row(dynamodb_table):
    """The card-less trial_local row (create_trial_local_if_absent) writes no
    stripe_* fields. set_stripe_customer_id must be able to backfill one onto
    that existing row without disturbing the rest of its state — this is the
    write billing_service.create_customer_for_owner uses to let a trial_local
    owner reach /billing/trial-checkout."""
    from core.repositories import billing_repo

    created = await billing_repo.create_trial_local_if_absent("user_backfill", "personal")
    assert created is True
    before = await billing_repo.get_by_owner_id("user_backfill")
    assert "stripe_customer_id" not in before

    await billing_repo.set_stripe_customer_id("user_backfill", stripe_customer_id="cus_backfilled")

    after = await billing_repo.get_by_owner_id("user_backfill")
    assert after["stripe_customer_id"] == "cus_backfilled"
    # Rest of the trial row is untouched.
    assert after["subscription_status"] == "trial_local"
    assert after["trial_end"] == before["trial_end"]


@pytest.mark.asyncio
async def test_set_stripe_customer_id_requires_existing_row(dynamodb_table):
    """No phantom billing rows — same invariant as set_metronome_ids /
    add_trial_daily_spend / clear_provider_choice. A conditional update
    against a missing owner_id must raise, never silently upsert."""
    from botocore.exceptions import ClientError

    from core.repositories import billing_repo

    with pytest.raises(ClientError):
        await billing_repo.set_stripe_customer_id("ghost_owner", stripe_customer_id="cus_phantom")
    assert await billing_repo.get_by_owner_id("ghost_owner") is None


# ---------------------------------------------------------------------------
# Nightly CEO arm claim / opt-out conditional writes (issue #1273)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_try_claim_nightly_arm_first_claim_wins(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("arm_owner", "cus_arm")

    won = await billing_repo.try_claim_nightly_arm("arm_owner", "co1")
    assert won is True
    row = await billing_repo.get_by_owner_id("arm_owner")
    assert row["nightly_ceo_armed_company"] == "co1"


@pytest.mark.asyncio
async def test_try_claim_nightly_arm_same_company_is_rejected(dynamodb_table):
    """The idempotency cache: a re-claim for the SAME company fails the
    conditional write (``nightly_ceo_armed_company <> :c`` is false), so exactly
    one caller ever seeds for a given company."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("arm_same", "cus_same")
    assert await billing_repo.try_claim_nightly_arm("arm_same", "co1") is True
    assert await billing_repo.try_claim_nightly_arm("arm_same", "co1") is False


@pytest.mark.asyncio
async def test_try_claim_nightly_arm_different_company_reclaims(dynamodb_table):
    """The reprovision re-arm branch: a DIFFERENT company_id satisfies the
    ``<>`` clause, so a fresh company re-arms."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("arm_diff", "cus_diff")
    assert await billing_repo.try_claim_nightly_arm("arm_diff", "co1") is True
    assert await billing_repo.try_claim_nightly_arm("arm_diff", "co2") is True
    row = await billing_repo.get_by_owner_id("arm_diff")
    assert row["nightly_ceo_armed_company"] == "co2"


@pytest.mark.asyncio
async def test_try_claim_nightly_arm_no_row_returns_false(dynamodb_table):
    """No billing row (never a paid owner) → claim fails, no phantom row."""
    from core.repositories import billing_repo

    assert await billing_repo.try_claim_nightly_arm("no_row_owner", "co1") is False
    assert await billing_repo.get_by_owner_id("no_row_owner") is None


@pytest.mark.asyncio
async def test_release_nightly_arm_removes_when_company_matches(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("rel_owner", "cus_rel")
    await billing_repo.try_claim_nightly_arm("rel_owner", "co1")

    await billing_repo.release_nightly_arm("rel_owner", "co1")
    row = await billing_repo.get_by_owner_id("rel_owner")
    assert "nightly_ceo_armed_company" not in row


@pytest.mark.asyncio
async def test_release_nightly_arm_noop_on_mismatched_company(dynamodb_table):
    """Releasing a DIFFERENT company must not clobber a newer arm (a concurrent
    successful arm for co2 survives a stale release for co1)."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("rel_mismatch", "cus_relm")
    await billing_repo.try_claim_nightly_arm("rel_mismatch", "co2")

    await billing_repo.release_nightly_arm("rel_mismatch", "co1")
    row = await billing_repo.get_by_owner_id("rel_mismatch")
    assert row["nightly_ceo_armed_company"] == "co2"


@pytest.mark.asyncio
async def test_set_nightly_ceo_opted_out_sets_and_clears(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("opt_owner", "cus_opt")

    await billing_repo.set_nightly_ceo_opted_out("opt_owner", True)
    assert (await billing_repo.get_by_owner_id("opt_owner"))["nightly_ceo_opted_out"] is True

    await billing_repo.set_nightly_ceo_opted_out("opt_owner", False)
    assert (await billing_repo.get_by_owner_id("opt_owner"))["nightly_ceo_opted_out"] is False


@pytest.mark.asyncio
async def test_set_nightly_ceo_opted_out_no_row_is_silent_noop(dynamodb_table):
    """A missing row (never a paid owner, so never armed) is a silent no-op —
    no raise, no phantom row."""
    from core.repositories import billing_repo

    await billing_repo.set_nightly_ceo_opted_out("opt_ghost", True)
    assert await billing_repo.get_by_owner_id("opt_ghost") is None


@pytest.mark.asyncio
async def test_try_claim_checkout_slot_first_claim_wins(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists(owner_id="slot_owner", stripe_customer_id="cus_s", owner_type="personal")
    assert await billing_repo.try_claim_checkout_slot("slot_owner")  # returns a claim token


@pytest.mark.asyncio
async def test_try_claim_checkout_slot_blocks_within_ttl(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists(owner_id="slot_owner", stripe_customer_id="cus_s", owner_type="personal")
    assert await billing_repo.try_claim_checkout_slot("slot_owner")
    assert await billing_repo.try_claim_checkout_slot("slot_owner") is None


@pytest.mark.asyncio
async def test_try_claim_checkout_slot_self_heals_after_ttl(dynamodb_table, monkeypatch):
    """A crashed request must not wedge the owner — an expired claim is
    reclaimable. Advance the repo's clock past the TTL instead of sleeping."""
    from types import SimpleNamespace

    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists(owner_id="slot_owner", stripe_customer_id="cus_s", owner_type="personal")
    assert await billing_repo.try_claim_checkout_slot("slot_owner")
    real_now = time.time()
    monkeypatch.setattr(billing_repo, "time", SimpleNamespace(time=lambda: real_now + 91))
    assert await billing_repo.try_claim_checkout_slot("slot_owner")


@pytest.mark.asyncio
async def test_release_checkout_slot_with_token_allows_immediate_reclaim(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists(owner_id="slot_owner", stripe_customer_id="cus_s", owner_type="personal")
    token = await billing_repo.try_claim_checkout_slot("slot_owner")
    assert token
    await billing_repo.release_checkout_slot("slot_owner", token=token)
    assert await billing_repo.try_claim_checkout_slot("slot_owner")


@pytest.mark.asyncio
async def test_release_checkout_slot_wrong_token_is_noop(dynamodb_table):
    """The ABA fence: a stale holder's release with the wrong token must NOT
    clear a newer holder's claim (this is what double-charged)."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists(owner_id="slot_owner", stripe_customer_id="cus_s", owner_type="personal")
    token = await billing_repo.try_claim_checkout_slot("slot_owner")
    assert token
    # A different request's failure-path release, carrying a stale token.
    await billing_repo.release_checkout_slot("slot_owner", token="not-the-holders-token")
    # The live claim survived — the slot is still held.
    assert await billing_repo.try_claim_checkout_slot("slot_owner") is None


@pytest.mark.asyncio
async def test_try_claim_checkout_slot_no_row_returns_none(dynamodb_table):
    from core.repositories import billing_repo

    assert await billing_repo.try_claim_checkout_slot("ghost_owner") is None


# ---------------------------------------------------------------------------
# Apple IAP (App Store 3.1.3(b)) — the fixture above mirrors prod's
# apple-original-transaction-index, so these exercise the real index.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_and_resolve_apple_subscription(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    written = await billing_repo.set_apple_subscription(
        "user_1",
        original_transaction_id="2000000000000001",
        status="active",
        expires_at=1_800_000_000_000,
    )

    assert written is not None
    assert written["subscription_status"] == "active"
    assert written["subscription_source"] == "apple_iap"

    # The GSI lookup is how every renewal/expiry/refund finds the account.
    found = await billing_repo.get_by_apple_original_transaction_id("2000000000000001")
    assert found is not None
    assert found["owner_id"] == "user_1"


@pytest.mark.asyncio
async def test_set_apple_subscription_preserves_stripe_history(dynamodb_table):
    """An owner who once paid via Stripe and now pays via Apple must keep the
    Stripe id — a renewal notification is not a reason to erase it."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    await billing_repo.set_subscription(owner_id="user_1", subscription_id="sub_old", status="canceled")

    await billing_repo.set_apple_subscription(
        "user_1",
        original_transaction_id="2000000000000001",
        status="active",
        expires_at=None,
    )

    row = await billing_repo.get_by_owner_id("user_1")
    assert row["stripe_subscription_id"] == "sub_old"
    assert row["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_set_apple_subscription_refuses_empty_transaction_id(dynamodb_table):
    """An empty string on a GSI key attribute makes DynamoDB reject the ENTIRE
    PutItem. On this table's read-then-put writers that would brick every
    unrelated billing write for the owner (the 2026-08-02 outage shape), so the
    setter refuses before it can reach the table."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    with pytest.raises(ValueError):
        await billing_repo.set_apple_subscription(
            "user_1", original_transaction_id="", status="active", expires_at=None
        )

    # The row is untouched and still writable.
    row = await billing_repo.get_by_owner_id("user_1")
    assert "apple_original_transaction_id" not in row


@pytest.mark.asyncio
async def test_set_apple_subscription_no_row_returns_none(dynamodb_table):
    """A notification for an owner we have no row for must not conjure one."""
    from core.repositories import billing_repo

    result = await billing_repo.set_apple_subscription(
        "ghost", original_transaction_id="2000000000000009", status="active", expires_at=None
    )
    assert result is None


@pytest.mark.asyncio
async def test_apple_signed_date_watermark_only_advances(dynamodb_table):
    """The link path carries no signedDate and must not clear the watermark —
    doing so would reopen the replay window for every applied notification."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    await billing_repo.set_apple_subscription(
        "user_1",
        original_transaction_id="2000000000000001",
        status="active",
        expires_at=None,
        signed_date=9_000,
    )
    await billing_repo.set_apple_subscription(
        "user_1",
        original_transaction_id="2000000000000001",
        status="active",
        expires_at=None,
    )

    row = await billing_repo.get_by_owner_id("user_1")
    assert row["apple_last_signed_date"] == 9_000
