"""Spark data store, write path: Firehose is a fake client, DynamoDB is moto; no AWS is ever called."""

import asyncio
import json
import logging

import boto3
import pytest
from moto import mock_aws

from core import dynamodb
from core.config import settings
from core.observability import spark_store
from core.voice import os_voice


class FakeFirehose:
    """put_record_batch like boto3's; `fail_first` = indexes of the first call's records to reject."""

    def __init__(self, fail_first=(), raises=None):
        self.calls: list[list[dict]] = []
        self.fail_first = set(fail_first)
        self.raises = raises

    def put_record_batch(self, DeliveryStreamName, Records):  # noqa: N803 -- boto3's names
        if self.raises:
            raise self.raises
        self.calls.append(Records)
        failing = self.fail_first if len(self.calls) == 1 else set()
        return {
            "FailedPutCount": len(failing),
            "RequestResponses": [{"ErrorCode": "ServiceUnavailableException"} if i in failing else {"RecordId": "r"} for i in range(len(Records))],
        }  # fmt: skip


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(spark_store, "RETRY_S", 0)
    spark_store._cache.clear()
    spark_store._refreshing.clear()
    spark_store._forgotten.clear()


@pytest.fixture
def fh(monkeypatch):
    fake = FakeFirehose()
    monkeypatch.setattr(spark_store, "_firehose", fake)
    return fake


def row(i=0, **kw):
    return {"row_id": f"r{i}", "ts": 1_732_500_000_123_456, "user_id": "user_a", "text": f"t{i}"} | kw


def sent(call):
    return [json.loads(r["Data"]) for r in call]


# --- bucket --------------------------------------------------------------------------------------------

# Computed by Python from the plan's formula; a later check compares them with Athena's SQL expression.
PINNED = {
    "user_a": 31,
    "user_2abcDEF123": 23,
    "00000000-0000-0000-0000-000000000000": 60,
    "user_ünï": 21,
    "x": 2,
}


@pytest.mark.parametrize(("uid", "want"), PINNED.items())
def test_bucket_is_pinned(uid, want):
    assert spark_store.bucket(uid) == want


def test_bucket_none_is_minus_one_and_all_in_range():
    assert spark_store.bucket(None) == -1
    assert {spark_store.bucket(f"user_{i}") for i in range(500)} <= set(range(64))


# --- _put ----------------------------------------------------------------------------------------------


async def test_one_row_per_record_newline_json_with_derived_columns(fh):
    await spark_store.put_turns([row()])
    (call,) = fh.calls
    (rec,) = call
    assert rec["Data"].endswith(b"\n") and rec["Data"].count(b"\n") == 1
    got = json.loads(rec["Data"])
    assert got == row() | {"dt": "2024-11-25", "user_bucket": 31}
    assert isinstance(got["ts"], int)  # epoch microseconds


async def test_1201_rows_go_in_500_500_201(fh):
    await spark_store.put_turns([row(i) for i in range(1201)])
    assert [len(c) for c in fh.calls] == [500, 500, 201]


async def test_a_batch_is_split_at_4_mib(fh):
    big = "x" * 1_500_000
    await spark_store.put_turns([row(i, text=big) for i in range(3)])
    assert [len(c) for c in fh.calls] == [2, 1]


async def test_streams_match_the_infra_names(monkeypatch):
    names = []

    class Spy(FakeFirehose):
        def put_record_batch(self, DeliveryStreamName, Records):  # noqa: N803
            names.append(DeliveryStreamName)
            return super().put_record_batch(DeliveryStreamName, Records)

    monkeypatch.setattr(spark_store, "_firehose", Spy())
    await spark_store.put_turns([row()])
    await spark_store.put_steps([row()])
    await spark_store._put("events", [row()])
    assert names == ["lightbulb-dev-spark-turns", "lightbulb-dev-spark-steps", "lightbulb-dev-spark-events"]


async def test_only_the_failed_records_are_retried_once(fh, monkeypatch):
    seen = []
    monkeypatch.setattr(spark_store, "put_metric", lambda name, value=1.0, **kw: seen.append((name, value)))
    fh.fail_first = {1, 3}
    await spark_store.put_turns([row(i) for i in range(5)])
    assert [r["row_id"] for r in sent(fh.calls[1])] == ["r1", "r3"] and len(fh.calls) == 2
    assert seen == []


async def test_a_record_that_fails_twice_is_counted_and_logged_without_text(fh, monkeypatch, caplog):
    seen = []
    monkeypatch.setattr(spark_store, "put_metric", lambda name, value=1.0, **kw: seen.append((name, value)))

    class Always(FakeFirehose):
        def put_record_batch(self, DeliveryStreamName, Records):  # noqa: N803
            self.calls.append(Records)
            return {"FailedPutCount": 1, "RequestResponses": [{"ErrorCode": "Boom"}] + [{}] * (len(Records) - 1)}

    monkeypatch.setattr(spark_store, "_firehose", Always())
    with caplog.at_level(logging.WARNING):
        await spark_store.put_turns([row(0, text="secret words"), row(1)])
    assert seen == [("spark_store.put_failed", 1)]
    assert "secret words" not in caplog.text and "user_a" not in caplog.text and "Boom" in caplog.text


