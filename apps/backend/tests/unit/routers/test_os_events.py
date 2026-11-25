"""Spark product events: POST /os/event (the app's allow-listed events) and the server-side events, all
through the PostHog bridge. PostHog is always a fake client; DynamoDB is moto."""

import asyncio
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
TASK_ID = "dlg_mhq2k8x1a3b4c5"
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
    "task_started": {"task_id": TASK_ID, "source": "voice", "talk_kind": "none"},
    "task_completed": {"task_id": TASK_ID, "source": "typed", "seconds_bucket": "lt30"},
    "first_tidy": {"outcome": "already_tidy"},
    "task_failed": {"task_id": TASK_ID, "reason": "unreachable"},
    "analytics_optout_set": {"on": False},
    "onboarding_step": {"step": "say_hi", "action": "skipped"},
    "brain_signin_failed": {"provider": "claude", "reason": "no_plan"},
    "permission_prompted": {"permission": "accessibility"},
    "permission_denied": {"permission": "microphone"},
    "heard_from_answered": {"choice": "hacker_news"},
    "problem_raised": {"kind": "permission.screenRecording"},
    "upgrade_clicked": {},
    "manage_subscription_clicked": {},
    "referral_link_copied": {},
    "update_installed": {"from_version": "2.1.0", "to_version": "2.1.1"},
    "automation_fired": {"ok": True, "brain": False},
    "task_stopped": {"task_id": TASK_ID, "how": "said"},
    "feature_used": {"feature": "dictation"},
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
    body = {
        "event": event,
        "properties": properties or {},
        "installId": install,
        "appVersion": "2.1.1",
        "platform": "mac",
    }
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
    assert {k: v for k, v in c["properties"].items() if k != "$set_once"} == VALID[event] | {
        "app_version": "2.1.1",
        "os_version": "15.1",
        "platform": "mac",
        "install_id": INSTALL,
        "product": "spark",
        "environment": c["properties"]["environment"],
    }


# The Spark engine's EVENTS (packages/engine/src/account/events.ts), event to property names: its test holds the
# same table, so a change on one side fails the other's test.
ENGINE_TABLE = {
    "analytics_optout_set": ["on"],
    "app_first_launch": [],
    "automation_fired": ["brain", "ok"],
    "feature_used": ["feature"],
    "task_stopped": ["how", "task_id"],
    "app_opened": [],
    "brain_chosen": ["provider"],
    "brain_signin_failed": ["provider", "reason"],
    "brain_signin_started": ["needed_download", "provider"],
    "first_conversation": [],
    "first_tidy": ["outcome"],
    "full_access_set": ["on"],
    "heard_from_answered": ["choice"],
    "manage_subscription_clicked": [],
    "onboarding_completed": ["seconds_total", "skipped_sign_in"],
    "onboarding_step": ["action", "step"],
    "permission_denied": ["permission"],
    "permission_granted": ["permission"],
    "permission_prompted": ["permission"],
    "problem_raised": ["kind"],
    "referral_link_copied": [],
    "signin_completed": ["is_new_account", "method"],
    "signin_started": ["method"],
    "task_completed": ["seconds_bucket", "source", "task_id"],
    "task_failed": ["reason", "task_id"],
    "task_started": ["source", "talk_kind", "task_id"],
    "update_installed": ["from_version", "to_version"],
    "upgrade_clicked": [],
}


ENUMS = {  # the canonical values of each new enum; the engine sends exactly these
    ("task_started", "source"): ["voice", "typed", "automation"],
    ("task_started", "talk_kind"): ["hold", "handsFree", "typed", "wakeWord", "none"],
    ("task_completed", "seconds_bucket"): ["lt10", "lt30", "lt120", "lt600", "gte600"],
    ("onboarding_step", "step"): ["sign_in", "brain", "permissions", "say_hi"],
    ("onboarding_step", "action"): ["shown", "completed", "skipped"],
    ("brain_signin_failed", "provider"): ["chatgpt", "claude"],
    ("brain_signin_failed", "reason"): ["signin", "timeout", "download", "offline", "space", "no_plan"],
    ("permission_prompted", "permission"): ["microphone", "accessibility", "screen_recording"],
    ("permission_denied", "permission"): ["microphone", "accessibility", "screen_recording"],
    ("task_stopped", "how"): ["pressed", "said"],
    ("feature_used", "feature"): ["automation_created", "dictation", "marks"],
    ("heard_from_answered", "choice"): [
        "friend_or_invite", "x_twitter", "youtube", "tiktok_reels", "reddit", "hacker_news", "product_hunt",
        "newsletter_or_podcast", "search_or_ai_assistant", "windows_store", "other", "skipped",
    ],
}  # fmt: skip


