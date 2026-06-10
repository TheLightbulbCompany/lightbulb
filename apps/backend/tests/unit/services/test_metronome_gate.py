"""is_owner_blocked: the shared credits gate over the Metronome fast-gate
row — blocked from the cached flag, lazy getNetBalance refresh when stale,
stale-row fallback on Metronome outage, and the pull-based Bedrock restore
when a refresh shows a revoked owner has paid up."""

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
    net_balance_cents: float | Exception | None = None,
    container_row: dict | None = None,
):
    """Patch the gate's collaborators; yields a namespace of the mocks."""
    fast_gate = MagicMock()
    fast_gate.get_row = AsyncMock(return_value=row)
    fast_gate.set_balance = AsyncMock()

    client = MagicMock()
    if isinstance(net_balance_cents, Exception):
        client.get_net_balance = AsyncMock(side_effect=net_balance_cents)
    else:
        client.get_net_balance = AsyncMock(
            return_value={} if net_balance_cents is None else {"net_balance": net_balance_cents}
        )

    account = {"owner_id": OWNER}
    if metronome_customer_id:
        account["metronome_customer_id"] = metronome_customer_id

    restore = AsyncMock()
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
    ):
        mocks = MagicMock()
        mocks.fast_gate = fast_gate
        mocks.client = client
        mocks.billing_read = billing_read
        mocks.restore = restore
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
async def test_refresh_positive_restores_revoked_owner():
    """The pull-based re-arm: a paid top-up has no webhook, so the first
    gate refresh that sees balance > 0 on a revoked owner un-revokes."""
    with _gate(
        row=_stale_row(0),
        metronome_customer_id="mc-uuid",
        net_balance_cents=1000,
        container_row={"owner_id": OWNER, "bedrock_revoked": True},
    ) as m:
        assert await is_owner_blocked(OWNER) is False
    m.restore.assert_awaited_once_with(OWNER)


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
