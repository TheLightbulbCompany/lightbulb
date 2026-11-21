"""Spark's account read (/os/account) and referrals (/os/referral), over moto DynamoDB with real Clerk session
tokens (the cross-account suite's Tenant and test JWKS)."""

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

A, B, C, D = (Tenant(f"user_{x}", None) for x in "abcd")
DAY = 86400
START = 1_800_000_000


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
    now = [START]
    monkeypatch.setattr("routers.os._now", lambda: now[0])
    return now


async def req(method, path, who: Tenant | None = None, body=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, f"/api/v1{path}", headers=who.headers if who else {}, json=body)


async def account(who: Tenant = A) -> dict:
    r = await req("GET", "/os/account", who)
    assert r.status_code == 200, r.text
    return r.json()


async def refer(who: Tenant, code) -> bool:
    r = await req("POST", "/os/referral", who, {"code": code})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"applied"}
    return r.json()["applied"]


async def test_first_account_read_starts_a_three_day_trial(users, clock):
    assert await account() == {
        "accountId": "user_a",
        "email": "user_a@example.com",
        "name": "",
        "status": "trial",
        "trialEndsAt": START + 3 * DAY,
        "referralUrl": f"{settings.FRONTEND_URL.rstrip('/')}/os?ref=a",
        "referralDaysEarned": 0,
        "referralCount": 0,
        "checkoutUrl": None,
        "portalUrl": None,
        "priceText": "$20/month",
        "trialDays": 3,
        "inviteBonusDays": 0,
        "nextChargeAt": None,
    }


async def test_later_reads_do_not_move_the_trial(users, clock):
    await account()
    clock[0] += DAY
    assert (await account())["trialEndsAt"] == START + 3 * DAY


async def test_two_concurrent_first_reads_create_one_account(users, clock):
    first = await asyncio.gather(*[account() for _ in range(3)])
    assert {a["trialEndsAt"] for a in first} == {START + 3 * DAY}
    assert len(users.scan()["Items"]) == 1


async def test_status_is_ended_after_trial(users, clock):
    await account()
    clock[0] += 3 * DAY - 1
    assert (await account())["status"] == "trial"
    clock[0] += 1
    assert (await account())["status"] == "ended"


@pytest.mark.parametrize("token", ["oauth", "template"])
async def test_account_requires_a_human_session(users, clock, token):
    assert (await req("GET", "/os/account", replace(A, token=token))).status_code == 403
    assert (await req("POST", "/os/referral", replace(A, token=token), {"code": "b"})).status_code == 403
    assert (await req("GET", "/os/account")).status_code in (401, 403)
    assert (await req("POST", "/os/referral", body={"code": "b"})).status_code in (401, 403)
    assert users.scan()["Items"] == []


async def test_works_with_and_without_an_organisation(users, clock):
    without = await account(A)
    clock[0] += DAY
    with_org = await account(replace(A, org_id="org_a"))
    assert without == with_org
    assert [i["user_id"] for i in users.scan()["Items"]] == ["user_a"]


async def test_account_has_referral_fields(users, clock):
    body = await account(B)
    assert body["referralUrl"] == f"{settings.FRONTEND_URL.rstrip('/')}/os?ref=b"
    assert body["referralDaysEarned"] == 0 and body["referralCount"] == 0


async def test_referral_credits_the_referrer_one_day(users, clock):
    await account(A)
    clock[0] += 100
    await account(B)
    assert await refer(B, "a")
    a = await account(A)
    assert a["trialEndsAt"] == START + 4 * DAY
    assert a["referralCount"] == 1 and a["referralDaysEarned"] == 1
    b = await account(B)
    assert b["trialEndsAt"] == clock[0] + 3 * DAY and b["referralCount"] == 0  # the referred person gets nothing
    assert users.get_item(Key={"user_id": "user_b"})["Item"]["os_referred_by"] == "user_a"


async def test_referral_applies_once_per_referred_person(users, clock):
    await account(A), await account(C), await account(B)
    assert await refer(B, "a")
    assert not await refer(B, "a")
    assert not await refer(B, "c")
    assert (await account(A))["referralCount"] == 1 and (await account(A))["trialEndsAt"] == START + 4 * DAY
    assert (await account(C))["referralCount"] == 0


async def test_referral_window_is_fourteen_days(users, clock):
    await account(A), await account(B), await account(C)
    clock[0] += 13 * DAY
    assert await refer(B, "a")
    assert (await account(A))["referralCount"] == 1
    clock[0] += 2 * DAY
    assert not await refer(C, "a")
    assert (await account(A))["referralCount"] == 1
    assert "os_referred_by" not in users.get_item(Key={"user_id": "user_c"})["Item"]


