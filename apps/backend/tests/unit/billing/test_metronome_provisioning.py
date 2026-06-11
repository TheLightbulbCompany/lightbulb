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
    monkeypatch.setattr("core.config.settings.METRONOME_USD_CENTS_CREDIT_TYPE_ID", "ct-usd-cents")


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
    fake.create_contract.assert_awaited_once_with(
        customer_id="cust-uuid-1", rate_card_id="rc-1", with_stripe_billing=True
    )
    fake.set_stripe_billing_config.assert_awaited_once_with(
        metronome_customer_id="cust-uuid-1",
        stripe_customer_id="cus_123",
    )
    # The billing-provider config must exist BEFORE the contract references
    # it: contract create with a billing config 404s ("Billing provider
    # configuration not found for STRIPE") when the customer-level config
    # isn't set yet.
    call_names = [name for name, _, _ in fake.mock_calls]
    assert call_names.index("set_stripe_billing_config") < call_names.index("create_contract")
    # Two alerts: the warn alert fires with a POSITIVE remaining (Metronome
    # reports remaining ≈ threshold on crossing), so only the zero-crossing
    # alert can ever deliver the remaining ≤ 0 that fires the kill-switch.
    # Both use the commit-balance alert type (our balance is prepaid commits;
    # sandbox-verified: the credit-balance type is not in the create enum)
    # and carry the credit_type_id the validator requires for that type.
    alert_kwargs = [call.kwargs for call in fake.create_alert.await_args_list]
    assert alert_kwargs == [
        {
            "customer_id": "cust-uuid-1",
            "name": "isol8-low-balance-org_abc-warn",
            "alert_type": "low_remaining_commit_balance_reached",
            "threshold_cents": 500,
            "credit_type_id": "ct-usd-cents",
        },
        {
            "customer_id": "cust-uuid-1",
            "name": "isol8-low-balance-org_abc-zero",
            "alert_type": "low_remaining_commit_balance_reached",
            "threshold_cents": 0,
            "credit_type_id": "ct-usd-cents",
        },
    ]
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="cust-uuid-1",
        metronome_contract_id="contract-uuid-1",
    )


@pytest.mark.asyncio
async def test_unset_rate_card_is_passed_as_none(billing_repo_mock, monkeypatch):
    """rate_card_id is optional on contract create (sandbox-verified minimal
    body) — an env without a configured rate card must pass None so the
    client omits the field instead of sending an empty string."""
    monkeypatch.setattr("core.config.settings.METRONOME_RATE_CARD_ID", "")
    fake = _fake_client()
    await ensure_metronome_customer(owner_id="org_abc", client=fake)
    fake.create_contract.assert_awaited_once_with(
        customer_id="cust-uuid-1", rate_card_id=None, with_stripe_billing=True
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
    fake.create_contract.assert_awaited_once_with(
        customer_id="existing-uuid", rate_card_id="rc-1", with_stripe_billing=True
    )
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
    fake.create_contract.assert_awaited_once_with(
        customer_id="adopted-uuid", rate_card_id="rc-1", with_stripe_billing=True
    )
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
async def test_missing_stripe_customer_skips_billing_provider_link(billing_repo_mock):
    """No stripe_customer_id ⇒ the billing-provider link is skipped and the
    contract is created WITHOUT a billing config, but the Metronome
    customer/contract/alerts still provision — a payment-gated top-up later
    fails loudly at the gate, never silently, and the metric makes the gap
    operator-visible."""
    get_mock, set_mock = billing_repo_mock
    get_mock.return_value = {"owner_id": "org_abc"}
    fake = _fake_client()
    with patch("core.observability.metrics.put_metric") as metric_mock:
        cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "cust-uuid-1"
    fake.set_stripe_billing_config.assert_not_awaited()
    fake.create_contract.assert_awaited_once_with(
        customer_id="cust-uuid-1", rate_card_id="rc-1", with_stripe_billing=False
    )
    fake.create_alert.assert_awaited()
    set_mock.assert_awaited_once()
    assert any(call.args[0] == "metronome.provision.no_stripe_customer" for call in metric_mock.call_args_list)


@pytest.mark.asyncio
async def test_stripe_link_failure_is_best_effort(billing_repo_mock):
    """A billing-provider-config failure logs + metrics but must not block
    provisioning — the customer/contract/alerts/persist all still land,
    with the contract created WITHOUT a billing config (gated top-ups for
    the owner fail loudly until re-provisioned)."""
    _, set_mock = billing_repo_mock
    fake = _fake_client()
    fake.set_stripe_billing_config = AsyncMock(side_effect=_http_error(500))
    with patch("core.observability.metrics.put_metric") as metric_mock:
        cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "cust-uuid-1"
    fake.create_contract.assert_awaited_once_with(
        customer_id="cust-uuid-1", rate_card_id="rc-1", with_stripe_billing=False
    )
    fake.create_alert.assert_awaited()
    set_mock.assert_awaited_once()
    assert any(call.args[0] == "metronome.provision.stripe_link_error" for call in metric_mock.call_args_list)
