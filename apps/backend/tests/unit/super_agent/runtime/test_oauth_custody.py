"""Spec 13 T1: custody, version and expiry on the ChatGPT token row; conditional token update."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, patch

import boto3
import httpx
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws

from core.services import oauth_service

TABLE = "test-oauth-tokens"
EXP = 2_000_000_000  # JWT exp, seconds


def _jwt(exp: int) -> str:
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc({'exp': exp})}.sig"


@pytest.fixture
def oauth_table(monkeypatch):
    from core.config import settings

    with mock_aws():
        boto3.client("dynamodb", region_name="us-east-1").create_table(
            TableName=TABLE,
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("OAUTH_TOKENS_TABLE", TABLE)
        monkeypatch.setattr(settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        yield boto3.resource("dynamodb", region_name="us-east-1").Table(TABLE)


def _ok(body: dict, url: str) -> httpx.Response:
    return httpx.Response(200, json=body, request=httpx.Request("POST", url))


async def _connect(switch_on: bool, user="u_1", owner="org_1"):
    async def seed(self, url, **kw):
        return _ok({"device_auth_id": "d", "user_code": "C-1", "interval": 5}, str(url))

    async def poll(self, url, **kw):
        if "deviceauth/token" in str(url):
            return _ok({"authorization_code": "ac", "code_verifier": "cv"}, str(url))
        return _ok({"access_token": _jwt(EXP), "refresh_token": "rt1", "id_token": "id1", "expires_in": 3600}, str(url))

    with patch.object(httpx.AsyncClient, "post", new=seed):
        await oauth_service.request_device_code(user_id=user, bound_owner_id=owner)
    with (
        patch.object(httpx.AsyncClient, "post", new=poll),
        patch("core.services.oauth_service.flags_for", AsyncMock(return_value=switch_on)) as flags,
    ):
        await oauth_service.poll_device_code(user_id=user)
    return flags


@pytest.mark.asyncio
async def test_new_connect_with_switch_on_is_backend(oauth_table):
    flags = await _connect(True)
    flags.assert_awaited_once_with("org_1")
    row = oauth_table.get_item(Key={"user_id": "u_1"})["Item"]
    assert row["custody"] == "backend"
    assert row["version"] == 1
    assert row["expires_at"] == EXP * 1000


@pytest.mark.asyncio
async def test_new_connect_switch_off_unchanged(oauth_table):
    await _connect(False)
    row = oauth_table.get_item(Key={"user_id": "u_1"})["Item"]
    assert not {"custody", "version", "expires_at", "last_refresh"} & row.keys()


@pytest.mark.asyncio
async def test_get_token_row_consistent_read(oauth_table):
    await _connect(True)
    real = oauth_service._table
    seen = []

    class _Spy:
        def __init__(self, t):
            self._t = t

        def get_item(self, **kw):
            seen.append(kw.get("ConsistentRead"))
            return self._t.get_item(**kw)

    with patch.object(oauth_service, "_table", lambda: _Spy(real())):
        row = await oauth_service.get_token_row("u_1")
        assert await oauth_service.get_token_row("u_1", bound_to="org_other") is None
    assert seen == [True, True]
    assert row["custody"] == "backend" and row["version"] == 1
    assert row["tokens"]["refresh_token"] == "rt1"
    assert "encrypted_tokens" not in row
    assert await oauth_service.get_token_row("nobody") is None


@pytest.mark.asyncio
async def test_cas_update_wins_once(oauth_table):
    await _connect(True)
    new = {"access_token": _jwt(EXP + 3600), "refresh_token": "rt2", "id_token": "id2", "account_id": None}
    results = await asyncio.gather(
        oauth_service.cas_update_tokens("u_1", expected_version=1, tokens=new),
        oauth_service.cas_update_tokens("u_1", expected_version=1, tokens={**new, "refresh_token": "rt3"}),
    )
    assert sorted(results) == [False, True]
    row = await oauth_service.get_token_row("u_1")
    assert row["version"] == 2
    assert row["expires_at"] == (EXP + 3600) * 1000
    assert row["last_refresh"]
    assert row["tokens"]["refresh_token"] in ("rt2", "rt3")
    assert row["tokens"]["expires_at"] == (EXP + 3600) * 1000


@pytest.mark.asyncio
async def test_cas_refuses_actor_row(oauth_table):
    await _connect(False)
    assert not await oauth_service.cas_update_tokens(
        "u_1", expected_version=0, tokens={"access_token": _jwt(EXP), "refresh_token": "rt2"}
    )
    # A revoked (deleted) row is never resurrected.
    await oauth_service.revoke_user_oauth(user_id="u_1")
    assert not await oauth_service.cas_update_tokens(
        "u_1", expected_version=1, tokens={"access_token": _jwt(EXP), "refresh_token": "rt2"}
    )
    assert "Item" not in oauth_table.get_item(Key={"user_id": "u_1"})
