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

    created = await billing_repo.create_trial_local_if_absent("org_trial_1", "org", trial_end=None)

    assert created is True
    item = await billing_repo.get_by_owner_id("org_trial_1")
    assert item["subscription_status"] == "trial_local"
    assert item["owner_type"] == "org"
    assert "trial_end" not in item
    assert not any(k.startswith("stripe_") for k in item)


@pytest.mark.asyncio
async def test_trial_arms_once_even_when_ready_polls_race(dynamodb_table):
    import asyncio
    from core.repositories import billing_repo

    dynamodb_table.put_item(Item={"owner_id": "org_trial", "owner_type": "org", "subscription_status": "trial_local"})
    ends = await asyncio.gather(
        billing_repo.arm_trial_if_unset("org_trial", now_s=1_000),
        billing_repo.arm_trial_if_unset("org_trial", now_s=9_999),
    )
    assert ends[0] == ends[1]
    assert ends[0] in (2_593_000, 2_601_999)
    assert await billing_repo.arm_trial_if_unset("org_trial", now_s=50_000) == ends[0]
    assert (await billing_repo.get_by_owner_id("org_trial"))["trial_end"] == ends[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "active", "trialing", "canceled"])
async def test_trial_armer_leaves_missing_and_non_trial_rows_untouched(dynamodb_table, status):
    from core.repositories import billing_repo

    if status:
        dynamodb_table.put_item(Item={"owner_id": "org_other", "subscription_status": status, "trial_end": 123})
    before = await billing_repo.get_by_owner_id("org_other")
    assert await billing_repo.arm_trial_if_unset("org_other", now_s=1_000) is None
    assert await billing_repo.get_by_owner_id("org_other") == before


@pytest.mark.asyncio
async def test_trial_armer_returns_first_ready_plus_thirty_days(dynamodb_table):
    from core.repositories import billing_repo

    dynamodb_table.put_item(Item={"owner_id": "org_trial", "subscription_status": "trial_local"})
    assert await billing_repo.arm_trial_if_unset("org_trial", now_s=1_000) == 2_593_000


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
    assert "trial_end" not in after


@pytest.mark.asyncio
async def test_set_stripe_customer_id_requires_existing_row(dynamodb_table):
    """No phantom billing rows — same invariant as set_metronome_ids /
    clear_provider_choice. A conditional update against a missing owner_id
    must raise, never silently upsert."""
    from botocore.exceptions import ClientError

    from core.repositories import billing_repo

    with pytest.raises(ClientError):
        await billing_repo.set_stripe_customer_id("ghost_owner", stripe_customer_id="cus_phantom")
    assert await billing_repo.get_by_owner_id("ghost_owner") is None


@pytest.mark.asyncio
async def test_set_stripe_customer_id_is_a_first_writer_wins_claim(dynamodb_table):
    """A concurrent first checkout cannot overwrite the workspace customer."""
    from botocore.exceptions import ClientError

    from core.repositories import billing_repo

    await billing_repo.create_trial_local_if_absent("workspace_claim", "org")
    await billing_repo.set_stripe_customer_id("workspace_claim", stripe_customer_id="cus_winner")

    with pytest.raises(ClientError):
        await billing_repo.set_stripe_customer_id("workspace_claim", stripe_customer_id="cus_loser")

    account = await billing_repo.get_by_owner_id("workspace_claim")
    assert account["stripe_customer_id"] == "cus_winner"


# ---------------------------------------------------------------------------
# Nightly CEO opt-out write (issue #1273)
# ---------------------------------------------------------------------------


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
    monkeypatch.setattr(billing_repo, "time", SimpleNamespace(time=lambda: real_now + 241))
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


