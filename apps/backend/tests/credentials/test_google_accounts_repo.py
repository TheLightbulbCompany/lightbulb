"""Tests for the per-agent Google account record store (google_accounts_repo).

The repo is a single-table overlay on the existing ``integrations`` table
(owner_id PK, provider SK). It namespaces two record families into the SK:

  - ``google#account#<email>``  — inventory of a connected Google account.
  - ``google#assign#<agent_id>`` — which account an agent is bound to.

These tests pin the exact SK strings written, so the namespacing scheme is
legible to a future reader straight from the assertions.
"""

import pytest

from core.repositories import google_accounts_repo


@pytest.fixture
def fake_table(monkeypatch):
    """In-memory stand-in for the ``integrations`` DynamoDB table.

    Mirrors the fake_table fixture in tests/credentials/test_integrations_repo.py:
    put_item / get_item / delete_item / query keyed by (owner_id, provider).
    ``query`` is owner-scoped (filters by the KeyConditionExpression's owner_id)
    so owner isolation can be exercised.
    """
    store = {}

    class _T:
        def put_item(self, Item):
            store[(Item["owner_id"], Item["provider"])] = Item

        def update_item(self, Key, UpdateExpression, ExpressionAttributeValues=None, **kw):
            # Minimal emulation of the single UpdateExpression the repo issues:
            # "SET connected_at = if_not_exists(connected_at, :now)". if_not_exists
            # keeps the stored value when the attr already exists, else writes the
            # placeholder — exactly DynamoDB's documented semantics. That is what
            # makes re-adding an account preserve its original connected_at.
            item = store.setdefault((Key["owner_id"], Key["provider"]), dict(Key))
            values = ExpressionAttributeValues or {}
            assert UpdateExpression == "SET connected_at = if_not_exists(connected_at, :now)"
            if "connected_at" not in item:
                item["connected_at"] = values[":now"]

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

    monkeypatch.setattr(google_accounts_repo, "_table", lambda: _T())
    return store


def _assign(store, owner_id: str, agent_id: str, email: str) -> None:
    """An existing assignment row (nothing writes new ones; old rows still resolve)."""
    store[(owner_id, f"google#assign#{agent_id}")] = {
        "owner_id": owner_id,
        "provider": f"google#assign#{agent_id}",
        "email": email,
    }


# ---------------------------------------------------------------------------
# Account inventory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_account_writes_namespaced_sk(fake_table):
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")

    # Pin the exact SK so the namespacing scheme is legible from the test.
    item = fake_table[("org_1", "google#account#alice@gmail.com")]
    assert item["owner_id"] == "org_1"
    assert item["provider"] == "google#account#alice@gmail.com"
    assert isinstance(item["connected_at"], str)


@pytest.mark.asyncio
async def test_add_account_is_idempotent_overwrite(fake_table):
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")

    # A second add must not create a second row.
    keys = [k for k in fake_table if k[0] == "org_1"]
    assert keys == [("org_1", "google#account#alice@gmail.com")]


@pytest.mark.asyncio
async def test_readd_account_preserves_original_connected_at(fake_table):
    # The default account is the OLDEST (smallest connected_at). Re-running the
    # OAuth callback for an already-connected account must NOT bump its
    # connected_at — otherwise reauthorizing the first-connected account would
    # make it newer than later accounts, silently flipping the owner's default.
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")
    fake_table[("org_1", "google#account#alice@gmail.com")]["connected_at"] = "2026-01-01T00:00:00+00:00"
    await google_accounts_repo.add_account("org_1", "bob@gmail.com")
    fake_table[("org_1", "google#account#bob@gmail.com")]["connected_at"] = "2026-01-02T00:00:00+00:00"

    # Re-add alice (reauthorize): her connected_at must stay t1, not jump to now.
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")
    assert fake_table[("org_1", "google#account#alice@gmail.com")]["connected_at"] == "2026-01-01T00:00:00+00:00"

    # The oldest-default ordering is therefore stable: alice still leads.
    accounts = await google_accounts_repo.list_accounts("org_1")
    assert [a["email"] for a in accounts] == ["alice@gmail.com", "bob@gmail.com"]


