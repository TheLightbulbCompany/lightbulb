"""is_owner_blocked: the shared credits gate over the Metronome fast-gate
row — blocked from the cached flag, lazy getNetBalance refresh when stale,
and stale-row fallback on Metronome outage."""

import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.metronome_gate import is_owner_blocked

OWNER = "org_Y"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _fresh_row(remaining: int) -> dict:
    return {"owner_id": OWNER, "remaining_microcents": remaining, "updated_at": _now_iso()}


def _stale_row(remaining: int) -> dict:
    return {"owner_id": OWNER, "remaining_microcents": remaining, "updated_at": "2020-01-01T00:00:00Z"}


@contextlib.contextmanager
def _gate(
    *,
    row: dict | None,
    metronome_customer_id: str | None = None,
    metronome_contract_id: str | None = "ct-default",
    net_balance_cents: float | Exception | list | None = None,
):
    """Patch the gate's collaborators; yields a namespace of the mocks.

    ``net_balance_cents`` accepts a list for multi-read sequences. The mocked
    response uses the live-verified getNetBalance shape: the cents number
    rides under ``balance``.
    """
    fast_gate = MagicMock()
    fast_gate.get_row = AsyncMock(return_value=row)
    fast_gate.set_balance = AsyncMock()

    client = MagicMock()
    if isinstance(net_balance_cents, Exception):
        client.get_net_balance = AsyncMock(side_effect=net_balance_cents)
    elif isinstance(net_balance_cents, list):
        client.get_net_balance = AsyncMock(side_effect=[{"balance": c} for c in net_balance_cents])
    else:
        client.get_net_balance = AsyncMock(
            return_value={} if net_balance_cents is None else {"balance": net_balance_cents}
        )

    # A customer id implies a COMPLETE ledger unless a test says otherwise:
    # is_owner_blocked requires customer AND contract, and every pre-existing
    # case that sets a customer means "fully provisioned". Pass
    # metronome_contract_id=None to opt into the partial-provisioning state.
    account = {"owner_id": OWNER}
    if metronome_customer_id:
        account["metronome_customer_id"] = metronome_customer_id
        if metronome_contract_id:
            account["metronome_contract_id"] = metronome_contract_id

    with (
        patch("core.services.metronome_gate.MetronomeFastGate", return_value=fast_gate),
        patch(
            "core.services.metronome_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ) as billing_read,
        patch("core.billing.metronome_client.for_settings", return_value=client),
    ):
        mocks = MagicMock()
        mocks.fast_gate = fast_gate
        mocks.client = client
        mocks.billing_read = billing_read
        yield mocks


@pytest.mark.asyncio
async def test_fresh_row_non_positive_blocks_without_refresh():
    with _gate(row=_fresh_row(0)) as m:
        assert await is_owner_blocked(OWNER) is True
    # A fresh row answers directly — no billing read, no Metronome call.
    m.billing_read.assert_not_called()
    m.client.get_net_balance.assert_not_called()


@pytest.mark.asyncio
async def test_fresh_row_positive_not_blocked():
    with _gate(row=_fresh_row(5_000_000)) as m:
        assert await is_owner_blocked(OWNER) is False
    m.client.get_net_balance.assert_not_called()


@pytest.mark.asyncio
async def test_no_metronome_ledger_blocks():
    """No ledger means no credits can exist, and every caller has already
    established the owner draws managed credits — i.e. billing inference to US.

    This used to return False, reasoning that a brand-new owner must not be
    gated by a missing projection. That conflated "projection not written yet"
    with "no ledger at all"; the second state handed owners unmetered
    Bedrock — Opus-tier for the CEO agent — on our account.

    Not a dead end: the gate renders credits_required and the paid workspace
    lifecycle provisions the Metronome customer before a top-up is allowed.
    """
    with _gate(row=None, metronome_customer_id=None) as m:
        assert await is_owner_blocked(OWNER) is True
    m.client.get_net_balance.assert_not_called()


@pytest.mark.asyncio
async def test_customer_without_contract_blocks():
    """The partial-provisioning state _provision persists when the Stripe link
    fails: customer only, contract deferred. No contract ⇒ no commits ⇒ no
    balance, and grant_prepaid_credits refuses it — so the customer id alone is
    not evidence of a spendable ledger. Lockstep with paidReserve."""
    with _gate(row=None, metronome_customer_id="mc-1", metronome_contract_id=None):
        assert await is_owner_blocked(OWNER) is True


@pytest.mark.asyncio
async def test_no_ledger_beats_a_positive_stale_row():
    """A leftover positive projection (e.g. from a since-wiped Metronome
    customer) must not re-open unmetered spend for an owner with no ledger."""
    with _gate(row=_stale_row(5_000_000), metronome_customer_id=None):
        assert await is_owner_blocked(OWNER) is True


@pytest.mark.asyncio
async def test_billing_read_failure_falls_back_to_the_stale_row():
    """The provisioning check fails OPEN on a DDB blip specifically: a
    transient infra error must not lock out a customer who already paid. Only
    a CONFIRMED absent ledger blocks."""
    with (
        _gate(row=_stale_row(5_000_000), metronome_customer_id=None),
        patch(
            "core.services.metronome_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(side_effect=RuntimeError("dynamo down")),
        ),
    ):
        assert await is_owner_blocked(OWNER) is False


@pytest.mark.asyncio
async def test_stale_row_refreshes_from_metronome_and_rewrites_row():
    """Stale row + Metronome customer ⇒ getNetBalance decides, and the
    fast-gate row is rewritten so the next read is cached again.
    250 cents → 2_500_000 microcents (a 100x slip would show)."""
    with _gate(row=_stale_row(0), metronome_customer_id="mc-uuid", net_balance_cents=250) as m:
        assert await is_owner_blocked(OWNER) is False
    m.client.get_net_balance.assert_awaited_once_with(customer_id="mc-uuid")
    m.fast_gate.set_balance.assert_awaited_once_with(OWNER, remaining_microcents=2_500_000)


@pytest.mark.asyncio
async def test_stale_row_refresh_non_positive_blocks():
    with _gate(row=_stale_row(5_000_000), metronome_customer_id="mc-uuid", net_balance_cents=0):
        assert await is_owner_blocked(OWNER) is True


@pytest.mark.asyncio
async def test_metronome_down_uses_stale_row_never_raises():
    """A Metronome outage must not 500 (or wrongly flip) the gate — the
    stale row's answer stands, in BOTH directions."""
    with _gate(
        row=_stale_row(0),
        metronome_customer_id="mc-uuid",
        net_balance_cents=RuntimeError("metronome down"),
    ):
        assert await is_owner_blocked(OWNER) is True
    with _gate(
        row=_stale_row(1_000_000),
        metronome_customer_id="mc-uuid",
        net_balance_cents=RuntimeError("metronome down"),
    ):
        assert await is_owner_blocked(OWNER) is False


@pytest.mark.asyncio
async def test_row_blocked_at_reserve_estimate():
    """The gate denies paid admission on `remaining - reserved - estimate <= 0`,
    so an owner holding exactly one reservation's worth is already refused. The
    old `remaining <= 0` predicate left the same $0.30 dead zone the trial side
    had: a paying customer with $0.20 left got 'Provider rate limit reached'
    and no out-of-credits banner."""
    from core.services.metronome_gate import _row_blocked
    from core.services.metronome_gate import RESERVE_ESTIMATE_MICROCENTS

    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS}) is True
    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS - 1}) is True
    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS + 1}) is False
    # A settlement overshoot can leave the pool negative (boto3 hands back Decimal).
    from decimal import Decimal

    assert _row_blocked({"remaining_microcents": Decimal("-5000000000")}) is True


