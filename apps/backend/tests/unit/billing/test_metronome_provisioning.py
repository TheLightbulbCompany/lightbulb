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
    # `subscription_status: "active"` is the state every case here implicitly
    # assumed: ensure_metronome_customer now refuses ineligible owners up front
    # (is_ledger_eligible), so a row with no status would short-circuit before
    # any of the provisioning mechanics under test could run. Pass an explicit
    # status to exercise the eligibility guard itself.
    row = {"owner_id": "org_abc", "stripe_customer_id": "cus_123", "subscription_status": "active"}
    row.update(overrides)
    return row


@pytest.fixture(autouse=True)
def metronome_env(monkeypatch):
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
        patch(
            "core.billing.metronome_provisioning.billing_repo.set_metronome_customer_id",
            new=AsyncMock(),
        ) as set_customer_mock,
    ):
        yield get_mock, set_mock, set_customer_mock


@pytest.mark.asyncio
async def test_creates_customer_contract_links_stripe_arms_alert_and_persists(billing_repo_mock, monkeypatch):
    monkeypatch.setattr("core.config.settings.METRONOME_LOW_BALANCE_THRESHOLD_CENTS", 500)
    _, set_mock, _ = billing_repo_mock
    fake = _fake_client()
    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "cust-uuid-1"
    fake.create_customer.assert_awaited_once_with(name="org_abc", ingest_aliases=["org_abc"])
    fake.create_contract.assert_awaited_once_with(
        customer_id="cust-uuid-1",
        rate_card_id="rc-1",
        with_stripe_billing=True,
        uniqueness_key="lightbulb-contract:cust-uuid-1:rc-1:v1",
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
            "name": "lightbulb-low-balance-org_abc-warn",
            "alert_type": "low_remaining_commit_balance_reached",
            "threshold_cents": 500,
            "credit_type_id": "ct-usd-cents",
            "uniqueness_key": "lightbulb-low-balance:cust-uuid-1:warn",
        },
        {
            "customer_id": "cust-uuid-1",
            "name": "lightbulb-low-balance-org_abc-zero",
            "alert_type": "low_remaining_commit_balance_reached",
            "threshold_cents": 0,
            "credit_type_id": "ct-usd-cents",
            "uniqueness_key": "lightbulb-low-balance:cust-uuid-1:zero",
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
        customer_id="cust-uuid-1",
        rate_card_id=None,
        with_stripe_billing=True,
        uniqueness_key="lightbulb-contract:cust-uuid-1:none:v1",
    )


@pytest.mark.asyncio
async def test_idempotent_skips_http_when_already_provisioned(billing_repo_mock):
    get_mock, set_mock, _ = billing_repo_mock
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
    get_mock, set_mock, _ = billing_repo_mock
    get_mock.return_value = _billing_row(metronome_customer_id="existing-uuid")
    fake = _fake_client(contract_id="contract-new")
    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "existing-uuid"
    fake.create_customer.assert_not_awaited()
    fake.create_contract.assert_awaited_once_with(
        customer_id="existing-uuid",
        rate_card_id="rc-1",
        with_stripe_billing=True,
        uniqueness_key="lightbulb-contract:existing-uuid:rc-1:v1",
    )
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="existing-uuid",
        metronome_contract_id="contract-new",
    )


@pytest.mark.asyncio
async def test_alias_conflict_adopts_existing_customer(billing_repo_mock):
    """A 409 on create means a prior attempt already claimed the alias
    (crashed before persisting the UUID) — adopt that customer instead of
    failing the retry forever."""
    _, set_mock, _ = billing_repo_mock
    fake = _fake_client()
    fake.create_customer = AsyncMock(side_effect=_http_error(409))
    fake.get_customer_id_by_ingest_alias = AsyncMock(return_value="adopted-uuid")

    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "adopted-uuid"
    fake.get_customer_id_by_ingest_alias.assert_awaited_once_with("org_abc")
    # The adopted customer still gets the contract + Stripe link + alert +
    # persist — the prior attempt may have crashed before any of them.
    fake.create_contract.assert_awaited_once_with(
        customer_id="adopted-uuid",
        rate_card_id="rc-1",
        with_stripe_billing=True,
        uniqueness_key="lightbulb-contract:adopted-uuid:rc-1:v1",
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
async def test_contract_conflict_adopts_keyed_contract(billing_repo_mock):
    """A 409 on contract-create means another attempt already created the
    keyed contract — adopt it (not merely the first listed) instead of
    failing the retry forever."""
    _, set_mock, _ = billing_repo_mock
    fake = _fake_client()
    fake.create_contract = AsyncMock(side_effect=_http_error(409))
    fake.list_contracts = AsyncMock(
        return_value=[
            {"id": "other-contract"},
            {"id": "adopted-contract", "uniqueness_key": "lightbulb-contract:cust-uuid-1:rc-1:v1"},
        ]
    )

    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "cust-uuid-1"
    fake.list_contracts.assert_awaited_with(customer_id="cust-uuid-1")
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="cust-uuid-1",
        metronome_contract_id="adopted-contract",
    )


