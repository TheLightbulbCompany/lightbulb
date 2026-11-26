"""Spark voice transcripts: the sideband turns GPT-Live transcript deltas into one saved item per turn, only
when SPARK_TRANSCRIPTS_ENABLED is on; a failed write never touches the session; account deletion removes them."""

import asyncio
import json
import time

import boto3
import pytest
from moto import mock_aws

from core import dynamodb
from core.config import settings
from core.observability import spark_store, spark_traces
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
    await os_voice.stop_transcripts("user_a", 1)
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
    await os_voice.stop_transcripts("user_a", 1)
    assert users.get_item(TableName=dynamodb.table_name("users"), Key={"user_id": "user_a"})["Item"]["os_analytics_off"]
    s = state()  # a session whose open was in flight with analytics on
    await hear(s, ("user", "late"))
    await os_voice._flush(s)
    assert rows(table) == []
    await os_voice.clear_opt_out("user_a", 2)
    s = state(sid="vs_2")  # the flagged session stays off; a new one saves
    await hear(s, ("user", "back on"))
    await os_voice._flush(s)
    assert [r["text"] for r in rows(table)] == ["back on"]


async def test_opt_out_never_creates_a_users_row(table, on):
    await os_voice.stop_transcripts("user_nobody", 1)
    await os_voice.clear_opt_out("user_nobody", 1)
    assert "Item" not in table.meta.client.get_item(
        TableName=dynamodb.table_name("users"), Key={"user_id": "user_nobody"}
    )


def test_the_script_hides_the_user_id():
    item = {"user_id": "user_a", "sk": f"1800000000#{SID}#1", "sessionId": SID, "role": "user", "text": "hi"}
    out = spark_transcripts.render([item])
    assert "user_a" not in out and spark_transcripts.person("user_a") in out and "user: hi" in out


# --- the Spark data store: one trace object per ended session ---------------------------------------------------


@pytest.fixture
def stored(table, on, monkeypatch):
    """Store on, the person allowed, `put` a recorder. Returns the list of (user_id, exports) objects written."""
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", True)
    table.meta.client.put_item(TableName=dynamodb.table_name("users"), Item={"user_id": "user_a"})
    calls = []

    async def put(user_id, exports, checked_at):
        calls.append((user_id, exports))

    monkeypatch.setattr(spark_store, "put", put)
    return calls


async def settle():
    await asyncio.gather(*(t for t in os_voice._BACKGROUND if t.get_loop() is asyncio.get_running_loop()))


def store_state(**kw):
    return state() | {"taskId": "dlg_abcdefghij12", "client": {"platform": "mac", "appVersion": "2.1.1"}} | kw


async def finish(table, s, reason="client_closed", **fields):
    """Save the held turns, put `fields` on the session's row (as the app's open would have), and end it."""
    await os_voice._flush(s)
    names = {f"#f{i}": k for i, k in enumerate(fields)}
    table.meta.client.update_item(
        TableName=dynamodb.table_name("containers"),
        Key=os_voice._key(s["sessionId"]),
        UpdateExpression="SET startedAt = :s, #c = :c" + "".join(f", {n} = :v{i}" for i, n in enumerate(names)),
        ExpressionAttributeNames={"#c": "client"} | names,
        ExpressionAttributeValues={":s": s["startedAt"], ":c": s.get("client") or {}}
        | {f":v{i}": v for i, v in enumerate(fields.values())},
    )
    assert await os_voice.end_session(s["sessionId"], reason, 5)
    await settle()


