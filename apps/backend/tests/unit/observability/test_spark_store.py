"""Spark data store: plain gzip JSON-lines objects in S3 (moto, no real AWS) and the opt-out read (moto DynamoDB)."""

import asyncio
import gzip
import json
import logging
import threading
import time
from datetime import UTC, datetime

import boto3
import pytest
from moto import mock_aws

from core import dynamodb
from core.config import settings
from core.observability import spark_store

BUCKET = "lightbulb-dev-spark-store"
NOW_US = 1_732_500_000_123_456  # 2024-11-25 02:00:00.123 UTC


@pytest.fixture(autouse=True)
def on(monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", True)
    monkeypatch.setattr(settings, "ENVIRONMENT", "dev")
    monkeypatch.setattr(spark_store, "SECOND_SWEEP_S", 0)
    monkeypatch.setattr(spark_store, "_s3", None)
    monkeypatch.setattr(spark_store, "_pending", 0)
    monkeypatch.setattr(spark_store, "_sweeping", set())


@pytest.fixture
def s3(monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        monkeypatch.setattr(spark_store, "_s3", client)
        yield client


def keys(s3):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET).get("Contents", []))


def lines(s3, key):
    body = gzip.decompress(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()).decode()
    return [json.loads(x) for x in body.splitlines()]


def row(i=0, **kw):
    return {"row_id": f"r{i}", "ts": NOW_US, "user_id": "user_a", "text": f"t{i}"} | kw


def fresh():
    return time.monotonic()


async def test_a_turn_is_one_gzipped_json_line_under_the_person_and_day(s3):
    await spark_store.put("turns", "user_a", "vs_1_1000_user", [row()], fresh())
    (key,) = keys(s3)
    today = datetime.now(UTC).date().isoformat()
    assert key == f"data/turns/user_a/{today}/vs_1_1000_user.json.gz"
    assert lines(s3, key) == [{"row_id": "r0", "ts": "2024-11-25 02:00:00.123", "user_id": "user_a", "text": "t0"}]


async def test_many_rows_are_many_lines_of_one_object(s3):
    await spark_store.put("task_steps", "user_a", "abc", [row(0), row(1)], fresh())
    (key,) = keys(s3)
    assert key.startswith("data/task_steps/user_a/") and key.endswith("/abc.json.gz")
    assert [x["row_id"] for x in lines(s3, key)] == ["r0", "r1"]
    assert all(isinstance(x["ts"], str) for x in lines(s3, key))


@pytest.mark.parametrize(("flag", "env"), [(False, "dev"), (True, "local"), (True, ""), (True, "staging")])
async def test_off_writes_nothing(s3, monkeypatch, flag, env):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", flag)
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    await spark_store.put("turns", "user_a", "n", [row()], fresh())
    assert keys(s3) == []


@pytest.mark.parametrize(
    ("user", "name"),
    [("a/b", "n"), ("a.b", "n"), ("..", "n"), ("", "n"), ("user_a", "a b"), ("user_a", "../x"), ("user_a", "x" * 129)],
)
async def test_a_bad_user_id_or_name_writes_nothing(s3, user, name):
    await spark_store.put("turns", user, name, [row()], fresh())
    assert keys(s3) == []


async def test_a_failing_put_never_raises_and_is_counted_without_text(monkeypatch, caplog):
    class Down:
        def put_object(self, **kw):
            raise RuntimeError("secret text here")

    seen = []
    monkeypatch.setattr(spark_store, "_s3", Down())
    monkeypatch.setattr(spark_store, "put_metric", lambda name, *a, **kw: seen.append(name))
    with caplog.at_level(logging.WARNING):
        await spark_store.put("turns", "user_a", "n", [row()], fresh())
    assert seen == ["spark_store.put_failed"] and "secret" not in caplog.text


async def test_a_write_whose_check_is_over_a_minute_old_is_dropped(s3, monkeypatch):
    seen = []
    monkeypatch.setattr(spark_store, "put_metric", lambda name, *a, **kw: seen.append(name))
    await spark_store.put("turns", "user_a", "n", [row()], fresh() - 61)
    assert keys(s3) == [] and seen == ["spark_store.put_dropped"]
    await spark_store.put("turns", "user_a", "n", [row()], fresh() - 30)
    assert len(keys(s3)) == 1


async def test_the_201st_pending_write_is_dropped_and_the_count_does_not_leak(s3, monkeypatch):
    seen = []
    monkeypatch.setattr(spark_store, "put_metric", lambda name, *a, **kw: seen.append(name))
    monkeypatch.setattr(spark_store, "_pending", 200)
    await spark_store.put("turns", "user_a", "n", [row()], fresh())
    assert keys(s3) == [] and seen == ["spark_store.put_dropped"] and spark_store._pending == 200
    monkeypatch.setattr(spark_store, "_pending", 199)
    await spark_store.put("turns", "user_a", "n", [row()], fresh())
    assert len(keys(s3)) == 1 and spark_store._pending == 199