@pytest.mark.asyncio
async def test_concurrent_provisions_create_one_contract(billing_repo_mock):
    """Two provisions racing past the persisted-ids short-circuit must end on
    ONE contract: the deterministic uniqueness_key makes Metronome 409 the
    loser, which adopts the winner's contract instead of minting a second."""
    import asyncio

    _, set_mock, _ = billing_repo_mock
    created: dict[str, dict] = {}

    async def create_contract(*, customer_id, uniqueness_key, **_):
        await asyncio.sleep(0)  # let the other provision reach the create
        if uniqueness_key in created:
            raise _http_error(409)
        created[uniqueness_key] = {
            "id": f"contract-{len(created) + 1}",
            "rate_card_id": "rc-1",
            "uniqueness_key": uniqueness_key,
        }
        return created[uniqueness_key]["id"]

    fake = _fake_client()
    fake.create_contract = AsyncMock(side_effect=create_contract)
    fake.list_contracts = AsyncMock(side_effect=lambda **_: list(created.values()))

    await asyncio.gather(
        ensure_metronome_customer(owner_id="org_abc", client=fake),
        ensure_metronome_customer(owner_id="org_abc", client=fake),
    )

    assert list(created) == ["lightbulb-contract:cust-uuid-1:rc-1:v1"]
    assert {c.kwargs["metronome_contract_id"] for c in set_mock.await_args_list} == {"contract-1"}


@pytest.mark.asyncio
async def test_live_unkeyed_contract_on_rate_card_is_adopted_not_duplicated(billing_repo_mock):
    """A customer already carrying a pre-key contract on this rate card (whose
    id never reached the billing row) must adopt it: a new keyed contract
    would be a second live contract pricing the same usage."""
    _, set_mock, _ = billing_repo_mock
    fake = _fake_client()
    fake.list_contracts = AsyncMock(
        return_value=[{"id": "other-card", "rate_card_id": "rc-0"}, {"id": "unkeyed", "rate_card_id": "rc-1"}]
    )

    await ensure_metronome_customer(owner_id="org_abc", client=fake)

    fake.create_contract.assert_not_awaited()
    assert set_mock.await_args.kwargs["metronome_contract_id"] == "unkeyed"


@pytest.mark.asyncio
async def test_contract_conflict_with_no_listed_contract_raises(billing_repo_mock):
    _, set_mock, _ = billing_repo_mock
    fake = _fake_client()
    fake.create_contract = AsyncMock(side_effect=_http_error(409))
    fake.list_contracts = AsyncMock(return_value=[])
    with pytest.raises(Exception, match="no contract is listed"):
        await ensure_metronome_customer(owner_id="org_abc", client=fake)
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_duplicate_alert_is_success(billing_repo_mock):
    _, set_mock, _ = billing_repo_mock
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
    _, set_mock, _ = billing_repo_mock
    fake = _fake_client()
    fake.create_alert = AsyncMock(side_effect=_http_error(503))
    with pytest.raises(httpx.HTTPStatusError):
        await ensure_metronome_customer(owner_id="org_abc", client=fake)
    set_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_billing_row_raises(billing_repo_mock):
    get_mock, _, _ = billing_repo_mock
    get_mock.return_value = None
    with pytest.raises(ProvisioningError, match="No billing row"):
        await ensure_metronome_customer(owner_id="org_missing", client=_fake_client())


@pytest.mark.asyncio
async def test_missing_stripe_customer_defers_contract_creation(billing_repo_mock):
    """No stripe_customer_id ⇒ NO contract: a contract's billing config is
    create-time-only, so creating one now would permanently strand the owner
    with payment-gated top-ups that can never collect. Only the customer
    UUID persists; the missing contract id keeps every later status poll
    re-entering until the link lands."""
    get_mock, set_mock, set_customer_mock = billing_repo_mock
    # Eligible owner, but no stripe_customer_id — that absence is the condition
    # under test, not the eligibility guard that now runs before it.
    get_mock.return_value = {"owner_id": "org_abc", "subscription_status": "active"}
    fake = _fake_client()
    with patch("core.observability.metrics.put_metric") as metric_mock:
        cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "cust-uuid-1"
    fake.set_stripe_billing_config.assert_not_awaited()
    fake.create_contract.assert_not_awaited()
    set_mock.assert_not_awaited()
    set_customer_mock.assert_awaited_once_with("org_abc", metronome_customer_id="cust-uuid-1")
    assert any(call.args[0] == "metronome.provision.no_stripe_customer" for call in metric_mock.call_args_list)


