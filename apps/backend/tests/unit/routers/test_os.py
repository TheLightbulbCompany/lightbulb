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


async def account(who: Tenant = A, started: bool = True) -> dict:
    """The account read; the person is then marked as having had a first conversation (the trial clock runs), unless
    `started` is False."""
    r = await req("GET", "/os/account", who)
    assert r.status_code == 200, r.text
    if started:
        users = dynamodb.get_table("users")
        users.update_item(
            Key={"user_id": who.user_id},
            UpdateExpression="SET os_trial_started_at = if_not_exists(os_trial_started_at, :t)",
            ExpressionAttributeValues={":t": START},
        )
    return r.json()


async def refer(who: Tenant, code) -> bool:
    r = await req("POST", "/os/referral", who, {"code": code})
    assert r.status_code == 200, r.text
    assert set(r.json()) == {"applied"}
    return r.json()["applied"]


async def test_first_account_read_starts_a_seven_day_trial(users, clock):
    assert await account() == {
        "accountId": "user_a",
        "email": "user_a@example.com",
        "name": "",
        "status": "trial",
        "trialEndsAt": START + 7 * DAY,
        "referralUrl": f"{settings.FRONTEND_URL.rstrip('/')}/os?ref=a",
        "referralDaysEarned": 0,
        "referralCount": 0,
        "checkoutUrl": None,
        "portalUrl": None,
        "priceText": "$20/month",
        "trialDays": 7,
        "inviteBonusDays": 0,
        "nextChargeAt": None,
        "trialVoiceLeftS": 120 * 60,
    }


async def test_later_reads_do_not_move_the_trial(users, clock):
    await account()
    clock[0] += DAY
    assert (await account())["trialEndsAt"] == START + 7 * DAY


async def test_two_concurrent_first_reads_create_one_account(users, clock):
    first = await asyncio.gather(*[account() for _ in range(3)])
    assert {a["trialEndsAt"] for a in first} == {START + 7 * DAY}
    assert len(users.scan()["Items"]) == 1


async def test_status_is_ended_after_trial(users, clock):
    await account()
    clock[0] += 7 * DAY - 1
    assert (await account())["status"] == "trial"
    clock[0] += 1
    assert (await account())["status"] == "ended"


@pytest.mark.parametrize("setting", ["SPARK_INTERNAL_USER_IDS", "PLATFORM_ADMIN_USER_IDS"])
async def test_a_team_account_is_never_asked_to_pay(users, clock, monkeypatch, setting):
    """The team (the accounts the metrics already ignore: SPARK_INTERNAL_USER_IDS and the platform admins) stays
    "active" past its free days, with no subscription behind it. Everyone else still ends."""
    monkeypatch.setattr(settings, setting, "user_a")
    await account(A), await account(B)
    clock[0] += 30 * DAY
    a, b = await account(A), await account(B)
    assert a["status"] == "active" and b["status"] == "ended"
    assert a["nextChargeAt"] is None and a["portalUrl"] is None


@pytest.mark.parametrize("token", ["oauth", "template"])
async def test_account_requires_a_human_session(users, clock, token):
    assert (await req("GET", "/os/account", replace(A, token=token))).status_code == 403
    assert (await req("POST", "/os/referral", replace(A, token=token), {"code": "b"})).status_code == 403
    assert (await req("GET", "/os/account")).status_code in (401, 403)
    assert (await req("POST", "/os/referral", body={"code": "b"})).status_code in (401, 403)
    assert users.scan()["Items"] == []


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
    assert a["trialEndsAt"] == START + 8 * DAY
    assert a["referralCount"] == 1 and a["referralDaysEarned"] == 1
    b = await account(B)
    assert b["trialEndsAt"] == clock[0] + 7 * DAY and b["referralCount"] == 0  # the referred person gets nothing
    assert users.get_item(Key={"user_id": "user_b"})["Item"]["os_referred_by"] == "user_a"


async def test_referral_applies_once_per_referred_person(users, clock):
    await account(A), await account(C), await account(B)
    assert await refer(B, "a")
    assert not await refer(B, "a")
    assert not await refer(B, "c")
    assert (await account(A))["referralCount"] == 1 and (await account(A))["trialEndsAt"] == START + 8 * DAY
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
    assert (await account(A)) == (await account(A)) | {"referralCount": 0, "trialEndsAt": START + 7 * DAY}


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
    assert b["trialEndsAt"] == clock[0] + 7 * DAY and b["status"] == "trial" and b["referralCount"] == 0
    a = await account(A)
    assert a["referralCount"] == 1 and a["referralDaysEarned"] == 1 and a["trialEndsAt"] == START + 8 * DAY
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


