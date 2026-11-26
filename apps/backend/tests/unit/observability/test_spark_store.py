"""Spark data store: plain gzip JSON-lines objects in S3 (moto, no real AWS) and the opt-out read (moto DynamoDB)."""

import gzip
import json
import logging

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
    return [
        json.loads(x)
        for x in gzip.decompress(s3.get_object(Bucket=BUCKET, Key=key)["Body"].read()).decode().splitlines()
    ]


def row(i=0, **kw):
    return {"row_id": f"r{i}", "ts": NOW_US, "user_id": "user_a", "text": f"t{i}"} | kw


async def test_a_turn_is_one_gzipped_json_line_under_the_day_and_person(s3):
    await spark_store.put("turns", "user_a", "vs_1_1000_user", [row()])
    (key,) = keys(s3)
    day = key.split("/")[2]
    assert key == f"data/turns/{day}/user_a__vs_1_1000_user.json.gz" and len(day) == len("dt=2026-11-25")
    assert lines(s3, key) == [{"row_id": "r0", "ts": "2024-11-25 02:00:00.123", "user_id": "user_a", "text": "t0"}]


async def test_many_rows_are_many_lines_of_one_object_and_the_day_is_the_servers(s3):
    await spark_store.put("task_steps", "user_a", "abc", [row(0), row(1)])
    (key,) = keys(s3)
    assert key.startswith("data/task_steps/dt=") and key.endswith("/user_a__abc.json.gz")
    assert [x["row_id"] for x in lines(s3, key)] == ["r0", "r1"]
    assert all(isinstance(x["ts"], str) for x in lines(s3, key))


@pytest.mark.parametrize(("flag", "env"), [(False, "dev"), (True, "local"), (True, ""), (True, "staging")])
async def test_off_writes_nothing(s3, monkeypatch, flag, env):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", flag)
    monkeypatch.setattr(settings, "ENVIRONMENT", env)
    await spark_store.put("turns", "user_a", "n", [row()])
    assert keys(s3) == []


@pytest.mark.parametrize(
    ("user", "name"), [("a__b", "n"), ("a/b", "n"), ("", "n"), ("user_a", "a b"), ("user_a", "x" * 129)]
)
async def test_a_bad_user_id_or_name_writes_nothing(s3, user, name):
    await spark_store.put("turns", user, name, [row()])
    assert keys(s3) == []


async def test_a_failing_put_never_raises_and_is_counted_without_text(monkeypatch, caplog):
    class Down:
        def put_object(self, **kw):
            raise RuntimeError("secret text here")

    seen = []
    monkeypatch.setattr(spark_store, "_s3", Down())
    monkeypatch.setattr(spark_store, "put_metric", lambda name, *a, **kw: seen.append(name))
    with caplog.at_level(logging.WARNING):
        await spark_store.put("turns", "user_a", "n", [row()])
    assert seen == ["spark_store.put_failed"] and "secret" not in caplog.text


# --- forget --------------------------------------------------------------------------------------------


def days_ago(n):
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC).date() - timedelta(days=n)).isoformat()


def seed(s3, user, table="turns", day=0, name="n"):
    s3.put_object(Bucket=BUCKET, Key=f"data/{table}/dt={days_ago(day)}/{user}__{name}.json.gz", Body=b"x")


async def settle():
    import asyncio

    from core.voice import os_voice

    await asyncio.gather(*(t for t in os_voice._BACKGROUND if t.get_loop() is asyncio.get_running_loop()))


async def test_forget_removes_the_persons_objects_in_both_tables_over_several_days(s3):
    for table in ("turns", "task_steps"):
        for day in (0, 1, 30, 91):
            seed(s3, "user_1", table, day, "a")
            seed(s3, "user_1", table, day, "b")
    seed(s3, "user_2", "turns", 0)
    spark_store.forget("user_1")
    await settle()
    assert keys(s3) == [f"data/turns/dt={days_ago(0)}/user_2__n.json.gz"]


async def test_forget_leaves_a_person_whose_id_starts_the_same(s3):
    seed(s3, "user_12")
    seed(s3, "user_1")
    spark_store.forget("user_1")
    await settle()
    assert keys(s3) == [f"data/turns/dt={days_ago(0)}/user_12__n.json.gz"]


async def test_forget_sweeps_a_second_time(s3, monkeypatch):
    sweeps = []
    real = spark_store._sweep
    monkeypatch.setattr(spark_store, "_sweep", lambda u: (sweeps.append(u), real(u)))
    spark_store.forget("user_1")
    await settle()
    assert sweeps == ["user_1", "user_1"]


async def test_the_second_sweep_catches_a_write_that_was_in_flight(s3, monkeypatch):
    async def late(_):
        seed(s3, "user_1")

    monkeypatch.setattr(spark_store.asyncio, "sleep", late)
    spark_store.forget("user_1")
    await settle()
    assert keys(s3) == []


async def test_forget_with_a_double_underscore_id_does_nothing(s3, monkeypatch):
    monkeypatch.setattr(spark_store, "_sweep", lambda u: pytest.fail("swept"))
    spark_store.forget("a__b")
    await settle()


async def test_a_failing_delete_never_raises(monkeypatch):
    def boom(u):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(spark_store, "_sweep", boom)
    spark_store.forget("user_1")
    await settle()


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