# ---------------------------------------------------------------------------
# Cross-provider entitlement. Apple and Stripe both used to write the shared
# subscription_status directly, making them last-writer-wins — an Apple expiry
# cancelled a customer who was still paying Stripe, and vice versa. Each
# provider now records its own status and the shared field is derived.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apple_expiry_does_not_cancel_a_live_stripe_subscription(dynamodb_table):
    """The money case: an owner pays Stripe, briefly tried Apple, Apple expires.
    Before this they lost access while still being charged by Stripe."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    await billing_repo.set_subscription(owner_id="user_1", subscription_id="sub_live", status="active")
    await billing_repo.set_apple_subscription(
        "user_1", original_transaction_id="2000000000000001", status="canceled", expires_at=None
    )

    row = await billing_repo.get_by_owner_id("user_1")
    assert row["apple_subscription_status"] == "canceled"
    assert row["stripe_subscription_status"] == "active"
    # The shared field the gates read must still say active.
    assert row["subscription_status"] == "active"

    from core.services.provision_gate import is_subscription_active

    assert is_subscription_active(row) is True


@pytest.mark.asyncio
async def test_stripe_cancellation_does_not_cancel_a_live_apple_subscription(dynamodb_table):
    """The inverse, which is just as real once IAP ships."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    await billing_repo.set_apple_subscription(
        "user_1", original_transaction_id="2000000000000001", status="active", expires_at=None
    )
    await billing_repo.set_subscription(owner_id="user_1", subscription_id=None, status="canceled")

    row = await billing_repo.get_by_owner_id("user_1")
    assert row["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_both_providers_inactive_leaves_the_owner_inactive(dynamodb_table):
    """The combine must not be a one-way ratchet that never lets go."""
    from core.repositories import billing_repo
    from core.services.provision_gate import is_subscription_active

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    await billing_repo.set_subscription(owner_id="user_1", subscription_id=None, status="canceled")
    await billing_repo.set_apple_subscription(
        "user_1", original_transaction_id="2000000000000001", status="canceled", expires_at=None
    )

    row = await billing_repo.get_by_owner_id("user_1")
    assert row["subscription_status"] == "canceled"
    assert is_subscription_active(row) is False


@pytest.mark.asyncio
async def test_free_tier_row_is_not_clobbered_when_no_provider_has_state(dynamodb_table):
    """trial_local is written by create_trial_local_if_absent, not by either
    provider, so the derivation must fall back to it rather than blank it."""
    from core.repositories import billing_repo

    await billing_repo.create_trial_local_if_absent("user_2", "personal")
    row = await billing_repo.get_by_owner_id("user_2")
    assert row["subscription_status"] == "trial_local"
    assert billing_repo._effective_status(row) == ("trial_local", None)


@pytest.mark.asyncio
async def test_apple_write_is_rejected_when_a_newer_notification_already_applied(dynamodb_table):
    """The out-of-order fence, exercised against a real conditional write.

    Two overlapping notifications can both read an old watermark through the
    eventually-consistent GSI. The condition re-evaluates at write time, so the
    older one loses regardless of how the reads interleaved.
    """
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    # Newer EXPIRED lands first.
    await billing_repo.set_apple_subscription(
        "user_1",
        original_transaction_id="2000000000000001",
        status="canceled",
        expires_at=None,
        signed_date=9_000,
    )

    # Older DID_RENEW arrives late and must be refused.
    result = await billing_repo.set_apple_subscription(
        "user_1",
        original_transaction_id="2000000000000001",
        status="active",
        expires_at=None,
        signed_date=1_000,
    )

    assert result is None, "stale write should be rejected by the condition"
    row = await billing_repo.get_by_owner_id("user_1")
    assert row["apple_subscription_status"] == "canceled"
    assert row["apple_last_signed_date"] == 9_000


@pytest.mark.asyncio
async def test_apple_write_accepts_a_newer_notification(dynamodb_table):
    """The fence must not be a wall — a genuinely newer event still applies."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    await billing_repo.set_apple_subscription(
        "user_1",
        original_transaction_id="2000000000000001",
        status="active",
        expires_at=None,
        signed_date=1_000,
    )
    result = await billing_repo.set_apple_subscription(
        "user_1",
        original_transaction_id="2000000000000001",
        status="canceled",
        expires_at=None,
        signed_date=9_000,
    )

    assert result is not None
    row = await billing_repo.get_by_owner_id("user_1")
    assert row["apple_subscription_status"] == "canceled"
    assert row["apple_last_signed_date"] == 9_000


@pytest.mark.asyncio
async def test_a_row_cannot_be_rebound_to_a_different_apple_transaction(dynamodb_table):
    """Ownership fence, as a real conditional write.

    link_purchase's cross-account lookup reads a non-unique, eventually
    consistent GSI, so two accounts submitting the same unlinked JWS can both
    see "unowned". The condition on the row is what actually decides it.
    """
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    await billing_repo.set_apple_subscription(
        "user_1", original_transaction_id="2000000000000001", status="active", expires_at=None
    )

    rebind = await billing_repo.set_apple_subscription(
        "user_1", original_transaction_id="9999999999999999", status="active", expires_at=None
    )

    assert rebind is None, "a row already bound to one transaction must not be rebound"
    row = await billing_repo.get_by_owner_id("user_1")
    assert row["apple_original_transaction_id"] == "2000000000000001"


@pytest.mark.asyncio
async def test_the_same_transaction_may_be_rewritten_by_its_own_owner(dynamodb_table):
    """Renewals, expiries and idempotent re-links all rewrite the same id — the
    fence must not block the normal case it exists alongside."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_1", "cus_abc")
    await billing_repo.set_apple_subscription(
        "user_1", original_transaction_id="2000000000000001", status="active", expires_at=None
    )
    again = await billing_repo.set_apple_subscription(
        "user_1", original_transaction_id="2000000000000001", status="canceled", expires_at=None
    )

    assert again is not None
    row = await billing_repo.get_by_owner_id("user_1")
    assert row["apple_subscription_status"] == "canceled"


