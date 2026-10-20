"""Agent keys: created once, stored hashed, resolved only while live, and verified as their person."""

import time
from unittest.mock import AsyncMock

import pytest

from core.services import agent_keys, hosted_mcp


class FakeTable:
    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)


@pytest.fixture
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(agent_keys.repo, "_table", lambda: t)

    async def get(pk, sk):
        return t.items.get((pk, sk))

    async def query(pk, begins_with=None, **_):
        return [v for (p, s), v in t.items.items() if p == pk and s.startswith(begins_with or "")]

    monkeypatch.setattr(agent_keys.repo, "_get", get)
    monkeypatch.setattr(agent_keys.repo, "_query", query)
    return t


@pytest.mark.asyncio
async def test_key_is_shown_once_stored_hashed_and_revocable(table):
    made = await agent_keys.create_key("org_a", "user_a", " Grok  Bot ", 90)
    assert made["key"].startswith("lbk_") and made["name"] == "Grok Bot"
    assert made["key"] not in repr(table.items)  # only the hash is stored
    assert "key" not in (await agent_keys.list_keys("org_a", "user_a"))[0]
    assert (await agent_keys.resolve(made["key"]))["userId"] == "user_a"
    assert await agent_keys.resolve("lbk_wrong") is None
    assert not await agent_keys.revoke_key("org_a", "user_b", made["keyId"])  # not theirs
    assert await agent_keys.revoke_key("org_a", "user_a", made["keyId"])
    assert await agent_keys.resolve(made["key"]) is None  # next call fails
    assert table.items == {}


@pytest.mark.asyncio
async def test_expired_key_and_key_cap(table):
    made = await agent_keys.create_key("org_a", "user_a", "Muse", 1)
    table.items[(agent_keys._lookup_pk(agent_keys._hash(made["key"])), "META")]["expiresAt"] = int(time.time()) - 1
    assert await agent_keys.resolve(made["key"]) is None
    for i in range(agent_keys.MAX_KEYS_PER_PERSON - 1):
        await agent_keys.create_key("org_a", "user_a", f"a{i}", None)
    with pytest.raises(agent_keys.AgentKeyError):
        await agent_keys.create_key("org_a", "user_a", "one too many", None)


@pytest.mark.asyncio
async def test_verifier_acts_as_person_labels_agent_and_dies_with_membership(table, monkeypatch):
    made = await agent_keys.create_key("org_a", "user_a", "Grok Bot", None)
    member = [{"id": "org_a"}]
    monkeypatch.setattr(
        hosted_mcp.clerk_admin, "list_user_organizations", AsyncMock(side_effect=lambda *a, **k: member)
    )
    token = await hosted_mcp.LightbulbVerifier().verify_token(made["key"])
    assert token.subject == "user_a" and token.claims["org_id"] == "org_a"
    assert {hosted_mcp.READ_SCOPE, hosted_mcp.WRITE_SCOPE} <= set(token.scopes)
    slug, name = hosted_mcp._sender(token)
    assert name == "Grok Bot" and slug.startswith("agent-")
    member.clear()  # removed from the workspace
    assert await hosted_mcp.LightbulbVerifier().verify_token(made["key"]) is None
