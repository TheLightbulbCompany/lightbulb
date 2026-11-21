"""Spark sign-in (/os/ticket, /os/session) and the account read (/os/account), over moto DynamoDB with real
Clerk session tokens (the cross-account suite's Tenant and test JWKS)."""

import asyncio
from dataclasses import replace

import boto3
import pytest
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from core import dynamodb
from core.config import settings
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
        "referralUrl": f"{settings.FRONTEND_URL.rstrip('/')}/os?ref=a",
        "referralDaysEarned": 0,
        "referralCount": 0,
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


async def user_deleted(monkeypatch, user_id: str):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    monkeypatch.setattr("core.services.workspace_memory.delete_person_memory", AsyncMock())
    monkeypatch.setattr("core.repositories.workspace_repo.WorkspaceRepo.delete", AsyncMock())
    payload = {"type": "user.deleted", "data": {"id": user_id, "deleted": True}}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.post("/api/v1/webhooks/clerk", json=payload, headers={"svix-timestamp": "1234567890"})


async def test_a_deleted_clerk_account_is_signed_out_everywhere(users, clock, monkeypatch):
    a, b = await session(A), await session(B)
    assert (await user_deleted(monkeypatch, "user_a")).status_code == 200
    assert answer(await account(a)) == REVOKED
    assert (await account(b)).status_code == 200


async def test_user_deleted_with_no_users_row_completes(users, clock, monkeypatch):
    assert (await user_deleted(monkeypatch, "user_nobody")).status_code == 200
    assert users.scan()["Items"] == []


async def test_a_failed_trim_still_signs_in(users, clock, monkeypatch):
    from unittest.mock import AsyncMock

    for _ in range(10):
        await session()
    monkeypatch.setattr("routers.os._drop_tokens", AsyncMock(side_effect=RuntimeError("down")))
    token = await session()
    assert (await account(token)).status_code == 200


C = Tenant("user_c", None)
D = Tenant("user_d", None)


async def sign_in(who: Tenant, ref=None):
    r = await req("POST", "/os/ticket", who=who, body=None if ref is None else {"ref": ref})
    assert r.status_code == 200, r.text
    return r


async def referrer_account(token: str) -> dict:
    return (await account(token)).json()


async def test_account_has_referral_fields(users, clock):
    body = await referrer_account(await session())
    assert body["referralUrl"] == f"{settings.FRONTEND_URL.rstrip('/')}/os?ref=a"
    assert body["referralDaysEarned"] == 0 and body["referralCount"] == 0


async def test_first_sign_in_with_ref_credits_referrer_one_day(users, clock):
    token = await session(A)
    before = (await referrer_account(token))["trialEndsAt"]
    clock[0] += 100
    await sign_in(B, ref="a")
    body = await referrer_account(token)
    assert body["trialEndsAt"] == before + DAY
    assert body["referralCount"] == 1 and body["referralDaysEarned"] == 1
    row = users.get_item(Key={"user_id": "user_b"})["Item"]
    assert row["os_trial_ends_at"] == clock[0] + 3 * DAY
    assert row["os_referred_by"] == "user_a"


async def test_ref_on_a_later_sign_in_is_ignored(users, clock):
    token = await session(A)
    await sign_in(B)
    await sign_in(B, ref="a")
    body = await referrer_account(token)
    assert body["referralCount"] == 0 and body["trialEndsAt"] == clock[0] + 3 * DAY
    assert "os_referred_by" not in users.get_item(Key={"user_id": "user_b"})["Item"]


async def test_self_referral_is_ignored(users, clock):
    await sign_in(A, ref="a")
    row = users.get_item(Key={"user_id": "user_a"})["Item"]
    assert row["os_trial_ends_at"] == clock[0] + 3 * DAY
    assert "os_referrals" not in row and "os_referred_by" not in row


async def test_unknown_or_malformed_ref_is_ignored_and_sign_in_succeeds(users, clock):
    from core.repositories import user_repo

    await user_repo.put("user_c", "user_c@example.com")  # a users row with no Spark account
    for bad in ["c", "nobody", "", "user_a", "a.b", "a b", "../x", "x" * 65, 7, None, ["a"]]:
        await sign_in(B, ref=bad) if bad is not None else await sign_in(B)
    assert users.get_item(Key={"user_id": "user_b"})["Item"]["os_trial_ends_at"] == clock[0] + 3 * DAY
    assert "os_trial_ends_at" not in users.get_item(Key={"user_id": "user_c"})["Item"]
    assert "Item" not in users.get_item(Key={"user_id": "user_nobody"})


async def test_two_concurrent_first_sign_ins_credit_once(users, clock):
    token = await session(A)
    before = (await referrer_account(token))["trialEndsAt"]
    await asyncio.gather(*[sign_in(B, ref="a") for _ in range(2)])
    body = await referrer_account(token)
    assert body["referralCount"] == 1 and body["trialEndsAt"] == before + DAY


async def test_many_referrals_have_no_cap(users, clock):
    token = await session(A)
    before = (await referrer_account(token))["trialEndsAt"]
    for who in (B, C, D):
        await sign_in(who, ref="a")
    body = await referrer_account(token)
    assert body["referralCount"] == 3 and body["referralDaysEarned"] == 3
    assert body["trialEndsAt"] == before + 3 * DAY


async def test_ended_trial_still_gets_the_day(users, clock):
    token = await session(A)
    ends = (await referrer_account(token))["trialEndsAt"]
    clock[0] += 10 * DAY
    await sign_in(B, ref="a")
    body = await referrer_account(token)
    assert body["trialEndsAt"] == ends + DAY and body["status"] == "ended"


async def test_credit_failure_does_not_fail_sign_in(users, clock, monkeypatch):
    from unittest.mock import AsyncMock

    token = await session(A)
    monkeypatch.setattr("routers.os._credit", AsyncMock(side_effect=RuntimeError("down")))
    r = await sign_in(B, ref="a")
    assert set(r.json()) == {"ticket"}
    assert users.get_item(Key={"user_id": "user_b"})["Item"]["os_referred_by"] == "user_a"
    assert (await referrer_account(token))["referralCount"] == 0


async def test_response_is_identical_with_and_without_valid_ref(users, clock):
    await session(A)
    shapes = []
    for who, ref in [(B, "a"), (C, "nobody"), (D, None)]:
        r = await sign_in(who, ref=ref)
        shapes.append((r.status_code, set(r.json()), dict(r.headers).get("content-type")))
    assert shapes[0] == shapes[1] == shapes[2]