async def test_each_referral_below_the_cap_adds_a_day(users, clock):
    await account(A)
    for who in (B, C, D):
        await account(who)
        assert await refer(who, "a")
    a = await account(A)
    assert a["referralCount"] == 3 and a["referralDaysEarned"] == 3 and a["trialEndsAt"] == START + 10 * DAY


async def test_free_days_stop_at_a_month(users, clock, monkeypatch):
    """Owner, 2026-11-22: a person gets at most 30 free days, the 7-day trial included, so 23 invite days."""
    moved = []
    monkeypatch.setattr("routers.os.os_billing.extend_trial", lambda uid: moved.append(uid) or asyncio.sleep(0))
    await account(A)
    set_row("user_a", os_referrals=22, os_referral_days=22, os_trial_ends_at=START + 29 * DAY)
    await account(B)
    assert await refer(B, "a")  # the 23rd day: the last one
    a = await account(A)
    assert a["referralDaysEarned"] == 23 and a["trialEndsAt"] == START + 30 * DAY and moved == ["user_a"]
    await account(C)
    assert await refer(C, "a")  # the invite still counts; nobody gets a day
    a = await account(A)
    assert a["referralCount"] == 24 and a["referralDaysEarned"] == 23 and a["trialEndsAt"] == START + 30 * DAY
    assert moved == ["user_a"], "no Stripe trial is moved for a day that was not given"
    c = users.get_item(Key={"user_id": "user_c"})["Item"]
    assert c["os_referred_by"] == "user_a" and c["os_referral_credited"] is True
    assert (await account(C))["trialEndsAt"] == START + 7 * DAY and (await account(C))["inviteBonusDays"] == 0
    assert not await refer(C, "a")  # still once per person


async def test_the_month_counts_a_day_a_paying_inviter_gave(users, clock):
    await account(A)
    set_row("user_a", os_referrals=22, os_referral_days=22, os_invite_bonus_days=1, os_trial_ends_at=START + 30 * DAY)
    await account(B)
    assert await refer(B, "a")
    a = await account(A)
    assert a["referralCount"] == 23 and a["referralDaysEarned"] == 22 and a["trialEndsAt"] == START + 30 * DAY


async def test_an_ended_trial_at_the_cap_stays_ended(users, clock):
    await account(A)
    set_row("user_a", os_referrals=23, os_referral_days=23)
    clock[0] = START + 40 * DAY
    await account(B)
    assert await refer(B, "a")
    a = await account(A)
    assert a["status"] == "ended" and a["trialEndsAt"] == START + 7 * DAY and a["referralCount"] == 24


@pytest.mark.parametrize("ago", [7 * DAY, 3600])
async def test_an_ended_trials_day_counts_from_now(users, clock, ago):
    await account(A)
    clock[0] = START + 7 * DAY + ago
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
    clock[0] = START + 7 * DAY - 10
    await account(B)
    real = os_router.transact
    calls = []

    async def crossing(table, items):
        calls.append(items[1]["Update"]["UpdateExpression"])
        if len(calls) == 1:
            clock[0] = START + 7 * DAY + 10  # the referrer's trial ends now
            items[1]["Update"]["ExpressionAttributeValues"][":now"] = clock[0]
        await real(table, items)

    monkeypatch.setattr(os_router, "transact", crossing)
    assert await refer(B, "a")
    assert len(calls) == 2 and ":until" in calls[1]
    a = await account(A)
    assert a["trialEndsAt"] == clock[0] + DAY and a["referralCount"] == 1 and a["referralDaysEarned"] == 1


async def test_a_paying_referrers_friend_gets_the_day(users, clock, monkeypatch):
    from core.services import os_billing

    from unittest.mock import AsyncMock

    extend = AsyncMock()
    monkeypatch.setattr(os_billing, "extend_trial", extend)  # the friend's Stripe trial moves, never the referrer's
    await account(A)
    set_row("user_a", os_subscription_status="active", os_subscription_id="sub_1")
    clock[0] += 100
    created = clock[0]
    await account(B)
    assert await refer(B, "a")
    b = await account(B)
    assert b["trialEndsAt"] == created + 7 * DAY + DAY and b["inviteBonusDays"] == 1
    a = row("user_a")
    assert int(a["os_trial_ends_at"]) == START + 7 * DAY
    a = await account(A)
    assert a["referralCount"] == 1 and a["referralDaysEarned"] == 0
    extend.assert_awaited_once_with("user_b")
    assert not await refer(B, "a")  # once only
    assert (await account(B))["trialEndsAt"] == created + 8 * DAY and extend.await_count == 1