def spans(exports):
    return [sp for e in exports for rs in e["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]]


def attr(span, key):
    return next((a["value"] for a in span["attributes"] if a["key"] == key), None)


async def test_turns_are_not_written_per_turn_but_once_when_the_session_ends(stored, table):
    s = store_state()
    await hear(s, ("user", "Text Sam, my code is 482913", 1000), ("spark", "Sent.", 3000))
    await os_voice._flush(s)
    await settle()
    assert stored == [] and len(rows(table)) == 2  # saved to DynamoDB, nothing in the store yet
    await finish(table, s)
    ((user, exports),) = stored  # one object for the whole session
    assert user == "user_a"
    turn, reply = sorted(spans(exports), key=lambda sp: int(sp["startTimeUnixNano"]))
    assert turn["startTimeUnixNano"] == str((1_800_000_000 * 1000 + 1000) * 1_000_000)
    assert attr(turn, "gen_ai.input.messages")["arrayValue"]["values"][0]["kvlistValue"]["values"][1]["value"][
        "arrayValue"
    ]["values"][0]["kvlistValue"]["values"][1]["value"] == {"stringValue": "Text Sam, my code is [code]"}
    assert attr(reply, "gen_ai.output.messages") and attr(turn, "gen_ai.realtime_session.id") == {"stringValue": SID}
    assert [r["text"] for r in rows(table)] == ["Text Sam, my code is 482913", "Sent."]  # DynamoDB copy untouched


async def test_a_turn_with_a_task_joins_the_tasks_trace_and_one_without_the_sessions(stored, table):
    s = store_state()
    await hear(s, ("user", "first", 1000))
    await os_voice._flush(s)
    s["taskId"] = None
    await hear(s, ("user", "second", 2000))
    await finish(table, s)
    ((_, exports),) = stored
    assert len(exports) == 2  # two traces, one object
    first, second = sorted(spans(exports), key=lambda sp: int(sp["startTimeUnixNano"]))
    root = spark_traces._id(8, "task", "user_a", "dlg_abcdefghij12")
    assert first["parentSpanId"] == root and first["traceId"] == spark_traces._id(
        16, "trace", "user_a", "dlg_abcdefghij12"
    )
    assert "parentSpanId" not in second and second["traceId"] == spark_traces._id(16, "session", "user_a", SID)


async def test_platform_and_version_are_on_the_resource_and_spark_text_is_cut_to_8000(stored, table):
    s = store_state()
    await hear(s, ("spark", "y" * 9000))
    await finish(table, s)
    ((_, exports),) = stored
    resource = {a["key"]: a["value"] for a in exports[0]["resourceSpans"][0]["resource"]["attributes"]}
    assert resource["service.version"] == {"stringValue": "2.1.1"} and resource["os.type"] == {"stringValue": "darwin"}
    assert resource["user.id"] == {"stringValue": "user_a"}
    (reply,) = spans(exports)
    text = attr(reply, "gen_ai.output.messages")["arrayValue"]["values"][0]["kvlistValue"]["values"][1]["value"]
    assert len(text["arrayValue"]["values"][0]["kvlistValue"]["values"][1]["value"]["stringValue"]) == 8000


async def test_a_session_with_no_saved_turns_writes_nothing(stored, table):
    await finish(table, store_state())
    assert stored == []


async def test_ending_twice_writes_once(stored, table):
    s = store_state()
    await hear(s, ("user", "hi"))
    await finish(table, s)
    assert await os_voice.end_session(SID, "client_closed", 5) is False
    await settle()
    assert len(stored) == 1


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"client": {"analytics": False}}, id="analytics off at open"),
        pytest.param({"noTranscript": True}, id="stopped mid-session"),
    ],
)
async def test_an_opted_out_session_is_not_stored(stored, table, fields):
    s = store_state()
    await hear(s, ("user", "private"))
    await os_voice._flush(s)
    table.meta.client.update_item(
        TableName=dynamodb.table_name("containers"), Key=os_voice._key(SID),
        UpdateExpression="SET startedAt = :s, " + ", ".join(f"{'#' + k} = :{k}" for k in fields),
        ExpressionAttributeNames={"#" + k: k for k in fields},
        ExpressionAttributeValues={":s": s["startedAt"]} | {":" + k: v for k, v in fields.items()},
    )  # fmt: skip
    await os_voice.end_session(SID, "client_closed", 5)
    await settle()
    assert stored == []


