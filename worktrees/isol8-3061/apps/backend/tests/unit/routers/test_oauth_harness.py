"""Tests for /api/v1/oauth/chatgpt/harness/* — the OpenCode coding-harness grant.

The harness is a SECOND, dedicated OAuth chain on the owner's ChatGPT account,
stored under the ``{user_id}#harness`` row key. The provider row and harness
row must never read as each other: sharing one rotating chain between two
holders is the refresh_token_reused failure mode.
"""

import json
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws

from core.services.oauth_service import (
    DeviceCodeResponse,
    DevicePollPending,
    DevicePollResult,
)


@pytest.fixture(autouse=True)
def workspace_oauth_dependencies():
    """The shared client is an org admin; router tests do not need billing."""
    with (
        patch("routers.oauth.billing_repo.get_by_owner_id", new=AsyncMock(return_value=None)),
        patch("routers.oauth.resolve_token_owner", new=AsyncMock(return_value="user_test_123")),
    ):
        yield


@pytest.fixture
def oauth_table(monkeypatch):
    from core.config import settings as _settings

    with mock_aws():
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="test-oauth-tokens",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("OAUTH_TOKENS_TABLE", "test-oauth-tokens")
        monkeypatch.setattr(_settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        yield


def _seed_active_row(*, row_key: str, bound_owner_id: str, account_id: str = "acc_h") -> None:
    from core.encryption import get_fernet

    plain = json.dumps(
        {
            "access_token": "harness_access",
            "refresh_token": "harness_refresh",
            "account_id": account_id,
        }
    ).encode()
    boto3.resource("dynamodb", region_name="us-east-1").Table("test-oauth-tokens").put_item(
        Item={
            "user_id": row_key,
            "state": "active",
            "encrypted_tokens": get_fernet().encrypt(plain),
            "account_id": account_id,
            "bound_owner_id": bound_owner_id,
        }
    )


@pytest.mark.asyncio
async def test_harness_start_uses_harness_purpose(async_client):
    fake = DeviceCodeResponse(
        user_code="HARN-1234",
        verification_uri="https://auth.openai.com/codex/device",
        expires_in=900,
        interval=5,
    )
    with patch("routers.oauth.request_device_code", new=AsyncMock(return_value=fake)) as mock_start:
        resp = await async_client.post("/api/v1/oauth/chatgpt/harness/start")
    assert resp.status_code == 200
    assert resp.json()["user_code"] == "HARN-1234"
    assert mock_start.await_args.kwargs["purpose"] == "harness"
    assert mock_start.await_args.kwargs["bound_owner_id"] == "org_test_456"


@pytest.mark.asyncio
async def test_harness_poll_pending(async_client):
    with patch("routers.oauth.poll_device_code", new=AsyncMock(return_value=DevicePollPending)):
        resp = await async_client.post("/api/v1/oauth/chatgpt/harness/poll")
    assert resp.status_code == 200
    assert resp.json() == {"status": "pending"}


@pytest.mark.asyncio
async def test_harness_poll_completes_on_the_row_alone(async_client, oauth_table):
    """KNOWN DEAD DOOR (M6): the row IS the grant now, and nothing consumes it.

    The staged OpenCode ``auth.json`` that used to make completion real went with the runtime,
    and a Hermes profile advertises no coding harness at all (``build_profile_config`` writes no
    ACP block) -- so this was already inert for every migrated workspace before the write was
    deleted. The row lifecycle stays so the onboarding step the frontend still renders does not
    404 mid-flow.
    """
    _seed_active_row(row_key="user_test_123#harness", bound_owner_id="org_test_456")
    done = DevicePollResult(account_id="acc_h", bound_owner_id="org_test_456")
    with patch("routers.oauth.poll_device_code", new=AsyncMock(return_value=done)):
        resp = await async_client.post("/api/v1/oauth/chatgpt/harness/poll")
    assert resp.status_code == 200
    assert resp.json() == {"status": "completed", "account_id": "acc_h"}
    status = await async_client.get("/api/v1/oauth/chatgpt/harness/status")
    assert status.json() == {"connected": True, "account_id": "acc_h"}


@pytest.mark.asyncio
async def test_harness_and_provider_rows_are_independent(async_client, oauth_table):
    """A harness grant must NOT make the provider look connected (or vice
    versa) — the whole point of the second chain is that the two never share."""
    _seed_active_row(row_key="user_test_123#harness", bound_owner_id="org_test_456")

    harness = await async_client.get("/api/v1/oauth/chatgpt/harness/status")
    provider = await async_client.get("/api/v1/oauth/chatgpt/status")
    assert harness.json() == {"connected": True, "account_id": "acc_h"}
    assert provider.json()["connected"] is False


@pytest.mark.asyncio
async def test_harness_status_rejects_row_bound_elsewhere(async_client, oauth_table):
    _seed_active_row(row_key="user_test_123#harness", bound_owner_id="org_other_999")
    resp = await async_client.get("/api/v1/oauth/chatgpt/harness/status")
    assert resp.json()["connected"] is False


@pytest.mark.asyncio
async def test_harness_disconnect_skips_row_bound_to_another_workspace(async_client, oauth_table):
    """A human who connected an ORG's harness must not nuke that org-bound
    row from their personal workspace: the binding check answers
    not-connected for this workspace, the endpoint reports disconnected
    (truthful — this workspace has no grant), and the org's row survives."""
    _seed_active_row(row_key="user_test_123#harness", bound_owner_id="org_other_999")
    resp = await async_client.post("/api/v1/oauth/chatgpt/harness/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"status": "disconnected"}
    row = (
        boto3.resource("dynamodb", region_name="us-east-1")
        .Table("test-oauth-tokens")
        .get_item(Key={"user_id": "user_test_123#harness"})
    )
    assert row.get("Item", {}).get("state") == "active"  # org grant untouched


@pytest.mark.asyncio
async def test_harness_disconnect_deletes_the_row(async_client, oauth_table):
    """The row IS the grant (see the poll test above), so deleting it is the whole disconnect --
    there is no staged file left to unlink first."""
    _seed_active_row(row_key="user_test_123#harness", bound_owner_id="org_test_456")

    resp = await async_client.post("/api/v1/oauth/chatgpt/harness/disconnect")
    assert resp.status_code == 200
    assert resp.json() == {"status": "disconnected"}
    status = await async_client.get("/api/v1/oauth/chatgpt/harness/status")
    assert status.json()["connected"] is False


@pytest.mark.asyncio
async def test_two_pending_sessions_coexist(oauth_table):
    """A provider device-code session and a harness session can be pending at
    the same time — the row keys must not collide."""
    from core.services.oauth_service import PURPOSE_HARNESS, request_device_code

    fake_usercode = {"device_auth_id": "d1", "user_code": "CODE-1", "interval": 5}

    class _Resp:
        status_code = 200

        def json(self):
            return fake_usercode

    with patch("core.services.oauth_service.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=_Resp())
        await request_device_code(user_id="u1", bound_owner_id="u1")
        await request_device_code(user_id="u1", bound_owner_id="u1", purpose=PURPOSE_HARNESS)

    table = boto3.resource("dynamodb", region_name="us-east-1").Table("test-oauth-tokens")
    assert table.get_item(Key={"user_id": "u1"}).get("Item")["state"] == "pending"
    assert table.get_item(Key={"user_id": "u1#harness"}).get("Item")["state"] == "pending"