# ---------------------------------------------------------------------------
# Legacy rows. Every account that existed before IAP carries ONLY the shared
# subscription_status — stripe_subscription_status did not exist to be written.
# The derivation must not read that absence as "Stripe has no opinion".
# ---------------------------------------------------------------------------


def _write_legacy_stripe_row(table, owner_id: str, status: str = "active"):
    """A billing row exactly as it looked before IAP: no per-provider status."""
    table.put_item(
        Item={
            "owner_id": owner_id,
            "stripe_customer_id": f"cus_{owner_id}",
            "stripe_subscription_id": "sub_live",
            "subscription_status": status,
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )


@pytest.mark.asyncio
async def test_apple_expiry_does_not_cancel_a_LEGACY_stripe_subscriber(dynamodb_table):
    """The regression the per-provider split introduced.

    A pre-IAP row has no stripe_subscription_status, so once Apple writes one
    the derivation sees a single candidate and skips the fallback — turning an
    actively-paying Stripe customer's row to "canceled" on an Apple expiry.
    """
    from core.repositories import billing_repo
    from core.services.provision_gate import is_subscription_active

    _write_legacy_stripe_row(dynamodb_table, "user_legacy")

    await billing_repo.set_apple_subscription(
        "user_legacy", original_transaction_id="2000000000000009", status="canceled", expires_at=None
    )

    row = await billing_repo.get_by_owner_id("user_legacy")
    assert row["apple_subscription_status"] == "canceled"
    # The legacy value was adopted as Stripe's, so it still counts.
    assert row["stripe_subscription_status"] == "active"
    assert row["subscription_status"] == "active"
    assert is_subscription_active(row) is True


@pytest.mark.asyncio
async def test_legacy_adoption_cannot_launder_an_apple_status_into_the_stripe_slot(dynamodb_table):
    """The adoption is one-shot. If it ran on every Apple write it would copy
    the DERIVED (possibly Apple-sourced) value back into the Stripe slot, and
    an owner who had only ever paid Apple would keep access forever."""
    from core.repositories import billing_repo

    dynamodb_table.put_item(
        Item={
            "owner_id": "user_appleonly",
            "stripe_customer_id": "cus_appleonly",
            "created_at": "2026-01-01T00:00:00Z",
            "updated_at": "2026-01-01T00:00:00Z",
        }
    )

    await billing_repo.set_apple_subscription(
        "user_appleonly", original_transaction_id="2000000000000010", status="active", expires_at=None
    )
    await billing_repo.set_apple_subscription(
        "user_appleonly", original_transaction_id="2000000000000010", status="canceled", expires_at=None
    )

    row = await billing_repo.get_by_owner_id("user_appleonly")
    assert row["subscription_status"] == "canceled"
    assert row.get("stripe_subscription_status") in (None, "")


# ---------------------------------------------------------------------------
# subscription_source decides where the customer is sent to CANCEL. Pinning it
# at the Apple write stranded anyone who later moved to Stripe in iOS Settings,
# where their subscription does not exist.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_follows_the_provider_that_is_actually_live(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_moved", "cus_moved")
    await billing_repo.set_apple_subscription(
        "user_moved", original_transaction_id="2000000000000011", status="active", expires_at=None
    )
    assert (await billing_repo.get_by_owner_id("user_moved"))["subscription_source"] == "apple_iap"

    # Apple lapses, the owner resubscribes on the web.
    await billing_repo.set_apple_subscription(
        "user_moved", original_transaction_id="2000000000000011", status="canceled", expires_at=None
    )
    await billing_repo.set_subscription(owner_id="user_moved", subscription_id="sub_new", status="active")

    row = await billing_repo.get_by_owner_id("user_moved")
    assert row["subscription_status"] == "active"
    # Not apple_iap — iOS Settings has nothing to cancel.
    assert row["subscription_source"] == "stripe"


@pytest.mark.asyncio
async def test_a_dead_stripe_status_does_not_steal_source_from_live_apple(dynamodb_table):
    """The inverse: a cancellation webhook for an old Stripe sub must not send
    a current Apple subscriber to the Stripe portal."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_apple", "cus_apple")
    await billing_repo.set_apple_subscription(
        "user_apple", original_transaction_id="2000000000000012", status="active", expires_at=None
    )
    await billing_repo.set_subscription(owner_id="user_apple", subscription_id=None, status="canceled")

    row = await billing_repo.get_by_owner_id("user_apple")
    assert row["subscription_status"] == "active"
    assert row["subscription_source"] == "apple_iap"


@pytest.mark.asyncio
async def test_free_tier_names_no_management_source(dynamodb_table):
    """trial_local is the card-less tier — there is no subscription to manage,
    so pointing the manage button at either portal is wrong."""
    from core.repositories import billing_repo

    await billing_repo.create_trial_local_if_absent("user_free", "personal")

    row = await billing_repo.get_by_owner_id("user_free")
    assert row["subscription_status"] == "trial_local"
    assert row.get("subscription_source") is None
    assert billing_repo._effective_status(row) == ("trial_local", None)


@pytest.mark.asyncio
async def test_a_lapsed_apple_purchase_does_not_hand_back_the_free_tier(dynamodb_table):
    """An owner who converted off the free tier and then let Apple lapse is
    CANCELED, not back on trial_local. Reverting would return the free
    allowance on every buy-then-cancel cycle, and would leave
    teardown_if_unentitled treating a lapsed subscriber as still entitled."""
    from core.repositories import billing_repo

    await billing_repo.create_trial_local_if_absent("user_lapsed", "personal")
    await billing_repo.set_apple_subscription(
        "user_lapsed", original_transaction_id="2000000000000013", status="active", expires_at=None
    )
    await billing_repo.set_apple_subscription(
        "user_lapsed", original_transaction_id="2000000000000013", status="canceled", expires_at=None
    )

    row = await billing_repo.get_by_owner_id("user_lapsed")
    assert row["subscription_status"] == "canceled"


# ---------------------------------------------------------------------------
# Cross-provider atomicity. Both setters read the row, derive the shared status
# from that snapshot, and re-put the WHOLE item — so a concurrent write by the
# other provider is restored from the stale snapshot along with everything else.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_apple_write_cannot_resurrect_a_stripe_cancellation(dynamodb_table):
    """The phantom-active row: both subscriptions end, the row says active.

    Apple reads {stripe: active}, Stripe cancels, Apple writes its whole item
    back — carrying the pre-cancellation Stripe status with it.
    """
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_race", "cus_race")
    await billing_repo.set_subscription(owner_id="user_race", subscription_id="sub_x", status="active")
    await billing_repo.set_apple_subscription(
        "user_race", original_transaction_id="2000000000000020", status="active", expires_at=None
    )

    # Apple's read happens here...
    real_get = billing_repo.get_by_owner_id
    interleaved = {"done": False}

    async def _get_then_let_stripe_cancel(owner_id, **kwargs):
        row = await real_get(owner_id, **kwargs)
        if not interleaved["done"]:
            interleaved["done"] = True
            # ...and Stripe's cancellation lands before Apple writes.
            billing_repo.get_by_owner_id = real_get
            await billing_repo.set_subscription(owner_id="user_race", subscription_id=None, status="canceled")
            billing_repo.get_by_owner_id = _get_then_let_stripe_cancel
        return row

    billing_repo.get_by_owner_id = _get_then_let_stripe_cancel
    try:
        await billing_repo.set_apple_subscription(
            "user_race", original_transaction_id="2000000000000020", status="canceled", expires_at=None
        )
    finally:
        billing_repo.get_by_owner_id = real_get

    row = await billing_repo.get_by_owner_id("user_race")
    assert row["stripe_subscription_status"] == "canceled"
    assert row["apple_subscription_status"] == "canceled"
    assert row["subscription_status"] == "canceled"


@pytest.mark.asyncio
async def test_stripe_write_cannot_resurrect_an_apple_expiry(dynamodb_table):
    """The mirror image, with Stripe as the stale writer."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_race2", "cus_race2")
    await billing_repo.set_apple_subscription(
        "user_race2", original_transaction_id="2000000000000021", status="active", expires_at=None
    )
    await billing_repo.set_subscription(owner_id="user_race2", subscription_id="sub_y", status="active")

    real_get = billing_repo.get_by_owner_id
    interleaved = {"done": False}

    async def _get_then_let_apple_expire(owner_id, **kwargs):
        row = await real_get(owner_id, **kwargs)
        if not interleaved["done"]:
            interleaved["done"] = True
            billing_repo.get_by_owner_id = real_get
            await billing_repo.set_apple_subscription(
                "user_race2", original_transaction_id="2000000000000021", status="canceled", expires_at=None
            )
            billing_repo.get_by_owner_id = _get_then_let_apple_expire
        return row

    billing_repo.get_by_owner_id = _get_then_let_apple_expire
    try:
        await billing_repo.set_subscription(owner_id="user_race2", subscription_id=None, status="canceled")
    finally:
        billing_repo.get_by_owner_id = real_get

    row = await billing_repo.get_by_owner_id("user_race2")
    assert row["apple_subscription_status"] == "canceled"
    assert row["stripe_subscription_status"] == "canceled"
    assert row["subscription_status"] == "canceled"