async def test_never_raises_when_firehose_is_down(monkeypatch, caplog):
    seen = []
    monkeypatch.setattr(spark_store, "put_metric", lambda name, value=1.0, **kw: seen.append((name, value)))
    monkeypatch.setattr(spark_store, "_firehose", FakeFirehose(raises=RuntimeError("down: secret words")))
    with caplog.at_level(logging.WARNING):
        await spark_store.put_turns([row(0, text="secret words"), row(1)])
    assert seen == [("spark_store.put_failed", 2)]
    assert "secret words" not in caplog.text


async def test_a_bad_row_never_raises(fh):
    await spark_store.put_turns([{"user_id": "u"}, object()])  # no ts, not a dict


@pytest.mark.parametrize(("flag", "env"), [(False, "dev"), (True, "local"), (True, ""), (True, "staging")])
async def test_off_is_a_no_op(fh, monkeypatch, flag, env):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", flag)
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    await spark_store.put_turns([row()])
    await spark_store.put_steps([row()])
    assert fh.calls == []


async def test_too_much_pending_work_is_dropped_not_queued(fh, monkeypatch):
    monkeypatch.setattr(spark_store, "_pending", spark_store.MAX_PENDING)
    await spark_store.put_turns([row()])
    assert fh.calls == []


# --- put_event ------------------------------------------------------------------------------------------


async def test_put_event_schedules_off_the_caller(fh, monkeypatch):
    monkeypatch.setattr(spark_store, "allowed", _yes)
    spark_store.put_event("user_a", "user_a", "inst", "task_started", {"a": 1}, event_uuid="u-1")
    assert fh.calls == []  # nothing ran on the caller's stack
    await settle()
    (got,) = sent(fh.calls[0])
    assert got["row_id"] == "u-1" and got["event"] == "task_started" and got["user_id"] == "user_a"
    assert got["install_id"] == "inst" and json.loads(got["properties"]) == {"a": 1}
    assert got["distinct_id"] == "user_a" and got["user_bucket"] == 31


async def settle():
    await asyncio.gather(*(t for t in os_voice._BACKGROUND if t.get_loop() is asyncio.get_running_loop()))


async def _yes(uid):
    return True


async def test_put_event_without_a_user_skips_the_opt_out_read(fh, monkeypatch):
    async def boom(uid):
        raise AssertionError("read")

    monkeypatch.setattr(spark_store, "allowed", boom)
    spark_store.put_event(
        "inst", None, "inst", "app_opened", {"environment": "dev", "platform": "mac", "app_version": "2"}
    )
    await settle()
    (got,) = sent(fh.calls[0])
    assert got["user_id"] is None and got["user_bucket"] == -1
    assert (got["platform"], got["app_version"], got["environment"]) == ("mac", "2", "dev")


async def test_put_event_for_an_opted_out_user_writes_nothing(fh, monkeypatch):
    async def no(uid):
        return False

    monkeypatch.setattr(spark_store, "allowed", no)
    spark_store.put_event("user_a", "user_a", None, "x", {})
    await settle()
    assert fh.calls == []


def test_put_event_with_no_running_loop_is_dropped_silently(fh):
    spark_store.put_event("user_a", "user_a", None, "x", {})  # no loop: must not raise
    assert fh.calls == []


