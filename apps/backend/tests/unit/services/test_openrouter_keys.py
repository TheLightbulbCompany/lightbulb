import httpx
import pytest
import boto3
from moto import mock_aws

from core.repositories import openrouter_key_repo
from core.services.openrouter_keys import (
    create_completion_key,
    ensure_owner_key,
    ensure_owner_key_for_settings,
)


@pytest.mark.asyncio
async def test_create_completion_key_keeps_management_key_out_of_result():
    async def respond(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url == "https://openrouter.ai/api/v1/keys"
        assert request.headers["Authorization"] == "Bearer management-secret"
        assert request.read().decode() == (
            '{"name":"lightbulb-dev-owner-123","workspace_id":"workspace-uuid","limit":2.5,"limit_reset":"daily"}'
        )
        return httpx.Response(
            201,
            json={
                "data": {"hash": "key-hash", "workspace_id": "workspace-uuid", "limit": 2.5},
                "key": "sk-or-v1-owner-secret",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
        result = await create_completion_key(
            http,
            management_key="management-secret",
            owner_id="owner-123",
            workspace_id="workspace-uuid",
            environment="dev",
            limit_usd=2.5,
        )

    assert result == {"hash": "key-hash", "key": "sk-or-v1-owner-secret"}


@pytest.mark.asyncio
async def test_create_completion_key_rejects_missing_one_time_plaintext():
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(201, json={"data": {"hash": "key-hash"}}))
    ) as http:
        with pytest.raises(ValueError, match="one-time key"):
            await create_completion_key(
                http,
                management_key="management-secret",
                owner_id="owner-123",
                workspace_id="workspace-uuid",
                environment="dev",
                limit_usd=2.5,
            )


@pytest.mark.asyncio
async def test_ensure_owner_key_persists_plaintext_only_in_secrets_manager(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-dev-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name="lightbulb/dev/openrouter/management", SecretString="management-secret")
        creations = 0

        def respond(request: httpx.Request) -> httpx.Response:
            nonlocal creations
            creations += 1
            return httpx.Response(201, json={"data": {"hash": "key-hash"}, "key": "sk-or-v1-owner-secret"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            for _ in range(2):
                assert (
                    await ensure_owner_key(
                        "owner-123",
                        http=http,
                        sm=sm,
                        environment="dev",
                        workspace_id="workspace-uuid",
                        limit_usd=2.5,
                        now_sec=100,
                    )
                    == "key-hash"
                )

        assert creations == 1
        assert sm.get_secret_value(SecretId="lightbulb/dev/openrouter/owners/owner-123/key-hash")["SecretString"] == (
            "sk-or-v1-owner-secret"
        )
        row = table.get_item(Key={"owner_id": "owner-123"})["Item"]
        assert row["state"] == "active"
        assert row["key_hash"] == "key-hash"
        assert row["workspace_id"] == "workspace-uuid"
        assert "sk-or-v1-owner-secret" not in str(row)


@pytest.mark.asyncio
async def test_failed_secret_write_disables_new_key_and_releases_claim(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-dev-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name="lightbulb/dev/openrouter/management", SecretString="management-secret")
        monkeypatch.setattr(
            "core.services.openrouter_keys.create_or_update_secret",
            lambda *_: (_ for _ in ()).throw(OSError("secret write failed")),
        )
        disabled = []

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(201, json={"data": {"hash": "key-hash"}, "key": "sk-or-v1-owner-secret"})
            if request.method == "PATCH":
                assert request.url.path == "/api/v1/keys/key-hash"
                assert request.read() == b'{"disabled":true}'
                disabled.append("key-hash")
                return httpx.Response(200, json={"data": {"hash": "key-hash", "disabled": True}})
            raise AssertionError(request.method)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with pytest.raises(OSError, match="secret write failed"):
                await ensure_owner_key(
                    "owner-123",
                    http=http,
                    sm=sm,
                    environment="dev",
                    workspace_id="workspace-uuid",
                    limit_usd=2.5,
                    now_sec=100,
                )
        assert disabled == ["key-hash"]
        assert table.get_item(Key={"owner_id": "owner-123"}).get("Item") is None


@pytest.mark.asyncio
async def test_stale_claim_disables_orphan_before_creating_replacement(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-dev-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        assert await openrouter_key_repo.claim_new("owner-123", "crashed-attempt", now_sec=1)
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name="lightbulb/dev/openrouter/management", SecretString="management-secret")
        calls = []

        def respond(request: httpx.Request) -> httpx.Response:
            calls.append((request.method, request.url.path))
            if request.method == "GET":
                assert request.url.params["workspace_id"] == "workspace-uuid"
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {"name": "lightbulb-dev-owner-123", "hash": "orphan-hash"},
                            {"name": "lightbulb-dev-other-owner", "hash": "other-hash"},
                        ]
                    },
                )
            if request.method == "PATCH":
                return httpx.Response(200, json={"data": {"hash": "orphan-hash", "disabled": True}})
            if request.method == "POST":
                return httpx.Response(201, json={"data": {"hash": "replacement-hash"}, "key": "sk-or-v1-new"})
            raise AssertionError(request.method)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            assert (
                await ensure_owner_key(
                    "owner-123",
                    http=http,
                    sm=sm,
                    environment="dev",
                    workspace_id="workspace-uuid",
                    limit_usd=2.5,
                    now_sec=500,
                )
                == "replacement-hash"
            )
        assert calls == [
            ("GET", "/api/v1/keys"),
            ("PATCH", "/api/v1/keys/orphan-hash"),
            ("POST", "/api/v1/keys"),
        ]


