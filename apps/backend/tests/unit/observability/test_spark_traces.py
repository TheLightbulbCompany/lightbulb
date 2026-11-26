"""Spark's rows as OTLP/JSON traces: valid shape, the mapping of every field, and ids that survive a retry."""

import json
import re

from core.observability import spark_store, spark_traces

NOW_US = 1_800_000_000_000_000
NOW_MS = NOW_US // 1000
SESSION = "vs_" + "ab" * 16
TASK = "dlg_abcdefghij12"
META = {"platform": "mac", "app_version": "2.1.18"}


def step(**kw):
    return {"kind": "tool", "tool": "click", "app": "Safari", "input": "button", "result": "ok", "ok": True, "ms": 340,
            "at": NOW_MS + 100} | kw  # fmt: skip


def task(status="failed", steps=None, **kw):
    return {
        "id": TASK, "status": status, "source": "voice", "sessionId": SESSION, "createdAt": NOW_MS, "doneAt": NOW_MS + 2000,
        "request": "book a table", "answer": "could not", "steps": [step()] if steps is None else steps,
    } | kw  # fmt: skip


def rows(body, scope="problem"):
    return spark_store.log_rows("user_a", "install", body, scope, NOW_US + 5_000_000, META)


def spans(exports):
    return [sp for e in exports for rs in e["resourceSpans"] for ss in rs["scopeSpans"] for sp in ss["spans"]]


def attrs(span):
    return {a["key"]: a["value"] for a in span["attributes"]}


def assert_valid_otlp(exports):
    json.dumps(exports)
    for e in exports:
        assert set(e) == {"resourceSpans"}
        for rs in e["resourceSpans"]:
            assert all(set(a) == {"key", "value"} for a in rs["resource"]["attributes"])
            for ss in rs["scopeSpans"]:
                for sp in ss["spans"]:
                    assert re.fullmatch(r"[0-9a-f]{32}", sp["traceId"])
                    assert re.fullmatch(r"[0-9a-f]{16}", sp["spanId"])
                    assert "parentSpanId" not in sp or re.fullmatch(r"[0-9a-f]{16}", sp["parentSpanId"])
                    assert isinstance(sp["kind"], int) and isinstance(sp["status"]["code"], int)
                    assert isinstance(sp["startTimeUnixNano"], str) and isinstance(sp["endTimeUnixNano"], str)
                    assert int(sp["endTimeUnixNano"]) >= int(sp["startTimeUnixNano"])
                    for a in sp["attributes"]:
                        (kind,) = set(a["value"])
                        assert kind in {
                            "stringValue",
                            "boolValue",
                            "intValue",
                            "doubleValue",
                            "arrayValue",
                            "kvlistValue",
                        }
                        assert a["value"][kind] is not None
                        if kind == "intValue":
                            assert isinstance(a["value"][kind], str)


def test_a_task_is_one_trace_a_root_and_a_child_per_step():
    steps = [step(), step(tool="type", ok=False, ms=None, input="hello", result=None)]
    exports = spark_traces.to_otlp(*rows({"task": task(steps=steps)}))
    assert_valid_otlp(exports)
    assert len(exports) == 1  # one trace, one line
    root, one, two = spans(exports)
    assert len({s["traceId"] for s in (root, one, two)}) == 1
    assert "parentSpanId" not in root and one["parentSpanId"] == two["parentSpanId"] == root["spanId"]
    assert root["name"] == "invoke_agent spark" and one["name"] == "execute_tool click"
    r = attrs(root)
    assert r["gen_ai.operation.name"] == {"stringValue": "invoke_agent"}
    assert r["gen_ai.conversation.id"] == {"stringValue": SESSION} and r["spark.source"] == {"stringValue": "voice"}
    assert r["spark.task_id"] == {"stringValue": TASK} and r["spark.task.status"] == {"stringValue": "failed"}
    assert r["spark.text_kept"] == {"boolValue": True} and r["spark.step_no"] == {"intValue": "0"}
    assert root["status"]["code"] == 2 and r["error.type"] == {"stringValue": "task_failed"}
    assert root["endTimeUnixNano"] == str((NOW_US + 2_000_000) * 1000)
    a = attrs(one)
    assert a["gen_ai.operation.name"] == {"stringValue": "execute_tool"}
    assert a["gen_ai.tool.name"] == {"stringValue": "click"} and a["spark.app"] == {"stringValue": "Safari"}
    assert a["gen_ai.tool.call.arguments"] == {"stringValue": "button"} and a["gen_ai.tool.call.result"] == {
        "stringValue": "ok"
    }
    assert one["status"]["code"] == 1 and one["endTimeUnixNano"] == str(int(one["startTimeUnixNano"]) + 340_000_000)
    assert two["status"]["code"] == 2 and two["endTimeUnixNano"] == two["startTimeUnixNano"]  # no duration known
    assert "spark.duration_ms" not in attrs(two) and "gen_ai.tool.call.result" not in attrs(two)
    res = {k: v for k, v in [(x["key"], x["value"]) for x in exports[0]["resourceSpans"][0]["resource"]["attributes"]]}
    assert res == {
        "service.name": {"stringValue": "spark"}, "service.version": {"stringValue": "2.1.18"},
        "os.type": {"stringValue": "darwin"}, "spark.platform": {"stringValue": "mac"}, "user.id": {"stringValue": "user_a"},
    }  # fmt: skip