async def test_self_referral_is_refused(users, clock):
    await account(A)
    assert not await refer(A, "a")
    assert (await account(A)) == (await account(A)) | {"referralCount": 0, "trialEndsAt": START + 3 * DAY}


async def test_unknown_or_malformed_code_is_refused_identically(users, clock):
    from core.repositories import user_repo

    await account(A)
    await user_repo.put("user_c", "user_c@example.com")  # a users row with no Spark account
    await account(B)
    bad = ["c", "nobody", "", "user_a", "a.b", "a b", "../x", "x" * 65, 7, None, ["a"], {"x": 1}]
    answers = [await req("POST", "/os/referral", B, {"code": code}) for code in bad]
    answers += [await req("POST", "/os/referral", B, body) for body in [None, {}, [], "a"]]
    assert {(r.status_code, r.text) for r in answers} == {(200, '{"applied":false}')}
    assert "os_trial_ends_at" not in users.get_item(Key={"user_id": "user_c"})["Item"]
    assert "Item" not in users.get_item(Key={"user_id": "user_nobody"})
    assert "os_referred_by" not in users.get_item(Key={"user_id": "user_b"})["Item"]
    assert await refer(B, "a")  # the refusals left the one referral unused


async def test_another_persons_referral_credits_them_but_never_marks_them_referred(users, clock):
    await account(A), await account(B)
    assert await refer(B, "a")
    a_row = users.get_item(Key={"user_id": "user_a"})["Item"]
    assert "os_referred_by" not in a_row and a_row["os_referrals"] == 1


def row(user_id: str) -> dict:
    return users_table().get_item(Key={"user_id": user_id}).get("Item") or {}


def users_table():
    return dynamodb.get_table("users")


async def test_web_signup_records_the_code_without_an_account(users, clock):
    await account(A)
    assert await refer(B, "a")
    assert row("user_b") == {"user_id": "user_b", "os_referred_by": "user_a"}  # no account, no trial
    assert (await account(A))["referralCount"] == 0  # not credited yet
    assert not await refer(B, "c") and not await refer(B, "a")
    assert row("user_b")["os_referred_by"] == "user_a"


async def test_web_signup_refuses_self_unknown_and_no_account_codes(users, clock):
    from core.repositories import user_repo

    await account(A)
    await user_repo.put("user_c", "user_c@example.com")  # a users row with no Spark account
    for code in ("b", "nobody", "c"):
        assert not await refer(B, code)
    assert "Item" not in users.get_item(Key={"user_id": "user_b"})
    assert await refer(B, "a")


async def test_web_signup_on_an_existing_users_row_keeps_it(users, clock):
    from core.repositories import user_repo

    await account(A)
    await user_repo.put("user_b", "user_b@example.com")
    assert await refer(B, "a")
    assert row("user_b")["email"] == "user_b@example.com" and row("user_b")["os_referred_by"] == "user_a"


async def test_first_account_read_after_web_signup_credits_the_referrer_once(users, clock):
    await account(A)
    assert await refer(B, "a")
    clock[0] += 100
    b = await account(B)
    assert b["trialEndsAt"] == clock[0] + 3 * DAY and b["status"] == "trial" and b["referralCount"] == 0
    a = await account(A)
    assert a["referralCount"] == 1 and a["referralDaysEarned"] == 1 and a["trialEndsAt"] == START + 4 * DAY
    await account(B)
    assert (await account(A))["referralCount"] == 1
    assert row("user_b")["os_referral_credited"] is True


async def test_concurrent_credits_credit_once(users, clock):
    from routers import os as os_router

    await account(A)
    assert await refer(B, "a")
    args = ("user_b", "user_a", "SET os_referral_credited = :t", os_router._NOT_CREDITED, {})
    assert [await os_router._credit(*args), await os_router._credit(*args)] == ["referrer", ""]
    await asyncio.gather(*[account(B) for _ in range(3)])
    assert (await account(A))["referralCount"] == 1


async def test_a_failed_credit_never_fails_the_account_read_and_is_retried(users, clock, monkeypatch):
    from unittest.mock import AsyncMock

    from botocore.exceptions import EndpointConnectionError

    from core.dynamodb import transact

    await account(A)
    assert await refer(B, "a")
    monkeypatch.setattr("routers.os.transact", AsyncMock(side_effect=EndpointConnectionError(endpoint_url="x")))
    assert (await account(B))["status"] == "trial"
    assert "os_referral_credited" not in row("user_b")
    monkeypatch.setattr("routers.os.transact", transact)
    await account(B)
    assert (await account(A))["referralCount"] == 1


async def test_in_app_referral_is_not_credited_again_by_the_account_read(users, clock):
    await account(A), await account(B)
    assert await refer(B, "a")
    await account(B)
    assert (await account(A))["referralCount"] == 1


