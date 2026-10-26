"""`lightbulb login` / `logout` / `invite --agents`: the computer signs a one-time challenge, the
signed-in person confirms in the browser, and the backend binds the computer's key to them (or,
for an admin, sends the workspace to join that computer's network)."""

# ruff: noqa: F811 -- `table` is the repo suite's moto fixture, imported and then requested by name.

import base64
import logging
import time
from unittest.mock import AsyncMock, patch

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.testclient import TestClient

from core.auth import AuthContext, get_current_user
from core.config import settings
from core.services import clerk_admin, device_login, network_login, network_member, network_proof
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

    monkeypatch.setattr(settings, "NETWORK_PROOF_KEY", "test-proof-seed")
    monkeypatch.setattr(settings, "FRONTEND_URL", "https://dev.example")
    monkeypatch.setattr(clerk_admin, "get_organization", AsyncMock(return_value={"name": "Acme"}))
    monkeypatch.setattr(clerk_admin, "get_user_strict", AsyncMock(return_value={"id": "user_ada"}))
    monkeypatch.setattr("core.services.messaging.human_display_name", AsyncMock(return_value="Ada Lovelace"))
    join = AsyncMock(return_value={"state": "joining"})
    monkeypatch.setattr(network_member, "join_network", join)

    app = FastAPI()
    app.include_router(network.router, prefix="/api/v1/network")
    who = {"ctx": AuthContext(user_id="user_ada", org_id=ORG, org_role="org:admin")}

    async def _override():
        return who["ctx"]

    app.dependency_overrides[get_current_user] = _override
    with patch("core.dynamodb._table_prefix", "test-"), TestClient(app) as client:
        yield client, who, join


def _start(client, computer, purpose="login", ticket=None, proof=None):
    body = {**computer.signed(client, purpose, ticket), "purpose": purpose, "computer": "Ada's Mac"}
    if ticket is not None:
        body["ticket"] = ticket
    if proof is not None:
        body["proof"] = proof
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


def _login(client, computer):
    """``lightbulb login`` end to end: the poll's answer (with the proof)."""
    s = _start(client, computer).json()
    assert _confirm(client, s["userCode"]).status_code == 200
    return _poll(client, s["code"])


def test_login_hands_the_computer_a_proof_of_its_person_and_polls_once(env):
    client, *_ = env
    mac = Computer()
    started = _start(client, mac)
    assert started.status_code == 200, started.text
    s = started.json()
    assert s["verifyUrl"] == "https://dev.example/auth/network"  # the person types the code
    assert _poll(client, s["code"]) == {"status": "pending"}

    seen = client.post("/api/v1/network/device/describe", json={"userCode": s["userCode"]}).json()
    assert (seen["purpose"], seen["computer"], seen["ip"]) == ("login", "Ada's Mac", "testclient")
    assert abs(seen["startedAt"] - time.time()) < 60
    confirmed = _confirm(client, s["userCode"])
    assert confirmed.status_code == 200 and "proof" not in confirmed.json()  # never to the browser

    polled = _poll(client, s["code"])
    assert (polled["status"], polled["name"]) == ("approved", "Ada Lovelace")
    assert polled["routeUrl"] == settings.GATEWAY_RELAY_URL.removesuffix("/ws") + "/network"
    claims = network_proof.verify(polled["proof"], mac.key)
    assert claims["sub"] == "user_ada" and claims["exp"] - claims["iat"] == network_proof.LIFE_S
    assert claims["auth_time"] == claims["iat"]  # the browser login itself
    assert network_proof.verify(polled["proof"], Computer().key) is None  # another computer's
    assert _poll(client, s["code"]) == {"status": "expired"}  # single use


def test_a_proof_is_ours_unaltered_and_in_force():
    settings.NETWORK_PROOF_KEY = "seed-a"
    try:
        token = network_proof.mint("k" * 52, "user_a", now=1_000)
        assert network_proof.verify(token, "k" * 52, now=1_001)["sub"] == "user_a"
        assert network_proof.verify(token, "k" * 52, now=1_000 + network_proof.LIFE_S + 1) is None
        h, p, sig = token.split(".")
        forged = network_proof._b64(b'{"iat":1000,"exp":9999999999,"sub":"user_b","computer":"' + b"k" * 52 + b'"}')
        assert network_proof.verify(f"{h}.{forged}.{sig}", "k" * 52, now=1_001) is None
        settings.NETWORK_PROOF_KEY = "seed-b"  # another issuer key
        assert network_proof.verify(token, "k" * 52, now=1_001) is None
    finally:
        settings.NETWORK_PROOF_KEY = ""


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