@pytest.mark.asyncio
async def test_ambiguous_create_failure_keeps_claim_for_orphan_reconciliation(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-dev-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name="lightbulb/dev/openrouter/management", SecretString="management-secret")

        def respond(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("upstream may have created a key", request=request)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            with pytest.raises(httpx.ReadTimeout):
                await ensure_owner_key(
                    "owner-123",
                    http=http,
                    sm=sm,
                    environment="dev",
                    workspace_id="workspace-uuid",
                    limit_usd=2.5,
                    now_sec=100,
                )
        assert table.get_item(Key={"owner_id": "owner-123"})["Item"]["state"] == "pending"


@pytest.mark.asyncio
async def test_settings_wrapper_fails_closed_without_workspace_configuration(monkeypatch):
    monkeypatch.setattr("core.services.openrouter_keys.settings.OPENROUTER_WORKSPACE_ID", "")
    with pytest.raises(RuntimeError, match="workspace"):
        await ensure_owner_key_for_settings("owner-123")


@pytest.mark.asyncio
async def test_disabled_owner_key_can_be_replaced_without_reenabling_old_key(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-dev-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        table.put_item(
            Item={
                "owner_id": "owner-123",
                "state": "disabled",
                "key_hash": "old-hash",
                "secret_name": "lightbulb/dev/openrouter/owners/owner-123/old-hash",
            }
        )
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name="lightbulb/dev/openrouter/management", SecretString="management-secret")

        def respond(request: httpx.Request) -> httpx.Response:
            if request.method == "GET":
                return httpx.Response(200, json={"data": []})
            assert request.method == "POST"
            return httpx.Response(201, json={"data": {"hash": "new-hash"}, "key": "sk-or-v1-new"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            assert (
                await ensure_owner_key(
                    "owner-123",
                    http=http,
                    sm=sm,
                    environment="dev",
                    workspace_id="workspace-uuid",
                    limit_usd=2.5,
                    now_sec=1000,
                )
                == "new-hash"
            )
        row = table.get_item(Key={"owner_id": "owner-123"})["Item"]
        assert row["state"] == "active"
        assert row["key_hash"] == "new-hash"


@pytest.mark.asyncio
async def test_existing_key_in_another_workspace_fails_closed(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-dev-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        table.put_item(
            Item={
                "owner_id": "owner-123",
                "state": "active",
                "key_hash": "old-hash",
                "workspace_id": "old-workspace",
                "secret_name": "historical-secret",
            }
        )
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("upstream called"))) as http:
            with pytest.raises(RuntimeError, match="workspace"):
                await ensure_owner_key(
                    "owner-123",
                    http=http,
                    sm=sm,
                    environment="dev",
                    workspace_id="new-workspace",
                    limit_usd=2.5,
                    now_sec=1000,
                )


@pytest.mark.asyncio
async def test_resubscribe_reuses_key_before_retirement_worker_claims_it(monkeypatch):
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-dev-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        name = "lightbulb/dev/openrouter/owners/owner-123/key-hash"
        table.put_item(
            Item={
                "owner_id": "owner-123",
                "state": "active",
                "key_hash": "key-hash",
                "workspace_id": "workspace-uuid",
                "secret_name": name,
            }
        )
        assert await openrouter_key_repo.mark_retiring("owner-123")
        table.update_item(
            Key={"owner_id": "owner-123"},
            UpdateExpression="SET retirement_observed_at = :ts",
            ExpressionAttributeValues={":ts": 123},
        )
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        sm.create_secret(Name=name, SecretString="sk-or-v1-owner-secret")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("upstream called"))) as http:
            assert (
                await ensure_owner_key(
                    "owner-123",
                    http=http,
                    sm=sm,
                    environment="dev",
                    workspace_id="workspace-uuid",
                    limit_usd=2.5,
                    now_sec=1000,
                )
                == "key-hash"
            )
        row = table.get_item(Key={"owner_id": "owner-123"})["Item"]
        assert row["state"] == "active"
        assert "retirement_observed_at" not in row