@pytest.mark.parametrize(("event", "prop"), sorted(ENUMS))
def test_each_new_enum_takes_exactly_its_values(event, prop):
    ok = spark_events.APP_EVENTS[event][prop]
    assert all(ok(v) for v in ENUMS[event, prop])
    assert not any(ok(v) for v in ("", "Voice", "other ", "x", None, 1, True))


@pytest.mark.parametrize("v", ["dlg_mhq2k8x1a3b4c5", "dlg_" + "a" * 10, "dlg_" + "0" * 24])
def test_task_id_accepts_dlg_ids(v):
    assert spark_events.APP_EVENTS["task_started"]["task_id"](v)


@pytest.mark.parametrize(
    "v",
    [
        "dlg_ABC",
        "x y",
        "dlg_abc",
        "dlg_" + "a" * 25,
        "dlg_mhq2k8x1a3\n",
        "DLG_mhq2k8x1a3b4",
        "mhq2k8x1a3b4c5",
        "",
        None,
        5,
    ],
)
def test_task_id_rejects_everything_else(v):
    assert not spark_events.APP_EVENTS["task_started"]["task_id"](v)


@pytest.mark.parametrize("event", ["task_completed", "task_failed"])
async def test_an_old_app_without_task_id_still_sends(event, ph, ddb):
    props = {k: v for k, v in VALID[event].items() if k != "task_id"}
    assert (await send(event, props)).status_code == 204
    assert ph.named(event)[0]["properties"].keys() >= props.keys()
    assert "task_id" not in ph.named(event)[0]["properties"]


async def test_heard_from_sets_the_person_once(ph, ddb):
    await send("heard_from_answered", {"choice": "youtube"})
    assert ph.captures[0]["properties"]["$set_once"] == {"spark_heard_from": "youtube"}


async def test_an_invalid_heard_from_choice_sets_nothing(ph, ddb):
    await send("heard_from_answered", {"choice": "my cousin told me"})
    assert "$set_once" not in ph.captures[0]["properties"]
    await send("app_opened")
    assert "$set_once" not in ph.captures[1]["properties"]


# --- opt-out and channel ---------------------------------------------------------------------------------


@pytest.fixture
def stopped(monkeypatch):
    calls = []

    async def fake(user_id):
        calls.append(user_id)

    async def clear(user_id):
        calls.append(("clear", user_id))

    monkeypatch.setattr(os_voice, "stop_transcripts", fake)
    monkeypatch.setattr(os_voice, "clear_opt_out", clear)
    return calls


async def _settle():
    await asyncio.gather(
        *(t for t in os_voice._BACKGROUND if t.get_loop() is asyncio.get_running_loop())
    )  # another test's loop may have left one behind


async def test_signed_in_optout_stops_transcripts(ph, ddb, stopped):
    assert (await send("analytics_optout_set", {"on": False}, A)).status_code == 204
    await _settle()
    assert stopped == ["user_a"]
    assert ph.named("analytics_optout_set")[0]["properties"]["on"] is False


@pytest.mark.parametrize("who,on", [(None, False), (None, True), (A, "no")])
async def test_other_optout_events_stop_nothing(ph, ddb, stopped, who, on):
    await send("analytics_optout_set", {"on": on}, who)
    await _settle()
    assert stopped == []


async def test_opting_back_in_clears_the_flag(ph, ddb, stopped):
    await send("analytics_optout_set", {"on": True}, A)
    await _settle()
    assert stopped == [("clear", "user_a")]


async def test_an_optout_is_acted_on_even_when_rate_limited(ph, ddb, stopped, monkeypatch):
    monkeypatch.setattr(os_router, "_PER_ID", os_router.RateBucket(0))
    assert (await send("analytics_optout_set", {"on": False}, A)).status_code == 204
    await _settle()
    assert stopped == ["user_a"] and ph.captures == []


