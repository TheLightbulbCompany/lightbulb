"""Unit tests for core.billing.bedrock_gate_tokens — per-owner gate bearer tokens.

Covers the load-bearing invariants: hash-only storage (no plaintext ever
persisted), the {tier: arn} -> {arn: tier} aip_tiers inversion the gateway keys
on, rotation deleting the superseded token row, and the deny kill-switch toggle.
"""

import hashlib
import time

import boto3
import pytest
from moto import mock_aws

from core.billing import bedrock_gate_tokens as mod

_AIPS = {
    "opus": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-opus",
    "sonnet": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-sonnet",
    "haiku": "arn:aws:bedrock:us-east-1:877352799272:application-inference-profile/aip-haiku",
}


@pytest.fixture
def gate_tables(monkeypatch):
    """moto-mocked containers + gate-tokens tables."""
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
        # container_repo resolves the containers table via
        # core.dynamodb.get_table("containers") -> table_name() which prepends
        # the module-level _table_prefix (cached at import). Patch it to "test-".
        monkeypatch.setattr("core.dynamodb._table_prefix", "test-")
        monkeypatch.setattr("core.dynamodb._dynamodb_resource", None)
        # The gate-tokens table name comes from the explicit override env var.
        monkeypatch.setenv("BEDROCK_GATE_TOKENS_TABLE", "test-gate-tokens")
        yield client


def _put_container_row(client, owner_id: str, **attrs) -> None:
    item = {
        "owner_id": {"S": owner_id},
        "id": {"S": "c-1"},
        "created_at": {"S": "2026-07-15T00:00:00Z"},
    }
    for k, v in attrs.items():
        item[k] = {"S": v}
    client.put_item(TableName="test-containers", Item=item)


def _token_rows(client) -> list[dict]:
    return client.scan(TableName="test-gate-tokens")["Items"]


class TestMint:
    @pytest.mark.asyncio
    async def test_writes_hash_only_row_with_inverted_aip_tiers(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")

        token = await mod.mint_gate_token("org_abc", _AIPS)

        rows = _token_rows(gate_tables)
        assert len(rows) == 1
        row = rows[0]
        # PK is the SHA-256 hex of the returned plaintext.
        assert row["token_hash"]["S"] == hashlib.sha256(token.encode()).hexdigest()
        assert row["owner_id"]["S"] == "org_abc"
        assert row["denied"]["BOOL"] is False
        assert "created_at" in row
        # aip_tiers is the INVERSE of the owner's {tier: arn} map: {arn: tier}.
        stored = {arn: v["S"] for arn, v in row["aip_tiers"]["M"].items()}
        assert stored == {arn: tier for tier, arn in _AIPS.items()}

    @pytest.mark.asyncio
    async def test_no_plaintext_token_is_persisted_anywhere(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")

        token = await mod.mint_gate_token("org_abc", _AIPS)

        # The plaintext must appear in NO stored attribute (hash-only storage).
        for row in _token_rows(gate_tables):
            for av in row.values():
                assert av.get("S") != token
        # And not on the containers row either — only the hash pointer.
        crow = gate_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert crow["bedrock_gate_token_hash"]["S"] == hashlib.sha256(token.encode()).hexdigest()
        for av in crow.values():
            assert av.get("S") != token

    @pytest.mark.asyncio
    async def test_points_containers_row_at_new_hash(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")

        token = await mod.mint_gate_token("org_abc", _AIPS)

        crow = gate_tables.get_item(TableName="test-containers", Key={"owner_id": {"S": "org_abc"}})["Item"]
        assert crow["bedrock_gate_token_hash"]["S"] == mod.hash_token(token)

    @pytest.mark.asyncio
    async def test_rotation_grace_expires_the_prior_token_row(self, gate_tables):
        old_hash = "deadbeef" * 8
        _put_container_row(gate_tables, "org_abc", bedrock_gate_token_hash=old_hash)
        gate_tables.put_item(
            TableName="test-gate-tokens",
            Item={"token_hash": {"S": old_hash}, "owner_id": {"S": "org_abc"}, "denied": {"BOOL": False}},
        )

        token = await mod.mint_gate_token("org_abc", _AIPS)

        rows = {r["token_hash"]["S"]: r for r in _token_rows(gate_tables)}
        # Both rows still exist (the running task keeps its old token through the
        # roll) — but the OLD row now carries a near-future ttl and the NEW one
        # does not, so DynamoDB reaps only the superseded token.
        assert old_hash in rows and mod.hash_token(token) in rows
        assert "ttl" in rows[old_hash]
        assert int(rows[old_hash]["ttl"]["N"]) > int(time.time())
        assert "ttl" not in rows[mod.hash_token(token)]

    @pytest.mark.asyncio
    async def test_returned_tokens_are_unique_per_mint(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")
        t1 = await mod.mint_gate_token("org_abc", _AIPS)
        t2 = await mod.mint_gate_token("org_abc", _AIPS)
        assert t1 != t2
        # Rotation keeps exactly ONE non-expiring (current) token per owner; the
        # prior one lingers with a ttl until DynamoDB reaps it.
        live = [r for r in _token_rows(gate_tables) if "ttl" not in r]
        assert len(live) == 1
        assert live[0]["token_hash"]["S"] == mod.hash_token(t2)


class TestDeny:
    @pytest.mark.asyncio
    async def test_toggles_denied_flag_on_the_current_token_row(self, gate_tables):
        _put_container_row(gate_tables, "org_abc")
        token = await mod.mint_gate_token("org_abc", _AIPS)
        h = mod.hash_token(token)

        await mod.deny_gate_token("org_abc", True)
        row = gate_tables.get_item(TableName="test-gate-tokens", Key={"token_hash": {"S": h}})["Item"]
        assert row["denied"]["BOOL"] is True

        await mod.deny_gate_token("org_abc", False)
        row = gate_tables.get_item(TableName="test-gate-tokens", Key={"token_hash": {"S": h}})["Item"]
        assert row["denied"]["BOOL"] is False

    @pytest.mark.asyncio
    async def test_noop_when_owner_has_no_token_hash(self, gate_tables):
        # Owner row exists but carries no bedrock_gate_token_hash.
        _put_container_row(gate_tables, "org_none")
        await mod.deny_gate_token("org_none", True)  # must not raise
        assert _token_rows(gate_tables) == []

    @pytest.mark.asyncio
    async def test_noop_when_owner_row_absent(self, gate_tables):
        await mod.deny_gate_token("ghost", True)  # must not raise
        assert _token_rows(gate_tables) == []


class TestTableName:
    def test_override_env_wins(self, monkeypatch):
        monkeypatch.setenv("BEDROCK_GATE_TOKENS_TABLE", "custom-tokens")
        assert mod._tokens_table_name() == "custom-tokens"

    def test_constructs_per_env_name_by_convention(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_GATE_TOKENS_TABLE", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod", raising=False)
        assert mod._tokens_table_name() == "isol8-prod-bedrock-gate-tokens"

    def test_defaults_env_to_dev_when_unset(self, monkeypatch):
        monkeypatch.delenv("BEDROCK_GATE_TOKENS_TABLE", raising=False)
        monkeypatch.setattr(mod.settings, "ENVIRONMENT", "", raising=False)
        assert mod._tokens_table_name() == "isol8-dev-bedrock-gate-tokens"
