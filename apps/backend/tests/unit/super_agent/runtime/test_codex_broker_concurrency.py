"""Spec 13 §8.2: the backend ChatGPT broker -- one refresh for N VMs, stale 401s, CAS, reconnect.

Drives the REAL ``owner_lifecycle_guard`` (containers table) and the real token row (oauth
table) on moto; OpenAI's token endpoint is a counted fake.
"""

import asyncio
import base64
import hashlib
import json
import logging
import time
from unittest.mock import AsyncMock, patch

import boto3
import httpx
import pytest
from cryptography.fernet import Fernet
from moto import mock_aws

from core.services import oauth_service, owner_lifecycle, token_custody
from core.services.credentials.access_token import ReconnectRequired

OWNER = "org_t"
USER = "user_t"


def _jwt(exp: int, tag: str) -> str:
    def enc(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")

    return f"{enc({'alg': 'none'})}.{enc({'exp': exp, 'tag': tag})}.sig"


def sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def world(monkeypatch):
    from core.config import settings

    with mock_aws():
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        for name, key in (("test-oauth-tokens", "user_id"), ("test-containers", "owner_id")):
            ddb.create_table(
                TableName=name,
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        monkeypatch.setenv("OAUTH_TOKENS_TABLE", "test-oauth-tokens")
        monkeypatch.setattr(settings, "ENCRYPTION_KEY", Fernet.generate_key().decode())
        monkeypatch.setattr(owner_lifecycle, "_POLL_INTERVAL_S", 0.01)
        monkeypatch.setattr(token_custody, "resolve_provider_choice", AsyncMock(return_value=("chatgpt_oauth", None)))
        monkeypatch.setattr(oauth_service, "resolve_token_owner", AsyncMock(return_value=USER))
        announce = AsyncMock()
        monkeypatch.setattr(token_custody, "_announce_reconnect", announce)
        posts: list[dict] = []
        state = {"responses": []}

        async def fake_post(self, url, *, data=None, headers=None, **kw):
            posts.append({"url": str(url), "data": data})
            await asyncio.sleep(0.05)  # long enough for every concurrent caller to queue on the lease
            status, body = state["responses"].pop(0) if state["responses"] else (200, _fresh_body("T2"))
            return httpx.Response(status, json=body, request=httpx.Request("POST", str(url)))

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        with patch("core.dynamodb._table_prefix", "test-"), patch("core.dynamodb._dynamodb_resource", ddb):
            yield {"ddb": ddb, "posts": posts, "state": state, "announce": announce}


def _fresh_body(tag: str, *, refresh: str | None = "RT2") -> dict:
    body = {"access_token": _jwt(int(time.time()) + 3600, tag), "id_token": "id2", "expires_in": 3600}
    if refresh:
        body["refresh_token"] = refresh
    return body


def _seed(world, *, exp_in: int, tag: str = "T1", custody: str | None = "backend", expires_at=True) -> str:
    access = _jwt(int(time.time()) + exp_in, tag)
    tokens = {"access_token": access, "refresh_token": "RT1", "id_token": "id1", "account_id": "acct-1"}
    item = {
        "user_id": USER,
        "state": "active",
        "bound_owner_id": OWNER,
        "account_id": "acct-1",
        "encrypted_tokens": oauth_service.get_fernet().encrypt(json.dumps(tokens).encode()),
    }
    if custody:
        item.update({"custody": custody, "version": 1})
        if expires_at:
            item["expires_at"] = (int(time.time()) + exp_in) * 1000
    world["ddb"].Table("test-oauth-tokens").put_item(Item=item)
    return access


def _row(world) -> dict:
    return world["ddb"].Table("test-oauth-tokens").get_item(Key={"user_id": USER})["Item"]


def _tokens(world) -> dict:
    return json.loads(oauth_service.get_fernet().decrypt(bytes(_row(world)["encrypted_tokens"])))


@pytest.mark.asyncio
async def test_two_concurrent_task_vms_refresh_once(world):
    t1 = _seed(world, exp_in=3600)
    a, b = await asyncio.gather(
        token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True),
        token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True),
    )
    assert len(world["posts"]) == 1
    assert a.access_token == b.access_token != t1
    assert int(_row(world)["version"]) == 2
    assert sorted([a.refreshed, b.refreshed]) == [False, True]
    assert world["posts"][0]["data"] == {
        "grant_type": "refresh_token",
        "refresh_token": "RT1",
        "client_id": oauth_service.CLIENT_ID,
    }  # the request Hermes's refresh_codex_oauth_pure sends


