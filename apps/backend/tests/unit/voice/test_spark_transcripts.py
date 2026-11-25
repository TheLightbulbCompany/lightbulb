"""Spark voice transcripts: the sideband turns GPT-Live transcript deltas into one saved item per turn, only
when SPARK_TRANSCRIPTS_ENABLED is on; a failed write never touches the session; account deletion removes them."""

import asyncio
import json
import uuid

import boto3
import pytest
from moto import mock_aws

from core import dynamodb
from core.config import settings
from core.observability import spark_events, spark_store
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
            AttributeDefinitions=[
                {"AttributeName": "owner_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "status-index",
                    "KeySchema": [{"AttributeName": "status", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        resource.create_table(
            TableName=dynamodb.table_name("users"),
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        for sid, user in ((SID, "user_a"), ("vs_2", "user_b")):
            sessions.put_item(Item={**os_voice._key(sid), "status": os_voice.ACTIVE, "sessionId": sid, "userId": user})
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


async def test_a_late_request_word_stamped_after_the_reply_began_stays_in_the_request(table, on):
    """Dev, 2026-11-23 re-run: the person's last delta carried a start after Spark's reply began."""
    s = state()
    await hear(
        s,
        ("user", "Text Sam the beach photos from ", 1800),
        ("spark", "Sure— ", 3000),
        ("spark", "could you share Sam's number or open Messages so I can draft it?", 3200),
        ("user", "today.", 3200),
    )
    await os_voice._flush(s)
    assert [(r["role"], r["text"]) for r in rows(table)] == [
        ("user", "Text Sam the beach photos from today."),
        ("spark", "Sure— could you share Sam's number or open Messages so I can draft it?"),
    ]


async def test_a_rapid_back_and_forth_merges_per_side(table, on):
    """Accepted trade-off (owner): only a side's own silence ends its turn."""
    s = state()
    await hear(s, ("user", "Are you there? ", 0), ("spark", "Yes. ", 500), ("user", "Thanks.", 1000))
    await os_voice._flush(s)
    assert [(r["role"], r["text"]) for r in rows(table)] == [("user", "Are you there? Thanks."), ("spark", "Yes.")]


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


async def test_a_session_opened_with_analytics_off_saves_nothing(table, on):
    s = state() | {"client": {"analytics": False}}
    await hear(s, ("user", "private"), ("spark", "reply"))
    await os_voice._flush(s)
    assert rows(table) == [] and "turns" not in s


@pytest.mark.parametrize("client", [{"analytics": True}, {}])
async def test_analytics_on_or_unsent_saves(table, on, client):
    s = state() | {"client": client}
    await hear(s, ("user", "hi"))
    await os_voice._flush(s)
    assert [r["text"] for r in rows(table)] == ["hi"]


async def test_no_transcript_set_mid_session_drops_held_turns_and_saves_nothing_more(table, on):
    s = state()
    await hear(s, ("user", "held when the person opted out"))
    s["noTranscript"] = True  # what _tick copies from the row
    await os_voice._flush(s)
    assert rows(table) == [] and "turns" not in s
    await hear(s, ("user", "said after"))
    await os_voice._flush(s)
    assert rows(table) == []


async def test_tick_picks_up_the_flag_from_the_row(table, on, monkeypatch):
    class WS:
        async def send(self, _):
            pass

    sessions = table.meta.client
    sessions.update_item(
        TableName=dynamodb.table_name("containers"), Key=os_voice._key(SID),
        UpdateExpression="SET noTranscript = :t", ExpressionAttributeValues={":t": True},
    )  # fmt: skip
    s = state() | {"trialEndsAt": None}
    await hear(s, ("user", "held"))
    assert await os_voice._tick(WS(), s)
    assert s["noTranscript"] is True and "turns" not in s
    assert rows(table) == []


async def test_stop_transcripts_flags_active_sessions_and_deletes_turns(table, on):
    for user, sid in (("user_a", SID), ("user_b", "vs_2")):
        s = state(user, sid)
        await hear(s, ("user", "hi"))
        await os_voice._flush(s)
    await os_voice.stop_transcripts("user_a")
    sessions = table.meta.client

    def flag(sid):
        item = sessions.get_item(TableName=dynamodb.table_name("containers"), Key=os_voice._key(sid))["Item"]
        return item.get("noTranscript")

    assert flag(SID) is True and flag("vs_2") is None
    assert [r["user_id"] for r in rows(table)] == ["user_b"]


async def test_a_stale_sideband_cannot_save_after_the_flag_is_set(table, on):
    s = state()  # its in-memory copy never saw noTranscript
    await hear(s, ("user", "held a tick too long"))
    table.meta.client.update_item(
        TableName=dynamodb.table_name("containers"), Key=os_voice._key(SID),
        UpdateExpression="SET noTranscript = :t", ExpressionAttributeValues={":t": True},
    )  # fmt: skip
    await os_voice._flush(s)
    assert rows(table) == []


async def test_the_persons_opt_out_blocks_saves_and_clears(table, on):
    users = table.meta.client
    users.put_item(TableName=dynamodb.table_name("users"), Item={"user_id": "user_a"})
    await os_voice.stop_transcripts("user_a")
    assert users.get_item(TableName=dynamodb.table_name("users"), Key={"user_id": "user_a"})["Item"]["os_analytics_off"]
    s = state()  # a session whose open was in flight with analytics on
    await hear(s, ("user", "late"))
    await os_voice._flush(s)
    assert rows(table) == []
    await os_voice.clear_opt_out("user_a")
    s = state(sid="vs_2")  # the flagged session stays off; a new one saves
    await hear(s, ("user", "back on"))
    await os_voice._flush(s)
    assert [r["text"] for r in rows(table)] == ["back on"]


async def test_opt_out_never_creates_a_users_row(table, on):
    await os_voice.stop_transcripts("user_nobody")
    await os_voice.clear_opt_out("user_nobody")
    assert "Item" not in table.meta.client.get_item(
        TableName=dynamodb.table_name("users"), Key={"user_id": "user_nobody"}
    )


def test_the_script_hides_the_user_id():
    item = {"user_id": "user_a", "sk": f"1800000000#{SID}#1", "sessionId": SID, "role": "user", "text": "hi"}
    out = spark_transcripts.render([item])
    assert "user_a" not in out and spark_transcripts.person("user_a") in out and "user: hi" in out


# --- the Spark data store dual-write ----------------------------------------------------------------------


@pytest.fixture
def stored(table, on, monkeypatch):
    """Store on; `put_turns` is a recorder. Returns the list of row batches it was given."""
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", True)
    calls = []

    async def put_turns(batch):
        calls.append(batch)

    monkeypatch.setattr(spark_store, "put_turns", put_turns)
    return calls


async def settle():
    await asyncio.gather(*(t for t in os_voice._BACKGROUND if t.get_loop() is asyncio.get_running_loop()))


def store_state(**kw):
    return (
        state()
        | {"storeOk": True, "taskId": "dlg_abcdefghij12", "client": {"platform": "mac", "appVersion": "2.1.1"}}
        | kw
    )


async def test_a_saved_turn_is_also_handed_to_the_store_once(stored, table):
    s = store_state()
    await hear(s, ("user", "Text Sam, my code is 482913", 1000))
    await os_voice._flush(s)
    await settle()
    assert stored == [
        [
            {
                "row_id": str(uuid.uuid5(spark_events.NS, f"{SID}:1000:user")),
                "ts": (1_800_000_000 * 1000 + 1000) * 1000,
                "user_id": "user_a",
                "session_id": SID,
                "task_id": "dlg_abcdefghij12",
                "role": "person",
                "source": "voice",
                "text": "Text Sam, my code is [code]",
                "platform": "mac",
                "app_version": "2.1.1",
            }
        ]
    ]
    assert [r["text"] for r in rows(table)] == ["Text Sam, my code is 482913"]  # the DynamoDB item is untouched


async def test_spark_turns_are_role_spark_and_text_is_cut_to_8000(stored):
    s = store_state()
    await hear(s, ("spark", "y" * 9000))
    await os_voice._flush(s)
    await settle()
    (row,) = stored[0]
    assert row["role"] == "spark" and len(row["text"]) == 8000


async def test_no_task_or_client_gives_nulls(stored):
    s = state() | {"storeOk": True}
    await hear(s, ("user", "hi"))
    await os_voice._flush(s)
    await settle()
    (row,) = stored[0]
    assert (row["task_id"], row["platform"], row["app_version"]) == (None, None, None)


@pytest.mark.parametrize(
    "setup",
    [
        pytest.param(lambda s: s.pop("storeOk"), id="no storeOk"),
        pytest.param(lambda s: s.update(storeOk=False), id="storeOk false"),
        pytest.param(lambda s: s.update(client={"analytics": False}), id="analytics off"),
    ],
)
async def test_nothing_is_stored_when_the_session_may_not(stored, setup):
    s = store_state()
    setup(s)
    await hear(s, ("user", "private"))
    await os_voice._flush(s)
    await settle()
    assert stored == []


async def test_flag_off_stores_nothing(stored, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", False)
    s = store_state()
    await hear(s, ("user", "hi"))
    await os_voice._flush(s)
    await settle()
    assert stored == []


async def test_no_transcript_stores_nothing(stored):
    s = store_state()
    await hear(s, ("user", "held"))
    s["noTranscript"] = True
    await os_voice._flush(s)
    await settle()
    assert stored == []


async def test_a_turn_the_dynamodb_transaction_refused_is_not_stored(stored, table):
    table.meta.client.put_item(
        TableName=dynamodb.table_name("users"), Item={"user_id": "user_a", "os_analytics_off": True}
    )
    s = store_state()
    await hear(s, ("user", "late"))
    await os_voice._flush(s)
    await settle()
    assert stored == [] and rows(table) == []


async def test_a_failing_store_never_breaks_the_save(stored, table, monkeypatch):
    async def boom(batch):
        raise RuntimeError("firehose down")

    monkeypatch.setattr(spark_store, "put_turns", boom)
    s = store_state()
    await hear(s, ("user", "hi"))
    await os_voice._flush(s)
    await settle()
    assert [r["text"] for r in rows(table)] == ["hi"]


class WS:
    async def send(self, _):
        pass


async def test_tick_copies_the_current_task_and_asks_the_store_every_tick(stored, table, monkeypatch):
    table.meta.client.update_item(
        TableName=dynamodb.table_name("containers"), Key=os_voice._key(SID),
        UpdateExpression="SET currentTaskId = :t", ExpressionAttributeValues={":t": "dlg_zzzzzzzzzz"},
    )  # fmt: skip
    answers = iter([True, False])

    monkeypatch.setattr(spark_store, "allowed_now", lambda uid: next(answers))
    s = state() | {"trialEndsAt": None, "storeOk": False}
    assert await os_voice._tick(WS(), s)
    assert s["taskId"] == "dlg_zzzzzzzzzz" and s["storeOk"] is True
    assert await os_voice._tick(WS(), s)
    assert s["storeOk"] is False  # the person opted out meanwhile


async def test_a_store_bug_is_not_a_failed_transcript_write(stored, table, monkeypatch):
    seen = []
    monkeypatch.setattr(os_voice, "put_metric", lambda name, *a, **kw: seen.append(name))

    def boom(text):
        raise RuntimeError("scrub bug")

    monkeypatch.setattr(spark_store, "scrub", boom)
    s = store_state()
    await hear(s, ("user", "secret words"))
    await os_voice._flush(s)
    assert [r["text"] for r in rows(table)] == ["secret words"]  # the DynamoDB save stands
    assert seen == ["spark_store.put_failed"]


async def test_tick_does_not_ask_when_the_store_is_off(table, on, monkeypatch):
    def asked(uid):
        raise AssertionError("asked")

    monkeypatch.setattr(spark_store, "allowed_now", asked)
    s = state() | {"trialEndsAt": None}
    for _ in range(3):
        assert await os_voice._tick(WS(), s)
    assert "storeOk" not in s


async def test_mark_task_sets_it_on_the_persons_active_sessions_only(table):
    containers = dynamodb.table_name("containers")
    client = table.meta.client
    client.put_item(
        TableName=dynamodb.table_name("users"), Item={"user_id": "user_a", "os_voice_active": {SID, "vs_gone"}}
    )
    await os_voice.mark_task("user_a", "dlg_abcdefghij12")
    got = lambda sid: client.get_item(TableName=containers, Key=os_voice._key(sid))["Item"].get("currentTaskId")  # noqa: E731
    assert got(SID) == "dlg_abcdefghij12" and got("vs_2") is None
    assert "Item" not in client.get_item(TableName=containers, Key=os_voice._key("vs_gone"))  # no row is created


async def test_mark_task_skips_an_ended_session(table):
    client = table.meta.client
    containers = dynamodb.table_name("containers")
    client.update_item(
        TableName=containers, Key=os_voice._key(SID), UpdateExpression="REMOVE #st",
        ExpressionAttributeNames={"#st": "status"},
    )  # fmt: skip
    client.put_item(TableName=dynamodb.table_name("users"), Item={"user_id": "user_a", "os_voice_active": {SID}})
    await os_voice.mark_task("user_a", "dlg_abcdefghij12")
    assert "currentTaskId" not in client.get_item(TableName=containers, Key=os_voice._key(SID))["Item"]


async def test_mark_task_for_a_person_with_no_row_does_nothing(table):
    await os_voice.mark_task("user_nobody", "dlg_abcdefghij12")


async def test_an_opt_out_stops_the_stores_cache_at_once(table, on, monkeypatch):
    seen = []
    monkeypatch.setattr(spark_store, "forget_locally", seen.append)
    table.meta.client.put_item(TableName=dynamodb.table_name("users"), Item={"user_id": "user_a"})
    await os_voice.stop_transcripts("user_a")
    assert seen == ["user_a"]


async def test_stop_transcripts_and_forget_start_the_stores_delete(table, on, monkeypatch):
    seen = []

    async def forget(user_id, bounded=True):
        seen.append((user_id, bounded))

    monkeypatch.setattr(spark_store, "forget", forget)
    await os_voice.stop_transcripts("user_a")
    await os_voice.forget("user_b", ())
    assert seen == [("user_a", True), ("user_b", False)]  # account deletion is unbounded