def test_ids_are_the_same_on_a_retried_upload_and_differ_between_people_and_tasks():
    body = {"task": task()}
    first = spark_traces.to_otlp(*rows(body))
    again = spark_traces.to_otlp(*rows(body))
    assert [s["spanId"] for s in spans(first)] == [s["spanId"] for s in spans(again)]
    assert [s["traceId"] for s in spans(first)] == [s["traceId"] for s in spans(again)]
    other_task = spark_traces.to_otlp(*rows({"task": task(id="dlg_zzzzzzzzzz")}))
    assert spans(other_task)[0]["traceId"] != spans(first)[0]["traceId"]
    t, s = rows(body)
    other_person = spark_traces.to_otlp([], [r | {"user_id": "user_b"} for r in s])
    assert spans(other_person)[0]["traceId"] != spans(first)[0]["traceId"]
    assert len({sp["spanId"] for sp in spans(first)}) == 2  # root and the one step


def test_a_voice_turn_is_a_realtime_span_in_its_tasks_trace_under_the_root():
    turn = {
        "row_id": "r1", "ts": NOW_US, "user_id": "user_a", "session_id": SESSION, "task_id": TASK, "role": "spark",
        "source": "voice", "text": "On it.", **META,
    }  # fmt: skip
    exports = spark_traces.to_otlp([turn], [])
    assert_valid_otlp(exports)
    (sp,) = spans(exports)
    a = attrs(sp)
    assert sp["name"] == "realtime_inference" and a["gen_ai.operation.name"] == {"stringValue": "realtime_inference"}
    assert a["gen_ai.realtime_session.id"] == {"stringValue": SESSION} and "gen_ai.conversation.id" not in a
    assert a["spark.role"] == {"stringValue": "spark"} and a["spark.source"] == {"stringValue": "voice"}
    out = a["gen_ai.output.messages"]["arrayValue"]["values"][0]["kvlistValue"]["values"]
    assert out[0] == {"key": "role", "value": {"stringValue": "assistant"}} and "gen_ai.input.messages" not in a
    (task_trace,) = spans(spark_traces.to_otlp(*rows({"task": task()})))[:1]
    assert sp["traceId"] == task_trace["traceId"] and sp["parentSpanId"] == task_trace["spanId"]


def test_a_typed_line_joins_its_session_when_it_has_no_task():
    body = {"typed": [{"id": "l1", "text": "hello there", "at": NOW_MS, "sessionId": SESSION}]}
    exports = spark_traces.to_otlp(*rows(body))
    assert_valid_otlp(exports)
    (sp,) = spans(exports)
    a = attrs(sp)
    assert sp["name"] == "chat" and "parentSpanId" not in sp
    assert a["gen_ai.conversation.id"] == {"stringValue": SESSION} and "gen_ai.realtime_session.id" not in a
    assert a["spark.role"] == {"stringValue": "person"} and a["spark.source"] == {"stringValue": "typed"}
    assert "gen_ai.input.messages" in a
    assert sp["traceId"] == spark_traces._id(16, "session", "user_a", SESSION)


def test_a_turn_with_neither_task_nor_session_has_a_trace_of_its_own():
    (sp,) = spans(spark_traces.to_otlp(*rows({"typed": [{"id": "l1", "text": "hi", "at": NOW_MS}]})))
    assert re.fullmatch(r"[0-9a-f]{32}", sp["traceId"])


def test_a_secure_task_keeps_no_text_anywhere():
    exports = spark_traces.to_otlp(*rows({"task": task("done", steps=[step(input="PASSWORD", result="SECRET")])}))
    assert_valid_otlp(exports)
    blob = json.dumps(exports)
    assert "PASSWORD" not in blob and "SECRET" not in blob and "book a table" not in blob
    root, one = spans(exports)
    assert attrs(root)["spark.text_kept"] == {"boolValue": False} and root["status"]["code"] == 1
    assert "gen_ai.input.messages" not in attrs(root) and "gen_ai.tool.call.arguments" not in attrs(one)


