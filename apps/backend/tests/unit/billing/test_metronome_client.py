"""Unit tests for MetronomeClient — exact HTTP request shapes, mocked httpx.

respx 0.20.2 mis-matches httpx 0.28 URLs (see tests/unit/services/
test_posthog_admin.py), so we patch the AsyncClient surface directly and
assert on the captured call args.
"""

import json
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


async def test_voice_contract_is_monthly_advance_and_scopes_usage_to_one_enrollment(monkeypatch):
    from core.config import settings

    monkeypatch.setattr(settings, "METRONOME_STRIPE_DELIVERY_METHOD_ID", "delivery-1")
    client, http = _client_with(_resp(200, {"data": {"id": "voice-contract"}}))
    assert (
        await client.create_voice_contract(
            customer_id="customer-1",
            rate_card_id="voice-card",
            subscription_product_id="voice-sub",
            uniqueness_key="enrollment-1",
        )
        == "voice-contract"
    )
    payload = http.post.await_args.kwargs["json"]
    assert http.post.await_args.args[0] == "/contracts/create"
    assert payload["usage_filter"] == {"group_key": "voice_plan", "group_values": ["enrollment-1"]}
    assert payload["billing_provider_configuration"] == {
        "billing_provider": "stripe",
        "delivery_method_id": "delivery-1",
    }
    assert payload["subscriptions"] == [
        {
            "collection_schedule": "ADVANCE",
            "billing_cycle_config": {"invoice_placement": "ON_SCHEDULED_INVOICE"},
            "initial_quantity": 1,
            "proration": {"invoice_behavior": "BILL_IMMEDIATELY", "is_prorated": True},
            "subscription_rate": {"billing_frequency": "MONTHLY", "product_id": "voice-sub"},
        }
    ]


async def test_voice_contract_cancellation_normalizes_python_microseconds_for_provider():
    client, http = _client_with(_resp(200, {}))
    await client.end_voice_contract(
        customer_id="customer-1", contract_id="voice-contract", ending_before="2026-11-08T12:34:56.123456+00:00"
    )
    assert http.post.await_args.args[0] == "/contracts/updateEndDate"
    assert http.post.await_args.kwargs["json"] == {
        "customer_id": "customer-1",
        "contract_id": "voice-contract",
        "ending_before": "2026-11-08T12:34:56.123Z",
    }


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
    # Plain REST collection path (sandbox-verified; "/customers/create" 404s).
    assert args[0] == "/customers"
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
    contract_id = await client.create_contract(customer_id="cust-uuid-1", rate_card_id="rc-1", uniqueness_key="k-1")

    assert contract_id == "contract-uuid-1"
    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/create"
    body = kwargs["json"]
    assert body["customer_id"] == "cust-uuid-1"
    assert body["rate_card_id"] == "rc-1"
    assert body["uniqueness_key"] == "k-1"
    # Contracts must start at UTC midnight — Metronome rejects mid-day starts.
    assert body["starting_at"].endswith("T00:00:00.000Z")
    # Not requested ⇒ no contract-level billing config in the body (sending
    # it without the customer's root-level config existing 404s the create).
    assert "billing_provider_configuration" not in body


@pytest.mark.asyncio
async def test_create_contract_with_stripe_billing_attaches_billing_provider_configuration():
    """Live-verified (sandbox, 2026-06-11): payment-gated commits collect
    through the CONTRACT-level billing_provider_configuration — a contract
    created without it cannot fund gated top-ups."""
    client, fake = _client_with(_resp(200, {"data": {"id": "contract-uuid-1"}}))
    await client.create_contract(
        customer_id="cust-uuid-1", rate_card_id="rc-1", with_stripe_billing=True, uniqueness_key="k-1"
    )

    _, kwargs = fake.post.call_args
    assert kwargs["json"]["billing_provider_configuration"] == {
        "billing_provider": "stripe",
        "delivery_method": "direct_to_billing_provider",
    }