@pytest.mark.asyncio
async def test_list_accounts_returns_both_sorted_by_connected_at(fake_table):
    # Write out of order to prove the sort, not insertion order.
    await google_accounts_repo.add_account("org_1", "bob@gmail.com")
    fake_table[("org_1", "google#account#bob@gmail.com")]["connected_at"] = "2026-01-02T00:00:00+00:00"
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")
    fake_table[("org_1", "google#account#alice@gmail.com")]["connected_at"] = "2026-01-01T00:00:00+00:00"

    accounts = await google_accounts_repo.list_accounts("org_1")
    assert [a["email"] for a in accounts] == ["alice@gmail.com", "bob@gmail.com"]
    assert accounts[0]["connected_at"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_list_accounts_ignores_assignment_rows(fake_table):
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")
    _assign(fake_table, "org_1", "agent_x", "alice@gmail.com")

    accounts = await google_accounts_repo.list_accounts("org_1")
    assert [a["email"] for a in accounts] == ["alice@gmail.com"]


@pytest.mark.asyncio
async def test_list_accounts_empty(fake_table):
    assert await google_accounts_repo.list_accounts("org_nobody") == []


@pytest.mark.asyncio
async def test_remove_account_deletes_the_right_item(fake_table):
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")
    await google_accounts_repo.add_account("org_1", "bob@gmail.com")

    await google_accounts_repo.remove_account("org_1", "alice@gmail.com")

    assert ("org_1", "google#account#alice@gmail.com") not in fake_table
    assert ("org_1", "google#account#bob@gmail.com") in fake_table


# ---------------------------------------------------------------------------
# Per-agent assignments
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_assignments_returns_map(fake_table):
    _assign(fake_table, "org_1", "agent_x", "alice@gmail.com")
    _assign(fake_table, "org_1", "agent_y", "bob@gmail.com")

    assert await google_accounts_repo.list_assignments("org_1") == {
        "agent_x": "alice@gmail.com",
        "agent_y": "bob@gmail.com",
    }


@pytest.mark.asyncio
async def test_list_assignments_ignores_account_rows(fake_table):
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")
    _assign(fake_table, "org_1", "agent_x", "alice@gmail.com")

    assert await google_accounts_repo.list_assignments("org_1") == {"agent_x": "alice@gmail.com"}


@pytest.mark.asyncio
async def test_list_assignments_empty(fake_table):
    assert await google_accounts_repo.list_assignments("org_nobody") == {}


@pytest.mark.asyncio
async def test_remove_assignment_deletes_the_right_item(fake_table):
    _assign(fake_table, "org_1", "agent_x", "alice@gmail.com")
    _assign(fake_table, "org_1", "agent_y", "bob@gmail.com")

    await google_accounts_repo.remove_assignment("org_1", "agent_x")

    assert ("org_1", "google#assign#agent_x") not in fake_table
    assert ("org_1", "google#assign#agent_y") in fake_table


@pytest.mark.asyncio
async def test_get_assignment_returns_the_explicit_email(fake_table):
    _assign(fake_table, "org_1", "agent_x", "alice@gmail.com")

    assert await google_accounts_repo.get_assignment("org_1", "agent_x") == "alice@gmail.com"


@pytest.mark.asyncio
async def test_get_assignment_returns_none_with_no_default_fallback(fake_table):
    # An owner with connected accounts but NO explicit assignment for this agent
    # must get None — get_assignment never falls back to the oldest/default
    # account. (That fallback is exactly what would leave a stale IDENTITY block.)
    await google_accounts_repo.add_account("org_1", "alice@gmail.com")

    assert await google_accounts_repo.get_assignment("org_1", "agent_unassigned") is None
