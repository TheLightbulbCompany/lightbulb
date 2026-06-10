"""Unit tests for Metronome customer + contract provisioning (mocked client + billing repo)."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from core.billing.metronome_provisioning import ProvisioningError, ensure_metronome_customer


def _fake_client(customer_id="cust-uuid-1", contract_id="contract-uuid-1"):
    fake = MagicMock()
    fake.create_customer = AsyncMock(return_value=customer_id)
    fake.get_customer_id_by_ingest_alias = AsyncMock(return_value=None)
    fake.create_contract = AsyncMock(return_value=contract_id)
    fake.list_contracts = AsyncMock(return_value=[])
    fake.set_stripe_billing_config = AsyncMock()
    fake.create_alert = AsyncMock()
    return fake


def _http_error(status_code: int) -> httpx.HTTPStatusError:
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    return httpx.HTTPStatusError(f"http {status_code}", request=MagicMock(), response=resp)


def _billing_row(**overrides):
    row = {"owner_id": "org_abc", "stripe_customer_id": "cus_123"}
    row.update(overrides)
    return row


@pytest.fixture(autouse=True)
def lifecycle_enabled(monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", True)
    monkeypatch.setattr("core.config.settings.METRONOME_RATE_CARD_ID", "rc-1")


@pytest.fixture
def billing_repo_mock():
    with (
        patch(
            "core.billing.metronome_provisioning.billing_repo.get_by_owner_id",
            new=AsyncMock(return_value=_billing_row()),
        ) as get_mock,
        patch(
            "core.billing.metronome_provisioning.billing_repo.set_metronome_ids",
            new=AsyncMock(),
        ) as set_mock,
    ):
        yield get_mock, set_mock


@pytest.mark.asyncio
async def test_creates_customer_contract_links_stripe_arms_alert_and_persists(billing_repo_mock, monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_LOW_BALANCE_THRESHOLD_CENTS", 500)
    _, set_mock = billing_repo_mock
    fake = _fake_client()
    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "cust-uuid-1"
    fake.create_customer.assert_awaited_once_with(name="org_abc", ingest_aliases=["org_abc"])
    fake.create_contract.assert_awaited_once_with(customer_id="cust-uuid-1", rate_card_id="rc-1")
    fake.set_stripe_billing_config.assert_awaited_once_with(
        metronome_customer_id="cust-uuid-1",
        stripe_customer_id="cus_123",
    )
    # Two alerts: the warn alert fires with a POSITIVE remaining (Metronome
    # reports remaining ≈ threshold on crossing), so only the zero-crossing
    # alert can ever deliver the remaining ≤ 0 that fires the kill-switch.
    alert_kwargs = [call.kwargs for call in fake.create_alert.await_args_list]
    assert alert_kwargs == [
        {
            "customer_id": "cust-uuid-1",
            "name": "isol8-low-balance-org_abc-warn",
            "alert_type": "low_remaining_credit_balance_reached",
            "threshold_cents": 500,
        },
        {
            "customer_id": "cust-uuid-1",
            "name": "isol8-low-balance-org_abc-zero",
            "alert_type": "low_remaining_credit_balance_reached",
            "threshold_cents": 0,
        },
    ]
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="cust-uuid-1",
        metronome_contract_id="contract-uuid-1",
    )


@pytest.mark.asyncio
async def test_idempotent_skips_http_when_already_provisioned(billing_repo_mock):
    get_mock, set_mock = billing_repo_mock
    get_mock.return_value = _billing_row(
        metronome_customer_id="existing-uuid",
        metronome_contract_id="existing-contract",
    )
    fake = _fake_client()
    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "existing-uuid"
    fake.create_customer.assert_not_awaited()
    fake.create_contract.assert_not_awaited()
    fake.set_stripe_billing_config.assert_not_awaited()
    fake.create_alert.assert_not_awaited()
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_persisted_customer_without_contract_creates_only_the_contract(billing_repo_mock):
    """A row provisioned before per-customer contracts existed has a customer
    UUID but no contract — re-entry must converge without re-creating the
    customer."""
    get_mock, set_mock = billing_repo_mock
    get_mock.return_value = _billing_row(metronome_customer_id="existing-uuid")
    fake = _fake_client(contract_id="contract-new")
    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "existing-uuid"
    fake.create_customer.assert_not_awaited()
    fake.create_contract.assert_awaited_once_with(customer_id="existing-uuid", rate_card_id="rc-1")
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="existing-uuid",
        metronome_contract_id="contract-new",
    )


@pytest.mark.asyncio
async def test_lifecycle_disabled_is_a_pure_noop(billing_repo_mock, monkeypatch):
    """With the master switch off, the ~3s /container/status poll must not
    spam doomed calls against placeholder secrets — no billing-row read, no
    HTTP client construction."""
    monkeypatch.setattr("core.config.settings.METRONOME_LIFECYCLE_ENABLED", False)
    get_mock, set_mock = billing_repo_mock
    with patch("core.billing.metronome_provisioning.httpx.AsyncClient") as client_cls:
        result = await ensure_metronome_customer(owner_id="org_abc")

    assert result is None
    client_cls.assert_not_called()
    get_mock.assert_not_awaited()
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_alias_conflict_adopts_existing_customer(billing_repo_mock):
    """A 409 on create means a prior attempt already claimed the alias
    (crashed before persisting the UUID) — adopt that customer instead of
    failing the retry forever."""
    _, set_mock = billing_repo_mock
    fake = _fake_client()
    fake.create_customer = AsyncMock(side_effect=_http_error(409))
    fake.get_customer_id_by_ingest_alias = AsyncMock(return_value="adopted-uuid")

    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "adopted-uuid"
    fake.get_customer_id_by_ingest_alias.assert_awaited_once_with("org_abc")
    # The adopted customer still gets the contract + Stripe link + alert +
    # persist — the prior attempt may have crashed before any of them.
    fake.create_contract.assert_awaited_once_with(customer_id="adopted-uuid", rate_card_id="rc-1")
    fake.set_stripe_billing_config.assert_awaited_once()
    fake.create_alert.assert_awaited()
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="adopted-uuid",
        metronome_contract_id="contract-uuid-1",
    )


@pytest.mark.asyncio
async def test_alias_conflict_with_no_match_raises(billing_repo_mock):
    fake = _fake_client()
    fake.create_customer = AsyncMock(side_effect=_http_error(409))
    fake.get_customer_id_by_ingest_alias = AsyncMock(return_value=None)
    with pytest.raises(Exception, match="no customer matches"):
        await ensure_metronome_customer(owner_id="org_abc", client=fake)


@pytest.mark.asyncio
async def test_contract_conflict_adopts_first_listed_contract(billing_repo_mock):
    """A 409 on contract-create means a prior attempt created the contract
    but crashed before persisting its id — adopt the customer's existing
    contract instead of failing the retry forever."""
    _, set_mock = billing_repo_mock
    fake = _fake_client()
    fake.create_contract = AsyncMock(side_effect=_http_error(409))
    fake.list_contracts = AsyncMock(return_value=[{"id": "adopted-contract"}, {"id": "later-contract"}])

    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "cust-uuid-1"
    fake.list_contracts.assert_awaited_once_with(customer_id="cust-uuid-1")
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="cust-uuid-1",
        metronome_contract_id="adopted-contract",
    )


@pytest.mark.asyncio
async def test_contract_conflict_with_no_listed_contract_raises(billing_repo_mock):
    _, set_mock = billing_repo_mock
    fake = _fake_client()
    fake.create_contract = AsyncMock(side_effect=_http_error(409))
    fake.list_contracts = AsyncMock(return_value=[])
    with pytest.raises(Exception, match="no contract is listed"):
        await ensure_metronome_customer(owner_id="org_abc", client=fake)
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_alert_is_success(billing_repo_mock):
    _, set_mock = billing_repo_mock
    fake = _fake_client()
    fake.create_alert = AsyncMock(side_effect=_http_error(409))
    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)
    assert cust_id == "cust-uuid-1"
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="cust-uuid-1",
        metronome_contract_id="contract-uuid-1",
    )


@pytest.mark.asyncio
async def test_alert_5xx_raises_and_does_not_persist(billing_repo_mock):
    """The short-circuit keys on the persisted ids, so persisting before the
    alert exists would leave the owner alert-less forever. A failed alert
    must abort the persist so the next poll retries the whole sequence."""
    _, set_mock = billing_repo_mock
    fake = _fake_client()
    fake.create_alert = AsyncMock(side_effect=_http_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await ensure_metronome_customer(owner_id="org_abc", client=fake)
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_billing_row_raises(billing_repo_mock):
    get_mock, _ = billing_repo_mock
    get_mock.return_value = None
    with pytest.raises(ProvisioningError, match="No billing row"):
        await ensure_metronome_customer(owner_id="org_missing", client=_fake_client())


@pytest.mark.asyncio
async def test_missing_stripe_customer_raises(billing_repo_mock):
    get_mock, _ = billing_repo_mock
    get_mock.return_value = {"owner_id": "org_abc"}
    with pytest.raises(ProvisioningError, match="no stripe_customer_id"):
        await ensure_metronome_customer(owner_id="org_abc", client=_fake_client())
