"""Spark sign-in (/os/ticket, /os/session) and the account read (/os/account), over moto DynamoDB with real
Clerk session tokens (the cross-account suite's Tenant and test JWKS)."""

import asyncio
from dataclasses import replace

import boto3
import pytest
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from core import dynamodb
from main import app
from tests.unit.auth.conftest import Tenant, jwks  # noqa: F401

A = Tenant("user_a", None)
B = Tenant("user_b", None)
DAY = 86400


@pytest.fixture
def users(monkeypatch, jwks):  # noqa: F811
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=dynamodb.table_name("users"),
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield resource.Table(dynamodb.table_name("users"))


@pytest.fixture
def clock(monkeypatch):
    now = [1_800_000_000]
    monkeypatch.setattr("routers.os._now", lambda: now[0])
    return now


async def req(method, path, *, who: Tenant | None = None, token: str | None = None, body=None):
    headers = who.headers if who else {"Authorization": f"Bearer {token}"} if token else {}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, f"/api/v1{path}", headers=headers, json=body)


async def ticket(who: Tenant = A) -> str:
    r = await req("POST", "/os/ticket", who=who)
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"ticket"}
    return r.json()["ticket"]


async def session(who: Tenant = A) -> str:
    r = await req("POST", "/os/session", body={"ticket": await ticket(who)})
    assert r.status_code == 200, r.text
    return r.json()["token"]


async def account(token: str):
    return await req("GET", "/os/account", token=token)


TICKET_INVALID = (401, {"detail": "ticket_invalid"})
REVOKED = (401, {"detail": "os_session_revoked"})


def answer(r):
    return r.status_code, r.json()


async def test_ticket_then_session_then_account_round_trip(users, clock):
    r = await req("POST", "/os/session", body={"ticket": await ticket()})
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"token", "accountId", "expiresAt"}
    assert body["accountId"] == "user_a" and body["expiresAt"] is None
    r = await account(body["token"])
    assert r.status_code == 200
    assert r.json() == {
        "accountId": "user_a",
        "email": "user_a@example.com",
        "name": "",
        "status": "trial",
        "trialEndsAt": clock[0] + 3 * DAY,
        "checkoutUrl": None,
        "portalUrl": None,
    }


async def test_ticket_is_one_time(users, clock):
    t = await ticket()
    assert (await req("POST", "/os/session", body={"ticket": t})).status_code == 200
    assert answer(await req("POST", "/os/session", body={"ticket": t})) == TICKET_INVALID


async def test_ticket_expires_after_60s(users, clock):
    t = await ticket()
    clock[0] += 60
    assert answer(await req("POST", "/os/session", body={"ticket": t})) == TICKET_INVALID
    t = await ticket()
    clock[0] += 59
    assert (await req("POST", "/os/session", body={"ticket": t})).status_code == 200


async def test_ticket_unknown_user_and_wrong_secret_look_identical(users, clock):
    t = await ticket()
    secret = t.split(".", 1)[1]
    for bad in [f"user_nobody.{secret}", "user_a.wrongsecret", "nonsense", "", "user_a.", ".x", "user_a"]:
        assert answer(await req("POST", "/os/session", body={"ticket": bad})) == TICKET_INVALID, bad


async def test_concurrent_redeem_yields_one_token(users, clock):
    t = await ticket()
    rs = await asyncio.gather(*[req("POST", "/os/session", body={"ticket": t}) for _ in range(2)])
    assert sorted(r.status_code for r in rs) == [200, 401]


async def test_trial_starts_at_first_sign_in_and_is_not_reset(users, clock):
    first = clock[0] + 3 * DAY
    token = await session()
    clock[0] += DAY
    await ticket()
    assert (await account(token)).json()["trialEndsAt"] == first


async def test_status_is_ended_after_trial(users, clock):
    token = await session()
    clock[0] += 3 * DAY - 1
    assert (await account(token)).json()["status"] == "trial"
    clock[0] += 1
    assert (await account(token)).json()["status"] == "ended"


async def test_sign_out_revokes_only_that_token(users, clock):
    one, two = await session(), await session()
    assert (await req("DELETE", "/os/session", token=one)).status_code == 204
    assert answer(await account(one)) == REVOKED
    assert answer(await req("DELETE", "/os/session", token=one)) == REVOKED
    assert (await account(two)).status_code == 200


async def test_token_cap_drops_oldest(users, clock):
    tokens = []
    for _ in range(11):
        clock[0] += 1
        tokens.append(await session())
    assert answer(await account(tokens[0])) == REVOKED
    for t in tokens[1:]:
        assert (await account(t)).status_code == 200


async def test_one_persons_token_cannot_read_anothers_account(users, clock):
    a, b = await session(A), await session(B)
    assert (await account(a)).json()["accountId"] == "user_a"
    forged = "user_b." + a.split(".", 1)[1]
    r = await account(forged)
    assert answer(r) == REVOKED
    assert (await account(b)).json()["accountId"] == "user_b"
    for bad in ["", "garbage", "user_a.", "user_nobody." + a.split(".", 1)[1]]:
        assert answer(await account(bad)) == REVOKED, bad
    assert answer(await req("GET", "/os/account")) == REVOKED


@pytest.mark.parametrize("token", ["oauth", "template"])
async def test_ticket_requires_a_human_session(users, clock, token):
    r = await req("POST", "/os/ticket", who=replace(A, token=token))
    assert r.status_code == 403
    assert users.scan()["Items"] == []


async def test_works_with_and_without_an_organisation(users, clock):
    without = await session(A)
    clock[0] += DAY
    with_org = await session(replace(A, org_id="org_a"))
    for t in (without, with_org):
        body = (await account(t)).json()
        assert body["accountId"] == "user_a" and body["trialEndsAt"] == 1_800_000_000 + 3 * DAY
    assert [i["user_id"] for i in users.scan()["Items"]] == ["user_a"]


async def test_a_replayed_user_created_webhook_keeps_the_account(users, clock):
    """Clerk's user.created writes the same users row (user_repo.put); a redelivery must not sign Spark out
    or restart the trial."""
    from core.repositories import user_repo

    token = await session()
    await user_repo.put("user_a", "user_a@example.com")
    body = (await account(token)).json()
    assert body["trialEndsAt"] == clock[0] + 3 * DAY
