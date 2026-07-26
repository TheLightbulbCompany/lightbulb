import pytest

from core.gateway import connection_pool
from core.gateway.connection_pool import GatewayConnection


def _conn(owner_id="owner1"):
    """A GatewayConnection with only the attributes _budget_error_code touches.

    __new__ skips __init__ deliberately — the constructor opens sockets and
    registers liveness callbacks, none of which this pure read needs.
    """
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.owner_id = owner_id
    return conn


@pytest.mark.asyncio
async def test_reclassifies_capped_trial_owner(monkeypatch):
    async def fake_account(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_reason(owner_id, account):
        return "trial_daily_cap"

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.trial_cap.reason_for_account", fake_reason)

    assert await _conn()._budget_error_code() == (
        "trial_daily_cap",
        connection_pool.BUDGET_BLOCK_MESSAGES["trial_daily_cap"],
    )


@pytest.mark.asyncio
async def test_reclassifies_expired_trial_owner(monkeypatch):
    async def fake_account(owner_id):
        return {"owner_id": owner_id, "subscription_status": "trial_local"}

    async def fake_reason(owner_id, account):
        return "trial_expired"

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.trial_cap.reason_for_account", fake_reason)

    assert await _conn()._budget_error_code() == (
        "trial_expired",
        connection_pool.BUDGET_BLOCK_MESSAGES["trial_expired"],
    )


@pytest.mark.asyncio
async def test_reclassifies_paid_bedrock_owner_out_of_credits(monkeypatch):
    async def fake_account(owner_id):
        return {
            "owner_id": owner_id,
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }

    async def fake_blocked(owner_id):
        return True

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.metronome_gate.is_owner_blocked", fake_blocked)

    assert await _conn()._budget_error_code() == (
        "out_of_credits",
        connection_pool.BUDGET_BLOCK_MESSAGES["out_of_credits"],
    )


@pytest.mark.asyncio
async def test_oauth_owner_is_never_reclassified(monkeypatch):
    """THE load-bearing negative case. An OAuth owner consumes no Isol8 budget,
    so their 429 is a genuine OpenAI throttle. Relabelling it would send them to
    an Isol8 billing page to fix someone else's quota."""

    async def fake_account(owner_id):
        return {
            "owner_id": owner_id,
            "subscription_status": "active",
            "provider_choice": "chatgpt_oauth",
        }

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)

    assert await _conn()._budget_error_code() is None


@pytest.mark.asyncio
async def test_byo_key_owner_is_never_reclassified(monkeypatch):
    async def fake_account(owner_id):
        return {
            "owner_id": owner_id,
            "subscription_status": "active",
            "provider_choice": "byo_key",
        }

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)

    assert await _conn()._budget_error_code() is None


@pytest.mark.asyncio
async def test_funded_bedrock_owner_keeps_the_real_throttle(monkeypatch):
    """A real Bedrock throttle for a funded owner must stay `rate_limit` —
    otherwise we mask an AWS incident as a billing problem."""

    async def fake_account(owner_id):
        return {
            "owner_id": owner_id,
            "subscription_status": "active",
            "provider_choice": "bedrock_claude",
        }

    async def fake_blocked(owner_id):
        return False

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)
    monkeypatch.setattr("core.services.metronome_gate.is_owner_blocked", fake_blocked)

    assert await _conn()._budget_error_code() is None


@pytest.mark.asyncio
async def test_missing_billing_row_is_not_reclassified(monkeypatch):
    async def fake_account(owner_id):
        return None

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", fake_account)

    assert await _conn()._budget_error_code() is None


@pytest.mark.asyncio
async def test_read_failure_leaves_the_error_untouched(monkeypatch):
    """The budget read is a network call on an error path. A failure must
    degrade to today's behaviour (show the upstream error), never raise into
    event forwarding and drop the terminal frame — that would hang the run."""

    async def boom(owner_id):
        raise RuntimeError("ddb down")

    monkeypatch.setattr("core.repositories.billing_repo.get_by_owner_id", boom)

    assert await _conn()._budget_error_code() is None
