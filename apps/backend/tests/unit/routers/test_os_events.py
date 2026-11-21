"""Spark product events: POST /os/event (the app's allow-listed events) and the server-side events, all
through the PostHog bridge. PostHog is always a fake client; DynamoDB is moto."""

import json
import uuid

import boto3
import pytest
import stripe
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from core import dynamodb
from core.observability import product_analytics, spark_events
from core.services import os_billing
from core.voice import os_voice
from main import app
from routers import os as os_router
from tests.unit.auth.conftest import Tenant, jwks  # noqa: F401

A, B = Tenant("user_a", None), Tenant("user_b", None)
INSTALL = str(uuid.uuid4())
SENTENCE = "please book my flight to Paris and email the file report.pdf from https://example.com/x"
VALID = {
    "app_first_launch": {},
    "app_opened": {},
    "signin_started": {"method": "apple"},
    "signin_completed": {"method": "google", "is_new_account": True},
    "brain_signin_started": {"provider": "chatgpt", "needed_download": False},
    "brain_chosen": {"provider": "claude"},
    "permission_granted": {"permission": "screen_recording"},
    "full_access_set": {"on": True},
    "onboarding_completed": {"skipped_sign_in": False, "seconds_total": 120},
    "first_conversation": {},
    "task_completed": {},
    "task_failed": {"reason": "brain_timeout"},
    "upgrade_clicked": {},
    "manage_subscription_clicked": {},
    "referral_link_copied": {},
    "referral_code_entered": {},
    "update_installed": {"from_version": "2.1.0", "to_version": "2.1.1"},
}


class FakePostHog:
    def __init__(self):
        self.captures: list[dict] = []
        self.aliases: list[tuple[str, str]] = []
        self.fail = False

    def capture(self, distinct_id, event, properties, uuid=None):
        if self.fail:
            raise RuntimeError("posthog down")
        self.captures.append({"distinct_id": distinct_id, "event": event, "properties": properties, "uuid": uuid})

    def alias(self, previous_id, distinct_id):
        if self.fail:
            raise RuntimeError("posthog down")
        self.aliases.append((previous_id, distinct_id))

    def named(self, event):
        return [c for c in self.captures if c["event"] == event]


@pytest.fixture
def ph(monkeypatch):
    fake = FakePostHog()
    monkeypatch.setattr(product_analytics, "_client_or_none", lambda: fake)
    os_router._PER_ID.clear()
    os_router._PER_IP.clear()
    return fake


@pytest.fixture
def ddb(monkeypatch, jwks):  # noqa: F811
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=dynamodb.table_name("users"),
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName=dynamodb.table_name("containers"),
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield resource.Table(dynamodb.table_name("users"))


async def req(method, path, who=None, body=None, content=None, headers=None):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        h = (who.headers if who else {}) | (headers or {})
        return await c.request(method, f"/api/v1{path}", headers=h, json=body, content=content)


async def send(event, properties=None, who=None, install=INSTALL, **extra):
    body = {"event": event, "properties": properties or {}, "installId": install, "appVersion": "2.1.1"}
    body |= {"osVersion": "15.1"} | extra
    return await req("POST", "/os/event", who, body)


# --- POST /os/event ------------------------------------------------------------------------------------


@pytest.mark.parametrize("signed_in", [False, True])
@pytest.mark.parametrize("event", sorted(VALID))
async def test_each_allowed_event_is_captured(event, signed_in, ph, ddb):
    r = await send(event, VALID[event], A if signed_in else None)
    assert r.status_code == 204, r.text
    (c,) = ph.captures
    assert c["event"] == event
    assert c["distinct_id"] == ("user_a" if signed_in else INSTALL)
    assert c["properties"] == VALID[event] | {
        "app_version": "2.1.1",
        "os_version": "15.1",
        "install_id": INSTALL,
        "product": "spark",
        "environment": c["properties"]["environment"],
    }


async def test_the_allow_list_is_the_one_table():
    assert set(VALID) == set(spark_events.APP_EVENTS)


async def test_unknown_event_is_204_and_sends_nothing(ph, ddb):
    assert (await send("screen_read", {"text": "hi"})).status_code == 204
    assert (await send(42)).status_code == 204
    assert ph.captures == []


async def test_unknown_keys_and_bad_values_are_dropped_and_the_event_still_goes(ph, ddb):
    props = {"method": "apple", "is_new_account": "yes", "extra": 1}
    await send("signin_completed", props)
    await send("signin_started", {"method": "carrier pigeon"})
    await send("onboarding_completed", {"seconds_total": 86401, "skipped_sign_in": 1})
    await send("task_failed", {"reason": "Bad Reason"})
    got = [
        {
            k: v
            for k, v in c["properties"].items()
            if k in ("method", "is_new_account", "extra", "seconds_total", "skipped_sign_in", "reason")
        }
        for c in ph.captures
    ]
    assert got == [{"method": "apple"}, {}, {}, {}]


