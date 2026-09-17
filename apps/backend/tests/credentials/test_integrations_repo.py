"""Tests for the secret-free integration STATUS index (integrations_repo).

The Phase-2 credential redesign removed the DynamoDB credential vault. This repo
now holds ONLY connection status — never a credential. These tests assert both
the happy-path CRUD behavior and, critically, that no secret-bearing key ever
lands in a stored item.
"""

import pytest

from core.repositories import integrations_repo


@pytest.fixture
def fake_table(monkeypatch):
    """In-memory stand-in for the ``integrations`` DynamoDB table.

    Mirrors the fake_table fixture in tests/credentials/test_apply_connection.py:
    put_item / get_item / delete_item / query keyed by (owner_id, provider).
    ``query`` is owner-scoped (filters by the KeyConditionExpression's owner_id)
    so list_for_owner isolation can be exercised.
    """
    store = {}

    class _T:
        def put_item(self, Item):
            store[(Item["owner_id"], Item["provider"])] = Item

        def get_item(self, Key):
            it = store.get((Key["owner_id"], Key["provider"]))
            return {"Item": it} if it else {}

        def delete_item(self, Key):
            store.pop((Key["owner_id"], Key["provider"]), None)

        def query(self, KeyConditionExpression=None, **kw):
            # Extract the owner_id the repo queried on from the boto3
            # Key("owner_id").eq(owner_id) condition so we can owner-scope.
            owner_id = None
            if KeyConditionExpression is not None:
                values = getattr(KeyConditionExpression, "_values", None)
                if values:
                    owner_id = values[-1]
            items = [it for (oid, _provider), it in store.items() if owner_id is None or oid == owner_id]
            return {"Items": items}

    monkeypatch.setattr(integrations_repo, "_table", lambda: _T())
    return store


_SECRET_KEYS = {"credential_enc", "credential", "token", "secret", "refresh_token", "access_token"}


@pytest.mark.asyncio
async def test_put_status_writes_metadata_and_no_secret(fake_table):
    await integrations_repo.put_status("org_1", "linear")
    item = fake_table[("org_1", "linear")]

    assert item["owner_id"] == "org_1"
    assert item["provider"] == "linear"
    assert item["status"] == "connected"
    assert isinstance(item["connected_at"], int)
    assert isinstance(item["updated_at"], int)
    # The status index is secret-free: NO credential-bearing key may appear.
    assert _SECRET_KEYS.isdisjoint(item.keys())
    assert "credential_enc" not in item


@pytest.mark.asyncio
async def test_put_status_defaults_to_connected(fake_table):
    await integrations_repo.put_status("org_1", "stripe")
    assert fake_table[("org_1", "stripe")]["status"] == "connected"


@pytest.mark.asyncio
async def test_put_status_honors_explicit_status(fake_table):
    await integrations_repo.put_status("org_1", "stripe", "pending")
    assert fake_table[("org_1", "stripe")]["status"] == "pending"


@pytest.mark.asyncio
async def test_get_returns_row(fake_table):
    await integrations_repo.put_status("org_1", "linear")
    row = await integrations_repo.get("org_1", "linear")
    assert row is not None
    assert row["provider"] == "linear"
    assert row["status"] == "connected"


@pytest.mark.asyncio
async def test_get_absent_returns_none(fake_table):
    assert await integrations_repo.get("org_1", "linear") is None


@pytest.mark.asyncio
async def test_list_for_owner_scopes_to_owner(fake_table):
    await integrations_repo.put_status("org_1", "linear")
    await integrations_repo.put_status("org_1", "stripe")
    await integrations_repo.put_status("org_2", "google")

    rows = await integrations_repo.list_for_owner("org_1")
    assert {r["provider"] for r in rows} == {"linear", "stripe"}
    # org_2's row must not leak into org_1's listing.
    assert all(r["owner_id"] == "org_1" for r in rows)


@pytest.mark.asyncio
async def test_list_for_owner_empty(fake_table):
    assert await integrations_repo.list_for_owner("org_nobody") == []


@pytest.mark.asyncio
async def test_delete_removes_row(fake_table):
    await integrations_repo.put_status("org_1", "linear")
    await integrations_repo.delete("org_1", "linear")
    assert await integrations_repo.get("org_1", "linear") is None
    assert ("org_1", "linear") not in fake_table


def test_manual_mcp_key_claims_no_registry_provider():
    """M6-R26 fix round 1, minor: MANUAL_MCP_KEY is a reserved sort key that
    must never collide with a real registry provider key -- if it ever did,
    mcp_server.build_connected_integration_servers's `list_for_owner` loop
    would try to registry.get_provider("manual_mcp") and either silently skip
    the manual bucket (KeyError -> continue) or, worse, treat it as a real
    provider row."""
    from core.services.credentials import registry

    with pytest.raises(KeyError):
        registry.get_provider(integrations_repo.MANUAL_MCP_KEY)
