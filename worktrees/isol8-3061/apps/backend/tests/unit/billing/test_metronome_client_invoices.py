"""MetronomeClient.list_customer_invoices — draft-invoice read for the spend surface."""

import httpx
import pytest

from core.billing.metronome_client import MetronomeClient


def _client(handler) -> MetronomeClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url="https://api.metronome.com/v1")
    return MetronomeClient(api_key="test-key", base_url="https://api.metronome.com/v1", http_client=http)


@pytest.mark.asyncio
async def test_list_customer_invoices_hits_customer_scoped_path_with_status():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={"data": [{"id": "inv-1", "line_items": []}]})

    client = _client(handler)
    invoices = await client.list_customer_invoices(customer_id="cus-123")

    assert seen["path"].endswith("/customers/cus-123/invoices")
    assert seen["params"] == {"status": "DRAFT"}
    assert invoices == [{"id": "inv-1", "line_items": []}]


@pytest.mark.asyncio
async def test_list_customer_invoices_empty_data_returns_empty_list():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)
    assert await client.list_customer_invoices(customer_id="cus-123") == []


@pytest.mark.asyncio
async def test_get_customer_invoice_hits_invoice_scoped_path():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200,
            json={
                "data": {"id": "inv-1", "line_items": [{"total": 100.0, "pricing_group_values": {"tier": "sonnet"}}]}
            },
        )

    client = _client(handler)
    invoice = await client.get_customer_invoice(customer_id="cus-123", invoice_id="inv-1")

    assert seen["path"].endswith("/customers/cus-123/invoices/inv-1")
    assert invoice == {"id": "inv-1", "line_items": [{"total": 100.0, "pricing_group_values": {"tier": "sonnet"}}]}


@pytest.mark.asyncio
async def test_get_customer_invoice_missing_data_returns_empty_dict():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    client = _client(handler)
    assert await client.get_customer_invoice(customer_id="cus-123", invoice_id="inv-1") == {}