async def test_bad_versions_are_omitted(ph, ddb):
    await send("app_first_launch", appVersion=SENTENCE, osVersion=3)
    assert {"app_version", "os_version"}.isdisjoint(ph.captures[0]["properties"])


# Letters-only text is mixed case: `task_failed.reason` takes lowercase snake_case codes until the fixed list.
SHORT = ["call my wife at 5 about rent", "rent due friday", "CallMyWife", "2.1 call wife", "1.2.3 beta"]


@pytest.mark.parametrize("text", [SENTENCE, *SHORT])
@pytest.mark.parametrize("event", sorted(VALID))
async def test_no_free_text_reaches_posthog(event, text, ph, ddb):
    props = dict.fromkeys(spark_events.APP_EVENTS[event], text) | {"note": text}
    r = await send(event, props, appVersion=text, osVersion=text)
    assert r.status_code == 204
    assert [c["event"] for c in ph.captures] == [event]
    assert text not in json.dumps(ph.captures + ph.aliases)


@pytest.mark.parametrize("version", ["2.1.1", "15.1", "26.0", "2.1.2-beta.1", "1+build.7"])
async def test_numeric_versions_pass(version, ph, ddb):
    await send("update_installed", {"from_version": version, "to_version": version}, appVersion=version)
    p = ph.captures[0]["properties"]
    assert p["app_version"] == p["from_version"] == p["to_version"] == version


async def test_a_bad_bearer_is_anonymous(ph, ddb):
    for headers in ({"Authorization": "Bearer not-a-jwt"}, Tenant("user_a", None, token="template").headers):
        r = await req("POST", "/os/event", body={"event": "app_first_launch", "installId": INSTALL}, headers=headers)
        assert r.status_code == 204
    assert [c["distinct_id"] for c in ph.captures] == [INSTALL, INSTALL]
    assert ph.aliases == []


@pytest.mark.parametrize(
    "body", [[], "x", {"event": "app_opened"}, {"event": "app_opened", "installId": "nope"}, {"installId": 5}]
)
async def test_missing_or_bad_install_id_is_400(body, ph, ddb):
    r = await req("POST", "/os/event", body=body)
    assert r.status_code == 400 and r.json() == {"detail": "invalid_event"}
    assert ph.captures == []


@pytest.mark.parametrize("raw", [b"{", b"[" * 4000])
async def test_not_json_is_400(raw, ph, ddb):
    r = await req("POST", "/os/event", content=raw, headers={"content-type": "application/json"})
    assert r.status_code == 400


async def test_over_4kb_is_413(ph, ddb):
    r = await send("app_first_launch", {"pad": "x" * 5000})
    assert r.status_code == 413
    assert ph.captures == []


async def test_rate_limit_per_distinct_id(ph, ddb):
    other = str(uuid.uuid4())
    for _ in range(61):
        assert (await send("task_completed")).status_code == 204
    assert (await send("task_completed", install=other)).status_code == 204
    assert len([c for c in ph.captures if c["distinct_id"] == INSTALL]) == 60
    assert len([c for c in ph.captures if c["distinct_id"] == other]) == 1


async def test_rate_limit_per_ip_for_anonymous(ph, ddb, monkeypatch):
    monkeypatch.setattr(os_router, "_PER_IP", os_router.RateBucket(3))
    for _ in range(4):
        await send("task_completed", install=str(uuid.uuid4()))
    assert len(ph.captures) == 3


async def test_alias_once_per_install_and_at_most_ten_installs(ph, ddb):
    await req("GET", "/os/account", A)
    await send("app_opened", who=A)
    await send("app_opened", who=A)
    assert ph.aliases == [("user_a", INSTALL)]
    installs = [str(uuid.uuid4()) for _ in range(10)]
    for i in installs:
        await send("task_completed", who=A, install=i)
    row = ddb.get_item(Key={"user_id": "user_a"})["Item"]
    assert len(row["os_install_ids"]) == 10 and installs[-1] not in row["os_install_ids"]
    assert len(ph.aliases) == 10


async def test_signed_in_before_an_account_writes_no_row_and_no_alias(ph, ddb):
    await send("signin_completed", {"method": "email"}, who=B)
    assert ph.aliases == [] and "Item" not in ddb.get_item(Key={"user_id": "user_b"})
    assert ph.captures[0]["distinct_id"] == "user_b"


async def test_app_opened_twice_a_day_has_one_uuid(ph, ddb):
    await send("app_opened")
    await send("app_opened")
    await send("task_completed")
    uuids = [c["uuid"] for c in ph.captures]
    assert uuids[0] and uuids[0] == uuids[1] and uuids[2] is None


async def test_posthog_failure_is_still_204(ph, ddb):
    await req("GET", "/os/account", A)
    ph.fail = True
    assert (await send("app_opened", who=A)).status_code == 204


# --- server events ---------------------------------------------------------------------------------------


