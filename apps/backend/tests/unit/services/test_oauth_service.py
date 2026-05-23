"""Unit tests for the ChatGPT device-code OAuth orchestration.

Covers OpenAI's actual Codex CLI device-code flow:
  POST /api/accounts/deviceauth/usercode  →  device_auth_id + user_code
  POST /api/accounts/deviceauth/token     →  authorization_code + code_verifier
  POST /oauth/token                       →  access_token + refresh_token
"""

from unittest.mock import patch

import boto3
import httpx
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws

from core.services.oauth_service import (
    DevicePollPending,
    DevicePollResult,
    OAuthAlreadyActiveError,
    OAuthExchangeFailedError,
    poll_device_code,
    request_device_code,
    revoke_user_oauth,
    VERIFICATION_URL,
)


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
        # core.encryption.get_fernet reads settings.ENCRYPTION_KEY (cached at
        # import time) — patching the env var alone is insufficient.
        monkeypatch.setattr(_settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        yield


def _ok(json_body: dict, url: str = "https://example.test/x") -> httpx.Response:
    return httpx.Response(200, json=json_body, request=httpx.Request("POST", url))


@pytest.mark.asyncio
async def test_request_device_code_returns_user_facing_fields(oauth_table):
    """Backend POST to /api/accounts/deviceauth/usercode returns the
    user-code that will go on screen + the verification URL we display."""

    async def fake_post(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_abc",
                "user_code": "ABCD-1234",
                "interval": 5,
            },
            url=str(url),
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_post):
        result = await request_device_code(user_id="u_1")

    assert result.user_code == "ABCD-1234"
    assert result.verification_uri == VERIFICATION_URL
    assert result.interval == 5
    # Server-side device_auth_id is NOT returned to the caller — kept in DDB.
    assert not hasattr(result, "device_auth_id")


@pytest.mark.asyncio
async def test_poll_pending_returns_pending(oauth_table):
    """While the user hasn't entered the code, OpenAI returns 403/404 —
    we translate to DevicePollPending."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_pending",
                "user_code": "WAIT-0001",
                "interval": 5,
            },
            url=str(url),
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1")

    async def fake_poll(self, url, **kwargs):
        return httpx.Response(403, request=httpx.Request("POST", str(url)))

    with patch.object(httpx.AsyncClient, "post", new=fake_poll):
        result = await poll_device_code(user_id="u_1")
    assert result is DevicePollPending


@pytest.mark.asyncio
async def test_poll_success_persists_encrypted_tokens(oauth_table):
    """Successful poll: deviceauth/token returns authorization_code +
    code_verifier; we exchange those at /oauth/token for the real
    access+refresh tokens, which we Fernet-encrypt into DDB."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_ok",
                "user_code": "OKAY-9999",
                "interval": 5,
            },
            url=str(url),
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1")

    # Two-stage success:
    #   1st call: deviceauth/token → returns authorization_code + verifier
    #   2nd call: /oauth/token      → returns access + refresh tokens
    call_count = {"n": 0}

    async def fake_poll_then_exchange(self, url, **kwargs):
        call_count["n"] += 1
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok(
                {
                    "authorization_code": "auth_code_xyz",
                    "code_verifier": "verifier_xyz",
                    "code_challenge": "challenge_xyz",
                },
                url=url_str,
            )
        if "/oauth/token" in url_str:
            return _ok(
                {
                    "access_token": "eyJ.fake-jwt.access",
                    "refresh_token": "rt_opaque_1",
                    "id_token": "eyJ.id-token.x",
                    "account_id": "chatgpt-account-1",
                },
                url=url_str,
            )
        raise AssertionError(f"unexpected URL: {url_str}")

    with patch.object(httpx.AsyncClient, "post", new=fake_poll_then_exchange):
        result = await poll_device_code(user_id="u_1")

    assert isinstance(result, DevicePollResult)
    assert result.account_id == "chatgpt-account-1"
    assert call_count["n"] == 2  # poll + exchange

    # Tokens are stored encrypted, so the raw DDB row should NOT contain them
    # in plaintext.
    client = boto3.client("dynamodb", region_name="us-east-1")
    raw_item = client.get_item(TableName="test-oauth-tokens", Key={"user_id": {"S": "u_1"}})["Item"]
    raw_payload_b = raw_item["encrypted_tokens"]["B"]
    assert b"eyJ.fake-jwt.access" not in raw_payload_b
    assert b"rt_opaque_1" not in raw_payload_b


