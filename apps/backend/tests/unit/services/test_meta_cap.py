"""Tests for the free-tier $3/day Meta cost cap: reserve-as-gate at
pre-send + reconcile at post-turn.

``reserve_or_block`` serializes concurrent turns through the atomic
``add_meta_daily_spend`` counter (ADD-then-check on the returned new
total) rather than a read-then-check, so there is no TOCTOU window
between concurrent turns for the same owner. ``reconcile`` trues the
reserve down to the actual cost, or -- if the actual cost is unknown
(0/None) -- fails closed by leaving the reserve in place.
"""

import pytest

from core.billing.meta_pricing import CAP_MICROCENTS, RESERVE_MICROCENTS
from core.services import meta_cap


@pytest.fixture(autouse=True)
def _clear_reserves():
    """``_reserves`` is module-level state; prevent leakage across tests."""
    meta_cap._reserves.clear()
    yield
    meta_cap._reserves.clear()


def _rec(fn, rolled, o, d, delta):
    if delta < 0:
        rolled.append(delta)
    return fn(o, d, delta)


@pytest.mark.asyncio
async def test_first_turn_reserves_and_allows(monkeypatch):
    totals = {}

    async def fake_add(o, d, delta):
        totals[d] = totals.get(d, 0) + delta
        return totals[d]

    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)
    assert await meta_cap.reserve_or_block("user_x", "sk1") is False  # allowed


@pytest.mark.asyncio
async def test_reconcile_trues_down_to_actual(monkeypatch):
    totals = {}

    async def fake_add(o, d, delta):
        totals[d] = totals.get(d, 0) + delta
        return totals[d]

    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)
    await meta_cap.reserve_or_block("user_x", "sk1")  # +RESERVE
    await meta_cap.reconcile("user_x", "sk1", actual_microcents=1000)
    assert sum(totals.values()) == 1000  # reserve replaced by actual


@pytest.mark.asyncio
async def test_block_when_already_at_cap(monkeypatch):
    async def fake_add(o, d, delta):
        return CAP_MICROCENTS + delta  # spent_before >= CAP

    rolled = []
    monkeypatch.setattr(
        "core.services.meta_cap.billing_repo.add_meta_daily_spend",
        lambda o, d, delta: _rec(fake_add, rolled, o, d, delta),
    )
    assert await meta_cap.reserve_or_block("user_x", "sk1") is True  # blocked
    assert rolled and rolled[-1] < 0  # reserve rolled back


@pytest.mark.asyncio
async def test_reconcile_missing_cost_is_failclosed(monkeypatch):
    totals = {}

    async def fake_add(o, d, delta):
        totals[d] = totals.get(d, 0) + delta
        return totals[d]

    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)
    await meta_cap.reserve_or_block("user_x", "sk1")
    await meta_cap.reconcile("user_x", "sk1", actual_microcents=0)  # cost unknown
    assert sum(totals.values()) == RESERVE_MICROCENTS  # reserve kept