async def test_optout_deletion_survives_a_posthog_failure(ph, ddb, stopped):
    ph.fail = True
    await send("analytics_optout_set", {"on": False}, A)
    await _settle()
    assert stopped == ["user_a"]


async def test_a_failing_stop_is_logged_not_raised(ph, ddb, monkeypatch):
    async def boom(user_id):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(os_voice, "stop_transcripts", boom)
    assert (await send("analytics_optout_set", {"on": False}, A)).status_code == 204
    await _settle()


async def test_channel_sets_the_person_once(ph, ddb):
    r = await req("POST", "/os/channel", A, {"channel": "yt-abc"})
    assert r.status_code == 204
    (c,) = ph.named("channel_applied")
    assert c["distinct_id"] == "user_a" and c["properties"]["$set_once"] == {"spark_channel": "yt-abc"}


@pytest.mark.parametrize(
    "body",
    [
        {"channel": "Bad Code!"},
        {"channel": "a"},
        {"channel": "a" * 33},
        {"channel": "ab\n"},
        {"channel": 5},
        {},
        [],
        "x",
    ],
)
async def test_a_bad_channel_is_204_and_sends_nothing(body, ph, ddb):
    assert (await req("POST", "/os/channel", A, body)).status_code == 204
    assert ph.captures == []


async def test_channel_needs_a_session(ph, ddb):
    assert (await req("POST", "/os/channel", None, {"channel": "yt-abc"})).status_code in (401, 403)


def test_the_voice_price_default():
    from core.config import Settings

    assert Settings.model_fields["SPARK_VOICE_USD_PER_MIN"].default == 0.05


async def test_the_allow_list_is_the_one_table():
    assert set(VALID) == set(spark_events.APP_EVENTS)
    assert {e: sorted(r) for e, r in spark_events.APP_EVENTS.items()} == ENGINE_TABLE
    assert all(sorted(VALID[e]) == sorted(r) for e, r in spark_events.APP_EVENTS.items())


@pytest.mark.parametrize("osv", ["10.0.26100", "10.0.22631", "10.0.19045", "26.2", "15.1.1"])
async def test_windows_and_mac_os_versions_pass(osv, ph, ddb):
    await send("app_opened", osVersion=osv, platform="windows")
    p = ph.captures[0]["properties"]
    assert p["os_version"] == osv and p["platform"] == "windows"


async def test_a_bad_platform_is_omitted(ph, ddb):
    await send("app_opened", platform="linux")
    assert "platform" not in ph.captures[0]["properties"]


async def test_unknown_event_is_204_and_sends_nothing(ph, ddb):
    assert (await send("screen_read", {"text": "hi"})).status_code == 204
    assert (await send(42)).status_code == 204
    assert ph.captures == []


async def test_unknown_keys_and_bad_values_are_dropped_and_the_event_still_goes(ph, ddb):
    props = {"method": "apple", "is_new_account": "yes", "extra": 1}
    await send("signin_completed", props)
    await send("signin_started", {"method": "carrier pigeon"})
    await send("onboarding_completed", {"seconds_total": 86401, "skipped_sign_in": 1})
    await send("task_failed", {"reason": "brain_timeout"})
    got = [
        {
            k: v
            for k, v in c["properties"].items()
            if k in ("method", "is_new_account", "extra", "seconds_total", "skipped_sign_in", "reason")
        }
        for c in ph.captures
    ]
    assert got == [{"method": "apple"}, {}, {}, {}]


async def test_retired_referral_code_entered_is_dropped(ph, ddb):
    assert (await send("referral_code_entered")).status_code == 204
    assert ph.captures == []


async def test_bad_versions_are_omitted(ph, ddb):
    await send("app_first_launch", appVersion=SENTENCE, osVersion=3)
    assert {"app_version", "os_version"}.isdisjoint(ph.captures[0]["properties"])


# Free text in every shape: none of it is an enum value or a version.
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
    assert len(ph.aliases) == 11  # the 11th install is not stored, but still joins the person
    await send("task_completed", who=A, install=INSTALL)
    assert len(ph.aliases) == 11  # an install already linked is not aliased again


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


