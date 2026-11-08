import time

import httpx
import pytest
import boto3
from moto import mock_aws

from core.repositories import openrouter_key_repo
from core.services.credentials import integration_secret_store
from core.services.openrouter_keys import (
    KeyClaimConflict,
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
async def test_ensure_owner_key_persists_plaintext_only_in_the_credential_store(monkeypatch):
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
        # The owner key is an integration_secret_store credential (an envelope in every deployed
        # stage), never an openrouter/owners/* secret.
        assert await integration_secret_store.get("owner-123", "openrouter/key-hash") == {
            "key": "sk-or-v1-owner-secret"
        }
        assert not sm.list_secrets(Filters=[{"Key": "name", "Values": ["lightbulb/dev/openrouter/owners/"]}])[
            "SecretList"
        ]
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

        async def failed_put(*_):
            raise OSError("secret write failed")

        monkeypatch.setattr(integration_secret_store, "put", failed_put)
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
async def test_concurrent_claim_loses_race_and_raises_claim_conflict(monkeypatch):
    """Two attempts race for the same owner's claim; the winner's row is already
    pending when the loser retries its own claim, too recently to be stale
    (ttl 300s). The loser must raise the specific transient-classified
    KeyClaimConflict, not a bare RuntimeError, and never call OpenRouter."""
    with mock_aws():
        table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
            TableName="lightbulb-dev-openrouter-keys",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
        assert await openrouter_key_repo.claim_new("owner-123", "winner-claim", now_sec=1000)
        sm = boto3.client("secretsmanager", region_name="us-east-1")
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: pytest.fail("upstream called"))) as http:
            with pytest.raises(KeyClaimConflict, match="already in progress"):
                await ensure_owner_key(
                    "owner-123",
                    http=http,
                    sm=sm,
                    environment="dev",
                    workspace_id="workspace-uuid",
                    limit_usd=2.5,
                    now_sec=1000,
                )


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


@pytest.mark.parametrize(
    "account,expected",
    [
        ({"subscription_status": "trial_local"}, 0.25),
        ({"subscription_status": "active", "plan": "base"}, 0.25),
        ({"subscription_status": "active", "plan": "standard"}, 0.25),
        # An explicit managed paid owner is Metronome-valved: never cut to Base's cap.
        ({"subscription_status": "active", "plan": "standard", "provider_choice": "openrouter_managed"}, 50.0),
        ({"subscription_status": "canceled", "plan": "base"}, 50.0),
        (None, 50.0),
    ],
)
def test_limit_for_account_is_per_plan(account, expected):
    from core.services.openrouter_keys import limit_for_account

    assert limit_for_account(account) == expected


