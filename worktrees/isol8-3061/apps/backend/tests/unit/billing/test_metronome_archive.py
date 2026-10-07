"""Unit tests for MetronomeClient.archive_customer — Admin Account-Wipe Task 2.

Mirrors the mocking style of tests/unit/billing/test_metronome_client.py:
mocked AsyncClient (respx mismatches httpx 0.28 URLs), asserting on the
captured call args.
"""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from core.billing.metronome_client import MetronomeClient


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
async def test_archive_customer_posts_id_to_archive_path():
    client, fake = _client_with(_resp(200, {}))

    result = await client.archive_customer(customer_id="cust-uuid-1")

    assert result is None
    fake.post.assert_awaited_once()
    args, kwargs = fake.post.call_args
    assert args[0] == "/customers/archive"
    assert kwargs["json"] == {"id": "cust-uuid-1"}
    assert kwargs["headers"]["Authorization"] == "Bearer tok_sandbox"


@pytest.mark.asyncio
async def test_archive_customer_swallows_404_as_success():
    """Already-gone (or never-provisioned) customer — a re-run after a
    partial wipe failure must not raise."""
    bad = _resp(404, {})
    bad.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("not found", request=MagicMock(), response=bad))
    client, _ = _client_with(bad)

    result = await client.archive_customer(customer_id="cust-gone")

    assert result is None


@pytest.mark.asyncio
async def test_archive_customer_reraises_other_http_errors():
    bad = _resp(500, {})
    bad.raise_for_status = MagicMock(side_effect=httpx.HTTPStatusError("boom", request=MagicMock(), response=bad))
    client, _ = _client_with(bad)

    with pytest.raises(httpx.HTTPStatusError):
        await client.archive_customer(customer_id="cust-uuid-1")