async def test_put_event_is_a_no_op_when_off(fh, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", False)
    spawned = []
    monkeypatch.setattr(os_voice, "spawn", spawned.append)
    spark_store.put_event("user_a", "user_a", None, "x", {})
    assert spawned == []


async def test_a_burst_of_events_over_the_cap_reads_nothing(fh, monkeypatch):
    reads = []

    async def allowed(uid):
        reads.append(uid)
        return True

    monkeypatch.setattr(spark_store, "allowed", allowed)
    monkeypatch.setattr(spark_store, "_pending", spark_store.MAX_PENDING)
    spark_store.put_event("user_a", "user_a", None, "x", {})
    await settle()
    assert reads == [] and fh.calls == []
    assert spark_store._pending == spark_store.MAX_PENDING  # a dropped event leaves the count alone


async def test_a_scheduled_event_counts_as_pending_until_it_finishes(fh, monkeypatch):
    seen = []

    async def allowed(uid):
        seen.append(spark_store._pending)
        return True

    monkeypatch.setattr(spark_store, "allowed", allowed)
    spark_store.put_event("user_a", "user_a", None, "x", {})
    assert spark_store._pending == 1  # from the moment it is scheduled
    await settle()
    assert seen == [1] and spark_store._pending == 0


# --- allowed -------------------------------------------------------------------------------------------


@pytest.fixture
def users(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        t = resource.create_table(
            TableName=dynamodb.table_name("users"),
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield t


async def test_allowed_cases(users):
    users.put_item(Item={"user_id": "ok"})
    users.put_item(Item={"user_id": "off", "os_analytics_off": True})
    users.put_item(Item={"user_id": "gone", "os_deleted_at": 123})
    assert [await spark_store.allowed(u) for u in ("ok", "off", "gone")] == [True, False, False]


async def test_a_user_with_no_row_is_not_allowed(users):
    assert await spark_store.allowed("nobody") is False


async def test_a_read_error_fails_closed(monkeypatch):
    def boom(name):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(spark_store, "get_table", boom)
    assert await spark_store.allowed("user_a") is False


async def test_allowed_is_cached_for_60_seconds_and_forget_locally_is_immediate(users, monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(spark_store.time, "monotonic", lambda: now[0])
    users.put_item(Item={"user_id": "u"})
    assert await spark_store.allowed("u") is True
    users.put_item(Item={"user_id": "u", "os_analytics_off": True})
    now[0] += 59
    assert await spark_store.allowed("u") is True  # cached
    now[0] += 2
    assert await spark_store.allowed("u") is False  # expired, re-read
    users.put_item(Item={"user_id": "v"})
    assert await spark_store.allowed("v") is True
    spark_store.forget_locally("v")
    assert await spark_store.allowed("v") is False


# --- allowed_now: the sideband's non-blocking read of the same answer ------------------------------------------


class Reads:
    """A stand-in for run_in_thread: each read waits on `gate`, then answers with `item` (or raises `error`)."""

    def __init__(self, item=None, error=None, hang=False):
        self.gate, self.item, self.error, self.hang, self.count = asyncio.Event(), item, error, hang, 0

    async def __call__(self, fn, *a, **kw):
        self.count += 1
        if self.hang:
            await asyncio.Event().wait()
        await self.gate.wait()
        if self.error:
            raise self.error
        return {"Item": self.item}


def reads(monkeypatch, **kw):
    r = Reads(**kw)
    monkeypatch.setattr(spark_store, "run_in_thread", r)
    monkeypatch.setattr(spark_store, "get_table", lambda name: type("T", (), {"get_item": None})())
    return r


async def test_an_expired_cache_returns_false_at_once_and_schedules_one_refresh(monkeypatch):
    r = reads(monkeypatch, item={"user_id": "u"})
    assert spark_store.allowed_now("u") is False  # no answer yet: off, without awaiting anything
    assert spark_store.allowed_now("u") is False  # the next tick while the refresh is in flight
    await asyncio.sleep(0)
    assert r.count == 1  # exactly one read
    r.gate.set()
    await settle()
    assert spark_store.allowed_now("u") is True  # the answer is used once it arrives
    assert r.count == 1


async def test_a_stale_answer_is_kept_while_it_refreshes(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(spark_store.time, "monotonic", lambda: now[0])
    r = reads(monkeypatch, item={"user_id": "u", "os_analytics_off": True})
    spark_store._cache["u"] = (now[0] + 60, True)
    now[0] += 61
    assert spark_store.allowed_now("u") is True  # last known answer
    r.gate.set()
    await settle()
    assert spark_store.allowed_now("u") is False  # the refresh found the opt-out


@pytest.mark.parametrize("how", ["error", "timeout"])
async def test_a_failed_or_slow_refresh_leaves_storage_off(monkeypatch, how):
    monkeypatch.setattr(spark_store, "READ_TIMEOUT_S", 0.01)
    r = reads(monkeypatch, error=RuntimeError("ddb down"), hang=how == "timeout")
    r.gate.set()
    assert spark_store.allowed_now("u") is False
    await settle()
    assert spark_store.allowed_now("u") is False and "u" not in spark_store._cache
    await settle()
    assert r.count == 2  # nothing is cached or stuck in flight: the next tick asks again


async def test_an_opt_out_seen_locally_wins_over_a_late_refresh(monkeypatch):
    r = reads(monkeypatch, item={"user_id": "u"})
    assert spark_store.allowed_now("u") is False
    await asyncio.sleep(0)
    spark_store.forget_locally("u")
    r.gate.set()
    await settle()
    assert spark_store.allowed_now("u") is False
    assert await spark_store.allowed("u") is False  # the cached False, not the late True


# --- scrub ---------------------------------------------------------------------------------------------

# Built from pieces at runtime so no line here looks like a credential to the repo's secret scanner.
SHAPES = {
    "private key": "-----BEGIN " + "RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK\n-----END " + "RSA PRIVATE KEY-----",
    "openai": "s" + "k-proj-" + "abcdefghij1234567890ABCD",
    "github": "gh" + "p_" + "a1B2c3D4e5" * 4,
    "github fine": "github" + "_pat_" + "A1b2C3d4E5" * 3,
    "aws": "AK" + "IA" + "X" * 16,
    "slack": "xo" + "xb-" + "1234567890-abcdefghij",
    "google": "AI" + "za" + "Sy" + "a" * 33,
    "jwt": ".".join(["ey" + "JhbGciOiJIUzI1NiJ9", "ey" + "JzdWIiOiIxMjM0NTYifQ", "SflKxwRJSMeKKF2QT4fwpMeJf36POk6"]),
    "gitlab": "gl" + "pat-" + "abcdefghij1234567890",
    "npm": "np" + "m_" + "a" * 36,
    "pypi": "py" + "pi-" + "AgEIcHlwaS5vcmcCJGFiY2RlZmdoaWo",
    "hf": "h" + "f_" + "a" * 34,
    "stripe": "s" + "k_li" + "ve_" + "abcdefghij1234567890",
    "stripe restricted": "r" + "k_li" + "ve_" + "abcdefghij1234567890",
}


@pytest.mark.parametrize("secret", SHAPES.values(), ids=SHAPES.keys())
def test_each_secret_shape_is_masked(secret):
    out = spark_store.scrub(f"use {secret} for the login")
    assert out == "use [redacted] for the login"


def test_a_luhn_valid_card_is_masked_in_every_spelling():
    for card in ("4242424242424242", "4242 4242 4242 4242", "4242-4242-4242-4242", "378282246310005"):
        assert spark_store.scrub(f"pay with {card} please") == "pay with [card] please"


def test_a_sixteen_digit_order_number_that_fails_luhn_is_left_alone():
    assert spark_store.scrub("order 1234567812345678 shipped") == "order 1234567812345678 shipped"


def test_one_time_codes_keep_the_word_and_lose_the_digits():
    assert spark_store.scrub("your code is 482913") == "your code is [code]"
    assert spark_store.scrub("code: 482913") == "code: [code]"
    assert spark_store.scrub("code=482913") == "code=[code]"
    assert spark_store.scrub("passcode 482913") == "passcode [code]"
    assert spark_store.scrub("PIN is 1234") == "PIN is [code]"
    assert spark_store.scrub("OTP: 1234") == "OTP: [code]"
    assert spark_store.scrub("the 2FA code 123456 expires") == "the 2FA code [code] expires"
    assert spark_store.scrub("the code was 12") == "the code was 12"  # too short to be a code
    assert spark_store.scrub("code 123456789") == "code 123456789"  # too long


def test_labelled_passwords_are_masked_written_or_spoken():
    assert "hunter2" not in spark_store.scrub("password: hunter2")
    assert spark_store.scrub("password: hunter2") == "password: [secret]"
    assert spark_store.scrub("my password is hunter2, ok") == "my password: [secret] ok"
    assert spark_store.scrub("PWD=abc123 done") == "PWD: [secret] done"


def test_plain_text_is_untouched():
    text = "book a flight to Paris on 2026-12-01 and email report.pdf"
    assert spark_store.scrub(text) == text


@pytest.mark.parametrize(
    "text",
    ["zip code 94110", "area code 415 555", "postal code 94110", "country code 1234", "the code was written in 2019",
     "pin it at 1500", "the code was 12"],
)  # fmt: skip
def test_ordinary_sentences_with_a_keyword_are_left_alone(text):
    assert spark_store.scrub(text) == text


def test_a_us_ssn_is_masked():
    assert spark_store.scrub("ssn 123-45-6789 ok") == "ssn [ssn] ok"
    assert spark_store.scrub("call 555-12-34567") == "call 555-12-34567"


def test_scrub_cuts_its_input_before_any_pattern_runs():
    text = "a" * 16_000 + " gh" + "p_" + "a1B2c3D4e5" * 4
    assert spark_store.scrub(text) == "a" * 16_000
    long = ("-----BEGIN " + "PRIVATE KEY-----") * 2000  # no END: must stay fast
    assert len(spark_store.scrub(long)) <= 16_000


def test_unterminated_private_key_markers_scrub_fast_and_are_masked():
    import time

    marker = "-----BEGIN RSA PRIVATE KEY-----"
    text = marker * (16_000 // len(marker))
    t0 = time.perf_counter()
    out = spark_store.scrub(text)
    assert time.perf_counter() - t0 < 0.1
    assert "-----BEGIN" not in out and len(out) < 200  # at most the stub of a marker cut by the window's end
    assert spark_store.scrub("a " + marker + " tail with no end") == "a [redacted]"