@pytest.mark.asyncio
async def test_create_contract_omits_rate_card_when_unset():
    # Sandbox-verified: the minimal body {customer_id, starting_at} is
    # accepted; an empty/absent rate card must not appear in the body.
    client, fake = _client_with(_resp(200, {"data": {"id": "contract-uuid-1"}}))
    contract_id = await client.create_contract(customer_id="cust-uuid-1", uniqueness_key="k-1")

    assert contract_id == "contract-uuid-1"
    _, kwargs = fake.post.call_args
    assert "rate_card_id" not in kwargs["json"]
    assert kwargs["json"]["customer_id"] == "cust-uuid-1"


@pytest.mark.asyncio
async def test_create_contract_raises_on_missing_id():
    client, _ = _client_with(_resp(200, {"data": {}}))
    with pytest.raises(MetronomeError, match="no data.id"):
        await client.create_contract(customer_id="cust-uuid-1", rate_card_id="rc-1", uniqueness_key="k-1")


@pytest.mark.asyncio
async def test_list_contracts_shape():
    client, fake = _client_with(_resp(200, {"data": [{"id": "contract-uuid-1"}]}))
    contracts = await client.list_contracts(customer_id="cust-uuid-1")

    assert contracts == [{"id": "contract-uuid-1"}]
    args, kwargs = fake.post.call_args
    # v1 list is disabled by Metronome; the v2 surface lives beside it.
    assert args[0] == "https://api.metronome.com/v2/contracts/list"
    assert kwargs["json"] == {"customer_id": "cust-uuid-1"}


@pytest.mark.asyncio
async def test_list_contracts_follows_cursor():
    client, fake = _client_with(_resp())
    fake.post = AsyncMock(
        side_effect=[_resp(200, {"data": [{"id": "c1"}], "cursor": "p2"}), _resp(200, {"data": [{"id": "c2"}]})]
    )

    assert await client.list_contracts(customer_id="cust-uuid-1") == [{"id": "c1"}, {"id": "c2"}]
    assert fake.post.call_args_list[1].kwargs["json"] == {"customer_id": "cust-uuid-1", "cursor": "p2"}


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
        alert_type="low_remaining_commit_balance_reached",
        threshold_cents=500,
        credit_type_id="ct-usd-cents",
        uniqueness_key="k-alert",
    )

    args, kwargs = fake.post.call_args
    assert args[0] == "/alerts/create"
    # Sandbox-verified create body: credit_type_id is REQUIRED for the
    # commit-balance alert type (the validator rejects it otherwise).
    assert kwargs["json"] == {
        "customer_id": "cust-uuid-1",
        "name": "isol8-low-balance-org_abc",
        "alert_type": "low_remaining_commit_balance_reached",
        "threshold": 500,
        "credit_type_id": "ct-usd-cents",
        "uniqueness_key": "k-alert",
    }


@pytest.mark.asyncio
async def test_set_stripe_billing_config_shape():
    client, fake = _client_with(_resp(200, {}))
    await client.set_stripe_billing_config(
        metronome_customer_id="cust-uuid-1",
        stripe_customer_id="cus_123",
    )

    args, kwargs = fake.post.call_args
    # Live-verified (sandbox, 2026-06-11): the ROOT-level new-style call is
    # the one whose record contract-level billing configs + payment gates
    # consult; the legacy per-customer billing-config/stripe path writes a
    # DIFFERENT record that gated commits ignore.
    assert args[0] == "/setCustomerBillingProviderConfigurations"
    assert kwargs["json"] == {
        "data": [
            {
                "customer_id": "cust-uuid-1",
                "billing_provider": "stripe",
                "delivery_method": "direct_to_billing_provider",
                "configuration": {
                    "stripe_customer_id": "cus_123",
                    "stripe_collection_method": "charge_automatically",
                },
            }
        ]
    }