@pytest.mark.asyncio
async def test_settings_wrapper_mints_with_the_owner_plan_limit(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("core.services.openrouter_keys.settings.OPENROUTER_WORKSPACE_ID", "workspace-uuid")
    monkeypatch.setattr(
        "core.services.openrouter_keys.billing_repo.get_by_owner_id",
        AsyncMock(return_value={"subscription_status": "trial_local"}),
    )
    ensure = AsyncMock(return_value="key-hash")
    monkeypatch.setattr("core.services.openrouter_keys.ensure_owner_key", ensure)
    assert await ensure_owner_key_for_settings("owner-123") == "key-hash"
    assert ensure.await_args.kwargs["limit_usd"] == 0.25


def _relimit_fixture(monkeypatch, *, account, current_limit):
    from unittest.mock import AsyncMock

    table = boto3.resource("dynamodb", region_name="us-east-1").create_table(
        TableName="lightbulb-dev-openrouter-keys",
        KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
        AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setattr(openrouter_key_repo, "get_table", lambda _: table)
    table.put_item(Item={"owner_id": "owner-123", "state": "active", "key_hash": "key-hash", "secret_name": "s"})
    sm = boto3.client("secretsmanager", region_name="us-east-1")
    sm.create_secret(Name="lightbulb/dev/openrouter/management", SecretString="management-secret")
    monkeypatch.setattr("core.services.openrouter_keys.settings.ENVIRONMENT", "dev")
    monkeypatch.setattr("core.services.openrouter_keys.billing_repo.get_by_owner_id", AsyncMock(return_value=account))
    clear = AsyncMock()
    monkeypatch.setattr("core.services.openrouter_keys.billing_repo.clear_openrouter_capped_day", clear)
    requests = []

    def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url == "https://openrouter.ai/api/v1/keys/key-hash"
        assert request.headers["Authorization"] == "Bearer management-secret"
        if request.method == "GET":
            return httpx.Response(200, json={"data": {"hash": "key-hash", "limit": current_limit}})
        return httpx.Response(200, json={"data": {"hash": "key-hash", "limit": 0.25, "limit_reset": "daily"}})

    return sm, requests, clear, respond


@pytest.mark.asyncio
async def test_relimit_patches_a_changed_limit_and_clears_the_capped_day(monkeypatch):
    from core.services.openrouter_keys import relimit_owner_key

    with mock_aws():
        sm, requests, clear, respond = _relimit_fixture(
            monkeypatch, account={"subscription_status": "active", "plan": "base"}, current_limit=0.01
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            assert await relimit_owner_key("owner-123", http=http, sm=sm) is True
    patch = [r for r in requests if r.method == "PATCH"]
    assert len(patch) == 1
    assert patch[0].read().decode() == '{"limit":0.25,"limit_reset":"daily"}'
    clear.assert_awaited_once_with("owner-123")


@pytest.mark.asyncio
async def test_relimit_is_a_no_op_when_the_limit_already_matches(monkeypatch):
    from core.services.openrouter_keys import relimit_owner_key

    with mock_aws():
        sm, requests, clear, respond = _relimit_fixture(
            monkeypatch, account={"subscription_status": "active", "plan": "base"}, current_limit=0.25
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            assert await relimit_owner_key("owner-123", http=http, sm=sm) is False
    assert [r.method for r in requests] == ["GET"]
    clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_every_provision_moves_an_existing_key_onto_its_plan_limit(monkeypatch):
    """A key minted before per-plan limits keeps its old limit until something re-limits it;
    the provision path is that something, and a failed re-limit never fails the provision."""
    from unittest.mock import AsyncMock

    from core.services import openrouter_keys

    monkeypatch.setattr(openrouter_keys.settings, "OPENROUTER_WORKSPACE_ID", "ws-1")
    monkeypatch.setattr(
        openrouter_keys.billing_repo, "get_by_owner_id", AsyncMock(return_value={"subscription_status": "trial_local"})
    )
    monkeypatch.setattr(openrouter_keys, "ensure_owner_key", AsyncMock(return_value="hash-1"))
    monkeypatch.setattr(openrouter_keys.boto3, "client", lambda *a, **k: object())
    relimit = AsyncMock(return_value=True)
    monkeypatch.setattr(openrouter_keys, "relimit_owner_key", relimit)
    assert await openrouter_keys.ensure_owner_key_for_settings("org_a") == "hash-1"
    relimit.assert_awaited_once()

    relimit.side_effect = RuntimeError("openrouter down")
    assert await openrouter_keys.ensure_owner_key_for_settings("org_a") == "hash-1"


@pytest.mark.asyncio
async def test_check_account_balance_emits_credits_minus_usage():
    from unittest.mock import MagicMock, patch

    from core.services.openrouter_keys import check_account_balance

    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": "management-secret"}

    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://openrouter.ai/api/v1/credits"
        assert request.headers["Authorization"] == "Bearer management-secret"
        return httpx.Response(200, json={"data": {"total_credits": 100.5, "total_usage": 25.75}})

    with patch("core.observability.metrics.gauge") as mock_gauge:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            await check_account_balance(http=http, sm=sm)

    mock_gauge.assert_called_once_with("openrouter.account.balance_usd", 74.75)


@pytest.mark.asyncio
async def test_check_account_balance_emits_nothing_on_error():
    from unittest.mock import MagicMock, patch

    from core.services.openrouter_keys import check_account_balance

    sm = MagicMock()
    sm.get_secret_value.return_value = {"SecretString": "management-secret"}

    def respond(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "upstream down"})

    with patch("core.observability.metrics.gauge") as mock_gauge:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            # Must not raise -- a flaky OpenRouter call can never crash the caller's loop.
            await check_account_balance(http=http, sm=sm)

    mock_gauge.assert_not_called()


@pytest.mark.asyncio
async def test_owner_allowance_reads_openrouters_daily_usage_and_caches_it(monkeypatch):
    from core.services import openrouter_keys

    monkeypatch.setattr(openrouter_keys, "_allowance_cache", {})
    with mock_aws():
        sm, requests, _, _ = _relimit_fixture(monkeypatch, account=None, current_limit=0.25)

        def respond(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            assert request.url == "https://openrouter.ai/api/v1/keys/key-hash"
            return httpx.Response(200, json={"data": {"limit": 0.25, "usage_daily": 0.155, "limit_remaining": 0.095}})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            first = await openrouter_keys.owner_allowance("owner-123", http=http, sm=sm)
            second = await openrouter_keys.owner_allowance("owner-123", http=http, sm=sm)
    assert first["used_usd"] == 0.155 and first["limit_usd"] == 0.25
    assert first["resets_at"] % 86400 == 0 and 0 < first["resets_at"] - time.time() <= 86400
    assert second == first
    assert len(requests) == 1  # the second read is served from the 60 s cache


@pytest.mark.asyncio
async def test_owner_allowance_fails_soft(monkeypatch):
    from core.services import openrouter_keys

    monkeypatch.setattr(openrouter_keys, "_allowance_cache", {})
    with mock_aws():
        sm, _, _, _ = _relimit_fixture(monkeypatch, account=None, current_limit=0.25)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as http:
            assert await openrouter_keys.owner_allowance("owner-123", http=http, sm=sm) is None
