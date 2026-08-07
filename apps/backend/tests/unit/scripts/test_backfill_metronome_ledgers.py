"""Selection predicate for the Metronome-ledger backfill.

The predicate is the whole risk surface of this script: too narrow and paid
owners stay locked out when the fences flip closed, too wide and we mint
ledgers for owners we never bill (which then feed the low-balance webhook as
permanent zero-balance customers).
"""

import pytest
from unittest.mock import AsyncMock, patch

from scripts.backfill_metronome_ledgers import _needs_backfill, main


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


class TestTableResolution:
    """The script must scan the table for the env it was ASKED for.

    Table naming reads DYNAMODB_TABLE_PREFIX, which defaults to "isol8-dev-";
    ENVIRONMENT does not affect it. Setting only ENVIRONMENT made an
    `--env prod` run scan the DEV table and report "0 need a ledger" — a clean
    bill of health for a fleet it never looked at, which is precisely the
    signal an operator uses to decide the fences are safe to deploy.

    The prefix must be set before `core.dynamodb` is imported (it snapshots the
    value at import time), which is why every import in the script is
    function-local. These tests run in a WARM process where that module is
    already imported, so `main()` correctly refuses with rc=2 — that refusal is
    itself the property worth pinning.
    """

    def test_env_sets_the_dynamodb_table_prefix(self, monkeypatch):
        import os

        monkeypatch.delenv("DYNAMODB_TABLE_PREFIX", raising=False)
        monkeypatch.setattr("sys.argv", ["backfill", "--env", "prod", "--dry-run"])
        with patch("scripts.backfill_metronome_ledgers.asyncio.run", return_value=0):
            main()
        assert os.environ["DYNAMODB_TABLE_PREFIX"] == "isol8-prod-"

    def test_inherited_dev_prefix_cannot_win(self, monkeypatch):
        """OVERWRITE, not setdefault — a dev prefix already exported in the
        operator's shell must not silently redirect a prod run."""
        import os

        monkeypatch.setenv("DYNAMODB_TABLE_PREFIX", "isol8-dev-")
        monkeypatch.setattr("sys.argv", ["backfill", "--env", "prod", "--dry-run"])
        with patch("scripts.backfill_metronome_ledgers.asyncio.run", return_value=0):
            main()
        assert os.environ["DYNAMODB_TABLE_PREFIX"] == "isol8-prod-"

    def test_refuses_to_run_on_a_table_mismatch(self, monkeypatch):
        """If the prefix did not take, REFUSE rather than report a confident
        zero about the wrong table — a zero is the signal that greenlights the
        deploy."""
        monkeypatch.setattr("sys.argv", ["backfill", "--env", "prod", "--dry-run"])
        with (
            patch("core.dynamodb.table_name", return_value="isol8-dev-billing-accounts"),
            patch("scripts.backfill_metronome_ledgers.asyncio.run") as run_mock,
        ):
            rc = main()
        assert rc == 2
        run_mock.assert_not_called()

    def test_proceeds_when_the_table_resolves_correctly(self, monkeypatch):
        monkeypatch.setattr("sys.argv", ["backfill", "--env", "prod", "--dry-run"])
        with (
            patch("core.dynamodb.table_name", return_value="isol8-prod-billing-accounts"),
            patch("scripts.backfill_metronome_ledgers.asyncio.run", return_value=0) as run_mock,
        ):
            rc = main()
        assert rc == 0
        run_mock.assert_called_once()


class TestLegacyPaidRows:
    """Selection now delegates to `metronome_provisioning.is_ledger_eligible`,
    the same predicate `ensure_metronome_customer` enforces. These cases pin the
    observable behaviour through `_needs_backfill` so a drift in either the
    shared predicate or this call site shows up here."""

    def test_legacy_paid_bedrock_row_is_selected(self):
        assert _needs_backfill(_row(subscription_status=None, stripe_subscription_id="sub_legacy")) is True

    def test_none_status_without_a_subscription_id_is_not_paying(self):
        assert _needs_backfill(_row(subscription_status=None)) is False

    def test_stale_subscription_id_does_not_revive_a_dead_status(self):
        """The None guard is strict, matching is_subscription_active: a
        non-None status outside the allowlist stays excluded even with a stale
        stripe_subscription_id on the row."""
        for status in ("canceled", "incomplete_expired"):
            assert _needs_backfill(_row(subscription_status=status, stripe_subscription_id="sub_stale")) is False, (
                status
            )


class TestRunVerifiesCompleteness:
    """`ensure_metronome_customer` returns NORMALLY on the deferred-contract
    path, so "no exception" is not "provisioned". Trusting it would exit 0 on a
    half-built ledger — a clean bill of health for an owner both fences still
    treat as unprovisioned, who is then locked out on deploy."""

    @staticmethod
    def _target() -> dict:
        return {
            "owner_id": "user_x",
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }

    @pytest.mark.asyncio
    async def test_half_built_ledger_counts_as_failed(self):
        from scripts import backfill_metronome_ledgers as mod

        with (
            patch.object(mod, "_scan_billing_accounts", new=AsyncMock(return_value=[self._target()])),
            patch(
                "core.billing.metronome_provisioning.ensure_metronome_customer",
                new=AsyncMock(return_value="mc-1"),
            ),
            patch(
                "core.repositories.billing_repo.get_by_owner_id",
                # Contract deferred — exactly what _provision persists when the
                # Stripe link is unavailable.
                new=AsyncMock(return_value={"metronome_customer_id": "mc-1"}),
            ),
        ):
            rc = await mod._run(dry_run=False)

        assert rc == 1, "a half-built ledger must not report success"

    @pytest.mark.asyncio
    async def test_complete_ledger_succeeds(self):
        from scripts import backfill_metronome_ledgers as mod

        with (
            patch.object(mod, "_scan_billing_accounts", new=AsyncMock(return_value=[self._target()])),
            patch(
                "core.billing.metronome_provisioning.ensure_metronome_customer",
                new=AsyncMock(return_value="mc-1"),
            ),
            patch(
                "core.repositories.billing_repo.get_by_owner_id",
                new=AsyncMock(return_value={"metronome_customer_id": "mc-1", "metronome_contract_id": "ct-1"}),
            ),
        ):
            rc = await mod._run(dry_run=False)

        assert rc == 0

    @pytest.mark.asyncio
    async def test_raising_provision_counts_as_failed_without_a_reread(self):
        from scripts import backfill_metronome_ledgers as mod

        reread = AsyncMock()
        with (
            patch.object(mod, "_scan_billing_accounts", new=AsyncMock(return_value=[self._target()])),
            patch(
                "core.billing.metronome_provisioning.ensure_metronome_customer",
                new=AsyncMock(side_effect=RuntimeError("metronome down")),
            ),
            patch("core.repositories.billing_repo.get_by_owner_id", new=reread),
        ):
            rc = await mod._run(dry_run=False)

        assert rc == 1
        reread.assert_not_awaited()