async def test_store_calls_run_on_the_stores_own_threads(monkeypatch):
    names = []

    class Spy:
        def put_object(self, **kw):
            names.append(threading.current_thread().name)

        def get_paginator(self, _):
            names.append(threading.current_thread().name)
            return type("P", (), {"paginate": lambda self, **kw: []})()

    monkeypatch.setattr(spark_store, "_s3", Spy())
    await spark_store.put("turns", "user_a", "n", [row()], fresh())
    await spark_store.forget_account("user_a")
    assert names and all(n.startswith("spark-store") for n in names)
    assert not any(n.startswith("asyncio") for n in names)


# --- forget --------------------------------------------------------------------------------------------


def seed(s3, user, table="turns", day="2026-11-01", name="n"):
    s3.put_object(Bucket=BUCKET, Key=f"data/{table}/{user}/{day}/{name}.json.gz", Body=b"x")


async def settle():
    from core.voice import os_voice

    await asyncio.gather(*(t for t in os_voice._BACKGROUND if t.get_loop() is asyncio.get_running_loop()))


class Flaky:
    """The real (moto) client, with chosen calls failing."""

    def __init__(self, real, list_fails=(), delete_errors=False):
        self.real, self.list_fails, self.delete_errors = real, list_fails, delete_errors

    def get_paginator(self, name):
        real = self.real.get_paginator(name)
        fails = self.list_fails

        class P:
            def paginate(self, **kw):
                if any(f in kw["Prefix"] for f in fails):
                    raise RuntimeError("list down")
                return real.paginate(**kw)

        return P()

    def delete_objects(self, **kw):
        if self.delete_errors and any("task_steps" in o["Key"] for o in kw["Delete"]["Objects"]):
            return {"Errors": [{"Key": "x", "Code": "InternalError"}]}
        return self.real.delete_objects(**kw)

    def __getattr__(self, name):
        return getattr(self.real, name)


async def test_forget_removes_the_persons_objects_in_both_tables_across_date_folders(s3):
    for table in ("turns", "task_steps"):
        for day in ("2026-11-01", "2026-09-01", "2026-06-01"):
            seed(s3, "user_1", table, day, "a")
            seed(s3, "user_1", table, day, "b")
    seed(s3, "user_2")
    spark_store.forget("user_1")
    await settle()
    assert keys(s3) == ["data/turns/user_2/2026-11-01/n.json.gz"]


@pytest.mark.parametrize(("gone", "kept"), [("user_1", "user_12"), ("user_a", "user_a_"), ("user_a_", "user_a")])
async def test_forget_leaves_a_person_whose_id_starts_the_same(s3, gone, kept):
    seed(s3, gone)
    seed(s3, kept)
    spark_store.forget(gone)
    await settle()
    assert keys(s3) == [f"data/turns/{kept}/2026-11-01/n.json.gz"]


async def test_forget_pages_past_a_thousand_objects(s3):
    for i in range(1001):
        seed(s3, "user_1", name=f"n{i}")
    seed(s3, "user_2")
    await spark_store.forget_account("user_1")
    assert keys(s3) == ["data/turns/user_2/2026-11-01/n.json.gz"]


async def test_one_failing_listing_or_delete_does_not_stop_the_rest_and_the_failure_is_reported(s3, monkeypatch):
    for table in ("turns", "task_steps"):
        seed(s3, "user_1", table)
    monkeypatch.setattr(spark_store, "_s3", Flaky(s3, list_fails=("data/turns/",)))
    with pytest.raises(RuntimeError):
        await spark_store.forget_account("user_1")
    assert keys(s3) == ["data/turns/user_1/2026-11-01/n.json.gz"]  # task_steps went
    seed(s3, "user_1", "task_steps")
    monkeypatch.setattr(spark_store, "_s3", Flaky(s3, delete_errors=True))
    with pytest.raises(RuntimeError):
        await spark_store.forget_account("user_1")
    assert keys(s3) == ["data/task_steps/user_1/2026-11-01/n.json.gz"]  # turns went, task_steps was refused


async def test_a_failed_background_delete_is_counted_not_raised(s3, monkeypatch):
    seen = []
    monkeypatch.setattr(spark_store, "put_metric", lambda name, *a, **kw: seen.append(name))
    monkeypatch.setattr(spark_store, "_s3", Flaky(s3, list_fails=("data/",)))
    monkeypatch.setattr(spark_store, "allowed", _no)
    spark_store.forget("user_1")
    await settle()
    assert seen == ["spark_store.forget_failed"] * 2  # both sweeps
    assert "user_1" not in spark_store._sweeping