@pytest.mark.asyncio
async def test_a_stale_apple_notification_is_still_refused_not_retried(dynamodb_table):
    """The retry must not become a way around the watermark: an out-of-order
    notification has to keep losing however many times it is re-attempted."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_wm", "cus_wm")
    await billing_repo.set_apple_subscription(
        "user_wm",
        original_transaction_id="2000000000000022",
        status="canceled",
        expires_at=None,
        signed_date=2_000,
    )

    result = await billing_repo.set_apple_subscription(
        "user_wm",
        original_transaction_id="2000000000000022",
        status="active",
        expires_at=None,
        signed_date=1_000,
    )

    assert result is None
    row = await billing_repo.get_by_owner_id("user_wm")
    assert row["apple_subscription_status"] == "canceled"


@pytest.mark.asyncio
async def test_a_stripe_write_cannot_roll_back_an_apple_renewal(dynamodb_table):
    """An Apple RENEWAL leaves the status "active" while advancing the watermark
    and the expiry. A compare-and-set on status alone therefore passes, and the
    whole-item write restores the PRE-renewal watermark — after which an older
    refund can clear the monotonicity check and cancel a live subscription."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_renew", "cus_renew")
    await billing_repo.set_apple_subscription(
        "user_renew",
        original_transaction_id="2000000000000030",
        status="active",
        expires_at=1_000,
        signed_date=1_000,
    )

    real_get = billing_repo.get_by_owner_id
    interleaved = {"done": False}

    async def _get_then_let_apple_renew(owner_id, **kwargs):
        row = await real_get(owner_id, **kwargs)
        if not interleaved["done"]:
            interleaved["done"] = True
            billing_repo.get_by_owner_id = real_get
            # Status stays "active"; only the watermark and expiry move.
            await billing_repo.set_apple_subscription(
                "user_renew",
                original_transaction_id="2000000000000030",
                status="active",
                expires_at=9_000,
                signed_date=9_000,
            )
            billing_repo.get_by_owner_id = _get_then_let_apple_renew
        return row

    billing_repo.get_by_owner_id = _get_then_let_apple_renew
    try:
        await billing_repo.set_subscription(owner_id="user_renew", subscription_id="sub_z", status="active")
    finally:
        billing_repo.get_by_owner_id = real_get

    row = await billing_repo.get_by_owner_id("user_renew")
    assert row["apple_last_signed_date"] == 9_000
    assert row["apple_expires_at"] == 9_000