async def test_account_created_once(ph, ddb):
    for _ in range(2):
        assert (await req("GET", "/os/account", A)).status_code == 200
    (c,) = ph.named("account_created")
    assert c["distinct_id"] == "user_a" and c["properties"]["product"] == "spark" and c["uuid"]
    assert set(c["properties"]["$set"]) == {"spark_status", "spark_trial_ends_at", "spark_referrals"}


async def test_referral_captures_both_events(ph, ddb):
    await req("GET", "/os/account", A)
    await req("GET", "/os/account", B)
    r = await req("POST", "/os/referral", B, {"code": "a"})
    assert r.json() == {"applied": True}
    assert [c["distinct_id"] for c in ph.named("referral_applied")] == ["user_a"]
    assert [c["distinct_id"] for c in ph.named("referred_by_applied")] == ["user_b"]


async def test_voice_session_ended_once(ph, ddb):
    await os_voice.reserve("user_a", "vs_1", "hold", {"brain": "claude", "fullAccess": True}, 1_800_000_000)
    assert await os_voice.end_session("vs_1", "client_closed", 12)
    assert not await os_voice.end_session("vs_1", "client_closed", 12)
    (c,) = ph.named("voice_session_ended")
    assert c["distinct_id"] == "user_a"
    assert {k: c["properties"][k] for k in ("seconds", "kind", "brain", "ended_reason")} == {
        "seconds": 12, "kind": "hold", "brain": "claude", "ended_reason": "client_closed"
    }  # fmt: skip


async def test_subscription_events_once_each_under_redelivery(ph, ddb, monkeypatch):
    await req("GET", "/os/account", A)
    status = {"sub_1": "trialing"}
    monkeypatch.setattr(
        stripe.Subscription, "retrieve", lambda sid, **kw: {"id": sid, "status": status[sid], "customer": "cus_1"}
    )
    monkeypatch.setattr(os_billing, "extend_trial", lambda user_id: _noop())
    event = {"data": {"object": {"id": "sub_1", "metadata": {"product": "spark", "user_id": "user_a"}}}}
    for _ in range(2):
        await os_billing.sync_subscription(event)
    status["sub_1"] = "active"  # trialing to active is not a new start
    await os_billing.sync_subscription(event)
    status["sub_1"] = "canceled"
    for _ in range(2):
        await os_billing.sync_subscription(event)
    assert [c["event"] for c in ph.captures if c["event"] != "account_created"] == [
        "subscription_started", "subscription_cancelled"
    ]  # fmt: skip
    assert ph.named("subscription_cancelled")[0]["properties"]["$set"]["spark_status"] in ("trial", "ended")


async def test_past_due_and_back_is_not_a_new_start(ph, ddb, monkeypatch):
    await req("GET", "/os/account", A)
    status = {"sub_1": "active"}
    monkeypatch.setattr(
        stripe.Subscription, "retrieve", lambda sid, **kw: {"id": sid, "status": status[sid], "customer": "c"}
    )
    event = {"data": {"object": {"id": "sub_1", "metadata": {"product": "spark", "user_id": "user_a"}}}}
    for s in ("active", "past_due", "active"):
        status["sub_1"] = s
        await os_billing.sync_subscription(event)
    assert len(ph.named("subscription_started")) == 1 and ph.named("subscription_cancelled") == []


async def test_facts_raising_fails_nothing(ph, ddb, monkeypatch):
    def boom(row, now):
        raise RuntimeError("facts")

    monkeypatch.setattr(spark_events, "facts", boom)
    monkeypatch.setattr(
        stripe.Subscription, "retrieve", lambda sid, **kw: {"id": sid, "status": "active", "customer": "c"}
    )
    assert (await req("GET", "/os/account", A)).status_code == 200
    await os_billing.sync_subscription({"data": {"object": {"id": "sub_1", "metadata": {"user_id": "user_a"}}}})
    assert ddb.get_item(Key={"user_id": "user_a"})["Item"]["os_subscription_status"] == "active"


async def _noop():
    return None


async def test_posthog_failure_never_fails_server_paths(ph, ddb, monkeypatch):
    def boom(**kw):
        raise RuntimeError("bridge down")

    monkeypatch.setattr(product_analytics, "capture_product_event", boom)
    assert (await req("GET", "/os/account", A)).status_code == 200
    assert (await req("GET", "/os/account", B)).status_code == 200
    assert (await req("POST", "/os/referral", B, {"code": "a"})).json() == {"applied": True}
    await os_voice.reserve("user_a", "vs_2", "hold", {}, 1_800_000_000)
    assert await os_voice.end_session("vs_2", "expired", 1)
    monkeypatch.setattr(
        stripe.Subscription, "retrieve", lambda sid, **kw: {"id": sid, "status": "active", "customer": "c"}
    )
    await os_billing.sync_subscription({"data": {"object": {"id": "sub_9", "metadata": {"user_id": "user_a"}}}})
    assert (await send("app_opened", who=A)).status_code == 204