@pytest.mark.asyncio
async def test_ten_vends_near_expiry_one_refresh(world):
    _seed(world, exp_in=120)  # inside the 600 s window
    vends = await asyncio.gather(*(token_custody.codex_vend(OWNER) for _ in range(10)))
    assert len(world["posts"]) == 1
    assert len({v.access_token for v in vends}) == 1


@pytest.mark.asyncio
async def test_stale_token_401_returns_current_without_refresh(world):
    _seed(world, exp_in=3600, tag="T2")
    vend = await token_custody.codex_vend(OWNER, presented_sha256=sha("an-older-token"), force=True)
    assert world["posts"] == [] and vend.refreshed is False
    assert vend.access_token == _tokens(world)["access_token"]


@pytest.mark.asyncio
async def test_current_token_401_forces_refresh(world):
    t1 = _seed(world, exp_in=3600)
    vend = await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    assert len(world["posts"]) == 1 and vend.refreshed is True


@pytest.mark.asyncio
async def test_fast_path_takes_no_lease(world, monkeypatch):
    _seed(world, exp_in=3600)
    claim = AsyncMock(side_effect=AssertionError("lease taken on the fast path"))
    monkeypatch.setattr(owner_lifecycle.container_repo, "try_claim_owner_lifecycle", claim)
    vend = await token_custody.codex_vend(OWNER)
    assert vend.refreshed is False and world["posts"] == []
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_cas_lost_after_revoke(world, monkeypatch):
    t1 = _seed(world, exp_in=3600)
    real_post = httpx.AsyncClient.post

    async def revoke_then_answer(self, url, **kw):
        await oauth_service.revoke_user_oauth(user_id=USER)  # disconnect lands mid-refresh
        return await real_post(self, url, **kw)

    monkeypatch.setattr(httpx.AsyncClient, "post", revoke_then_answer)
    with pytest.raises(RuntimeError, match="conditional write lost"):
        await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    assert "Item" not in world["ddb"].Table("test-oauth-tokens").get_item(Key={"user_id": USER})


@pytest.mark.asyncio
async def test_refresh_token_reused_marks_reconnect(world):
    t1 = _seed(world, exp_in=3600)
    world["state"]["responses"] = [(400, {"error": {"code": "refresh_token_reused", "message": "used"}})]
    with pytest.raises(ReconnectRequired):
        await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    assert _row(world)["state"] == "reconnect_required"
    world["announce"].assert_awaited_once_with(OWNER, "openai-codex", reconcile=False)
    with pytest.raises(ReconnectRequired):  # later vends say "reconnect", never "not brokered"
        await token_custody.codex_vend(OWNER)
    assert len(world["posts"]) == 1


@pytest.mark.asyncio
async def test_transient_refresh_failure_keeps_the_grant(world):
    t1 = _seed(world, exp_in=3600)
    world["state"]["responses"] = [(429, {"error": "rate_limited"})]
    with pytest.raises(token_custody.RefreshUnavailable):
        await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    assert _row(world)["state"] == "active" and int(_row(world)["version"]) == 1


@pytest.mark.asyncio
async def test_refresh_without_refresh_token_keeps_ours(world):
    """Lead (5 Nov): merge onto the current tokens -- a response missing refresh_token /
    account_id must not erase them."""
    t1 = _seed(world, exp_in=3600)
    world["state"]["responses"] = [(200, _fresh_body("T2", refresh=None))]
    await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    tokens = _tokens(world)
    assert tokens["refresh_token"] == "RT1" and tokens["account_id"] == "acct-1"
    assert tokens["access_token"] != t1