@pytest.mark.asyncio
async def test_create_prepaid_commit_flat_body_shape():
    client, fake = _client_with(_resp(200, {}))
    await client.create_prepaid_commit(
        customer_id="cust-uuid-1",
        contract_id="contract-1",
        product_id="prod-credits",
        credit_type_id="ct-usd-cents",
        amount_cents=2573,
        description="isol8 top-up owner=org_abc amount_cents=2573",
        idempotency_key="topup-org_abc-uuid-1",
    )

    args, kwargs = fake.post.call_args
    # Ungated commits land via v1 customerCommits/create with a FLAT body.
    assert args[0] == "/contracts/customerCommits/create"
    body = kwargs["json"]
    assert body["customer_id"] == "cust-uuid-1"
    assert body["type"] == "PREPAID"
    assert body["product_id"] == "prod-credits"
    # Sandbox-verified required field; fixed ranking — we issue one commit class.
    assert body["priority"] == 100
    assert body["applicable_contract_ids"] == ["contract-1"]
    assert body["invoice_contract_id"] == "contract-1"
    assert body["description"] == "isol8 top-up owner=org_abc amount_cents=2573"
    # This API has no payment_gate_config field at all (live-verified: the
    # validator rejects it) — the body must never carry one.
    assert "payment_gate_config" not in body
    schedule = body["access_schedule"]
    assert schedule["credit_type_id"] == "ct-usd-cents"
    [item] = schedule["schedule_items"]
    assert item["amount"] == 2573  # CENTS (Metronome's USD credit type)
    # UTC midnight, not now(): the body must be byte-identical on a retried
    # request or Metronome 409s instead of replaying the original commit.
    assert item["starting_at"].endswith("T00:00:00.000Z")
    assert item["ending_before"] == "9999-12-31T00:00:00.000Z"


@pytest.mark.asyncio
async def test_create_prepaid_commit_sends_idempotency_key_header():
    """The exactly-once guard lives at the money source: Metronome's native
    Idempotency-Key header. Live-verified (sandbox, 2026-06-10): same key +
    same body replays the SAME commit (one charge); same key + different
    body → 409. The Bearer auth must survive the header merge."""
    client, fake = _client_with(_resp(200, {}))
    await client.create_prepaid_commit(
        customer_id="cust-uuid-1",
        contract_id="contract-1",
        product_id="prod-credits",
        credit_type_id="ct-usd-cents",
        amount_cents=2500,
        description="isol8 top-up owner=org_abc amount_cents=2500",
        idempotency_key="topup-org_abc-3f1d2c",
    )

    _, kwargs = fake.post.call_args
    assert kwargs["headers"]["Idempotency-Key"] == "topup-org_abc-3f1d2c"
    assert kwargs["headers"]["Authorization"] == "Bearer tok_sandbox"


@pytest.mark.asyncio
async def test_create_gated_prepaid_commit_v2_body_shape():
    """The Metronome-native funding path. Live-verified (sandbox,
    2026-06-11): gating only exists on the v2 contract-edit surface (the v1
    create validator rejects payment_gate_config), and a gated commit must
    carry BOTH schedules — invoice_schedule is when money is collected (the
    Stripe invoice), access_schedule is when the credits unlock."""
    client, fake = _client_with(_resp(200, {}))
    await client.create_gated_prepaid_commit(
        customer_id="cust-uuid-1",
        contract_id="contract-1",
        product_id="prod-credits",
        credit_type_id="ct-usd-cents",
        amount_cents=2573,
        description="isol8 top-up owner=org_abc amount_cents=2573",
        idempotency_key="topup-org_abc-uuid-2",
        effective_at="2026-09-10T00:00:00.000Z",
    )

    args, kwargs = fake.post.call_args
    # ABSOLUTE v2 URL: the client's base is the v1 root; httpx passes an
    # absolute URL through untouched even with a base_url set.
    assert args[0] == "https://api.metronome.com/v2/contracts/edit"
    assert kwargs["headers"]["Idempotency-Key"] == "topup-org_abc-uuid-2"
    assert kwargs["headers"]["Authorization"] == "Bearer tok_sandbox"
    body = kwargs["json"]
    assert body["customer_id"] == "cust-uuid-1"
    assert body["contract_id"] == "contract-1"
    [commit] = body["add_commits"]
    assert commit["type"] == "PREPAID"
    assert commit["product_id"] == "prod-credits"
    assert commit["priority"] == 100
    assert commit["description"] == "isol8 top-up owner=org_abc amount_cents=2573"
    assert commit["payment_gate_config"] == {"payment_gate_type": "STRIPE"}
    access = commit["access_schedule"]
    assert access["credit_type_id"] == "ct-usd-cents"
    [access_item] = access["schedule_items"]
    assert access_item["amount"] == 2573  # CENTS (Metronome's USD credit type)
    invoice = commit["invoice_schedule"]
    assert invoice["credit_type_id"] == "ct-usd-cents"
    [invoice_item] = invoice["schedule_items"]
    # Credits unlocked == cents invoiced, 1:1.
    assert invoice_item["amount"] == 2573
    # UTC midnight everywhere, never now(): the body must be byte-identical
    # on a retried request or Metronome 409s instead of replaying.
    assert access_item["starting_at"] == "2026-09-10T00:00:00.000Z"
    assert access_item["ending_before"] == "9999-12-31T00:00:00.000Z"
    assert invoice_item["timestamp"] == "2026-09-10T00:00:00.000Z"
    assert set(access_item) == {"amount", "starting_at", "ending_before"}
    assert set(invoice_item) == {"amount", "timestamp"}


