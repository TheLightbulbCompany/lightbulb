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


@pytest.mark.asyncio
async def test_sequential_reserves_are_monotonic_and_block_at_cap(monkeypatch):
    """A stateful fake counter (keyed by date) makes net effects real: each
    sequential reserve for one owner bumps the running total by exactly
    RESERVE_MICROCENTS, until spent_before >= CAP, at which point the next
    reserve is blocked *and* rolled back -- leaving the counter unchanged by
    the blocked turn."""
    totals = {}

    async def fake_add(o, d, delta):
        totals[d] = totals.get(d, 0) + delta
        return totals[d]

    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)

    date = meta_cap.utc_today()
    seen_totals = []
    i = 0
    blocked = False
    while not blocked:
        blocked = await meta_cap.reserve_or_block("user_x", f"sk{i}")
        i += 1
        if not blocked:
            seen_totals.append(totals[date])

    # Each successive allowed reserve strictly increases the running total.
    assert seen_totals == sorted(seen_totals)
    assert len(set(seen_totals)) == len(seen_totals)
    assert seen_totals[-1] >= CAP_MICROCENTS  # the cap was actually reached

    # The blocking reserve itself must have been rolled back -- net
    # unchanged from the last allowed total.
    assert totals[date] == seen_totals[-1]


@pytest.mark.asyncio
async def test_boundary_spent_before_cap_minus_one_allows(monkeypatch):
    """spent_before == CAP - 1 is still under the cap -- must be allowed,
    not blocked (the block condition is spent_before >= CAP)."""

    async def fake_add(o, d, delta):
        return (CAP_MICROCENTS - 1) + delta  # spent_before = CAP - 1

    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)
    assert await meta_cap.reserve_or_block("user_x", "sk1") is False  # allowed


@pytest.mark.asyncio
async def test_blocked_turn_leaves_no_reserve_entry(monkeypatch):
    """After a blocked reserve_or_block, no entry is stored for the
    (owner, session_key) pair -- a subsequent reconcile call is a no-op."""
    totals = {}

    async def fake_add(o, d, delta):
        totals[d] = totals.get(d, 0) + delta
        return totals[d]

    async def fake_add_at_cap(o, d, delta):
        # Force spent_before >= CAP on the very first (reserving) call.
        totals[d] = totals.get(d, 0) + delta
        return CAP_MICROCENTS + delta

    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add_at_cap)
    blocked = await meta_cap.reserve_or_block("user_x", "sk1")
    assert blocked is True
    assert ("user_x", "sk1") not in meta_cap._reserves

    total_before_reconcile = totals[meta_cap.utc_today()]
    await meta_cap.reconcile("user_x", "sk1", actual_microcents=999)
    assert totals[meta_cap.utc_today()] == total_before_reconcile  # no-op


@pytest.mark.asyncio
async def test_reconcile_uses_stored_date_across_midnight(monkeypatch):
    """The reserve stores the date it was placed on (D1). If reconcile runs
    after midnight (utc_today() now returns D2), the true-down delta must
    still land on D1, not D2."""
    totals = {}

    async def fake_add(o, d, delta):
        totals[d] = totals.get(d, 0) + delta
        return totals[d]

    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)
    monkeypatch.setattr("core.services.meta_cap.utc_today", lambda: "2026-07-10")
    await meta_cap.reserve_or_block("user_x", "sk1")  # reserve stamped D1
    assert totals["2026-07-10"] == RESERVE_MICROCENTS

    monkeypatch.setattr("core.services.meta_cap.utc_today", lambda: "2026-07-11")
    await meta_cap.reconcile("user_x", "sk1", actual_microcents=1000)

    assert totals["2026-07-10"] == 1000  # delta applied to D1
    assert "2026-07-11" not in totals  # D2 untouched


@pytest.mark.asyncio
async def test_cross_owner_reconcile_does_not_collide(monkeypatch):
    """reconcile(ownerB, session_key, ...) must never pop ownerA's reserve
    or apply a delta against ownerB's counter for a session_key ownerB
    never reserved. This is the core money-boundary fix: keying _reserves
    by session_key alone let a wrong-owner reconcile steal another owner's
    reserve and apply a large negative delta to the wrong owner."""
    totals = {}

    async def fake_add(o, d, delta):
        key = (o, d)
        totals[key] = totals.get(key, 0) + delta
        return totals[key]

    monkeypatch.setattr("core.services.meta_cap.billing_repo.add_meta_daily_spend", fake_add)

    blocked = await meta_cap.reserve_or_block("ownerA", "sk")
    assert blocked is False
    date = meta_cap.utc_today()
    assert totals[("ownerA", date)] == RESERVE_MICROCENTS
    assert ("ownerA", "sk") in meta_cap._reserves

    # ownerB never reserved "sk" -- reconcile for ownerB must be a no-op.
    await meta_cap.reconcile("ownerB", "sk", actual_microcents=999)

    assert ("ownerB", date) not in totals  # ownerB's counter untouched
    assert totals[("ownerA", date)] == RESERVE_MICROCENTS  # ownerA unaffected
    assert ("ownerA", "sk") in meta_cap._reserves  # ownerA's reserve still present
