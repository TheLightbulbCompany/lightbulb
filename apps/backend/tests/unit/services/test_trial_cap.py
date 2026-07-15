"""Tests for the free-tier trial predicate.

``trial_block_reason`` is the ONLY trial primitive: it is what gate_chat's
Layer 2, the container-start chokepoint, /container/recover, the container-wake
check, the chat-steer path and the Teams BFF run guards all call. It is a pure
read of the daily-spend counter the Bedrock meter Lambda maintains (plus the
locally-stamped trial_end) — the backend never writes to that counter and places
no reserve.

There is deliberately no cap-ONLY primitive: one used to exist and four doors
reached for it, checking only half the predicate.
"""

import pytest

from core.services import trial_cap
from core.services.trial_cap import CAP_MICROCENTS


@pytest.mark.asyncio
async def test_trial_block_reason_true_for_trial_local_at_cap(monkeypatch):
    """A trial_local owner whose spend has reached exactly CAP_MICROCENTS is
    over the cap (the block condition is >=)."""

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_trial_daily_spend(owner_id, date):
        return CAP_MICROCENTS

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") == trial_cap.DAILY_CAP


@pytest.mark.asyncio
async def test_trial_block_reason_true_for_trial_local_over_cap(monkeypatch):
    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_trial_daily_spend(owner_id, date):
        return CAP_MICROCENTS + 1

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") == trial_cap.DAILY_CAP


@pytest.mark.asyncio
async def test_trial_block_reason_false_for_trial_local_under_cap(monkeypatch):
    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_trial_daily_spend(owner_id, date):
        return CAP_MICROCENTS - 1

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") is None


@pytest.mark.asyncio
async def test_trial_block_reason_false_for_paid_owner_even_if_over_cap(monkeypatch):
    """A paid (active) owner is never blocked by this check, even if their
    (hypothetical) daily-spend counter is over CAP_MICROCENTS -- the free-tier
    cap only applies to trial_local owners."""

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "active"}

    spend_calls = []

    async def fake_get_trial_daily_spend(owner_id, date):
        spend_calls.append((owner_id, date))
        return CAP_MICROCENTS + 1_000_000

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") is None
    assert spend_calls == []  # short-circuits before the spend read


@pytest.mark.asyncio
async def test_trial_block_reason_false_for_no_account(monkeypatch):
    async def fake_get_by_owner_id(owner_id):
        return None

    spend_calls = []

    async def fake_get_trial_daily_spend(owner_id, date):
        spend_calls.append((owner_id, date))
        return CAP_MICROCENTS + 1

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)

    assert await trial_cap.trial_block_reason("owner1") is None
    assert spend_calls == []  # short-circuits before the spend read


@pytest.mark.asyncio
async def test_no_backend_path_writes_the_counter(monkeypatch):
    """The meter Lambda is the SOLE writer. ``trial_block_reason`` — the only
    surviving cap primitive — must never ADD to the counter, or the Lambda's
    tally would double-count."""
    writes = []

    async def fake_get_by_owner_id(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_get_trial_daily_spend(owner_id, date):
        return 0

    async def fake_add(owner_id, date, delta):
        writes.append((owner_id, date, delta))
        return delta

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_get_trial_daily_spend)
    monkeypatch.setattr("core.services.trial_cap.billing_repo.add_trial_daily_spend", fake_add)

    await trial_cap.trial_block_reason("owner1")

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
    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_spend)

    # Expiry blocks even at zero spend. There is no longer a cap-only predicate
    # that could miss this: it was deleted precisely because four doors reached
    # for it and so checked only half the question.
    assert await trial_cap.trial_block_reason("user_x") == trial_cap.TRIAL_EXPIRED


@pytest.mark.asyncio
async def test_trial_block_reason_none_for_a_healthy_trial_and_for_paid(monkeypatch):
    async def fake_spend(owner_id, date):
        return 0

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_trial_daily_spend", fake_spend)

    async def healthy_trial(owner_id):
        return {"subscription_status": "trial_local", "trial_end": 2**33}  # far future

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", healthy_trial)
    assert await trial_cap.trial_block_reason("user_x") is None

    async def paid(owner_id):
        return {"subscription_status": "active", "trial_end": 1}  # stale stamp, paid now

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", paid)
    assert await trial_cap.trial_block_reason("user_x") is None


# ---------------------------------------------------------------------------
# The ONE reason -> Gate mapping. Every door that can start, wake, or dispatch
# work on a trial container routes through this, so they cannot disagree.


def test_gate_for_trial_reason_maps_both_reasons_and_passes_none_through():
    from core.services import provision_gate

    assert provision_gate.gate_for_trial_reason(None, "admin") is None

    expired = provision_gate.gate_for_trial_reason(trial_cap.TRIAL_EXPIRED, "admin")
    assert expired.code == "trial_expired"

    capped = provision_gate.gate_for_trial_reason(trial_cap.DAILY_CAP, "member")
    assert capped.code == "trial_daily_cap"
    assert capped.owner_role == "member"