async def test_a_paying_referrers_trialing_friend_has_their_stripe_trial_moved(users, clock, monkeypatch):
    from unittest.mock import AsyncMock

    from core.services import os_billing

    extend = AsyncMock()
    monkeypatch.setattr(os_billing, "extend_trial", extend)
    await account(A), await account(B)
    set_row("user_a", os_subscription_status="active", os_subscription_id="sub_1")
    set_row("user_b", os_subscription_status="trialing", os_subscription_id="sub_2")
    assert await refer(B, "a")
    extend.assert_awaited_once_with("user_b")
    assert int(row("user_b")["os_trial_ends_at"]) == START + 8 * DAY


async def test_a_paying_referrers_friend_with_an_ended_trial_gets_the_day_from_now(users, clock, monkeypatch):
    from unittest.mock import AsyncMock

    from core.services import os_billing

    monkeypatch.setattr(os_billing, "extend_trial", AsyncMock())
    await account(A), await account(B)
    set_row("user_a", os_subscription_status="active", os_subscription_id="sub_1")
    clock[0] = START + 10 * DAY  # B's trial ended three days ago, inside the 14-day window
    assert (await account(B))["status"] == "ended"
    assert await refer(B, "a")
    b = await account(B)
    assert b["trialEndsAt"] == clock[0] + DAY and b["status"] == "trial" and b["inviteBonusDays"] == 1
    assert not await refer(B, "a")


async def test_a_friend_whose_trial_ends_between_the_read_and_the_write_is_retried_once(users, clock, monkeypatch):
    from unittest.mock import AsyncMock

    from core.services import os_billing
    from routers import os as os_router

    monkeypatch.setattr(os_billing, "extend_trial", AsyncMock())
    await account(A), await account(B)
    set_row("user_a", os_subscription_status="active", os_subscription_id="sub_1")
    clock[0] = START + 7 * DAY - 10
    real, calls = os_router.transact, []

    async def crossing(table, items):
        calls.append(items[0]["Update"]["UpdateExpression"])
        if len(calls) == 1:
            clock[0] = START + 7 * DAY + 10  # the friend's trial ends now
            items[0]["Update"]["ExpressionAttributeValues"][":now"] = clock[0]
        await real(table, items)

    monkeypatch.setattr(os_router, "transact", crossing)
    assert await refer(B, "a")
    assert len(calls) == 2 and ":until" in calls[1]
    assert int(row("user_b")["os_trial_ends_at"]) == clock[0] + DAY


async def test_a_paying_referrers_web_invite_shows_the_day_on_the_first_read(users, clock, monkeypatch):
    await account(A)
    set_row("user_a", os_subscription_status="active", os_subscription_id="sub_1")
    assert await refer(B, "a")  # no account yet: only the code is stored
    b = await account(B)
    assert b["trialEndsAt"] == START + 8 * DAY and b["inviteBonusDays"] == 1
    assert (await account(B))["trialEndsAt"] == START + 8 * DAY


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
    assert a["referralCount"] == applied.count(True) and a["trialEndsAt"] == START + (7 + a["referralCount"]) * DAY


async def test_a_transport_failure_is_503(users, clock, monkeypatch):
    from unittest.mock import AsyncMock

    from botocore.exceptions import EndpointConnectionError

    await account(A), await account(B)
    monkeypatch.setattr("routers.os.transact", AsyncMock(side_effect=EndpointConnectionError(endpoint_url="x")))
    assert (await req("POST", "/os/referral", B, {"code": "a"})).status_code == 503


async def test_deleted_routes_are_gone(users, clock):
    for method, path in [("POST", "/os/ticket"), ("POST", "/os/session"), ("DELETE", "/os/session")]:
        assert (await req(method, path, A, {})).status_code in (404, 405), path


async def test_account_delete_is_502_and_keeps_the_account_when_the_store_delete_fails(users, clock, monkeypatch):
    from core.observability import spark_store
    from core.services import clerk_admin
    from core.voice import os_voice

    def boom(_):
        raise RuntimeError("s3 down")

    async def none(*_):
        return []

    async def clerk(uid):
        raise AssertionError("the Clerk user must stay until the data is gone")

    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(spark_store, "_sweep", boom)
    monkeypatch.setattr(os_voice, "_active_sessions", none)
    monkeypatch.setattr(os_voice, "delete_transcripts", lambda uid: none())
    monkeypatch.setattr(clerk_admin, "delete_user", clerk)
    await account()
    r = await req("DELETE", "/os/account", A)
    assert r.status_code == 502 and r.json()["detail"] == "delete_failed"
    assert "Item" in users.meta.client.get_item(TableName=users.name, Key={"user_id": "user_a"})


