"""Incomplete directory reads cannot become terminal shared-email routing choices."""

from unittest.mock import AsyncMock

import httpx
import pytest

from core.services import clerk_admin


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(clerk_admin.settings, "CLERK_SECRET_KEY", "sk_test")
    client = AsyncMock()
    client.__aenter__.return_value = client
    monkeypatch.setattr(clerk_admin.httpx, "AsyncClient", lambda **kwargs: client)
    return client


@pytest.mark.parametrize("status,payload", [(503, {}), (200, {}), (200, None), (200, [{"id": "one"}, {"id": "two"}])])
async def test_strict_user_lookup_refuses_failure_malformed_or_nonunique(client, status, payload):
    client.get.return_value = httpx.Response(status, json=payload)
    with pytest.raises((RuntimeError, ValueError)):
        await clerk_admin.find_user_by_email("sender@example.com", strict=True)
    assert client.get.await_args.kwargs["params"]["limit"] == 2


@pytest.mark.parametrize(
    "payload",
    [None, {}, {"data": [{}], "total_count": 1}, {"data": [{"organization": {"id": "org_one"}}], "total_count": 2}],
)
async def test_strict_membership_lookup_never_routes_partial_data(client, payload):
    client.get.return_value = httpx.Response(200, json=payload)
    with pytest.raises(RuntimeError):
        await clerk_admin.list_user_organizations("user_sender", limit=100, strict=True)
