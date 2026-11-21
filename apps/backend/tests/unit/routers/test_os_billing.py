"""Spark billing: the account's billing fields, the signed checkout/portal links, the Spark webhook branch and
the cancel on Clerk `user.deleted`. Stripe is always faked; DynamoDB is moto."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlparse

import boto3
import jwt as pyjwt
import pytest
import stripe
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from core import dynamodb
from core.config import settings
from core.services import os_billing, stripe_webhook_handlers, webhook_dedup
from main import app
from tests.unit.auth.conftest import Tenant, jwks  # noqa: F401

A, B = Tenant("user_a", None), Tenant("user_b", None)
DAY, HOUR = 86400, 3600
START = 1_800_000_000
PRICE = "price_spark_test"
KEY = "test-signing-key-0123456789abcdef0123456789"


@pytest.fixture
def ddb(monkeypatch, jwks):  # noqa: F811
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        for name, key in (("users", "user_id"), ("billing-accounts", "owner_id")):
            resource.create_table(
                TableName=dynamodb.table_name(name),
                KeySchema=[{"AttributeName": key, "KeyType": "HASH"}],
                AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
                BillingMode="PAY_PER_REQUEST",
            )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield SimpleNamespace(
            users=resource.Table(dynamodb.table_name("users")),
            billing=resource.Table(dynamodb.table_name("billing-accounts")),
        )


@pytest.fixture
def clock(monkeypatch):
    now = [START]
    monkeypatch.setattr("routers.os._now", lambda: now[0])
    monkeypatch.setattr(os_billing, "_now", lambda: now[0])
    return now


class FakeStripe:
    """Every Stripe call the Spark code makes, recorded. Subscriptions live in `subs` (id -> status)."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.subs: dict[str, str] = {}
        self.customers = 0

    def customer_create(self, **kw):
        self.calls.append(("customer", kw))
        self.customers += 1
        return {"id": f"cus_{self.customers}"}

    def checkout_create(self, **kw):
        self.calls.append(("checkout", kw))
        return {"url": "https://checkout.stripe.test/s"}

    def portal_create(self, **kw):
        self.calls.append(("portal", kw))
        return {"url": "https://billing.stripe.test/p"}

    def sub_retrieve(self, sub_id, **kw):
        self.calls.append(("retrieve", {"id": sub_id}))
        return {"id": sub_id, "status": self.subs[sub_id], "customer": "cus_1"}

    def sub_cancel(self, sub_id, **kw):
        self.calls.append(("cancel", {"id": sub_id}))
        self.subs[sub_id] = "canceled"
        return {"id": sub_id, "status": "canceled"}

    def kinds(self):
        return [k for k, _ in self.calls]


@pytest.fixture
def fake(monkeypatch):
    f = FakeStripe()
    monkeypatch.setattr(stripe.Customer, "create", f.customer_create)
    monkeypatch.setattr(stripe.checkout.Session, "create", f.checkout_create)
    monkeypatch.setattr(stripe.billing_portal.Session, "create", f.portal_create)
    monkeypatch.setattr(stripe.Subscription, "retrieve", f.sub_retrieve)
    monkeypatch.setattr(stripe.Subscription, "cancel", f.sub_cancel)
    monkeypatch.setattr(settings, "STRIPE_SPARK_PRICE_ID", PRICE)
    monkeypatch.setattr(settings, "PAPERCLIP_SERVICE_TOKEN_KEY", KEY)
    return f


async def req(method, path, who: Tenant | None = None, headers=None, **kw):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.request(method, f"/api/v1{path}", headers=(who.headers if who else {}) | (headers or {}), **kw)


async def account(who: Tenant = A) -> dict:
    r = await req("GET", "/os/account", who)
    assert r.status_code == 200, r.text
    return r.json()


def subscribe(ddb, user_id="user_a", status="active", sub="sub_1", customer="cus_1"):
    ddb.users.update_item(
        Key={"user_id": user_id},
        UpdateExpression="SET os_subscription_id = :s, os_subscription_status = :st, os_stripe_customer_id = :c",
        ExpressionAttributeValues={":s": sub, ":st": status, ":c": customer},
    )