@pytest.mark.asyncio
async def test_poll_propagates_exchange_failure(oauth_table):
    """If /oauth/token rejects the exchange, we surface a clean error."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_fail",
                "user_code": "FAIL-0001",
                "interval": 5,
            },
            url=str(url),
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1")

    async def fake_poll_exchange_400(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok(
                {
                    "authorization_code": "ac",
                    "code_verifier": "v",
                },
                url=url_str,
            )
        return httpx.Response(400, json={"error": "invalid_grant"}, request=httpx.Request("POST", url_str))

    with patch.object(httpx.AsyncClient, "post", new=fake_poll_exchange_400):
        with pytest.raises(OAuthExchangeFailedError):
            await poll_device_code(user_id="u_1")


@pytest.mark.asyncio
async def test_poll_legacy_row_raises_clean_error(oauth_table):
    """A pending row from the pre-Codex-CLI flow has `device_code` in
    place of `device_auth_id`. We must NOT KeyError on it — instead
    raise OAuthExchangeFailedError so the router returns 502 and the
    user can click Try Again to restart."""
    # Manually write a legacy-shape pending row (no device_auth_id).
    client = boto3.client("dynamodb", region_name="us-east-1")
    client.put_item(
        TableName="test-oauth-tokens",
        Item={
            "user_id": {"S": "u_legacy"},
            "state": {"S": "pending"},
            "device_code": {"S": "old-style-code"},
            "user_code": {"S": "LEGACY-1"},
            "interval": {"N": "5"},
        },
    )
    with pytest.raises(OAuthExchangeFailedError, match="Stale OAuth session"):
        await poll_device_code(user_id="u_legacy")


@pytest.mark.asyncio
async def test_request_device_code_refuses_to_clobber_active_session(oauth_table):
    """Once a user has active tokens, request_device_code refuses to
    overwrite them — caller must revoke first."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_x",
                "user_code": "OK-9",
                "interval": 5,
            },
            url=str(url),
        )

    async def fake_complete(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok({"authorization_code": "ac", "code_verifier": "v"}, url=url_str)
        return _ok(
            {
                "access_token": "eyJ.x.y",
                "refresh_token": "rt",
                "id_token": "eyJ.z",
                "account_id": "acc",
            },
            url=url_str,
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1")
    with patch.object(httpx.AsyncClient, "post", new=fake_complete):
        await poll_device_code(user_id="u_1")

    # User now has state=active. A second request_device_code must refuse.
    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        with pytest.raises(OAuthAlreadyActiveError):
            await request_device_code(user_id="u_1")


@pytest.mark.asyncio
async def test_revoke_deletes_oauth_row(oauth_table):
    """revoke_user_oauth removes the persisted token row."""

    async def fake_seed(self, url, **kwargs):
        return _ok(
            {
                "device_auth_id": "dev_rev",
                "user_code": "BYE-0001",
                "interval": 5,
            },
            url=str(url),
        )

    async def fake_complete(self, url, **kwargs):
        url_str = str(url)
        if "deviceauth/token" in url_str:
            return _ok({"authorization_code": "ac", "code_verifier": "v"}, url=url_str)
        return _ok(
            {
                "access_token": "eyJ.x.y",
                "refresh_token": "rt",
                "id_token": "eyJ.z",
                "account_id": "acc",
            },
            url=url_str,
        )

    with patch.object(httpx.AsyncClient, "post", new=fake_seed):
        await request_device_code(user_id="u_1")
    with patch.object(httpx.AsyncClient, "post", new=fake_complete):
        await poll_device_code(user_id="u_1")

    await revoke_user_oauth(user_id="u_1")

    client = boto3.client("dynamodb", region_name="us-east-1")
    resp = client.get_item(TableName="test-oauth-tokens", Key={"user_id": {"S": "u_1"}})
    assert "Item" not in resp