@pytest.mark.asyncio
async def test_an_apple_write_cannot_revert_a_concurrent_provider_change(dynamodb_table):
    """provider_choice belongs to NO billing provider, so no cross-provider
    compare-and-set would ever have covered it. A whole-item write restored it
    from the snapshot anyway — which can send a paying owner back to
    provider_required, or silently undo the inference provider they picked."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_pc", "cus_pc")
    await billing_repo.set_provider_choice("user_pc", provider_choice="openrouter_managed", byo_provider=None)

    real_get = billing_repo.get_by_owner_id
    interleaved = {"done": False}

    async def _get_then_change_provider(owner_id, **kwargs):
        row = await real_get(owner_id, **kwargs)
        if not interleaved["done"]:
            interleaved["done"] = True
            billing_repo.get_by_owner_id = real_get
            await billing_repo.set_provider_choice("user_pc", provider_choice="chatgpt_oauth", byo_provider=None)
            billing_repo.get_by_owner_id = _get_then_change_provider
        return row

    billing_repo.get_by_owner_id = _get_then_change_provider
    try:
        await billing_repo.set_apple_subscription(
            "user_pc", original_transaction_id="2000000000000040", status="active", expires_at=None
        )
    finally:
        billing_repo.get_by_owner_id = real_get

    row = await billing_repo.get_by_owner_id("user_pc")
    assert row["provider_choice"] == "chatgpt_oauth", "the Apple write rolled back an unrelated field"
    assert row["apple_subscription_status"] == "active"


@pytest.mark.asyncio
async def test_a_removed_field_is_actually_removed(dynamodb_table):
    """The diff write has to express REMOVE, not just SET — cancellation clears
    trial_end, and a lingering countdown misreports the account."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_rm", "cus_rm")
    await billing_repo.set_subscription(
        owner_id="user_rm", subscription_id="sub_t", status="trialing", trial_end=1_700_000_000
    )
    assert (await billing_repo.get_by_owner_id("user_rm"))["trial_end"] == 1_700_000_000

    await billing_repo.set_subscription(owner_id="user_rm", subscription_id=None, status="canceled")

    row = await billing_repo.get_by_owner_id("user_rm")
    assert "trial_end" not in row


