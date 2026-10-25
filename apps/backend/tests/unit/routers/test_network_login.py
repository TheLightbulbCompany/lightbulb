"""`lightbulb login` / `logout` / `invite --agents`: the computer signs a one-time challenge, the
signed-in person confirms in the browser, and the backend binds the computer's key to them (or,
for an admin, sends the workspace to join that computer's network)."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import base64
import logging
import time
from unittest.mock import AsyncMock, patch

import boto3
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.repositories import network_keys_repo
from core.services import clerk_admin, device_login, network_login, network_member
from tests.unit.repositories.test_messaging_repo import table  # noqa: F401

ORG = "org_a"
TICKET = "eyJ0aWNrZXQiOiJzZWNyZXQifQ"


class Computer:
    """What the engine is: an Ed25519 member key, spelled lowercase base32 without padding."""

    def __init__(self):
        self.private = Ed25519PrivateKey.generate()
        raw = self.private.public_key().public_bytes_raw()
        self.key = base64.b32encode(raw).decode().lower().rstrip("=")

    def signed(self, client, purpose, ticket=None):
        nonce = client.post("/api/v1/network/device/challenge").json()["challenge"]
        sig = self.private.sign(network_login.message(self.key, purpose, nonce, ticket))
        sig = base64.urlsafe_b64encode(sig).decode().rstrip("=")
        return {"key": self.key, "challenge": nonce, "sig": sig}


@pytest.fixture
def env(table, monkeypatch):
    from routers import network

    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName="test-network-keys",
        KeySchema=[{"AttributeName": "key", "KeyType": "HASH"}],
        AttributeDefinitions=[
            {"AttributeName": "key", "AttributeType": "S"},
            {"AttributeName": "user_id", "AttributeType": "S"},
        ],
        GlobalSecondaryIndexes=[
            {
                "IndexName": "by-user",
                "KeySchema": [{"AttributeName": "user_id", "KeyType": "HASH"}],
                "Projection": {"ProjectionType": "ALL"},
            }
        ],
        BillingMode="PAY_PER_REQUEST",
    )
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dev.example")
    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"name": "Acme"}))
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Ada Lovelace"))
    published = AsyncMock()
    monkeypatch.setattr(network_member, "republish", published)
    monkeypatch.setattr(
        network_member, "workspaces_of", AsyncMock(side_effect=lambda *u: sorted(f"org_{x}" for x in u if x))
    )
    join = AsyncMock(return_value={"state": "joining"})
    monkeypatch.setattr(network_member, "join_network", join)

    app = FastAPI()
    app.include_router(network.router, prefix="/api/v1/network")
    who = {"ctx": AuthContext(user_id="user_ada", org_id=ORG, org_role="org:admin")}

    async def _override():
        return who["ctx"]

    app.dependency_overrides[get_current_user] = _override
    with patch("core.dynamodb._table_prefix", "test-"), TestClient(app) as client:
        yield client, who, published, join


def _start(client, computer, purpose="login", ticket=None):
    body = {**computer.signed(client, purpose, ticket), "purpose": purpose, "computer": "Ada's Mac"}
    if ticket is not None:
        body["ticket"] = ticket
    return client.post("/api/v1/network/device/start", json=body)


def _confirm(client, user_code):
    return client.post("/api/v1/network/device/approve", json={"userCode": user_code})


def _poll(client, code):
    return client.post("/api/v1/network/device/poll", json={"code": code}).json()


def test_the_signed_message_is_the_engines_canonical_json():
    # The engine's `login::proof` test pins the same bytes from the Rust side.
    key = "a" * 52
    assert network_login.message(key, "agents", "n0nce", "t1") == (
        f'{{"challenge":"n0nce","key":"{key}","purpose":"lightbulb-agents","ticket":"t1"}}'.encode()
    )
    assert network_login.message(key, "login", "n0nce") == (
        f'{{"challenge":"n0nce","key":"{key}","purpose":"lightbulb-login"}}'.encode()
    )


def test_login_binds_the_computer_to_the_confirming_person_and_polls_once(env):
    client, _, published, _ = env
    mac = Computer()
    started = _start(client, mac)
    assert started.status_code == 200, started.text
    s = started.json()
    assert s["verifyUrl"] == "https://dev.example/auth/network"  # the person types the code
    assert _poll(client, s["code"]) == {"status": "pending"}

    seen = client.post("/api/v1/network/device/describe", json={"userCode": s["userCode"]})
    seen = seen.json()
    assert (seen["purpose"], seen["computer"], seen["ip"]) == ("login", "Ada's Mac", "testclient")
    assert abs(seen["startedAt"] - time.time()) < 60
    assert _confirm(client, s["userCode"]).status_code == 200

    import asyncio

    row = asyncio.run(network_keys_repo.get(mac.key))
    assert (row["user_id"], row["computer"]) == ("user_ada", "Ada's Mac")
    published.assert_awaited_once_with(["org_user_ada"], extra=(mac.key,))
    assert _poll(client, s["code"]) == {
        "status": "approved",
        "name": "Ada Lovelace",
        "routeUrl": settings.GATEWAY_RELAY_URL.removesuffix("/ws") + "/network",
    }
    assert _poll(client, s["code"]) == {"status": "expired"}  # single use


def test_a_challenge_is_single_use_and_needs_this_keys_signature(env):
    client, *_ = env
    mac, other = Computer(), Computer()
    body = {**mac.signed(client, "login"), "purpose": "login"}
    assert client.post("/api/v1/network/device/start", json=body).status_code == 200
    replay = client.post("/api/v1/network/device/start", json=body)
    assert replay.status_code == 401

    # Another key's signature (or another purpose's) spends nothing: the nonce still works once.
    fresh = mac.signed(client, "login")
    forged = {**fresh, "key": other.key, "purpose": "login"}
    assert client.post("/api/v1/network/device/start", json=forged).status_code == 401
    wrong = {**fresh, "purpose": "agents", "ticket": TICKET}
    assert client.post("/api/v1/network/device/start", json=wrong).status_code == 401
    assert client.post("/api/v1/network/device/start", json={**fresh, "purpose": "login"}).status_code == 200


def test_an_expired_challenge_is_refused(env, table):
    client, *_ = env
    mac = Computer()
    body = {**mac.signed(client, "login"), "purpose": "login"}
    table.update_item(
        Key=network_login._nonce_key(body["challenge"]),
        UpdateExpression="SET expiresAt = :past",
        ExpressionAttributeValues={":past": int(time.time()) - 1},
    )
    assert client.post("/api/v1/network/device/start", json=body).status_code == 401


def test_invite_agents_needs_a_signed_in_computer_of_this_admin(env):
    client, who, _, join = env
    mac = Computer()
    refused = _start(client, mac, "agents", TICKET)
    assert refused.status_code == 409 and "lightbulb login" in refused.json()["detail"]

    s = _start(client, mac).json()
    _confirm(client, s["userCode"])
    started = _start(client, mac, "agents", TICKET).json()
    seen = client.post("/api/v1/network/device/describe", json={"userCode": started["userCode"]}).json()
    assert (seen["purpose"], seen["computer"]) == ("agents", "Ada's Mac")

    # A member is not an admin; an admin whose computer this is not, neither is it theirs to use.
    who["ctx"] = AuthContext(user_id="user_ada", org_id=ORG, org_role="org:member")
    assert _confirm(client, started["userCode"]).status_code == 403
    who["ctx"] = AuthContext(user_id="user_bob", org_id=ORG, org_role="org:admin")
    assert _confirm(client, started["userCode"]).status_code == 409
    join.assert_not_awaited()

    who["ctx"] = AuthContext(user_id="user_ada", org_id=ORG, org_role="org:admin")
    ok = _confirm(client, started["userCode"])
    assert ok.status_code == 200 and ok.json()["workspaceName"] == "Acme"
    join.assert_awaited_once_with(ORG, TICKET)
    assert _confirm(client, started["userCode"]).status_code == 200  # a retry joins nothing again
    assert join.await_count == 1
    assert _poll(client, started["code"]) == {"status": "approved", "workspaceName": "Acme"}


def test_an_agents_start_must_carry_its_link_and_a_login_none(env):
    client, *_ = env
    mac = Computer()
    no_link = {**mac.signed(client, "agents"), "purpose": "agents"}
    assert client.post("/api/v1/network/device/start", json=no_link).status_code == 422
    with_link = {**mac.signed(client, "login", TICKET), "purpose": "login", "ticket": TICKET}
    assert client.post("/api/v1/network/device/start", json=with_link).status_code == 422


def test_desktop_and_network_codes_never_cross(env):
    import asyncio

    client, *_ = env
    desktop = asyncio.run(device_login.create({"machineName": "m"}, "/auth/connect-machine"))
    assert _confirm(client, desktop["userCode"]).status_code == 410
    s = _start(client, Computer()).json()
    with pytest.raises(Exception) as gone:
        asyncio.run(device_login.live_row(s["userCode"]))
    assert gone.value.status_code == 410


def test_logout_drops_the_binding_and_republishes_its_person(env):
    client, _, published, _ = env
    mac = Computer()
    s = _start(client, mac).json()
    _confirm(client, s["userCode"])
    published.reset_mock()
    assert client.post("/api/v1/network/logout", json=mac.signed(client, "logout")).status_code == 200
    import asyncio

    assert asyncio.run(network_keys_repo.get(mac.key)) is None
    published.assert_awaited_once_with(["org_user_ada"], exclude=(mac.key,))
    # A login proof is no logout proof.
    body = mac.signed(client, "login")
    assert client.post("/api/v1/network/logout", json=body).status_code == 401


def test_a_relogin_by_someone_else_republishes_both_people(env):
    client, who, published, _ = env
    mac = Computer()
    _confirm(client, _start(client, mac).json()["userCode"])
    who["ctx"] = AuthContext(user_id="user_bob", org_id=ORG)
    _confirm(client, _start(client, mac).json()["userCode"])
    assert published.await_args.args == (["org_user_ada", "org_user_bob"],)


def test_no_key_code_or_link_reaches_the_logs(env, caplog):
    client, *_ = env
    mac = Computer()
    with caplog.at_level(logging.DEBUG):
        s = _start(client, mac).json()
        _confirm(client, s["userCode"])
        a = _start(client, mac, "agents", TICKET).json()
        _confirm(client, a["userCode"])
        _poll(client, a["code"])
    # Ours, not the AWS SDK's own DEBUG wire dumps (off in production).
    sdk = ("botocore", "boto3", "urllib3", "moto", "s3transfer")
    text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(sdk))
    assert caplog.records
    for secret in (mac.key, s["code"], a["code"], TICKET):
        assert secret not in text


def test_a_clerk_error_changes_nothing_and_a_retry_completes_the_logout(env):
    client, _, published, _ = env
    mac = Computer()
    _confirm(client, _start(client, mac).json()["userCode"])
    network_member.workspaces_of.side_effect = RuntimeError("clerk down")
    assert client.post("/api/v1/network/logout", json=mac.signed(client, "logout")).status_code == 503
    import asyncio

    assert asyncio.run(network_keys_repo.get(mac.key)) is not None  # still bound: retry republishes
    network_member.workspaces_of.side_effect = lambda *u: ["org_a"]
    published.side_effect = RuntimeError("s3")
    assert client.post("/api/v1/network/logout", json=mac.signed(client, "logout")).status_code == 503
    assert asyncio.run(network_keys_repo.get(mac.key)) is not None
    published.side_effect = None
    assert client.post("/api/v1/network/logout", json=mac.signed(client, "logout")).status_code == 200
    assert asyncio.run(network_keys_repo.get(mac.key)) is None


def test_someone_else_confirming_an_approved_code_is_told_so(env):
    client, who, *_ = env
    s = _start(client, Computer()).json()
    assert _confirm(client, s["userCode"]).status_code == 200
    who["ctx"] = AuthContext(user_id="user_eve", org_id=None)
    taken = _confirm(client, s["userCode"])
    assert taken.status_code == 409 and taken.json()["detail"] == "Someone else already confirmed this code."


def test_a_free_account_with_no_workspace_signs_a_computer_in(env):
    client, who, published, _ = env
    who["ctx"] = AuthContext(user_id="user_free", org_id=None)
    mac = Computer()
    assert _confirm(client, _start(client, mac).json()["userCode"]).status_code == 200
    import asyncio

    assert asyncio.run(network_keys_repo.get(mac.key))["user_id"] == "user_free"
    published.assert_awaited_once_with(["org_user_free"], extra=(mac.key,))


def test_an_admin_takes_the_workspace_off_its_network(env, monkeypatch):
    client, who, *_ = env
    leave = AsyncMock(return_value={})
    monkeypatch.setattr(network_member, "leave_network", leave)
    who["ctx"] = AuthContext(user_id="user_ada", org_id=ORG, org_role="org:member")
    assert client.post("/api/v1/network/leave").status_code == 403
    who["ctx"] = AuthContext(user_id="user_ada", org_id=ORG, org_role="org:admin")
    assert client.post("/api/v1/network/leave").json() == {"state": "left"}
    leave.assert_awaited_once_with(ORG)
