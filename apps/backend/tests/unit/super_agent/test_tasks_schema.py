"""The frozen shared contract (contracts-v2 §2–§5, §14.2): ids, enums, rows, Router decisions, GSIs.

Other specs cite these names; a value changing here is a contract change, so the assertions pin values.
"""

import json
import uuid
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from core import tasks_schema as ts
from scripts.export_tasks_schema import SCHEMA_PATH

WS = "org_abc"
TASK = "8f14e45f-ceea-467a-9575-8a1b2c3d4e5f"


def test_stored_statuses_are_the_seven_of_section_4():
    assert set(get_args(ts.TaskStatus)) == {"proposed", "queued", "working", "waiting", "idle", "resolved", "failed"}
    assert "stopped" not in get_args(ts.TaskStatus)  # stopped = resolved with a reason


def test_shown_groups_are_derived_names_only():
    assert get_args(ts.ShownGroup) == (
        "waiting_on_you",
        "ready_for_review",
        "landing",
        "working",
        "idle",
        "resolved",
    )


def test_keys():
    assert ts.ws_pk(WS) == "WS#org_abc"
    assert ts.task_sk(TASK) == f"TASK#{TASK}"
    assert ts.attempt_sk(TASK, 3) == f"TASK#{TASK}#ATT#0003"
    assert ts.subscription_sk(TASK, "room:r1:m1") == f"TASK#{TASK}#SUB#room:r1:m1"
    assert ts.thread_sk("room:r1:m1") == "THREAD#room:r1:m1"
    assert ts.question_sk(TASK, "q1") == f"TASK#{TASK}#Q#q1"
    assert ts.needs_sk("user_1", TASK, "q1") == f"NEEDS#user_1#{TASK}#q1"
    assert ts.effect_sk("k1") == "EFFECT#k1"
    assert ts.trigger_sk("chat:m1") == "TRIG#chat:m1"
    assert ts.schedule_sk("s1") == "SCHED#s1"
    assert ts.hook_sk("pub_1") == "HOOK#pub_1"
    assert ts.agent_sk("a1") == "AGENT#a1"
    assert ts.project_sk("p1") == "PROJECT#p1"
    assert ts.goal_sk("g1") == "GOAL#g1"
    assert ts.creation_sk("c1") == "CAP#c1"
    assert ts.task_workflow_id(WS, TASK) == f"task:{WS}:{TASK}"
    assert ts.task_room_id(TASK) == f"task:{TASK}"
    assert ts.status_key(WS, "working") == "org_abc#working"


def test_task_id_is_a_uuid_and_router_made_ids_are_stable():
    uuid.UUID(ts.new_task_id())
    a = ts.router_task_id("batch1", 0, 0)
    assert a == ts.router_task_id("batch1", 0, 0) != ts.router_task_id("batch1", 0, 1)
    assert uuid.UUID(a).version == 5


def test_run_id_is_uuid5_of_ws_task_attempt():
    rid = ts.run_id(WS, TASK, 1)
    assert rid == str(uuid.uuid5(ts.RUN_NS, f"{WS}:{TASK}:1"))
    assert rid != ts.run_id(WS, TASK, 2)


def test_router_lane_key_hides_the_user_id():
    key = ts.router_lane_key("user_123")
    assert key.startswith("u:") and len(key) == 18 and "user_123" not in key


@pytest.mark.parametrize(
    ("source", "parts", "expected"),
    [
        ("chat", ("m1",), "chat:m1"),
        ("task_result", (TASK, 2), f"task_result:{TASK}:2"),
        ("job_result", ("d1",), "job:d1"),
        ("schedule", ("s1", 1700000000), "sched:s1:1700000000"),
        ("webhook", ("pub1", "dk1"), "hook:pub1:dk1"),
        ("manual", ("s1", "01HX"), "manual:s1:01HX"),
        ("mail", ("<abc@x>",), "mail:<abc@x>"),
        ("a2a", ("t1",), "a2a:t1"),
        ("network", ("r1",), "net:r1"),
        ("voice", ("call1", "u7"), "voice:call1:u7"),
        ("text", ("sb1",), "text:sb1"),
    ],
)
def test_trigger_ids_are_deterministic_per_source(source, parts, expected):
    assert ts.trigger_id(source, *parts) == expected


def test_trigger_id_refuses_the_wrong_number_of_parts():
    with pytest.raises(ValueError):
        ts.trigger_id("chat", "m1", "extra")


def test_every_trigger_source_has_an_id_format():
    assert set(get_args(ts.TriggerSource)) == set(ts.TRIGGER_ID_FORMATS)