def page(state: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/os/billing?state={state}"


async def open_link(url: str):
    u = urlparse(url)
    assert f"{u.scheme}://{u.netloc}" == settings.PUBLIC_API_BASE.rstrip("/")
    return await req("GET", u.path.removeprefix("/api/v1"), params=parse_qs(u.query))


def token(user_id: str, kind: str, exp: int) -> str:
    return pyjwt.encode({"sub": user_id, "kind": kind, "exp": exp}, KEY, algorithm="HS256")


# --- the account's billing fields ----------------------------------------------------------------------------


async def test_no_price_configured_has_no_links(ddb, clock, fake, monkeypatch):
    monkeypatch.setattr(settings, "STRIPE_SPARK_PRICE_ID", "")
    a = await account()
    assert (a["checkoutUrl"], a["portalUrl"], a["status"]) == (None, None, "trial")


async def test_price_and_display_fields(ddb, clock, fake):
    a = await account()
    assert a["portalUrl"] is None and a["status"] == "trial"
    assert a["checkoutUrl"].startswith(f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/os/checkout?t=")
    assert (a["priceText"], a["trialDays"]) == ("$20/month", 3)
    assert fake.calls == []  # the account read never calls Stripe


@pytest.mark.parametrize("live", ["active", "trialing"])
async def test_live_subscription_is_active_with_portal_only(ddb, clock, fake, live):
    await account()
    subscribe(ddb, status=live)
    clock[0] += 10 * DAY  # long past the trial
    a = await account()
    assert a["status"] == "active" and a["checkoutUrl"] is None
    assert a["portalUrl"].startswith(f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/os/portal?t=")
    assert fake.calls == []


@pytest.mark.parametrize("billed", ["past_due", "unpaid"])
async def test_a_subscription_stripe_still_bills_is_not_active_and_offers_only_the_portal(ddb, clock, fake, billed):
    """Not active; no checkout either (a second subscription), the card is fixed in the portal."""
    await account()
    subscribe(ddb, status=billed)
    clock[0] += 10 * DAY
    a = await account()
    assert a["status"] == "ended" and a["checkoutUrl"] is None and a["portalUrl"]


@pytest.mark.parametrize("over", ["canceled", "incomplete", "incomplete_expired", "paused"])
async def test_an_ended_subscription_offers_checkout_and_portal(ddb, clock, fake, over):
    await account()
    subscribe(ddb, status=over)
    a = await account()
    assert a["status"] == "trial" and a["checkoutUrl"] and a["portalUrl"]


async def test_config_is_public_and_matches_the_account(ddb, clock, fake):
    r = await req("GET", "/os/config")
    assert r.status_code == 200 and r.json() == {"priceText": "$20/month", "trialDays": 3}
    a = await account()
    assert {k: a[k] for k in ("priceText", "trialDays")} == r.json()


# --- the signed links ----------------------------------------------------------------------------------------


async def test_bad_tokens_go_to_the_expired_page_without_calling_stripe(ddb, clock, fake):
    await account(A)
    good = token("user_a", os_billing.CHECKOUT, START + 900)
    bad = [
        "",
        "garbage",
        token("user_a", os_billing.CHECKOUT, START),  # expired
        good[:-2] + ("AA" if not good.endswith("AA") else "BB"),  # tampered
        token("user_a", os_billing.PORTAL, START + 900),  # wrong purpose
        pyjwt.encode(
            {"sub": "user_a", "kind": os_billing.CHECKOUT, "exp": START + 900},
            "other-key-0123456789abcdef0123",
            algorithm="HS256",
        ),
        token("user_a", "away_email_unsubscribe", START + 900),  # another token of the same key
    ]
    for t in bad:
        r = await req("GET", "/os/checkout", params={"t": t})
        assert (r.status_code, r.headers["location"]) == (303, page("expired")), t
    r = await req("GET", "/os/portal", params={"t": token("user_a", os_billing.CHECKOUT, START + 900)})
    assert (r.status_code, r.headers["location"]) == (303, page("expired"))
    assert (await req("GET", "/os/checkout")).headers["location"] == page("expired")
    assert fake.calls == []


async def test_link_expires_after_fifteen_minutes(ddb, clock, fake):
    url = (await account())["checkoutUrl"]
    clock[0] += 15 * 60
    assert (await open_link(url)).headers["location"] == page("expired")
    assert fake.calls == []


async def test_checkout_creates_a_session_for_the_person(ddb, clock, fake):
    url = (await account())["checkoutUrl"]
    r = await open_link(url)
    assert (r.status_code, r.headers["location"]) == (303, "https://checkout.stripe.test/s")
    (_, cust), (_, s) = fake.calls
    assert cust["email"] == "user_a@example.com" and cust["metadata"] == {"user_id": "user_a"}
    assert s["mode"] == "subscription" and s["line_items"] == [{"price": PRICE, "quantity": 1}]
    assert s["allow_promotion_codes"] is False and s["customer"] == "cus_1"
    assert s["client_reference_id"] == "user_a"
    assert s["subscription_data"]["metadata"] == {"product": "spark", "user_id": "user_a"}
    assert s["subscription_data"]["trial_end"] == START + 3 * DAY  # the trial's own end
    assert (s["success_url"], s["cancel_url"]) == (page("success"), page("cancelled"))
    assert s["idempotency_key"]
    assert ddb.users.get_item(Key={"user_id": "user_a"})["Item"]["os_stripe_customer_id"] == "cus_1"
    await open_link(url)  # the stored customer is reused
    assert fake.customers == 1 and fake.calls[-1][1]["customer"] == "cus_1"


async def test_one_hour_left_is_never_charged_before_the_trial_ends(ddb, clock, fake):
    await account()
    clock[0] += 3 * DAY - HOUR
    url = (await account())["checkoutUrl"]
    await open_link(url)
    trial_end = fake.calls[-1][1]["subscription_data"]["trial_end"]
    assert trial_end >= START + 3 * DAY and trial_end - clock[0] >= 48 * HOUR  # Stripe's floor, a few free hours


async def test_trial_over_is_charged_now(ddb, clock, fake):
    await account()
    clock[0] += 4 * DAY
    await open_link((await account())["checkoutUrl"])
    assert "trial_end" not in fake.calls[-1][1]["subscription_data"]


async def test_concurrent_opens_store_one_customer(ddb, clock, fake):
    url = (await account())["checkoutUrl"]
    rs = await asyncio.gather(*[open_link(url) for _ in range(3)])
    assert {r.status_code for r in rs} == {303}
    stored = ddb.users.get_item(Key={"user_id": "user_a"})["Item"]["os_stripe_customer_id"]
    assert {kw["customer"] for k, kw in fake.calls if k == "checkout"} == {stored}


async def test_already_subscribed_opens_no_second_checkout(ddb, clock, fake):
    url = (await account())["checkoutUrl"]
    subscribe(ddb)
    r = await open_link(url)
    assert (r.status_code, r.headers["location"]) == (303, page("already"))
    assert fake.calls == []


async def test_portal_with_and_without_a_customer(ddb, clock, fake):
    await account()
    r = await req("GET", "/os/portal", params={"t": token("user_a", os_billing.PORTAL, START + 900)})
    assert (r.status_code, r.headers["location"]) == (303, page("expired")) and fake.calls == []
    subscribe(ddb, status="canceled")
    r = await open_link((await account())["portalUrl"])
    assert (r.status_code, r.headers["location"]) == (303, "https://billing.stripe.test/p")
    assert fake.calls == [
        ("portal", {"customer": "cus_1", "return_url": page("portal"), "api_key": settings.STRIPE_SECRET_KEY})
    ]


async def test_a_token_opens_only_its_own_persons_checkout_and_portal(ddb, clock, fake):
    await account(A), await account(B)
    subscribe(ddb, "user_a", status="canceled", customer="cus_a")
    subscribe(ddb, "user_b", status="canceled", sub="sub_b", customer="cus_b")
    await open_link((await account(B))["checkoutUrl"])
    await open_link((await account(B))["portalUrl"])
    assert [kw["customer"] for _, kw in fake.calls] == ["cus_b", "cus_b"]
    assert fake.calls[0][1]["client_reference_id"] == "user_b"


# --- webhooks ------------------------------------------------------------------------------------------------


@pytest.fixture
def webhook(monkeypatch, fake):
    monkeypatch.setattr(
        webhook_dedup, "record_event_or_skip", AsyncMock(return_value=webhook_dedup.WebhookDedupResult.RECORDED)
    )
    old = AsyncMock(return_value=None)
    for t in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        monkeypatch.setitem(stripe_webhook_handlers.STRIPE_WEBHOOK_HANDLERS, t, old)
    sent: list[dict] = []

    async def send(kind: str, sub: str, status: str, metadata: dict | None = None):
        fake.subs[sub] = status
        event = {
            "id": f"evt_{len(sent)}",
            "type": f"customer.subscription.{kind}",
            "data": {
                "object": {
                    "id": sub,
                    "status": status,
                    "customer": "cus_1",
                    "metadata": metadata if metadata is not None else {"product": "spark", "user_id": "user_a"},
                }
            },
        }
        sent.append(event)
        monkeypatch.setattr(stripe.Webhook, "construct_event", lambda body, sig, secret: event)
        r = await req("POST", "/billing/webhooks/stripe", content=json.dumps(event), headers={"stripe-signature": "x"})
        assert r.status_code == 200, r.text

    return SimpleNamespace(send=send, old=old)


def sub_fields(ddb, user_id="user_a"):
    row = ddb.users.get_item(Key={"user_id": user_id})["Item"]
    return {k: row.get(k) for k in ("os_subscription_id", "os_subscription_status", "os_stripe_customer_id")}


async def test_webhook_sets_the_status_and_never_reaches_the_workspace_handler(ddb, clock, webhook):
    await account()
    await webhook.send("created", "sub_1", "trialing")
    assert sub_fields(ddb) == {
        "os_subscription_id": "sub_1",
        "os_subscription_status": "trialing",
        "os_stripe_customer_id": "cus_1",
    }
    assert (await account())["status"] == "active"
    await webhook.send("updated", "sub_1", "past_due")
    assert sub_fields(ddb)["os_subscription_status"] == "past_due"
    await webhook.send("deleted", "sub_1", "canceled")
    assert sub_fields(ddb)["os_subscription_status"] == "canceled"
    webhook.old.assert_not_awaited()
    assert ddb.billing.scan()["Items"] == []


async def test_other_subscriptions_still_reach_the_existing_handler(ddb, clock, webhook):
    await account()
    await webhook.send("updated", "sub_ws", "active", {"workspace_id": "org_x"})
    await webhook.send("updated", "sub_ws2", "active", {})
    assert webhook.old.await_count == 2
    assert sub_fields(ddb)["os_subscription_id"] is None


async def test_late_events_mirror_stripes_current_state(ddb, clock, webhook, fake, monkeypatch):
    """Every event re-reads the subscription: a late `updated` after `deleted` cannot resurrect it."""
    await account()
    await webhook.send("created", "sub_1", "active")
    await webhook.send("deleted", "sub_1", "canceled")
    late = {
        "id": "evt_late",
        "type": "customer.subscription.updated",
        "data": {
            "object": {
                "id": "sub_1",
                "status": "active",
                "customer": "cus_1",
                "metadata": {"product": "spark", "user_id": "user_a"},
            }
        },
    }
    monkeypatch.setattr(stripe.Webhook, "construct_event", lambda body, sig, secret: late)  # Stripe still says canceled
    r = await req("POST", "/billing/webhooks/stripe", content="{}", headers={"stripe-signature": "x"})
    assert r.status_code == 200
    assert sub_fields(ddb)["os_subscription_status"] == "canceled"


async def test_an_old_subscriptions_late_event_never_replaces_a_live_one(ddb, clock, webhook, fake):
    await account()
    await webhook.send("created", "sub_new", "active")
    await webhook.send("deleted", "sub_old", "canceled")
    assert sub_fields(ddb)["os_subscription_id"] == "sub_new"
    assert sub_fields(ddb)["os_subscription_status"] == "active"


async def test_redelivery_is_idempotent(ddb, clock, webhook):
    await account()
    await webhook.send("created", "sub_1", "active")
    await webhook.send("created", "sub_1", "active")
    assert sub_fields(ddb)["os_subscription_status"] == "active"


async def test_an_event_for_a_person_with_no_spark_account_writes_nothing(ddb, clock, webhook):
    await webhook.send("created", "sub_1", "active", {"product": "spark", "user_id": "user_zz"})
    assert "Item" not in ddb.users.get_item(Key={"user_id": "user_zz"})


# --- account deletion ----------------------------------------------------------------------------------------


async def test_user_deleted_cancels_a_live_spark_subscription(ddb, clock, fake, monkeypatch):
    monkeypatch.setattr("routers.webhooks._verify_svix_signature", lambda body, headers: None)
    monkeypatch.setattr("routers.webhooks._handle_user_deleted_paperclip", AsyncMock())
    monkeypatch.setattr("core.services.workspace_memory.delete_person_memory", AsyncMock())
    monkeypatch.setattr(webhook_dedup, "begin_event_processing", AsyncMock(return_value="claim"))
    monkeypatch.setattr(webhook_dedup, "complete_event_processing", AsyncMock())
    await account(A), await account(B)
    subscribe(ddb, "user_a", status="active")
    subscribe(ddb, "user_b", status="canceled", sub="sub_b")
    fake.subs["sub_1"] = "active"
    for who in ("user_a", "user_b"):
        r = await req(
            "POST",
            "/webhooks/clerk",
            json={"type": "user.deleted", "data": {"id": who, "deleted": True}},
            headers={"svix-id": f"msg_{who}", "svix-timestamp": "1", "svix-signature": "x"},
        )
        assert r.status_code == 200, r.text
    assert [c for c in fake.calls if c[0] == "cancel"] == [("cancel", {"id": "sub_1"})]