def test_invite_agents_needs_this_admins_own_signed_in_computer(env):
    client, who, join = env
    mac = Computer()
    refused = _start(client, mac, "agents", TICKET)
    assert refused.status_code == 409 and "lightbulb login" in refused.json()["detail"]
    proof = _login(client, mac)["proof"]
    assert _start(client, mac, "agents", TICKET, proof=_login(client, Computer())["proof"]).status_code == 409
    started = _start(client, mac, "agents", TICKET, proof=proof).json()
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


def test_a_proof_is_renewed_only_in_its_last_week_and_never_once_expired(env, monkeypatch):
    client, *_ = env
    mac = Computer()
    proof = _login(client, mac)["proof"]

    def refresh(token):
        return client.post("/api/v1/network/proof/refresh", json={**mac.signed(client, "refresh"), "proof": token})

    assert refresh(proof).json() == {"proof": proof}  # weeks left: unchanged
    late = int(time.time()) - network_proof.LIFE_S + 3 * 86_400
    old = network_proof.mint(mac.key, "user_ada", now=late)
    renewed = refresh(old).json()["proof"]
    assert renewed != old and network_proof.verify(renewed, mac.key)["sub"] == "user_ada"
    expired = network_proof.mint(mac.key, "user_ada", now=late - 7 * 86_400)
    assert refresh(expired).status_code == 401
    other = Computer()
    assert refresh(network_proof.mint(other.key, "user_ada", now=late)).status_code == 401
    assert client.get("/api/v1/network/issuer").json()["env"].startswith(network_proof.kid() + ":")


def test_no_key_code_link_or_proof_reaches_the_logs(env, caplog):
    client, *_ = env
    mac = Computer()
    with caplog.at_level(logging.DEBUG):
        s = _start(client, mac).json()
        _confirm(client, s["userCode"])
        proof = _poll(client, s["code"])["proof"]
        a = _start(client, mac, "agents", TICKET, proof=proof).json()
        _confirm(client, a["userCode"])
        _poll(client, a["code"])
    # Ours, not the AWS SDK's own DEBUG wire dumps (off in production).
    sdk = ("botocore", "boto3", "urllib3", "moto", "s3transfer")
    text = "\n".join(r.getMessage() for r in caplog.records if not r.name.startswith(sdk))
    assert caplog.records
    for secret in (mac.key, s["code"], a["code"], TICKET, proof):
        assert secret not in text


def test_someone_else_confirming_an_approved_code_is_told_so(env):
    client, who, *_ = env
    s = _start(client, Computer()).json()
    assert _confirm(client, s["userCode"]).status_code == 200
    who["ctx"] = AuthContext(user_id="user_eve", org_id=None)
    taken = _confirm(client, s["userCode"])
    assert taken.status_code == 409 and taken.json()["detail"] == "Someone else already confirmed this code."


def test_a_free_account_with_no_workspace_signs_a_computer_in(env):
    client, who, *_ = env
    who["ctx"] = AuthContext(user_id="user_free", org_id=None)
    mac = Computer()
    assert network_proof.verify(_login(client, mac)["proof"], mac.key)["sub"] == "user_free"


def test_an_admin_takes_the_workspace_off_its_network(env, monkeypatch):
    client, who, *_ = env
    leave = AsyncMock(return_value={})
    monkeypatch.setattr(network_member, "leave_network", leave)
    who["ctx"] = AuthContext(user_id="user_ada", org_id=ORG, org_role="org:member")
    assert client.post("/api/v1/network/leave").status_code == 403
    who["ctx"] = AuthContext(user_id="user_ada", org_id=ORG, org_role="org:admin")
    assert client.post("/api/v1/network/leave").json() == {"state": "left"}
    leave.assert_awaited_once_with(ORG)


