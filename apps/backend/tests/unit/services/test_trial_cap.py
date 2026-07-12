"""Tests for the free-tier $3/day cap read primitive.

``is_over_daily_cap`` is the ONLY cap primitive: it is what gate_chat's
Layer 2, the Teams BFF guards, the
chat-steer path and the container-wake check all call. It is a pure read of
the daily-spend counter the Bedrock meter Lambda maintains — the backend
never writes to that counter and places no reserve.
"""

import pytest

from core.services import trial_cap
from core.services.trial_cap import CAP_MICROCENTS


@pytest.mark.asyncio
async def test_is_over_daily_cap_true_for_trial_local_at_cap(monkeypatch):
    """A trial_local owner whose spend has reached exactly CAP_MICROCENTS is
    over the cap (the block condition is >=)."""

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_meta_daily_spend(owner_id, date):
        return CAP_MICROCENTS

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_meta_daily_spend", fake_get_meta_daily_spend)

    assert await trial_cap.is_over_daily_cap("owner1") is True


@pytest.mark.asyncio
async def test_is_over_daily_cap_true_for_trial_local_over_cap(monkeypatch):
    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_meta_daily_spend(owner_id, date):
        return CAP_MICROCENTS + 1

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_meta_daily_spend", fake_get_meta_daily_spend)

    assert await trial_cap.is_over_daily_cap("owner1") is True


@pytest.mark.asyncio
async def test_is_over_daily_cap_false_for_trial_local_under_cap(monkeypatch):
    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_meta_daily_spend(owner_id, date):
        return CAP_MICROCENTS - 1

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_meta_daily_spend", fake_get_meta_daily_spend)

    assert await trial_cap.is_over_daily_cap("owner1") is False


@pytest.mark.asyncio
async def test_is_over_daily_cap_false_for_paid_owner_even_if_over_cap(monkeypatch):
    """A paid (active) owner is never blocked by this check, even if their
    (hypothetical) daily-spend counter is over CAP_MICROCENTS -- the free-tier
    cap only applies to trial_local owners."""

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "active"}

    spend_calls = []

    async def fake_get_meta_daily_spend(owner_id, date):
        spend_calls.append((owner_id, date))
        return CAP_MICROCENTS + 1_000_000

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_meta_daily_spend", fake_get_meta_daily_spend)

    assert await trial_cap.is_over_daily_cap("owner1") is False
    assert spend_calls == []  # short-circuits before the spend read


@pytest.mark.asyncio
async def test_is_over_daily_cap_false_for_no_account(monkeypatch):
    async def fake_get_by_owner_id(owner_id):
        return None

    spend_calls = []

    async def fake_get_meta_daily_spend(owner_id, date):
        spend_calls.append((owner_id, date))
        return CAP_MICROCENTS + 1

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_meta_daily_spend", fake_get_meta_daily_spend)

    assert await trial_cap.is_over_daily_cap("owner1") is False
    assert spend_calls == []  # short-circuits before the spend read


@pytest.mark.asyncio
async def test_no_backend_path_writes_the_counter(monkeypatch):
    """The meter Lambda is the SOLE writer. ``is_over_daily_cap`` — the only
    surviving cap primitive — must never ADD to the counter, or the Lambda's
    tally would double-count."""
    writes = []

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_meta_daily_spend(owner_id, date):
        return 0

    async def fake_add(owner_id, date, delta):
        writes.append((owner_id, date, delta))
        return delta

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_meta_daily_spend", fake_get_meta_daily_spend)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.add_meta_daily_spend", fake_add)

    await trial_cap.is_over_daily_cap("owner1")

    assert writes == []


@pytest.mark.asyncio
async def test_trial_block_reason_catches_an_expired_trial(monkeypatch):
    """The container-start chokepoint checked only the CAP, so an EXPIRED trial
    could wake its own container through a start path the provision gate would
    have refused. One predicate, one read, both reasons — so the gate and the
    chokepoint can never disagree about whether a trial owner may have a box."""
    import time as _time

    async def fake_account(owner_id):
        return {
            "subscription_status": "trial_local",
            "trial_end": int(_time.time()) - 60,  # window elapsed a minute ago
        }

    async def fake_spend(owner_id, date):
        return 0  # well under the cap — expiry must still block

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_meta_daily_spend", fake_spend)

    assert await trial_cap.trial_block_reason("user_x") == trial_cap.TRIAL_EXPIRED
    # ...and the cap-only predicate does NOT catch it — which is exactly the gap.
    assert await trial_cap.is_over_daily_cap("user_x") is False


@pytest.mark.asyncio
async def test_trial_block_reason_none_for_a_healthy_trial_and_for_paid(monkeypatch):
    async def fake_spend(owner_id, date):
        return 0

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_meta_daily_spend", fake_spend)

    async def healthy_trial(owner_id):
        return {"subscription_status": "trial_local", "trial_end": 2**33}  # far future

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", healthy_trial)
    assert await trial_cap.trial_block_reason("user_x") is None

    async def paid(owner_id):
        return {"subscription_status": "active", "trial_end": 1}  # stale stamp, paid now

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", paid)
    assert await trial_cap.trial_block_reason("user_x") is None