@pytest.mark.asyncio
async def test_a_free_tier_row_does_not_gain_a_phantom_stripe_trial(dynamodb_table):
    """trial_local is the ORDINARY state of a first Apple purchase, and it is
    not a Stripe status — nothing Stripe wrote it. Adopting it invents a
    subscription that outranks Apple's own expiry, so the owner never gets torn
    down and shows as back on the free plan instead of lapsed."""
    from core.repositories import billing_repo
    from core.services.entitlement_teardown import still_entitled

    await billing_repo.create_trial_local_if_absent("user_conv", "personal")
    await billing_repo.set_apple_subscription(
        "user_conv", original_transaction_id="2000000000000050", status="active", expires_at=None
    )

    row = await billing_repo.get_by_owner_id("user_conv")
    assert row.get("stripe_subscription_status") in (None, ""), "invented a Stripe subscription"

    # And when Apple ends, the owner is genuinely unentitled.
    await billing_repo.set_apple_subscription(
        "user_conv", original_transaction_id="2000000000000050", status="canceled", expires_at=None
    )
    row = await billing_repo.get_by_owner_id("user_conv")
    assert row["subscription_status"] == "canceled"
    assert still_entitled(row) is False


# ---------------------------------------------------------------------------
# mark_trial_first_agent_response — the once-ever gate behind the product event
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mark_trial_first_agent_response_wins_once_on_a_trial_row(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_trial_local_if_absent("user_tfr", "personal")

    assert await billing_repo.mark_trial_first_agent_response("user_tfr") is True
    row = await billing_repo.get_by_owner_id("user_tfr")
    assert row["first_agent_response_at"]  # the time-to-first-value stamp
    # Every later call — same process, another process, a later day — loses.
    assert await billing_repo.mark_trial_first_agent_response("user_tfr") is False
    assert (await billing_repo.get_by_owner_id("user_tfr"))["first_agent_response_at"] == row["first_agent_response_at"]


@pytest.mark.asyncio
async def test_mark_trial_first_agent_response_refuses_without_a_trial_row(dynamodb_table):
    """No row (an org owner, a wiped owner) is a clean False, not an upsert."""
    from core.repositories import billing_repo

    assert await billing_repo.mark_trial_first_agent_response("user_nobody") is False
    assert await billing_repo.get_by_owner_id("user_nobody") is None


@pytest.mark.asyncio
async def test_mark_trial_first_agent_response_refuses_a_paid_row(dynamodb_table):
    """The branch the feature exists to enforce: a converted (or org) owner is
    not on a trial, so the milestone must never be stamped — even if the row
    began life as trial_local."""
    from core.repositories import billing_repo

    await billing_repo.create_trial_local_if_absent("user_paid", "personal")
    await billing_repo.set_subscription(owner_id="user_paid", subscription_id="sub_x", status="active")

    assert await billing_repo.mark_trial_first_agent_response("user_paid") is False
    assert "first_agent_response_at" not in (await billing_repo.get_by_owner_id("user_paid"))


@pytest.mark.asyncio
async def test_short_trial_extends_a_pre_30_day_window_to_signup_plus_thirty_days(dynamodb_table):
    """Rows armed under the old 3-day window (2026-09-12..17 prod cohort) get signup + 30 days,
    not a fresh 30 from now; a row already at 30 days is left alone."""
    from datetime import datetime, timezone

    from core.repositories import billing_repo

    signup = "2026-09-12T01:26:00+00:00"
    signup_s = int(datetime.fromisoformat(signup).timestamp())
    dynamodb_table.put_item(
        Item={
            "owner_id": "org_old",
            "subscription_status": "trial_local",
            "created_at": signup,
            "trial_end": signup_s + 3 * 86400,
        }
    )
    row = await billing_repo.get_by_owner_id("org_old")
    assert await billing_repo.extend_short_trial("org_old", row) == signup_s + 30 * 86400
    assert (await billing_repo.get_by_owner_id("org_old"))["trial_end"] == signup_s + 30 * 86400
    # Idempotent: a stale snapshot from a racing poll loses the condition and reads the winner.
    assert await billing_repo.extend_short_trial("org_old", row) == signup_s + 30 * 86400

    armed = int(datetime(2026, 9, 20, tzinfo=timezone.utc).timestamp()) + 30 * 86400
    dynamodb_table.put_item(
        Item={"owner_id": "org_new", "subscription_status": "trial_local", "created_at": signup, "trial_end": armed}
    )
    assert await billing_repo.extend_short_trial("org_new", await billing_repo.get_by_owner_id("org_new")) == armed


# --- Apple expiry is enforced at READ time ---------------------------------
# Nothing re-evaluates entitlement on a timer, and a sandbox/TestFlight purchase
# verified in prod never sends its EXPIRED notification to the prod endpoint —
# so an Apple "active" whose paid-through date has passed must read as lapsed.

_FUTURE_MS = int(time.time() * 1000) + 30 * 24 * 3600 * 1000
_PAST_MS = int(time.time() * 1000) - 60 * 1000


@pytest.mark.asyncio
async def test_apple_active_with_future_expiry_is_live(dynamodb_table):
    from core.repositories import billing_repo
    from core.services.apple_iap import apple_is_live
    from core.services.provision_gate import is_subscription_active

    await billing_repo.create_if_not_exists("user_exp1", "cus_exp1")
    await billing_repo.set_apple_subscription(
        "user_exp1", original_transaction_id="2000000000000101", status="active", expires_at=_FUTURE_MS
    )
    row = await billing_repo.get_by_owner_id("user_exp1")
    assert row["subscription_status"] == "active"
    assert is_subscription_active(row) is True
    assert apple_is_live(row) is True


@pytest.mark.asyncio
async def test_apple_active_with_past_expiry_is_not_live(dynamodb_table):
    from core.repositories import billing_repo
    from core.services.apple_iap import apple_is_live
    from core.services.provision_gate import is_subscription_active

    await billing_repo.create_if_not_exists("user_exp2", "cus_exp2")
    await billing_repo.set_apple_subscription(
        "user_exp2", original_transaction_id="2000000000000102", status="active", expires_at=_PAST_MS
    )
    for row in (
        await billing_repo.get_by_owner_id("user_exp2"),
        await billing_repo.get_by_owner_id("user_exp2", consistent_read=True),
        await billing_repo.get_by_apple_original_transaction_id("2000000000000102"),
    ):
        assert row["subscription_status"] == "canceled"
        assert is_subscription_active(row) is False
        assert apple_is_live(row) is False
        # Apple's own field is untouched: the cross-provider CAS compares it.
        assert row["apple_subscription_status"] == "active"


@pytest.mark.asyncio
async def test_lapsed_apple_does_not_affect_a_live_stripe_subscription(dynamodb_table):
    from core.repositories import billing_repo
    from core.services.provision_gate import is_subscription_active

    await billing_repo.create_if_not_exists("user_exp3", "cus_exp3")
    await billing_repo.set_subscription(owner_id="user_exp3", subscription_id="sub_live", status="active")
    await billing_repo.set_apple_subscription(
        "user_exp3", original_transaction_id="2000000000000103", status="active", expires_at=_PAST_MS
    )
    row = await billing_repo.get_by_owner_id("user_exp3")
    assert row["subscription_status"] == "active"
    assert row["subscription_source"] == "stripe"
    assert is_subscription_active(row) is True
    # And a later Stripe write still passes the cross-provider CAS.
    await billing_repo.set_subscription(owner_id="user_exp3", subscription_id="sub_live", status="past_due")
    row = await billing_repo.get_by_owner_id("user_exp3")
    assert row["stripe_subscription_status"] == "past_due"


@pytest.mark.asyncio
async def test_resubscribe_after_expiry_derives_active_from_the_new_expiry(dynamodb_table):
    """The derived status must be computed from the expiry being WRITTEN, not the
    stale one on the row — or a resubscriber stays gated until the next renewal."""
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("user_exp4", "cus_exp4")
    tx = "2000000000000104"
    await billing_repo.set_apple_subscription(
        "user_exp4", original_transaction_id=tx, status="active", expires_at=_PAST_MS, signed_date=1
    )
    await billing_repo.set_apple_subscription(
        "user_exp4", original_transaction_id=tx, status="canceled", expires_at=_PAST_MS, signed_date=2
    )
    await billing_repo.set_apple_subscription(
        "user_exp4", original_transaction_id=tx, status="active", expires_at=_FUTURE_MS, signed_date=3
    )
    table_row = dynamodb_table.get_item(Key={"owner_id": "user_exp4"})["Item"]
    assert table_row["subscription_status"] == "active"
    assert (await billing_repo.get_by_owner_id("user_exp4"))["subscription_status"] == "active"


@pytest.mark.asyncio
async def test_front_desk_claim_is_single_flight(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_fd", "cus_fd")
    assert await billing_repo.claim_front_desk("org_fd") == 0
    assert await billing_repo.claim_front_desk("org_fd") is None
    await billing_repo.release_front_desk_claim("org_fd")
    assert (await billing_repo.get_by_owner_id("org_fd"))["front_desk"] is False
    assert await billing_repo.claim_front_desk("org_fd") == 1
    assert await billing_repo.claim_front_desk("ghost") is None


@pytest.mark.asyncio
async def test_front_desk_claim_stamps_time_and_release_bumps_generation(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_fd2", "cus_fd2")
    assert await billing_repo.claim_front_desk("org_fd2") == 0
    row = await billing_repo.get_by_owner_id("org_fd2")
    assert row["front_desk_claim_at"] and row["front_desk"] is True
    await billing_repo.release_front_desk_claim("org_fd2")
    row = await billing_repo.get_by_owner_id("org_fd2")
    assert "front_desk_claim_at" not in row and int(row["front_desk_gen"]) == 1
    await billing_repo.release_front_desk_claim("ghost")  # no row, no claim: a no-op
    assert await billing_repo.get_by_owner_id("ghost") is None


@pytest.mark.asyncio
async def test_stale_claim_is_retaken_in_place_without_bumping_the_generation(dynamodb_table):
    from core.repositories import billing_repo

    await billing_repo.create_if_not_exists("org_fd3", "cus_fd3")
    await billing_repo.release_front_desk_claim("org_fd3")  # gen 1
    assert await billing_repo.claim_front_desk("org_fd3") == 1
    first_at = (await billing_repo.get_by_owner_id("org_fd3"))["front_desk_claim_at"]
    assert await billing_repo.claim_front_desk("org_fd3", stale_before="1999-01-01T00:00:00+00:00") is None
    assert await billing_repo.claim_front_desk("org_fd3", stale_before="2999-01-01T00:00:00+00:00") == 1
    assert (await billing_repo.get_by_owner_id("org_fd3"))["front_desk_claim_at"] >= first_at

    # front_desk = true with no timestamp is also stale
    await billing_repo.create_if_not_exists("org_fd4", "cus_fd4")
    dynamodb_table.update_item(
        Key={"owner_id": "org_fd4"}, UpdateExpression="SET front_desk = :t", ExpressionAttributeValues={":t": True}
    )
    assert await billing_repo.claim_front_desk("org_fd4") is None
    assert await billing_repo.claim_front_desk("org_fd4", stale_before="1999-01-01T00:00:00+00:00") == 0