@pytest.mark.asyncio
async def test_stripe_link_failure_defers_contract_and_does_not_raise(billing_repo_mock):
    """A billing-provider-config failure logs + metrics but must not raise
    (the caller treats provisioning as best-effort and retries on the next
    poll) — and must NOT create a contract, which would bake in the missing
    billing config forever. Only the customer UUID persists."""
    _, set_mock, set_customer_mock = billing_repo_mock
    fake = _fake_client()
    fake.set_stripe_billing_config = AsyncMock(side_effect=_http_error(500))
    with patch("core.observability.metrics.put_metric") as metric_mock:
        cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "cust-uuid-1"
    fake.create_contract.assert_not_awaited()
    set_mock.assert_not_awaited()
    set_customer_mock.assert_awaited_once_with("org_abc", metronome_customer_id="cust-uuid-1")
    assert any(call.args[0] == "metronome.provision.stripe_link_error" for call in metric_mock.call_args_list)


@pytest.mark.asyncio
async def test_link_retry_after_deferred_contract_converges(billing_repo_mock):
    """Re-entry after a deferred contract (customer UUID persisted, no
    contract id): the link is retried and, once it succeeds, the contract is
    created WITH billing config and both ids persist — without re-creating
    or re-persisting the customer."""
    get_mock, set_mock, set_customer_mock = billing_repo_mock
    get_mock.return_value = _billing_row(metronome_customer_id="existing-uuid")
    fake = _fake_client(contract_id="contract-new")
    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "existing-uuid"
    fake.create_customer.assert_not_awaited()
    fake.set_stripe_billing_config.assert_awaited_once()
    fake.create_contract.assert_awaited_once_with(
        customer_id="existing-uuid",
        rate_card_id="rc-1",
        with_stripe_billing=True,
        uniqueness_key="lightbulb-contract:existing-uuid:rc-1:v1",
    )
    set_customer_mock.assert_not_awaited()
    set_mock.assert_awaited_once_with(
        "org_abc",
        metronome_customer_id="existing-uuid",
        metronome_contract_id="contract-new",
    )


@pytest.mark.asyncio
async def test_deferred_persist_skipped_when_customer_id_already_stored(billing_repo_mock):
    """While the link keeps failing, the ~3s poll re-enters repeatedly — the
    customer-id-only persist must not rewrite the row on every poll once the
    UUID is already stored."""
    get_mock, set_mock, set_customer_mock = billing_repo_mock
    get_mock.return_value = _billing_row(metronome_customer_id="existing-uuid")
    fake = _fake_client()
    fake.set_stripe_billing_config = AsyncMock(side_effect=_http_error(500))
    cust_id = await ensure_metronome_customer(owner_id="org_abc", client=fake)

    assert cust_id == "existing-uuid"
    fake.create_contract.assert_not_awaited()
    set_mock.assert_not_awaited()
    set_customer_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_trial_local_owner_is_never_provisioned_into_metronome(billing_repo_mock, monkeypatch):
    """A free-tier (trial_local) owner has no Stripe customer and draws no
    prepaid credits, so they must never get a Metronome customer. This is the
    upstream half of the free-tier fix: a stray Metronome customer gives them a
    balance of 0 that the low-balance enforcer reads as 'out of credits' and
    revokes Bedrock on — which was killing every free trial ~30s after signup."""
    get_mock, set_mock, set_customer_mock = billing_repo_mock
    get_mock.return_value = _billing_row(subscription_status="trial_local", stripe_customer_id=None)

    # No HTTP client must be constructed and nothing persisted — a pure no-op.
    with patch("core.billing.metronome_provisioning.httpx.AsyncClient") as client_cls:
        result = await ensure_metronome_customer(owner_id="user_trial")

    assert result is None
    client_cls.assert_not_called()
    set_mock.assert_not_awaited()
    set_customer_mock.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["trial_local", "canceled", "incomplete", "incomplete_expired", "paused"])
async def test_ineligible_statuses_get_no_ledger(billing_repo_mock, status):
    """Only live/recoverable subscriptions get a ledger.

    trial_local was always excluded (a zero balance reads as "out of credits"
    to the low-balance enforcer). The others matter since `ensure_user_ready`
    began calling this BEFORE the provision gate: previously a canceled owner
    never reached this function because the gate returned first, so a canceled
    owner polling /container/status would have had a real customer + contract
    + two alerts created over live HTTP for an account that can never spend.
    """
    get_mock, set_mock, set_customer_mock = billing_repo_mock
    get_mock.return_value = _billing_row(subscription_status=status)
    client = MagicMock()
    client.create_customer = AsyncMock()

    assert await ensure_metronome_customer(owner_id="org_abc", client=client) is None

    client.create_customer.assert_not_awaited()
    set_mock.assert_not_awaited()
    set_customer_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_row_without_status_is_eligible(billing_repo_mock):
    """`status is None` + a stripe_subscription_id is a real paid subscription
    whose status was never backfilled — the gates treat it as active, so it
    must get a ledger or the fences lock out exactly the owners they consider
    paid."""
    from core.billing.metronome_provisioning import is_ledger_eligible

    assert is_ledger_eligible({"subscription_status": None, "stripe_subscription_id": "sub_1"}) is True
    assert is_ledger_eligible({"subscription_status": None}) is False
    # A stale subscription id must not revive an explicitly dead status.
    assert is_ledger_eligible({"subscription_status": "canceled", "stripe_subscription_id": "sub_1"}) is False