@pytest.mark.parametrize("field", ["os_analytics_off", "os_deleted_at"])
async def test_an_opt_out_after_the_turns_were_saved_but_before_the_end_stores_nothing(stored, table, field):
    """The turns are saved (the transaction passed), then the person's row flips: only the consistent `allowed()` read
    in `_store_session` stops the write. Deleting that read makes this fail."""
    s = store_state()
    await hear(s, ("user", "private words", 1000))
    await os_voice._flush(s)
    assert len(rows(table)) == 1  # saved while they were still allowed
    table.meta.client.update_item(
        TableName=dynamodb.table_name("users"), Key={"user_id": "user_a"},
        UpdateExpression="SET #f = :t", ExpressionAttributeNames={"#f": field}, ExpressionAttributeValues={":t": True},
    )  # fmt: skip
    await finish(table, s)
    assert stored == []


async def test_the_staleness_clock_starts_after_the_opt_out_read_and_the_conversion(stored, table, monkeypatch):
    order, real_allowed, real_to_otlp = [], spark_store.allowed, spark_traces.to_otlp

    async def allowed(user_id):
        order.append("allowed")
        return await real_allowed(user_id)

    def to_otlp(turns, steps):
        order.append("convert")
        return real_to_otlp(turns, steps)

    async def put(user_id, exports, checked_at):
        order.append(("put", checked_at))

    monkeypatch.setattr(spark_store, "allowed", allowed)
    monkeypatch.setattr(spark_traces, "to_otlp", to_otlp)
    monkeypatch.setattr(spark_store, "put", put)
    s = store_state()
    await hear(s, ("user", "hi"))
    before = time.monotonic()
    await finish(table, s)
    assert order[:2] == ["convert", "allowed"] and order[2][0] == "put"
    assert order[2][1] >= before  # taken after the read, not at the start of the session's end


async def test_the_conversion_runs_off_the_event_loop(stored, table, monkeypatch):
    import threading

    seen = []
    real = spark_traces.to_otlp
    monkeypatch.setattr(
        spark_traces, "to_otlp", lambda t, st: (seen.append(threading.current_thread()), real(t, st))[1]
    )
    s = store_state()
    await hear(s, ("user", "hi"))
    await finish(table, s)
    assert seen and all(t is not threading.main_thread() for t in seen)


async def test_the_sessions_model_names_the_realtime_span(stored, table):
    s = store_state()
    await hear(s, ("user", "hi"))
    await finish(table, s, model="gpt-live")
    ((_, exports),) = stored
    assert spans(exports)[0]["name"] == "realtime_inference gpt-live"


async def test_an_account_deletion_close_is_not_stored(stored, table):
    s = store_state()
    await hear(s, ("user", "hi"))
    await finish(table, s, reason="signed_out")
    assert stored == []


async def test_flag_off_stores_nothing(stored, table, monkeypatch):
    monkeypatch.setattr(settings, "SPARK_STORE_ENABLED", False)
    s = store_state()
    await hear(s, ("user", "hi"))
    await finish(table, s)
    assert stored == []


async def test_a_failing_store_never_breaks_the_end_of_the_session(stored, table, monkeypatch):
    seen = []
    monkeypatch.setattr(os_voice, "put_metric", lambda name, *a, **kw: seen.append(name))

    async def boom(*args):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(spark_store, "put", boom)
    s = store_state()
    await hear(s, ("user", "hi"))
    await finish(table, s)  # end_session returned True
    assert seen == ["spark_store.put_failed"] and [r["text"] for r in rows(table)] == ["hi"]


class WS:
    async def send(self, _):
        pass


async def test_tick_copies_the_current_task(stored, table):
    table.meta.client.update_item(
        TableName=dynamodb.table_name("containers"), Key=os_voice._key(SID),
        UpdateExpression="SET currentTaskId = :t", ExpressionAttributeValues={":t": "dlg_zzzzzzzzzz"},
    )  # fmt: skip
    s = state() | {"trialEndsAt": None}
    assert await os_voice._tick(WS(), s)
    assert s["taskId"] == "dlg_zzzzzzzzzz"


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


