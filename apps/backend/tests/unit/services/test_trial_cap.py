"""Tests for the free-tier / capped-plan predicate.

``trial_block_reason`` is the ONLY trial primitive: it is what gate_chat's
Layer 2, the container-start chokepoint, /container/recover, the container-wake
check, the chat-steer path and the Teams BFF run guards all call. It reads the
billing row only: the locally-stamped trial_end and the openrouter-gate's
``openrouter_capped_day`` stamp. There is no Bedrock daily-spend counter any more.

There is deliberately no cap-ONLY primitive: one used to exist and four doors
reached for it, checking only half the predicate.
"""

import time

import pytest

from core.services import trial_cap


@pytest.mark.parametrize(
    "account,expected",
    [
        ({"subscription_status": "trial_local", "trial_end": int(time.time()) - 86400}, True),
        ({"subscription_status": "trial_local", "trial_end": int(time.time()) + 86400}, False),
        ({"subscription_status": "trial_local"}, False),  # missing trial_end -> not expired
        ({"subscription_status": "active", "trial_end": int(time.time()) - 86400}, False),  # not a trial
        (None, False),
    ],
)
def test_is_trial_expired(account, expected):
    assert trial_cap.is_trial_expired(account) is expected


@pytest.mark.asyncio
async def test_trial_block_reason_ignores_a_stale_bedrock_spend_counter(monkeypatch):
    """A leftover ``trial_daily_spend_microcents`` map is no longer read: only expiry and the
    OpenRouter capped-day stamp block."""

    async def fake_get_by_owner_id(owner_id):
        return {
            "subscription_status": "trial_local",
            "trial_daily_spend_microcents": {trial_cap.utc_today(): 10_000_000},
        }

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)
    assert await trial_cap.trial_block_reason("owner1") is None


@pytest.mark.asyncio
async def test_trial_block_reason_false_for_no_account(monkeypatch):
    async def fake_get_by_owner_id(owner_id):
        return None

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_get_by_owner_id)

    assert await trial_cap.trial_block_reason("owner1") is None


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

    monkeypatch.setattr("core.services.trial_cap.billing_repo.get_by_owner_id", fake_account)

    # Expiry blocks on its own. There is no longer a cap-only predicate
    # that could miss this: it was deleted precisely because four doors reached
    # for it and so checked only half the question.
    assert await trial_cap.trial_block_reason("user_x") == trial_cap.TRIAL_EXPIRED


@pytest.mark.asyncio
async def test_trial_block_reason_none_for_a_healthy_trial_and_for_paid(monkeypatch):
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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "account",
    [
        {"subscription_status": "trial_local", "trial_end": 2**33},
        {"subscription_status": "active", "plan": "base"},
        {"subscription_status": "active", "plan": "standard"},
        {"subscription_status": "trialing", "plan": "ultra_pro"},
    ],
)
async def test_openrouter_capped_day_blocks_every_managed_capped_owner(account):
    capped = {**account, "openrouter_capped_day": trial_cap.utc_today()}
    # The trial sees the free-trial card; a paying Base/paid-default owner sees "included usage".
    expected = trial_cap.DAILY_CAP if account["subscription_status"] == "trial_local" else trial_cap.INCLUDED_CAP
    assert await trial_cap.reason_for_account("org_x", capped) == expected
    stale = {**account, "openrouter_capped_day": "2000-01-01"}
    assert await trial_cap.reason_for_account("org_x", stale) is None


@pytest.mark.asyncio
async def test_openrouter_capped_day_ignored_for_explicit_provider_and_expiry_wins():
    today = trial_cap.utc_today()
    explicit = {"subscription_status": "active", "plan": "standard", "provider_choice": "openrouter_managed"}
    assert await trial_cap.reason_for_account("org_x", {**explicit, "openrouter_capped_day": today}) is None
    expired = {"subscription_status": "trial_local", "trial_end": 1, "openrouter_capped_day": today}
    assert await trial_cap.reason_for_account("org_x", expired) == trial_cap.TRIAL_EXPIRED
