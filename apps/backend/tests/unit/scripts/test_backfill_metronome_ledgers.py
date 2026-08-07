"""Selection predicate for the Metronome-ledger backfill.

The predicate is the whole risk surface of this script: too narrow and paid
owners stay locked out when the fences flip closed, too wide and we mint
ledgers for owners we never bill (which then feed the low-balance webhook as
permanent zero-balance customers).
"""

from scripts.backfill_metronome_ledgers import _needs_backfill


def _row(**over) -> dict:
    base = {
        "owner_id": "user_x",
        "subscription_status": "active",
        "provider_choice": "bedrock_claude",
    }
    base.update(over)
    return base


class TestNeedsBackfill:
    def test_paid_bedrock_owner_with_no_ledger_is_selected(self):
        assert _needs_backfill(_row()) is True

    def test_customer_without_contract_is_selected(self):
        """The partial-provisioning state _provision persists when the Stripe
        link fails. Both fences treat it as unprovisioned, so the backfill must
        agree — re-running ensure completes the deferred contract."""
        assert _needs_backfill(_row(metronome_customer_id="mc-1")) is True

    def test_complete_ledger_is_skipped(self):
        assert _needs_backfill(_row(metronome_customer_id="mc-1", metronome_contract_id="ct-1")) is False

    def test_trial_local_is_never_selected(self):
        """Free-tier owners are governed by the $3/day meter cap and must never
        get a ledger: ensure_metronome_customer short-circuits them, and a zero
        balance would read as 'out of credits' to the low-balance enforcer.

        They can carry provider_choice=bedrock_claude from an abandoned
        Checkout (/billing/trial-checkout writes it before redirecting), so the
        status check has to be the one that excludes them.
        """
        assert _needs_backfill(_row(subscription_status="trial_local")) is False

    def test_non_bedrock_providers_are_never_selected(self):
        for provider in ("chatgpt_oauth", "byo_key", None):
            assert _needs_backfill(_row(provider_choice=provider)) is False, provider

    def test_dead_subscriptions_are_skipped(self):
        for status in ("canceled", "incomplete", "incomplete_expired", "paused", None):
            assert _needs_backfill(_row(subscription_status=status)) is False, status

    def test_recoverable_paid_statuses_are_selected(self):
        """past_due / unpaid still have a live subscription and can recover. A
        ledger they cannot yet spend from blocks nothing, and having it ready
        means recovery does not also require a provisioning round trip."""
        for status in ("active", "trialing", "past_due", "unpaid"):
            assert _needs_backfill(_row(subscription_status=status)) is True, status