async def test_stop_transcripts_and_account_forget_start_the_stores_delete(table, on, monkeypatch):
    seen = []

    async def forget_account(user_id):
        seen.append(("account", user_id))

    monkeypatch.setattr(spark_store, "forget", lambda u: seen.append(("optout", u)))
    monkeypatch.setattr(spark_store, "forget_account", forget_account)
    await os_voice.stop_transcripts("user_a", 1)
    await os_voice.forget("user_b", ())
    assert seen == [("optout", "user_a"), ("account", "user_b")]


async def test_an_account_forget_that_fails_raises_to_its_caller(table, on, monkeypatch):
    async def boom(user_id):
        raise RuntimeError("s3 down")

    monkeypatch.setattr(spark_store, "forget_account", boom)
    with pytest.raises(RuntimeError):
        await os_voice.forget("user_b", ())


def _user_row(table, user="user_a"):
    users = table.meta.client
    users.put_item(TableName=dynamodb.table_name("users"), Item={"user_id": user})
    return lambda: users.get_item(TableName=dynamodb.table_name("users"), Key={"user_id": user})["Item"]


async def test_an_off_arriving_after_a_newer_on_ends_on_and_deletes_nothing(table, on):
    row = _user_row(table)
    await os_voice.clear_opt_out("user_a", 2)  # the later choice lands first
    s = state()
    await hear(s, ("user", "written after the on"))
    await os_voice._flush(s)
    await os_voice.stop_transcripts("user_a", 1)  # the earlier choice arrives late
    assert "os_analytics_off" not in row() and row()["os_analytics_at"] == 2
    assert [r["text"] for r in rows(table)] == ["written after the on"]


async def test_an_on_arriving_after_a_newer_off_ends_off(table, on):
    row = _user_row(table)
    await os_voice.stop_transcripts("user_a", 2)
    await os_voice.clear_opt_out("user_a", 1)
    assert row()["os_analytics_off"] is True and row()["os_analytics_at"] == 2


async def test_a_stale_off_after_a_newer_off_still_deletes(table, on):
    _user_row(table)
    await os_voice.stop_transcripts("user_a", 2)
    s = state()
    await hear(s, ("user", "x"))
    await os_voice._flush(s)  # blocked by the flag, so nothing to delete; the call must not raise
    await os_voice.stop_transcripts("user_a", 1)
    assert rows(table) == []


async def test_a_throttled_opt_out_write_is_counted_and_logged_not_silent(table, on, monkeypatch, caplog):
    from botocore.exceptions import ClientError

    seen = []
    monkeypatch.setattr(os_voice, "put_metric", lambda name, *a, **k: seen.append(name))
    _user_row(table)
    s = state()
    await hear(s, ("user", "kept"))
    await os_voice._flush(s)

    def throttled(*a, **k):
        raise ClientError({"Error": {"Code": "ProvisionedThroughputExceededException"}}, "UpdateItem")

    monkeypatch.setattr(os_voice, "get_table", lambda name: type("T", (), {"update_item": staticmethod(throttled)}))
    await os_voice.stop_transcripts("user_a", 1)
    assert seen == ["os_voice.opt_out_write_failed"]
    assert "ProvisionedThroughputExceededException" in caplog.text
    assert [r["text"] for r in rows(table)] == ["kept"]  # the flag did not apply, so nothing is deleted


@pytest.mark.parametrize("off_first", [True, False])
async def test_an_off_and_an_on_with_the_same_timestamp_end_off(table, on, off_first):
    row = _user_row(table)
    writes = [os_voice.stop_transcripts("user_a", 5), os_voice.clear_opt_out("user_a", 5)]
    for w in writes if off_first else writes[::-1]:
        await w
    assert row()["os_analytics_off"] is True