async def test_account_delete_completes_when_the_posthog_delete_fails(users, clock, monkeypatch):
    from core.observability import product_analytics, spark_store
    from core.services import clerk_admin, posthog_admin

    gone = []

    async def clerk(uid):
        gone.append(uid)

    async def none(*_):
        return []

    from core.voice import os_voice

    monkeypatch.setattr(os_voice, "_active_sessions", none)
    monkeypatch.setattr(os_voice, "delete_transcripts", lambda uid: none())
    monkeypatch.setattr(spark_store, "_sweep", lambda uid: None)
    monkeypatch.setattr(clerk_admin, "delete_user", clerk)
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(settings, "POSTHOG_PROJECT_ID", "1")
    monkeypatch.setattr(settings, "POSTHOG_PROJECT_API_KEY", "k")
    called = []

    class Client:
        def __init__(self, **kw): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a): ...
        async def post(self, url, **kw):
            called.append(kw["json"])
            return type("R", (), {"status_code": 403, "text": "scope"})()

    monkeypatch.setattr(posthog_admin.httpx, "AsyncClient", Client)
    sent = []
    fake = type("C", (), {"capture": lambda self, d, e, p, **kw: sent.append((d, e, p, kw))})()
    monkeypatch.setattr(product_analytics, "_client_or_none", lambda: fake)
    await account()
    assert (await req("DELETE", "/os/account", A)).status_code == 204
    ((distinct_id, event, props, kw),) = [c for c in sent if c[1] == "account_deleted"]
    assert distinct_id != "user_a" and props["$process_person_profile"] is False and "$set" not in props
    assert (await req("DELETE", "/os/account", A)).status_code == 204  # a retry: nothing left to delete, no event
    assert len([c for c in sent if c[1] == "account_deleted"]) == 1
    assert gone == ["user_a"] * 2 and called == [{"distinct_ids": ["user_a"], "delete_events": True}] * 2


async def test_subscribing_during_the_trial_charges_at_once(users, clock, monkeypatch):
    """owner, 2026-11-28: no Stripe trial, even with free days and talking time left. A `trialing` subscription
    would never be limited by the trial's voice allowance."""
    import stripe

    from core.services import os_billing

    users.put_item(
        Item={"user_id": "user_a", "os_trial_ends_at": START + 5 * DAY, "os_voice_seconds": 60, "os_created_at": START}
    )
    sent = {}

    async def customer(*_):
        return "cus_test"

    async def no_pending(_):
        return None

    monkeypatch.setattr(settings, "STRIPE_SPARK_PRICE_ID", "price_test")
    monkeypatch.setattr(os_billing, "_now", lambda: START)
    monkeypatch.setattr(os_billing, "_customer", customer)
    monkeypatch.setattr(os_billing, "_retire_pending", no_pending)
    monkeypatch.setattr(stripe.Subscription, "list", lambda **_: {"data": []})
    monkeypatch.setattr(
        stripe.checkout.Session, "create", lambda **kw: sent.update(kw) or {"id": "cs_test", "url": "https://pay"}
    )
    await os_billing.checkout("user_a")
    assert sent["subscription_data"] == {"metadata": {"product": "spark", "user_id": "user_a"}}


async def test_the_account_shows_the_trials_talking_time_left(users, clock):
    """While the trial runs; none once it has ended."""
    assert (await account())["trialVoiceLeftS"] == 120 * 60
    users.update_item(
        Key={"user_id": "user_a"},
        UpdateExpression="SET os_voice_seconds = :s",
        ExpressionAttributeValues={":s": 100 * 60},
    )
    assert (await account())["trialVoiceLeftS"] == 20 * 60
    clock[0] += 8 * DAY
    assert (await account())["trialVoiceLeftS"] is None


async def test_an_invite_day_adds_twenty_minutes_and_gives_back_talking_time_to_one_whose_minutes_ran_out(users, clock):
    await account(A)
    set_row("user_a", os_voice_seconds=120 * 60)  # minutes gone, days left
    assert (await account(A))["status"] == "ended"
    await account(B)
    assert await refer(B, "a")
    a = await account(A)
    assert a["status"] == "trial" and a["trialVoiceLeftS"] == 20 * 60 and a["trialEndsAt"] == START + 8 * DAY


async def test_an_ended_trials_invite_day_comes_with_twenty_minutes_from_now(users, clock):
    await account(A)
    set_row("user_a", os_voice_seconds=100 * 60)
    clock[0] = START + 9 * DAY  # days over, minutes left
    await account(B)
    assert await refer(B, "a")
    a = await account(A)
    assert a["status"] == "trial" and a["trialEndsAt"] == clock[0] + DAY and a["trialVoiceLeftS"] == 40 * 60


def test_the_voice_allowance_counts_days_as_the_trial_extension_does():
    from core.services import os_billing

    assert os_billing.voice_allowance({}) == 120 * 60
    assert os_billing.voice_allowance({"os_referrals": 2, "os_invite_bonus_days": 1}) == 180 * 60  # old rows
    assert os_billing.voice_allowance({"os_referral_days": 3, "os_referrals": 9}) == 180 * 60
