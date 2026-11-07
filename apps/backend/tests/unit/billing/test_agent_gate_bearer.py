"""Per-agent gate bearers: one token row per roster agent, ``{owner_id, agent_id}``, hash-only."""

import json

import boto3
import pytest
from boto3.dynamodb.types import TypeDeserializer
from moto import mock_aws

from core.billing import bedrock_gate_tokens as mod


@pytest.fixture
def gate_tables(monkeypatch):
    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        client.create_table(
            TableName="test-gate-tokens",
            KeySchema=[{"AttributeName": "token_hash", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "token_hash", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", None)
        monkeypatch.setenv("BEDROCK_GATE_TOKENS_TABLE", "test-gate-tokens")
        # No runtime ARN: the mint's retention pass presents nothing, so it needs no AgentCore fake.
        client.put_item(
            TableName="test-containers",
            Item={"owner_id": {"S": "org_abc"}, "id": {"S": "c-1"}, "created_at": {"S": "2026-07-15T00:00:00Z"}},
        )
        yield client


def _rows(client) -> dict:
    return {r["token_hash"]["S"]: r for r in client.scan(TableName="test-gate-tokens")["Items"]}


@pytest.mark.asyncio
async def test_agent_bearer_row_carries_owner_and_agent_and_is_distinct(gate_tables):
    owner = await mod.mint_gate_token("org_abc", row_id="c-1")
    a1 = await mod.mint_agent_gate_token("org_abc", "agent-1", row_id="c-1")
    a2 = await mod.mint_agent_gate_token("org_abc", "agent-2", row_id="c-1")
    assert len({owner, a1, a2}) == 3
    # deterministic: a reconcile re-mint yields the same bearer and no extra row
    assert await mod.mint_agent_gate_token("org_abc", "agent-1", row_id="c-1") == a1
    rows = _rows(gate_tables)
    assert len(rows) == 3
    assert rows[mod.hash_token(a1)]["owner_id"]["S"] == "org_abc"
    assert rows[mod.hash_token(a1)]["agent_id"]["S"] == "agent-1"
    assert rows[mod.hash_token(a2)]["agent_id"]["S"] == "agent-2"
    assert "agent_id" not in rows[mod.hash_token(owner)]  # legacy owner-level bearer unchanged
    assert a1 not in json.dumps(list(rows.values()))  # hash-only storage


@pytest.mark.asyncio
async def test_deny_reaches_agent_bearers_and_a_new_agent_inherits_the_deny(gate_tables):
    await mod.mint_gate_token("org_abc", row_id="c-1")
    a1 = await mod.mint_agent_gate_token("org_abc", "agent-1", row_id="c-1")
    item = gate_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
    plain = {k: TypeDeserializer().deserialize(v) for k, v in item.items()}
    assert mod.hash_token(a1) in mod.owner_gate_token_hashes(plain)  # wipe + deny use this set

    await mod.deny_gate_token("org_abc", True)
    assert {r["denied"]["BOOL"] for r in _rows(gate_tables).values()} == {True}

    a2 = await mod.mint_agent_gate_token("org_abc", "agent-2", row_id="c-1")
    assert _rows(gate_tables)[mod.hash_token(a2)]["denied"]["BOOL"] is True


@pytest.mark.asyncio
async def test_write_profile_for_agent_mints_and_writes_that_agents_bearer(monkeypatch):
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from core.services import hermes_provisioning as hp

    inputs = SimpleNamespace(
        provider_choice="chatgpt_oauth",
        integrations={},
        env_vars={},
        byo_provider=None,
        byo_api_key=None,
        front_desk_number_for=lambda a: None,
    )
    monkeypatch.setattr(hp, "resolve_profile_inputs", AsyncMock(return_value=inputs))
    monkeypatch.setattr(hp, "_stage_owner_facts", AsyncMock())
    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", AsyncMock())
    monkeypatch.setattr("core.services.network_member.publish_sessions", AsyncMock())
    monkeypatch.setattr(
        "core.repositories.container_repo.get_by_owner_id",
        AsyncMock(return_value={"id": "c-1", "gate_bearer_generation": 2}),
    )
    mint = AsyncMock(return_value="agent-bearer")
    monkeypatch.setattr(mod, "mint_agent_gate_token", mint)
    uploaded = {}

    async def capture(owner, agent, files):
        uploaded["dotenv"] = files.dotenv

    monkeypatch.setattr("core.services.hermes_profiles.upload_profile", capture)

    await hp.write_profile_for_agent("org_abc", {"id": "agent-1", "name": "Friday", "role": "ceo"})

    mint.assert_awaited_once_with("org_abc", "agent-1", row_id="c-1", generation=2)
    assert "LIGHTBULB_AGENT_GATE_BEARER=agent-bearer" in uploaded["dotenv"]


def test_profile_env_carries_the_agents_own_bearer():
    from core.services.hermes_profiles import build_profile_env

    env = build_profile_env(env_vars={}, gate_bearer="agent-bearer")
    assert env["LIGHTBULB_AGENT_GATE_BEARER"] == "agent-bearer"
    assert "LIGHTBULB_AGENT_GATE_BEARER" not in build_profile_env(env_vars={})