@pytest.mark.parametrize(
    ("event", "props", "kept"),
    [
        ("automation_fired", {"ok": "yes", "brain": True}, {"brain": True}),
        ("automation_fired", {"ok": True, "brain": 1}, {"ok": True}),
        ("automation_fired", {"ok": True, "brain": False, "name": "x"}, {"ok": True, "brain": False}),
        ("automation_fired", {"ok": True}, {"ok": True}),
        ("task_stopped", {"task_id": TASK_ID, "how": "clicked"}, {"task_id": TASK_ID}),
        ("task_stopped", {"task_id": TASK_ID, "how": "said", "why": "x"}, {"task_id": TASK_ID, "how": "said"}),
        ("task_stopped", {"how": "said"}, {"how": "said"}),
        ("feature_used", {"feature": "memory"}, {}),
        ("feature_used", {"feature": "marks", "extra": 1}, {"feature": "marks"}),
        ("feature_used", {}, {}),
    ],
)
def test_new_events_drop_bad_extra_and_missing_properties(event, props, kept):
    assert spark_events.allowed_properties(event, props) == kept


async def test_a_task_stopped_twice_has_one_uuid_per_task(ph, ddb):
    other = "dlg_zzzzzzzzzzzz"
    for tid in (TASK_ID, TASK_ID, other):
        await send("task_stopped", {"task_id": tid, "how": "said"})
    a, b, c = (x["uuid"] for x in ph.captures)
    assert a and a == b and c and c != a


async def test_feature_used_has_one_uuid_per_install_day_and_feature(ph, ddb):
    for f in ("dictation", "dictation", "marks"):
        await send("feature_used", {"feature": f})
    await send("feature_used", {"feature": "dictation"}, install=str(uuid.uuid4()))
    a, b, c, d = (x["uuid"] for x in ph.captures)
    assert a and a == b and c != a and d != a


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
    assert set(c["properties"]["$set"]) == {
        "spark_status",
        "spark_trial_ends_at",
        "spark_referrals",
        "spark_subscription_status",
    }
    assert c["properties"]["$set"]["spark_subscription_status"] == "none"


async def test_referral_captures_both_events(ph, ddb):
    await req("GET", "/os/account", A)
    await req("GET", "/os/account", B)
    r = await req("POST", "/os/referral", B, {"code": "a"})
    assert r.json() == {"applied": True}
    assert [c["distinct_id"] for c in ph.named("referral_applied")] == ["user_a"]
    assert [c["distinct_id"] for c in ph.named("referred_by_applied")] == ["user_b"]
    assert ph.named("referred_by_applied")[0]["properties"]["$set_once"] == {
        "spark_channel": "referral",
        "spark_referrer": "user_a",
    }


async def test_web_signup_referral_events_fire_at_record_and_at_credit(ph, ddb):
    await req("GET", "/os/account", A)
    assert (await req("POST", "/os/referral", B, {"code": "a"})).json() == {"applied": True}
    assert [c["distinct_id"] for c in ph.named("referred_by_applied")] == ["user_b"]
    assert ph.named("referral_applied") == []
    for _ in range(2):
        await req("GET", "/os/account", B)
    assert [c["distinct_id"] for c in ph.named("referral_applied")] == ["user_a"]
    assert len(ph.named("referred_by_applied")) == 1


async def test_voice_session_ended_once(ph, ddb):
    await os_voice.reserve("user_a", "vs_1", "hold", {"brain": "claude", "fullAccess": True}, 1_800_000_000)
    assert await os_voice.end_session("vs_1", "client_closed", 12)
    assert not await os_voice.end_session("vs_1", "client_closed", 12)
    (c,) = ph.named("voice_session_ended")
    assert c["distinct_id"] == "user_a"
    assert {k: c["properties"][k] for k in ("seconds", "kind", "brain", "ended_reason")} == {
        "seconds": 12, "kind": "hold", "brain": "claude", "ended_reason": "client_closed"
    }  # fmt: skip


async def test_voice_session_ended_carries_the_money_facts(ph, ddb):
    client = {"appVersion": "2.1.1", "brain": "chatgpt", "fullAccess": False, "platform": "windows"}
    await os_voice.reserve("user_a", "vs_3", "typed", client, 1_800_000_000, model="gpt-live-1")
    await os_voice.end_session("vs_3", "client_closed", 5)
    p = ph.named("voice_session_ended")[0]["properties"]
    assert (p["platform"], p["app_version"], p["full_access"], p["model"]) == ("windows", "2.1.1", False, "gpt-live-1")