def test_audience_rules():
    ts.Audience(kind="person", user_ids=["user_1"])
    ts.Audience(kind="room", room_id="r1")
    ts.Audience(kind="workspace")
    with pytest.raises(ValidationError):
        ts.Audience(kind="room")  # a room audience names its room
    with pytest.raises(ValidationError):
        ts.Audience(kind="person", user_ids=[])


def _task(**over):
    row = {
        "ws": WS,
        "taskId": TASK,
        "identifier": "LB-1",
        "title": "Draft the Q3 memo",
        "status": "queued",
        "audience": {"kind": "person", "userIds": ["user_1"]},
        "lane": "private",
        "ownerUserId": "user_1",
        "triggerId": "chat:m1",
        "createdAt": "2026-11-04T00:00:00Z",
        "updatedAt": "2026-11-04T00:00:00Z",
    }
    return ts.Task.model_validate(row | over)


def test_task_row_derives_keys_and_serializes_camel():
    task = _task()
    item = task.to_item()
    assert item["pk"] == f"WS#{WS}" and item["sk"] == f"TASK#{TASK}"
    assert item["type"] == "task" and item["statusKey"] == f"{WS}#queued"
    assert item["workflowId"] == f"task:{WS}:{TASK}"
    assert item["audience"] == {"kind": "person", "userIds": ["user_1"], "roomId": None}
    assert "ws" not in item  # the owner id lives in pk


def test_rows_round_trip_through_a_dynamodb_item():
    task = _task(specialist="a1")
    assert ts.Task.from_item(task.to_item()) == task


def test_task_refuses_stopped_and_unknown_fields():
    with pytest.raises(ValidationError):
        _task(status="stopped")
    with pytest.raises(ValidationError):
        _task(colour="red")


def test_question_states_and_effect_lifecycle():
    assert get_args(ts.QuestionState) == ("open", "answered", "cancelled")
    assert get_args(ts.EffectState) == ("proposed", "approved", "denied", "intent", "done", "unknown")
    assert get_args(ts.EffectDecision) == ("proceed", "replay", "refuse", "needs_approval", "denied")
    assert get_args(ts.CreationStatus) == ("draft", "active", "off")
    assert get_args(ts.CreationKind) == ("skill",)


def test_every_row_type_has_a_model():
    assert set(ts.ROW_MODELS) == {
        "task",
        "attempt",
        "subscription",
        "thread",
        "question",
        "needs",
        "effect",
        "trigger",
        "schedule",
        "hook",
        "agent",
        "project",
        "goal",
        "creation",
    }
    for name, model in ts.ROW_MODELS.items():
        assert model.model_fields["type"].default == name


def test_cut_rows_have_no_builders():
    for cut in ("router_turn_sk", "read_sk", "spend_sk", "watch_sk", "output_rev_sk"):
        assert not hasattr(ts, cut)


def test_gsis():
    assert [g.name for g in ts.GSIS] == ["by-status", "ws-meta"]
    assert (ts.GSIS[0].pk_attr, ts.GSIS[0].sk_attr) == ("statusKey", "updatedAt")
    assert (ts.GSIS[1].pk_attr, ts.GSIS[1].sk_attr) == ("metaKind", "pk")


DECISIONS = [
    {"kind": "reply", "triggerIds": ["chat:m1"], "text": "hi"},
    {
        "kind": "start_task",
        "triggerIds": ["chat:m1"],
        "title": "t",
        "brief": "b",
        "specialist": None,
        "ack": "On it",
    },
    {"kind": "propose_tasks", "triggerIds": ["chat:m1"], "tasks": [{"title": "t", "brief": "b"}], "ack": "?"},
    {"kind": "pass_through", "triggerIds": ["chat:m1"], "taskId": TASK, "ack": None},
    {"kind": "ask", "triggerIds": ["chat:m1"], "text": "Which one?"},
    {"kind": "ignore", "triggerIds": ["chat:m1"]},
]


def test_router_decision_union_parses_every_kind():
    parsed = ts.RouterDecisions.model_validate({"decisions": DECISIONS})
    assert [d.kind for d in parsed.decisions] == [d["kind"] for d in DECISIONS]
    assert set(get_args(ts.RouterDecisionKind)) == {d["kind"] for d in DECISIONS}


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "approve", "triggerIds": ["chat:m1"]},  # typed text never approves (§8)
        {"kind": "reply", "triggerIds": [], "text": "x"},  # every decision covers a trigger
        {"kind": "pass_through", "triggerIds": ["chat:m1"], "taskId": "not-a-uuid"},
    ],
)
def test_router_decision_rejects(bad):
    with pytest.raises(ValidationError):
        ts.RouterDecisions.model_validate({"decisions": [bad]})


def test_checked_in_json_matches_the_module():
    """Fails when tasks_schema.py changed without `uv run python -m scripts.export_tasks_schema`."""
    assert json.loads(Path(SCHEMA_PATH).read_text()) == ts.export_schema()
