"""Agent keys: created once, stored hashed, resolved only while live, and verified as their person."""

import time
from unittest.mock import AsyncMock

import pytest
from botocore.exceptions import ClientError

from core.services import agent_keys, hosted_mcp


class FakeTable:
    name = "t"

    def __init__(self):
        self.items = {}

    def put_item(self, Item):
        self.items[(Item["pk"], Item["sk"])] = dict(Item)

    def delete_item(self, Key):
        self.items.pop((Key["pk"], Key["sk"]), None)

    def transact(self, actions):
        """Just the shapes agent_keys sends; all conditions hold or nothing is written."""
        cancelled = ClientError({"Error": {"Code": "TransactionCanceledException"}}, "TransactWriteItems")
        for a in actions:
            if "Update" in a:
                u = a["Update"]
                n = self.items.get((u["Key"]["pk"], u["Key"]["sk"]), {}).get("n")
                if ":max" in u["ExpressionAttributeValues"] and n is not None and n >= agent_keys.MAX_KEYS_PER_PERSON:
                    raise cancelled
                if ":zero" in u["ExpressionAttributeValues"] and not n:
                    raise cancelled
            if "Delete" in a and "ConditionExpression" in a["Delete"]:
                if (a["Delete"]["Key"]["pk"], a["Delete"]["Key"]["sk"]) not in self.items:
                    raise cancelled
        for a in actions:
            if "Update" in a:
                u = a["Update"]
                row = self.items.setdefault((u["Key"]["pk"], u["Key"]["sk"]), {**u["Key"], "n": 0})
                row["n"] += next(v for k, v in u["ExpressionAttributeValues"].items() if k in (":one", ":neg"))
            elif "Put" in a:
                self.put_item(a["Put"]["Item"])
            else:
                self.delete_item(a["Delete"]["Key"])


@pytest.fixture
def table(monkeypatch):
    t = FakeTable()
    monkeypatch.setattr(agent_keys.repo, "_table", lambda: t)

    async def transact(items):
        t.transact(items)

    async def get(pk, sk):
        return t.items.get((pk, sk))

    async def query(pk, begins_with=None, **_):
        return [v for (p, s), v in t.items.items() if p == pk and s.startswith(begins_with or "")]

    monkeypatch.setattr(agent_keys.repo, "_transact", transact)
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
    assert not [k for k in table.items if k[0].startswith("AGENTKEY#")]
    assert table.items[("OWNER#org_a", "AGENTKEYCOUNT#user_a")]["n"] == 0


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


@pytest.mark.asyncio
async def test_removed_member_keys_are_gone_even_if_they_rejoin(table):
    mine = await agent_keys.create_key("org_a", "user_a", "Grok Bot", None)
    other = await agent_keys.create_key("org_a", "user_b", "Muse", None)
    await agent_keys.delete_for_member("org_a", "user_a")
    assert await agent_keys.resolve(mine["key"]) is None
    assert await agent_keys.list_keys("org_a", "user_a") == []
    assert (await agent_keys.resolve(other["key"]))["userId"] == "user_b"
    await agent_keys.create_key("org_a", "user_a", "fresh", None)  # counter reset too


@pytest.mark.asyncio
async def test_owner_wipe_deletes_lookup_rows_outside_the_partition(table):
    made = await agent_keys.create_key("org_a", "user_a", "Grok Bot", None)
    await agent_keys.delete_for_owner("org_a")
    assert await agent_keys.resolve(made["key"]) is None
    assert not [k for k in table.items if k[0].startswith("AGENTKEY#")]


@pytest.mark.asyncio
async def test_non_member_create_is_refused_and_writes_nothing(table, monkeypatch):
    from fastapi import HTTPException

    from core.auth import AuthContext
    from routers import agent_keys as router

    auth = AuthContext(user_id="user_gone", org_id="org_a")
    body = router.CreateAgentKey(name="Grok Bot")
    for orgs in ([{"id": "org_other"}], RuntimeError("clerk down")):  # removed, then fail closed
        mock = AsyncMock(side_effect=orgs) if isinstance(orgs, Exception) else AsyncMock(return_value=orgs)
        monkeypatch.setattr(router.clerk_admin, "list_user_organizations", mock)
        with pytest.raises(HTTPException) as err:
            await router.create_agent_key(body, auth)
        assert err.value.status_code == 403
        assert table.items == {}