@pytest.mark.asyncio
async def test_row_blocked_missing_row_still_admits():
    """A brand-new owner with no projection row must not be gated by its
    absence — unchanged by the threshold move."""
    from core.services.metronome_gate import _row_blocked

    assert _row_blocked(None) is False
    assert _row_blocked({}) is False


@pytest.mark.asyncio
async def test_refresh_boundary_pins_reserve_estimate_via_is_owner_blocked():
    """`test_row_blocked_at_reserve_estimate` pins the boundary by calling
    `_row_blocked` directly — it never exercises `is_owner_blocked`'s OWN
    post-refresh comparison (metronome_gate.py, the `refreshed <=
    RESERVE_ESTIMATE_MICROCENTS` line). A revert of that line alone back to
    `<= 0` — while leaving `_row_blocked` untouched — would pass every other
    test in this file, including the two above. Drive the stale-row +
    getNetBalance REFRESH branch (not the direct-call path) at the same
    boundary to close that gap.

    30 cents / 29.9999 cents / 30.0001 cents -> 300_000 / 299_999 / 300_001
    microcents (RESERVE_ESTIMATE_MICROCENTS exactly, one under, one over)."""
    with _gate(row=_stale_row(0), metronome_customer_id="mc-uuid", net_balance_cents=30) as m:
        assert await is_owner_blocked(OWNER) is True
    m.fast_gate.set_balance.assert_awaited_once_with(OWNER, remaining_microcents=300_000)

    with _gate(row=_stale_row(0), metronome_customer_id="mc-uuid", net_balance_cents=29.9999) as m:
        assert await is_owner_blocked(OWNER) is True

    with _gate(row=_stale_row(0), metronome_customer_id="mc-uuid", net_balance_cents=30.0001) as m:
        assert await is_owner_blocked(OWNER) is False