async def test_voice_session_ended_omits_what_is_missing_or_invalid(ph, ddb):
    await os_voice.reserve("user_a", "vs_4", "hold", {"appVersion": SENTENCE[:60]}, 1_800_000_000)
    await os_voice.end_session("vs_4", "client_closed", 5)
    p = ph.named("voice_session_ended")[0]["properties"]
    assert {"platform", "app_version", "full_access", "model"}.isdisjoint(p)


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
        "subscription_started", "subscription_updated", "subscription_updated", "subscription_cancelled",
        "subscription_updated",
    ]  # fmt: skip
    assert [(c["properties"]["status_from"], c["properties"]["status_to"]) for c in ph.named("subscription_updated")] == [
        ("none", "trialing"), ("trialing", "active"), ("active", "canceled")
    ]  # fmt: skip
    assert ph.named("subscription_cancelled")[0]["properties"]["$set"]["spark_status"] in ("trial", "ended")


async def _sync(monkeypatch, sub):
    monkeypatch.setattr(stripe.Subscription, "retrieve", lambda sid, **kw: {"id": sid, "customer": "c"} | sub)
    event = {"data": {"object": {"id": "sub_1", "metadata": {"product": "spark", "user_id": "user_a"}}}}
    await os_billing.sync_subscription(event)


async def test_trialing_to_active_sends_subscription_updated(ph, ddb, monkeypatch):
    await req("GET", "/os/account", A)
    await _sync(monkeypatch, {"status": "trialing"})
    await _sync(monkeypatch, {"status": "active"})
    (_, c) = ph.named("subscription_updated")
    assert (c["properties"]["status_from"], c["properties"]["status_to"]) == ("trialing", "active")
    assert c["properties"]["$set"]["spark_subscription_status"] == "active"


async def test_flapping_statuses_are_each_sent(ph, ddb, monkeypatch):
    await req("GET", "/os/account", A)
    for st in ("active", "past_due", "active", "past_due"):
        await _sync(monkeypatch, {"status": st})
    assert [c["properties"]["status_to"] for c in ph.named("subscription_updated")] == [
        "active", "past_due", "active", "past_due"
    ]  # fmt: skip
    assert len({c["uuid"] for c in ph.named("subscription_updated")}) == 4


async def test_an_unknown_status_becomes_other(ph, ddb, monkeypatch):
    await req("GET", "/os/account", A)
    await _sync(monkeypatch, {"status": "active"})
    await _sync(monkeypatch, {"status": "frobnicated"})
    assert ph.named("subscription_updated")[-1]["properties"]["status_to"] == "other"


async def test_cancel_carries_reason_and_feedback(ph, ddb, monkeypatch):
    await req("GET", "/os/account", A)
    await _sync(monkeypatch, {"status": "active"})
    details = {"reason": "cancellation_requested", "feedback": "too_expensive", "comment": "I hate Paris"}
    await _sync(monkeypatch, {"status": "canceled", "cancellation_details": details})
    p = ph.named("subscription_cancelled")[0]["properties"]
    assert (p["reason"], p["feedback"]) == ("cancellation_requested", "too_expensive")
    assert "I hate Paris" not in json.dumps(ph.captures)


@pytest.mark.parametrize(
    "details", [{"reason": "sad", "feedback": "meh"}, {"reason": None, "feedback": None}, None, {}, {"feedback": 3}]
)
async def test_unknown_cancel_details_are_omitted(ph, ddb, monkeypatch, details):
    await req("GET", "/os/account", A)
    await _sync(monkeypatch, {"status": "active"})
    await _sync(monkeypatch, {"status": "canceled", "cancellation_details": details})
    p = ph.named("subscription_cancelled")[0]["properties"]
    assert "reason" not in p and "feedback" not in p


async def test_subscription_status_fact_is_the_raw_stripe_status(ph, ddb, monkeypatch):
    await req("GET", "/os/account", A)
    await _sync(monkeypatch, {"status": "past_due"})
    assert ph.named("subscription_updated")[0]["properties"]["$set"]["spark_subscription_status"] == "past_due"


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
