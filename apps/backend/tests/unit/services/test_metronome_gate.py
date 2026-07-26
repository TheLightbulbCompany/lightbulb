"""is_owner_blocked: the shared credits gate over the Metronome fast-gate
row — blocked from the cached flag, lazy getNetBalance refresh when stale,
stale-row fallback on Metronome outage, the pull-based Bedrock restore
(with confirming re-read) when a refresh shows a revoked owner has paid up,
and the revoke re-assert when a refresh comes back non-positive."""

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
    net_balance_cents: float | Exception | list | None = None,
    container_row: dict | None = None,
):
    """Patch the gate's collaborators; yields a namespace of the mocks.

    ``net_balance_cents`` accepts a list for multi-read sequences (the
    restore path does a confirming second getNetBalance). The mocked
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

    account = {"owner_id": OWNER}
    if metronome_customer_id:
        account["metronome_customer_id"] = metronome_customer_id

    restore = AsyncMock()
    enforce = AsyncMock()
    with (
        patch("core.services.metronome_gate.MetronomeFastGate", return_value=fast_gate),
        patch(
            "core.services.metronome_gate.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=account),
        ) as billing_read,
        patch("core.billing.metronome_client.for_settings", return_value=client),
        patch(
            "core.repositories.container_repo.get_by_owner_id",
            new=AsyncMock(return_value=container_row),
        ),
        patch("core.billing.bedrock_aip.restore_owner_bedrock", new=restore),
        patch("core.services.metronome_gate.enforce_on_low_balance", new=enforce),
    ):
        mocks = MagicMock()
        mocks.fast_gate = fast_gate
        mocks.client = client
        mocks.billing_read = billing_read
        mocks.restore = restore
        mocks.enforce = enforce
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
async def test_missing_row_without_metronome_customer_not_blocked():
    """A brand-new owner has neither a fast-gate row nor a Metronome
    customer — the gate must not block on a missing projection."""
    with _gate(row=None, metronome_customer_id=None) as m:
        assert await is_owner_blocked(OWNER) is False
    m.client.get_net_balance.assert_not_called()


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
    with _gate(row=_stale_row(5_000_000), metronome_customer_id="mc-uuid", net_balance_cents=0) as m:
        assert await is_owner_blocked(OWNER) is True
    m.restore.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_non_positive_reasserts_enforcement():
    """The low-balance webhook is one-shot and can be lost (stale-skip race,
    or a stale-positive refresh un-revoking a drained owner) — every fresh
    non-positive gate read must re-fire enforce_on_low_balance (itself
    idempotent on the bedrock_revoked stamp)."""
    with _gate(row=_stale_row(5_000_000), metronome_customer_id="mc-uuid", net_balance_cents=0) as m:
        assert await is_owner_blocked(OWNER) is True
    m.enforce.assert_awaited_once_with(OWNER, 0)


@pytest.mark.asyncio
async def test_refresh_positive_never_fires_enforcement():
    with _gate(
        row=_stale_row(0),
        metronome_customer_id="mc-uuid",
        net_balance_cents=1000,
        container_row={"owner_id": OWNER, "bedrock_revoked": False},
    ) as m:
        assert await is_owner_blocked(OWNER) is False
    m.enforce.assert_not_called()


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
async def test_refresh_positive_restores_revoked_owner_after_confirm_read():
    """The pull-based re-arm: a paid top-up has no webhook, so the first
    gate refresh that sees balance > 0 on a revoked owner un-revokes — but
    only after a confirming second getNetBalance also says > 0."""
    with _gate(
        row=_stale_row(0),
        metronome_customer_id="mc-uuid",
        net_balance_cents=[1000, 1000],
        container_row={"owner_id": OWNER, "bedrock_revoked": True},
    ) as m:
        assert await is_owner_blocked(OWNER) is False
    m.restore.assert_awaited_once_with(OWNER)
    # Exactly two reads: the refresh + the restore's confirming re-read.
    assert m.client.get_net_balance.await_count == 2


@pytest.mark.asyncio
async def test_refresh_positive_but_confirm_read_non_positive_skips_restore():
    """getNetBalance is async/no-SLA: a single stale-positive read against a
    truly-drained owner must NOT restore — that would permanently disarm the
    one-shot low-balance alert (it already fired for this crossing)."""
    with _gate(
        row=_stale_row(0),
        metronome_customer_id="mc-uuid",
        net_balance_cents=[1000, 0],
        container_row={"owner_id": OWNER, "bedrock_revoked": True},
    ) as m:
        assert await is_owner_blocked(OWNER) is False
    m.restore.assert_not_called()


@pytest.mark.asyncio
async def test_confirm_read_failure_skips_restore_without_blocking():
    """A failed confirming re-read aborts the restore (the owner stays
    revoked until the next refresh) but never flips the gate answer."""
    with _gate(
        row=_stale_row(0),
        metronome_customer_id="mc-uuid",
        net_balance_cents=1000,
        container_row={"owner_id": OWNER, "bedrock_revoked": True},
    ) as m:
        m.client.get_net_balance.side_effect = [{"balance": 1000}, RuntimeError("metronome blip")]
        assert await is_owner_blocked(OWNER) is False
    m.restore.assert_not_called()


@pytest.mark.asyncio
async def test_refresh_positive_skips_restore_when_not_revoked():
    with _gate(
        row=_stale_row(0),
        metronome_customer_id="mc-uuid",
        net_balance_cents=1000,
        container_row={"owner_id": OWNER, "bedrock_revoked": False},
    ) as m:
        assert await is_owner_blocked(OWNER) is False
    m.restore.assert_not_called()


@pytest.mark.asyncio
async def test_restore_failure_does_not_block_the_gate():
    """Restore is best-effort: an IAM blip must not turn a positive-balance
    gate read into blocked (or an exception)."""
    with _gate(
        row=_stale_row(0),
        metronome_customer_id="mc-uuid",
        net_balance_cents=1000,
        container_row={"owner_id": OWNER, "bedrock_revoked": True},
    ) as m:
        m.restore.side_effect = RuntimeError("iam down")
        assert await is_owner_blocked(OWNER) is False


@pytest.mark.asyncio
async def test_row_blocked_at_reserve_estimate():
    """The gate denies paid admission on `remaining - reserved - estimate <= 0`,
    so an owner holding exactly one reservation's worth is already refused. The
    old `remaining <= 0` predicate left the same $0.30 dead zone the trial side
    had: a paying customer with $0.20 left got 'Provider rate limit reached'
    and no out-of-credits banner."""
    from core.services.metronome_gate import _row_blocked
    from core.services.trial_cap import RESERVE_ESTIMATE_MICROCENTS

    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS}) is True
    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS - 1}) is True
    assert _row_blocked({"remaining_microcents": RESERVE_ESTIMATE_MICROCENTS + 1}) is False


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