def test_an_automation_task_has_its_id_and_no_conversation():
    exports = spark_traces.to_otlp(*rows({"task": task("failed", source="automation", automationId="auto-1")}))
    assert_valid_otlp(exports)
    a = attrs(spans(exports)[0])
    assert a["spark.automation_id"] == {"stringValue": "auto-1"} and a["spark.source"] == {"stringValue": "automation"}
    assert "gen_ai.conversation.id" not in a


def test_a_step_with_no_tool_is_named_by_its_kind_and_a_cancelled_task_has_no_status():
    exports = spark_traces.to_otlp(*rows({"task": task("cancelled", steps=[step(kind="note", tool=None, ok=None)])}))
    root, note = spans(exports)
    assert root["status"]["code"] == 0 and note["status"]["code"] == 0
    assert note["name"] == "chat note" and attrs(note)["spark.kind"] == {"stringValue": "note"}
    assert "spark.tool" not in attrs(note)


def test_thinking_commentary_and_note_text_is_an_assistant_message_and_the_thread_is_an_attribute():
    st = [
        step(kind=k, tool=None, ok=None, input=None, result=f"said {k}", thread="Ada")
        for k in ("thinking", "commentary", "note")
    ]
    exports = spark_traces.to_otlp(*rows({"task": task(steps=st, firstThinkingMs=250)}))
    assert_valid_otlp(exports)
    root, *said = spans(exports)
    assert attrs(root)["spark.first_thinking_ms"] == {"intValue": "250"}
    assert len(said) == 3
    for sp, k in zip(said, ("thinking", "commentary", "note")):
        a = attrs(sp)
        assert sp["name"] == f"chat {k}" and a["gen_ai.operation.name"] == {"stringValue": "chat"}
        assert a["spark.kind"] == {"stringValue": k} and a["spark.thread"] == {"stringValue": "Ada"}
        assert "gen_ai.tool.call.result" not in a and "gen_ai.tool.name" not in a
        assert f"said {k}" in json.dumps(a["gen_ai.output.messages"])


def test_the_same_trace_is_one_line_and_people_are_never_mixed():
    t, s = rows({"task": task(), "typed": [{"id": "l1", "text": "hi", "at": NOW_MS, "taskId": TASK}]})
    exports = spark_traces.to_otlp(t, s)
    assert len(exports) == 1 and len(spans(exports)) == 3
    assert {
        a["value"]["stringValue"]
        for e in exports
        for rs in e["resourceSpans"]
        for a in rs["resource"]["attributes"]
        if a["key"] == "user.id"
    } == {"user_a"}


def test_every_span_carries_the_session_id_as_the_join_key():
    t, st = rows(
        {"task": task(), "typed": [{"id": "l1", "text": "hi", "at": NOW_MS, "taskId": TASK, "sessionId": SESSION}]}
    )
    voice = {"row_id": "r1", "ts": NOW_US, "user_id": "user_a", "session_id": SESSION, "task_id": None, "role": "user",
             "source": "voice", "text": "x"} | META  # fmt: skip
    all_spans = spans(spark_traces.to_otlp(t + [voice | {"role": "person"}], st))
    assert len(all_spans) == 4
    assert all(attrs(sp)["spark.session_id"] == {"stringValue": SESSION} for sp in all_spans)


def test_the_outcome_is_a_response_finish_reason_and_messages_have_no_deprecated_finish_reason():
    for status, reasons in (("done", "stop"), ("failed", "error"), ("cancelled", None)):
        root = spans(spark_traces.to_otlp(*rows({"task": task(status)}, scope="all")))[0]
        got = attrs(root).get("gen_ai.response.finish_reasons")
        assert (got["arrayValue"]["values"][0]["stringValue"] if got else None) == reasons
        assert attrs(root)["spark.task.status"] == {"stringValue": status}
        assert "finish_reason" not in json.dumps(attrs(root)["gen_ai.output.messages"])


def test_a_voice_turn_with_a_model_is_named_operation_then_model():
    turn = {"row_id": "r1", "ts": NOW_US, "user_id": "user_a", "session_id": SESSION, "task_id": None, "role": "spark",
            "source": "voice", "text": "x", "model": "gpt-live"} | META  # fmt: skip
    (sp,) = spans(spark_traces.to_otlp([turn], []))
    assert sp["name"] == "realtime_inference gpt-live"
    assert attrs(sp)["gen_ai.request.model"] == {"stringValue": "gpt-live"}
    assert attrs(sp)["gen_ai.operation.name"] == {"stringValue": "realtime_inference"}
