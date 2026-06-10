"""Unit tests for MetronomeClient — exact HTTP request shapes, mocked httpx.

respx 0.20.2 mis-matches httpx 0.28 URLs (see tests/unit/services/
test_posthog_admin.py), so we patch the AsyncClient surface directly and
assert on the captured call args.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.billing.metronome_client import MetronomeClient, MetronomeError


def _resp(status_code: int = 200, json_body: dict | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status_code
    r.json = MagicMock(return_value=json_body or {})
    r.text = ""
    r.raise_for_status = MagicMock()
    return r


def _client_with(response: MagicMock) -> tuple[MetronomeClient, MagicMock]:
    fake = MagicMock()
    fake.post = AsyncMock(return_value=response)
    fake.get = AsyncMock(return_value=response)
    return MetronomeClient(api_key="tok_sandbox", base_url="https://api.metronome.com/v1", http_client=fake), fake


@pytest.mark.asyncio
async def test_ingest_posts_event_array_with_bearer_auth():
    client, fake = _client_with(_resp(200, {}))
    event = {
        "transaction_id": "req-1",
        "customer_id": "org_abc",
        "event_type": "bedrock_invocation",
        "timestamp": "2026-06-08T00:00:00Z",
        "properties": {"input_tokens": "1000"},
    }
    await client.ingest([event])

    fake.post.assert_awaited_once()
    args, kwargs = fake.post.call_args
    assert args[0] == "/ingest"
    # Body is a JSON ARRAY of events (Metronome contract).
    assert kwargs["json"] == [event]
    assert kwargs["headers"]["Authorization"] == "Bearer tok_sandbox"


@pytest.mark.asyncio
async def test_create_customer_sends_name_and_ingest_aliases():
    client, fake = _client_with(_resp(200, {"data": {"id": "cust-uuid-1"}}))
    cust_id = await client.create_customer(name="org_abc", ingest_aliases=["org_abc"])

    assert cust_id == "cust-uuid-1"
    args, kwargs = fake.post.call_args
    assert args[0] == "/customers/create"
    assert kwargs["json"] == {"name": "org_abc", "ingest_aliases": ["org_abc"]}


@pytest.mark.asyncio
async def test_create_customer_raises_on_missing_id():
    # A well-formed-HTTP 2xx whose envelope lacks data.id raises a typed
    # MetronomeError (not a bare KeyError -> 500) so provisioning surfaces a
    # clean, attributable failure.
    client, _ = _client_with(_resp(200, {"data": {}}))
    with pytest.raises(MetronomeError, match="no data.id"):
        await client.create_customer(name="org_abc", ingest_aliases=["org_abc"])


@pytest.mark.asyncio
async def test_get_customer_id_by_ingest_alias_returns_first_match():
    client, fake = _client_with(_resp(200, {"data": [{"id": "cust-uuid-1", "name": "org_abc"}]}))
    cust_id = await client.get_customer_id_by_ingest_alias("org_abc")

    assert cust_id == "cust-uuid-1"
    args, kwargs = fake.get.call_args
    assert args[0] == "/customers"
    assert kwargs["params"] == {"ingest_alias": "org_abc"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok_sandbox"


@pytest.mark.asyncio
async def test_get_customer_id_by_ingest_alias_returns_none_when_no_match():
    client, _ = _client_with(_resp(200, {"data": []}))
    assert await client.get_customer_id_by_ingest_alias("org_unknown") is None


@pytest.mark.asyncio
async def test_create_contract_sends_rate_card_and_returns_id():
    client, fake = _client_with(_resp(200, {"data": {"id": "contract-uuid-1"}}))
    contract_id = await client.create_contract(customer_id="cust-uuid-1", rate_card_id="rc-1")

    assert contract_id == "contract-uuid-1"
    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/create"
    body = kwargs["json"]
    assert body["customer_id"] == "cust-uuid-1"
    assert body["rate_card_id"] == "rc-1"
    # Contracts must start at UTC midnight — Metronome rejects mid-day starts.
    assert body["starting_at"].endswith("T00:00:00.000Z")


@pytest.mark.asyncio
async def test_create_contract_raises_on_missing_id():
    client, _ = _client_with(_resp(200, {"data": {}}))
    with pytest.raises(MetronomeError, match="no data.id"):
        await client.create_contract(customer_id="cust-uuid-1", rate_card_id="rc-1")


@pytest.mark.asyncio
async def test_list_contracts_shape():
    client, fake = _client_with(_resp(200, {"data": [{"id": "contract-uuid-1"}]}))
    contracts = await client.list_contracts(customer_id="cust-uuid-1")

    assert contracts == [{"id": "contract-uuid-1"}]
    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/list"
    assert kwargs["json"] == {"customer_id": "cust-uuid-1"}


@pytest.mark.asyncio
async def test_list_contracts_empty():
    client, _ = _client_with(_resp(200, {"data": []}))
    assert await client.list_contracts(customer_id="cust-uuid-1") == []


@pytest.mark.asyncio
async def test_create_alert_shape():
    client, fake = _client_with(_resp(200, {}))
    await client.create_alert(
        customer_id="cust-uuid-1",
        name="isol8-low-balance-org_abc",
        alert_type="low_remaining_credit_balance_reached",
        threshold_cents=500,
    )

    args, kwargs = fake.post.call_args
    assert args[0] == "/alerts/create"
    assert kwargs["json"] == {
        "customer_id": "cust-uuid-1",
        "name": "isol8-low-balance-org_abc",
        "alert_type": "low_remaining_credit_balance_reached",
        "threshold": 500,
    }


@pytest.mark.asyncio
async def test_set_stripe_billing_config_shape():
    client, fake = _client_with(_resp(200, {}))
    await client.set_stripe_billing_config(
        metronome_customer_id="cust-uuid-1",
        stripe_customer_id="cus_123",
    )

    args, kwargs = fake.post.call_args
    assert args[0] == "/customers/cust-uuid-1/setCustomerBillingProviderConfigurations"
    assert kwargs["json"] == {
        "billing_provider": "stripe",
        "delivery_method": "direct_to_billing_provider",
        "configuration": {
            "stripe_customer_id": "cus_123",
            "stripe_collection_method": "charge_automatically",
        },
    }


@pytest.mark.asyncio
async def test_edit_contract_add_commit_shape():
    client, fake = _client_with(_resp(200, {}))
    commit = {"product_id": "prod-credits", "amount": 25.0}
    await client.edit_contract_add_commits(
        metronome_customer_id="cust-uuid-1",
        contract_id="contract-1",
        add_commits=[commit],
    )

    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/edit"
    assert kwargs["json"] == {
        "customer_id": "cust-uuid-1",
        "contract_id": "contract-1",
        "add_commits": [commit],
    }


@pytest.mark.asyncio
async def test_get_net_balance_returns_parsed_data():
    client, fake = _client_with(_resp(200, {"data": {"net_balance": 12.34}}))
    body = await client.get_net_balance(customer_id="org_abc")

    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/customerBalances/getNetBalance"
    assert kwargs["json"] == {"customer_id": "org_abc"}
    assert body == {"net_balance": 12.34}


@pytest.mark.asyncio
async def test_raises_on_http_error():
    bad = _resp(500, {})
    bad.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=bad))
    client, _ = _client_with(bad)
    with pytest.raises(httpx.HTTPStatusError):
        await client.ingest([{"transaction_id": "x"}])