@pytest.mark.asyncio
async def test_reads_workspace_billing_provider_and_contract_links():
    requests: list[httpx.Request] = []

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        body = (
            {"data": [{"billing_provider": "stripe"}]}
            if request.url.path.endswith("getCustomerBillingProviderConfigurations")
            else {"data": {"customer_id": "metro_1"}}
        )
        return httpx.Response(200, json=body, request=request)

    async with httpx.AsyncClient(
        base_url="https://api.metronome.com/v1",
        transport=httpx.MockTransport(respond),
    ) as http:
        client = MetronomeClient(
            api_key="tok_sandbox",
            base_url="https://api.metronome.com/v1",
            http_client=http,
        )
        assert await client.get_customer_billing_provider_configurations(customer_id="metro_1") == [
            {"billing_provider": "stripe"}
        ]
        assert await client.get_contract(customer_id="metro_1", contract_id="contract_1") == {"customer_id": "metro_1"}

    assert requests[0].url == httpx.URL("https://api.metronome.com/v1/getCustomerBillingProviderConfigurations")
    assert json.loads(requests[0].content) == {"customer_id": "metro_1"}
    assert requests[1].url == httpx.URL("https://api.metronome.com/v2/contracts/get")


@pytest.mark.asyncio
async def test_get_net_balance_returns_parsed_data():
    # Live-verified response shape (sandbox, 2026-06-10): the cents number
    # rides under "balance" in the data sub-object the client passes through.
    client, fake = _client_with(_resp(200, {"data": {"balance": 1960.6684, "credit_type_id": "ct-usd-cents"}}))
    body = await client.get_net_balance(customer_id="org_abc")

    args, kwargs = fake.post.call_args
    assert args[0] == "/contracts/customerBalances/getNetBalance"
    assert kwargs["json"] == {"customer_id": "org_abc"}
    assert body == {"balance": 1960.6684, "credit_type_id": "ct-usd-cents"}


@pytest.mark.asyncio
async def test_raises_on_http_error():
    bad = _resp(500, {})
    bad.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=bad))
    client, _ = _client_with(bad)
    with pytest.raises(httpx.HTTPStatusError):
        await client.ingest([{"transaction_id": "x"}])


@pytest.mark.asyncio
async def test_a_configured_delivery_method_id_replaces_the_bare_method(monkeypatch):
    """Sandbox-verified 2026-09-22: with two Stripe integrations Metronome rejects the bare
    delivery_method ("'delivery_method_id' is a required parameter...") and takes exactly one of
    the two keys."""
    from core.billing import metronome_client

    monkeypatch.setattr(metronome_client.settings, "METRONOME_STRIPE_DELIVERY_METHOD_ID", "dm-1")
    client, fake = _client_with(_resp(200, {"data": {"id": "contract-uuid-1"}}))
    await client.create_contract(
        customer_id="cust-uuid-1", rate_card_id="rc-1", with_stripe_billing=True, uniqueness_key="k-1"
    )
    assert fake.post.call_args.kwargs["json"]["billing_provider_configuration"] == {
        "billing_provider": "stripe",
        "delivery_method_id": "dm-1",
    }

    client, fake = _client_with(_resp(200, {}))
    await client.set_stripe_billing_config(metronome_customer_id="cust-uuid-1", stripe_customer_id="cus_123")
    entry = fake.post.call_args.kwargs["json"]["data"][0]
    assert entry["delivery_method_id"] == "dm-1"
    assert "delivery_method" not in entry