async def test_many_referrals_have_no_cap(users, clock):
    await account(A)
    for who in (B, C, D):
        await account(who)
        assert await refer(who, "a")
    a = await account(A)
    assert a["referralCount"] == 3 and a["referralDaysEarned"] == 3 and a["trialEndsAt"] == START + 6 * DAY


@pytest.mark.parametrize("ago", [3 * DAY, 3600])
async def test_an_ended_trials_day_counts_from_now(users, clock, ago):
    await account(A)
    clock[0] = START + 3 * DAY + ago
    await account(B)
    assert await refer(B, "a")
    a = await account(A)
    assert a["trialEndsAt"] == clock[0] + DAY and a["status"] == "trial" and a["referralDaysEarned"] == 1
    assert (await account(B))["inviteBonusDays"] == 0


def set_row(user_id: str, **fields):
    names = {f"#k{i}": k for i, k in enumerate(fields)}
    dynamodb.get_table("users").update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET " + ", ".join(f"#k{i} = :v{i}" for i in range(len(fields))),
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":v{i}": v for i, v in enumerate(fields.values())},
    )


async def test_a_trial_that_ends_between_the_read_and_the_write_is_retried_once(users, clock, monkeypatch):
    """The read saw a running trial; by the write it had ended. The second try counts the day from now."""
    from routers import os as os_router

    await account(A)
    clock[0] = START + 3 * DAY - 10
    await account(B)
    real = os_router.transact
    calls = []

    async def crossing(table, items):
        calls.append(items[1]["Update"]["UpdateExpression"])
        if len(calls) == 1:
            clock[0] = START + 3 * DAY + 10  # the referrer's trial ends now
            items[1]["Update"]["ExpressionAttributeValues"][":now"] = clock[0]
        await real(table, items)

    monkeypatch.setattr(os_router, "transact", crossing)
    assert await refer(B, "a")
    assert len(calls) == 2 and ":until" in calls[1]
    a = await account(A)
    assert a["trialEndsAt"] == clock[0] + DAY and a["referralCount"] == 1 and a["referralDaysEarned"] == 1


async def test_a_paying_referrers_friend_gets_the_day(users, clock, monkeypatch):
    from core.services import os_billing

    monkeypatch.setattr(os_billing, "extend_trial", None)  # no Stripe path is reached at all
    await account(A)
    set_row("user_a", os_subscription_status="active", os_subscription_id="sub_1")
    clock[0] += 100
    created = clock[0]
    await account(B)
    assert await refer(B, "a")
    b = await account(B)
    assert b["trialEndsAt"] == created + 3 * DAY + DAY and b["inviteBonusDays"] == 1
    a = row("user_a")
    assert int(a["os_trial_ends_at"]) == START + 3 * DAY
    a = await account(A)
    assert a["referralCount"] == 1 and a["referralDaysEarned"] == 0


async def test_a_paying_referrers_web_invite_shows_the_day_on_the_first_read(users, clock, monkeypatch):
    await account(A)
    set_row("user_a", os_subscription_status="active", os_subscription_id="sub_1")
    assert await refer(B, "a")  # no account yet: only the code is stored
    b = await account(B)
    assert b["trialEndsAt"] == START + 4 * DAY and b["inviteBonusDays"] == 1
    assert (await account(B))["trialEndsAt"] == START + 4 * DAY


async def test_a_legacy_row_reads_its_days_from_the_count(users, clock):
    await account(A)
    set_row("user_a", os_referrals=3)
    assert (await account(A))["referralDaysEarned"] == 3
    await account(B)
    assert await refer(B, "a")
    a = await account(A)
    assert a["referralCount"] == 4 and a["referralDaysEarned"] == 4


async def test_two_concurrent_referral_posts_credit_once(users, clock):
    await account(A), await account(B)
    rs = await asyncio.gather(*[req("POST", "/os/referral", B, {"code": "a"}) for _ in range(3)])
    applied = [r.json().get("applied") for r in rs if r.status_code == 200]
    assert applied.count(True) <= 1 and all(r.status_code in (200, 503) for r in rs)
    a = await account(A)
    assert a["referralCount"] == applied.count(True) and a["trialEndsAt"] == START + (3 + a["referralCount"]) * DAY


async def test_a_transport_failure_is_503(users, clock, monkeypatch):
    from unittest.mock import AsyncMock

    from botocore.exceptions import EndpointConnectionError

    await account(A), await account(B)
    monkeypatch.setattr("routers.os.transact", AsyncMock(side_effect=EndpointConnectionError(endpoint_url="x")))
    assert (await req("POST", "/os/referral", B, {"code": "a"})).status_code == 503


async def test_deleted_routes_are_gone(users, clock):
    for method, path in [("POST", "/os/ticket"), ("POST", "/os/session"), ("DELETE", "/os/session")]:
        assert (await req(method, path, A, {})).status_code in (404, 405), path