def test_a_renewal_keeps_the_login_time_and_has_no_age_cap(env):
    client, *_ = env
    mac = Computer()

    def refresh(token):
        return client.post("/api/v1/network/proof/refresh", json={**mac.signed(client, "refresh"), "proof": token})

    now = int(time.time())
    late = now - network_proof.LIFE_S + 3 * 86_400  # in its last week
    for logged_in in (now - 60 * 86_400, now - 400 * 86_400):  # no 90-day cap: a live account renews
        renewed = refresh(network_proof.mint(mac.key, "user_ada", now=late, auth_time=logged_in))
        assert renewed.status_code == 200
        assert network_proof.verify(renewed.json()["proof"], mac.key)["auth_time"] == logged_in


def test_every_refresh_checks_the_account_even_far_from_expiry(env):
    client, *_ = env
    mac = Computer()
    fresh = network_proof.mint(mac.key, "user_ada")
    clerk_admin.get_user_strict.return_value = {"id": "user_ada", "banned": True}
    response = client.post("/api/v1/network/proof/refresh", json={**mac.signed(client, "refresh"), "proof": fresh})
    assert response.status_code == 401


def test_a_signed_out_computer_cannot_refresh(env):
    import asyncio

    client, *_ = env
    mac = Computer()
    late = int(time.time()) - network_proof.LIFE_S + 3 * 86_400
    old = network_proof.mint(mac.key, "user_ada", now=late)
    asyncio.run(network_login.revoke("user_ada", mac.key))
    response = client.post("/api/v1/network/proof/refresh", json={**mac.signed(client, "refresh"), "proof": old})
    assert response.status_code == 401 and "lightbulb login" in response.json()["detail"]
    assert asyncio.run(network_login.revoked_keys(["user_ada", "user_bob"])) == [mac.key]


def test_a_renewal_needs_a_live_account_and_fails_closed_when_clerk_cannot_say(env):
    client, *_ = env
    mac = Computer()
    late = int(time.time()) - network_proof.LIFE_S + 3 * 86_400
    old = network_proof.mint(mac.key, "user_ada", now=late)

    def refresh():
        return client.post("/api/v1/network/proof/refresh", json={**mac.signed(client, "refresh"), "proof": old})

    for gone in (None, {"id": "user_ada", "banned": True}, {"id": "user_ada", "locked": True}):
        clerk_admin.get_user_strict.return_value = gone
        assert refresh().status_code == 401, gone
    clerk_admin.get_user_strict.side_effect = clerk_admin.ClerkLookupError("blip")
    assert refresh().status_code == 503
    clerk_admin.get_user_strict.side_effect = None
    clerk_admin.get_user_strict.return_value = {"id": "user_ada"}
    assert refresh().status_code == 200


def test_logout_signs_the_computer_out_everywhere_and_a_new_login_lifts_it(env, monkeypatch):
    import asyncio

    from core.services import network_route

    client, *_ = env
    closed, published = AsyncMock(), AsyncMock()
    monkeypatch.setattr(network_route, "sign_out", closed)
    monkeypatch.setattr(network_member, "publish_sessions", published)
    monkeypatch.setattr(clerk_admin, "list_user_organizations", AsyncMock(return_value=[{"id": ORG}]))
    mac = Computer()
    proof = _login(client, mac)["proof"]
    published.assert_not_awaited()  # nothing to lift on a first login

    def logout(token):
        return client.post("/api/v1/network/proof/revoke", json={**mac.signed(client, "logout"), "proof": token})

    # Another key's signature, or a login-purpose one, signs nobody out.
    forged = {**Computer().signed(client, "logout"), "key": mac.key, "proof": proof}
    assert client.post("/api/v1/network/proof/revoke", json=forged).status_code == 401
    login_sig = {**mac.signed(client, "login"), "proof": proof}
    assert client.post("/api/v1/network/proof/revoke", json=login_sig).status_code == 401
    assert not asyncio.run(network_login.is_revoked("user_ada", mac.key))

    assert logout(proof).json() == {"state": "signed_out"}
    assert asyncio.run(network_login.revoked_keys(["user_ada"])) == [mac.key]
    closed.assert_awaited_once_with("user_ada", mac.key)
    published.assert_awaited_once_with(ORG)  # the workspace's engines refuse it now
    assert logout(network_proof.mint(Computer().key, "user_ada")).status_code == 200  # another's proof
    assert closed.await_count == 1

    _login(client, mac)
    assert not asyncio.run(network_login.is_revoked("user_ada", mac.key))
    assert published.await_count == 2  # the lift is republished too