@pytest.mark.asyncio
async def test_unknown_expiry_does_not_refresh_every_call(world):
    """Lead (5 Nov): an unreadable expiry must not mean "refresh every call"."""
    tokens = {"access_token": "opaque-not-a-jwt", "refresh_token": "RT1"}
    world["ddb"].Table("test-oauth-tokens").put_item(
        Item={
            "user_id": USER,
            "state": "active",
            "bound_owner_id": OWNER,
            "custody": "backend",
            "version": 1,
            "encrypted_tokens": oauth_service.get_fernet().encrypt(json.dumps(tokens).encode()),
        }
    )
    for _ in range(3):
        assert (await token_custody.codex_vend(OWNER)).access_token == "opaque-not-a-jwt"
    assert world["posts"] == []


@pytest.mark.asyncio
async def test_actor_row_is_not_brokered(world):
    _seed(world, exp_in=60, custody=None)
    assert await token_custody.codex_vend(OWNER) is None
    assert world["posts"] == []


@pytest.mark.asyncio
async def test_lease_busy_raises_busy_without_refresh(world, monkeypatch):
    t1 = _seed(world, exp_in=3600)
    monkeypatch.setattr(owner_lifecycle, "OWNER_LIFECYCLE_TIMEOUT_S", 0.05)
    monkeypatch.setattr(owner_lifecycle.container_repo, "try_claim_owner_lifecycle", AsyncMock(return_value=None))
    with pytest.raises(owner_lifecycle.OwnerLifecycleBusyError):
        await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    assert world["posts"] == []


@pytest.mark.asyncio
async def test_no_token_value_in_logs(world, caplog):
    t1 = _seed(world, exp_in=3600)
    world["state"]["responses"] = [(400, {"error": "invalid_grant"})]
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ReconnectRequired):
            await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    for secret in (t1, "RT1", "id1"):
        assert secret not in caplog.text


@pytest.mark.asyncio
async def test_force_without_hash_never_refreshes(world):
    """Review of #4723: ``force`` names the token it saw stale; without the hash it is no force
    (a bridge bug must not be able to rotate the family every call)."""
    t1 = _seed(world, exp_in=3600)
    vend = await token_custody.codex_vend(OWNER, force=True)
    assert world["posts"] == [] and vend.refreshed is False and vend.access_token == t1


@pytest.mark.asyncio
async def test_non_json_200_is_transient(world, monkeypatch):
    t1 = _seed(world, exp_in=3600)

    async def html_200(self, url, **kw):
        return httpx.Response(200, text="<html>upstream</html>", request=httpx.Request("POST", str(url)))

    monkeypatch.setattr(httpx.AsyncClient, "post", html_200)
    with pytest.raises(token_custody.RefreshUnavailable, match="bad_json"):
        await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    assert _row(world)["state"] == "active" and int(_row(world)["version"]) == 1


@pytest.mark.asyncio
async def test_error_text_is_truncated(world, caplog):
    t1 = _seed(world, exp_in=3600)
    world["state"]["responses"] = [(401, {"error": "x" * 5000})]
    with caplog.at_level(logging.WARNING):
        with pytest.raises(ReconnectRequired) as info:
            await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    assert len(str(info.value.__cause__)) == 200
    assert "x" * 201 not in caplog.text


@pytest.mark.asyncio
async def test_peer_refusal_during_lease_wait_says_reconnect(world, monkeypatch):
    """The leased re-read finds no active row because a peer's refresh was just refused: answer
    reconnect at once (the route's 410), not "not brokered"."""
    t1 = _seed(world, exp_in=3600)
    real_get = oauth_service.get_token_row
    calls: list[int] = []

    async def get(user, **kw):
        calls.append(1)
        if len(calls) == 2:  # the leased re-read: a peer's refresh was refused while we waited
            await oauth_service.mark_reconnect_required(USER, expected_version=1)
            return None
        return await real_get(user, **kw)

    monkeypatch.setattr(oauth_service, "get_token_row", get)
    with pytest.raises(ReconnectRequired):
        await token_custody.codex_vend(OWNER, presented_sha256=sha(t1), force=True)
    assert world["posts"] == [] and len(calls) == 2
