"""Spark voice transcripts: the sideband turns GPT-Live transcript deltas into one saved item per turn, only
when SPARK_TRANSCRIPTS_ENABLED is on; a failed write never touches the session; account deletion removes them."""

import json

import boto3
import pytest
from moto import mock_aws

from core import dynamodb
from core.config import settings
from core.voice import os_voice
from scripts import spark_transcripts

SID = "vs_1"


@pytest.fixture
def table(monkeypatch):
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        t = resource.create_table(
            TableName=dynamodb.table_name(os_voice.TRANSCRIPTS),
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        sessions = resource.create_table(
            TableName=dynamodb.table_name("containers"),
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        for sid in (SID, "vs_2"):
            sessions.put_item(Item={**os_voice._key(sid), "status": os_voice.ACTIVE, "sessionId": sid})
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        yield t


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "SPARK_TRANSCRIPTS_ENABLED", True)


def state(user="user_a", sid=SID):
    return {"sessionId": sid, "userId": user, "startedAt": 1_800_000_000, "usageSeconds": 0}


def delta(role, text, start_ms=0):
    kind = "session.input_transcript.delta" if role == "user" else "session.output_transcript.delta"
    return json.dumps({"type": kind, "delta": text, "start_ms": start_ms, "end_ms": start_ms + 1})


async def hear(s, *events):
    for role, text, *start in events:
        assert await os_voice._event(s, delta(role, text, *start)) is False


def rows(table):
    return sorted(table.scan()["Items"], key=lambda i: i["sk"])


async def test_interleaved_deltas_become_one_item_per_side_in_spoken_order(table, on):
    """Dev, 2026-11-23: Spark started replying before the person's last word arrived."""
    s = state()
    await hear(
        s,
        ("user", "Text Sam the beach photos from ", 1000),
        ("spark", "Sure, I'll ", 3200),
        ("user", "today.", 2900),
        ("spark", "send today's beach photos to Sam.", 3600),
    )
    await os_voice._flush(s, idle=60)  # neither side silent long enough: nothing saved yet
    assert rows(table) == []
    await os_voice._flush(s)  # the session closing saves what is held
    items = rows(table)
    assert [(r["role"], r["text"]) for r in items] == [
        ("user", "Text Sam the beach photos from today."), ("spark", "Sure, I'll send today's beach photos to Sam."),
    ]  # fmt: skip
    first = items[0]
    assert first["user_id"] == "user_a" and first["sessionId"] == SID
    assert first["sk"] == f"1800000000#{SID}#000000001000#user"
    assert first["ttl"] == first["at"] + 90 * 86400


async def test_a_pause_ends_a_turn(table, on, monkeypatch):
    s = state()
    await hear(s, ("user", "what time is it"))
    await os_voice._flush(s, idle=60)  # not silent long enough
    assert rows(table) == []
    await os_voice._flush(s, idle=0)
    assert [r["text"] for r in rows(table)] == ["what time is it"]


async def test_off_writes_nothing(table, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_TRANSCRIPTS_ENABLED", False)
    s = state()
    await hear(s, ("user", "hello"), ("spark", "hi"))
    await os_voice._flush(s)
    assert rows(table) == [] and "turns" not in s


async def test_a_failed_write_never_breaks_the_session(on, monkeypatch, caplog):
    seen = []
    monkeypatch.setattr(os_voice, "put_metric", lambda name, **kw: seen.append(name))

    def boom(name):
        raise RuntimeError("no table")

    monkeypatch.setattr(os_voice, "get_table", boom)
    s = state()
    await hear(s, ("user", "secret words"), ("spark", "reply"))
    await os_voice._flush(s)
    assert seen == ["os_voice.transcript.write_failed"] * 2
    assert "secret words" not in caplog.text


async def test_delete_removes_only_that_persons_transcripts(table, on):
    for user, sid in (("user_a", SID), ("user_b", "vs_2")):
        s = state(user, sid)
        await hear(s, ("user", "hi"))
        await os_voice._flush(s)
    await os_voice.delete_transcripts("user_a")
    assert [r["user_id"] for r in rows(table)] == ["user_b"]


async def test_an_ended_session_saves_no_more_turns(table, on, monkeypatch):
    seen = []
    monkeypatch.setattr(os_voice, "put_metric", lambda name, **kw: seen.append(name))
    s = state()
    await hear(s, ("user", "said before the account was deleted"))
    table.meta.client.update_item(
        TableName=dynamodb.table_name("containers"), Key=os_voice._key(SID), UpdateExpression="REMOVE #st",
        ExpressionAttributeNames={"#st": "status"},
    )  # fmt: skip
    await os_voice._flush(s)  # the sideband's late final flush
    assert rows(table) == [] and seen == []


def test_the_script_hides_the_user_id():
    item = {"user_id": "user_a", "sk": f"1800000000#{SID}#1", "sessionId": SID, "role": "user", "text": "hi"}
    out = spark_transcripts.render([item])
    assert "user_a" not in out and spark_transcripts.person("user_a") in out and "user: hi" in out