async def _no(_):
    return False


async def _yes(_):
    return True


def count_sweeps(monkeypatch):
    sweeps = []
    real = spark_store._sweep
    monkeypatch.setattr(spark_store, "_sweep", lambda u: (sweeps.append(u), real(u))[1])
    return sweeps


async def test_forget_sweeps_twice_for_opt_out(s3, monkeypatch):
    sweeps = count_sweeps(monkeypatch)
    monkeypatch.setattr(spark_store, "allowed", _no)
    spark_store.forget("user_1")
    await settle()
    assert sweeps == ["user_1", "user_1"]


async def test_the_second_sweep_catches_a_write_that_was_in_flight(s3, monkeypatch):
    async def late(_):
        seed(s3, "user_1")

    monkeypatch.setattr(spark_store, "allowed", _no)
    monkeypatch.setattr(spark_store.asyncio, "sleep", late)
    spark_store.forget("user_1")
    await settle()
    assert keys(s3) == []


async def test_the_second_sweep_is_skipped_when_usage_data_is_back_on(s3, monkeypatch):
    sweeps = count_sweeps(monkeypatch)
    monkeypatch.setattr(spark_store, "allowed", _yes)
    spark_store.forget("user_1")
    await settle()
    assert sweeps == ["user_1"]


async def test_account_deletion_always_runs_the_second_sweep(s3, monkeypatch):
    sweeps = count_sweeps(monkeypatch)
    monkeypatch.setattr(spark_store, "allowed", _yes)  # would skip an opt-out's second sweep
    await spark_store.forget_account("user_1")
    await settle()
    assert sweeps == ["user_1", "user_1"]


async def test_a_second_forget_while_one_runs_does_not_start_another(s3, monkeypatch):
    sweeps = count_sweeps(monkeypatch)
    monkeypatch.setattr(spark_store, "allowed", _no)
    spark_store.forget("user_1")
    spark_store.forget("user_1")
    await settle()
    assert sweeps == ["user_1", "user_1"]
    spark_store.forget("user_1")  # finished: a new one starts
    await settle()
    assert len(sweeps) == 4


async def test_account_deletion_sweeps_even_while_an_opt_out_delete_runs(s3, monkeypatch):
    sweeps = count_sweeps(monkeypatch)
    monkeypatch.setattr(spark_store, "allowed", _no)
    spark_store.forget("user_1")
    await spark_store.forget_account("user_1")
    await settle()
    assert len(sweeps) == 4


async def test_opt_out_in_write_out_inside_two_minutes_is_cleaned_by_the_pending_second_sweep(s3, monkeypatch):
    state = {"allowed": False}

    async def allowed(_):
        return state["allowed"]

    async def during_the_wait(_):
        # the person opted in, a write landed, and they opted out again: the second `forget` is skipped
        state["allowed"] = True
        await spark_store.put("turns", "user_1", "between", [row()], fresh())
        state["allowed"] = False
        spark_store.forget("user_1")

    monkeypatch.setattr(spark_store, "allowed", allowed)
    monkeypatch.setattr(spark_store.asyncio, "sleep", during_the_wait)
    seed(s3, "user_1")
    spark_store.forget("user_1")
    await settle()
    assert keys(s3) == []


async def test_forget_with_an_invalid_id_does_nothing(s3, monkeypatch):
    monkeypatch.setattr(spark_store, "_sweep", lambda u: pytest.fail("swept"))
    spark_store.forget("a/b")
    await spark_store.forget_account("a/b")
    await settle()


async def test_off_environments_never_sweep(s3, monkeypatch):
    monkeypatch.setattr(settings, "ENVIRONMENT", "local")
    monkeypatch.setattr(spark_store, "_sweep", lambda u: pytest.fail("swept"))
    spark_store.forget("user_1")
    await spark_store.forget_account("user_1")


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
    assert [await spark_store.allowed(u) for u in ("ok", "off", "gone", "nobody")] == [True, False, False, False]


async def test_allowed_reads_fresh_every_time(users):
    users.put_item(Item={"user_id": "u"})
    assert await spark_store.allowed("u") is True
    users.update_item(
        Key={"user_id": "u"}, UpdateExpression="SET os_analytics_off = :t", ExpressionAttributeValues={":t": True}
    )
    assert await spark_store.allowed("u") is False


async def test_a_read_error_fails_closed(monkeypatch):
    def boom(name):
        raise RuntimeError("ddb down")

    monkeypatch.setattr(spark_store, "get_table", boom)
    assert await spark_store.allowed("user_a") is False


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
