"""`lightbulb_router` tools record decisions and nothing else; Workers post only in their task room."""
import json
import uuid

import pytest

from bridge import turn
from plugins.lightbulb import router as rt
from plugins.lightbulb import task_room_id, TASK_NS, _send_room_message, TASK_ROOM_ONLY

RUN = "1c6d7e40-5b6c-4d7e-8f90-a1b2c3d4e5f6"
TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
AGENT = "a9e8d7c6-b5a4-4392-8170-6f5e4d3c2b1a"


@pytest.fixture
def router_turn(monkeypatch):
    turn.RUNS[RUN] = {"identity": {"mode": "router"}, "state": "running"}
    ctx = {"runId": RUN, "mode": "router"}
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: ctx)
    yield
    turn.RUNS.pop(RUN, None)


def _call(kind, **args):
    return rt._handler(kind)(args, session_id="s")


def test_tools_record_in_order(router_turn):
    assert _call("start_task", trigger_ids=["chat:m1"], title="Pay", brief="Pay Acme", ack="On it") == "recorded"
    assert _call("ask", trigger_ids=["chat:m2"], text="Which card?") == "recorded"
    assert [d["kind"] for d in turn.RUNS[RUN]["decisions"]] == ["start_task", "ask"]
    assert turn.RUNS[RUN]["decisions"][0]["triggerIds"] == ["chat:m1"]


def test_router_tool_rejects_bad_args(router_turn):
    six = [{"title": f"t{i}", "brief": "b"} for i in range(6)]
    out = _call("propose_tasks", trigger_ids=["chat:m1"], tasks=six, ack="Here are options")
    assert "error" in json.loads(out)
    assert json.loads(_call("reply", trigger_ids=[], text="hi"))["error"]
    assert "decisions" not in turn.RUNS[RUN]


def test_router_tools_refused_outside_router_runs(monkeypatch):
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: {"runId": RUN, "mode": "worker"})
    assert _call("ignore", trigger_ids=["chat:m1"]) == rt.NOT_ROUTER


def test_registered_under_router_toolset():
    seen = []

    class Ctx:
        def register_tool(self, name, toolset, schema, handler, **kw):
            seen.append((name, toolset))

    rt.register(Ctx())
    assert seen == [(k, "lightbulb_router") for k in ("start_task", "propose_tasks", "pass_through", "reply", "ask", "ignore", "follow_link")]


def test_follow_link_calls_memory_link_route(router_turn, monkeypatch):
    import plugins.lightbulb as lb
    from bridge.memory_ctx import MEMORY_CTX

    calls = []
    monkeypatch.setattr(lb, "_post", lambda url, token, body: calls.append((url, token, body)) or {"task": {"taskId": TASK}})
    ctx = {"runId": RUN, "mode": "router", "ownerId": "org_test", "apiUrl": "https://api.test/", "hookToken": "hook"}
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: ctx)
    token = MEMORY_CTX.set({"lane": "shared", "member": None, "audience": {"kind": "room", "roomId": "r1"}, "tags": {}, "sync": "none"})
    try:
        out = json.loads(rt._follow_link({"task_id": TASK}, session_id="s"))
        assert out["task"]["taskId"] == TASK
        assert calls == [("https://api.test/api/v1/internal/memory/link/org_test", "hook",
                          {"id": TASK, "audience": {"kind": "room", "roomId": "r1"}})]
        assert "error" in json.loads(rt._follow_link({"task_id": "nope"}, session_id="s"))
    finally:
        MEMORY_CTX.reset(token)
    assert rt._follow_link({"task_id": TASK}, session_id="s") == rt.NO_LINK  # no block: nothing to read with
    monkeypatch.setattr(rt.turn_context, "read", lambda sid: {"runId": RUN, "mode": "worker"})
    assert rt._follow_link({"task_id": TASK}, session_id="s") == rt.NOT_ROUTER


def test_worker_run_has_no_send_room_message(monkeypatch):
    """A Worker (family task) posts only in its own task room (contracts §6)."""
    key = f"agent:{AGENT}:lightbulb:task:{TASK}"
    ctx = {"family": "task", "sessionKey": key, "agentId": AGENT, "apiUrl": "http://x", "ownerId": "o", "hookToken": "t"}
    monkeypatch.setattr("plugins.lightbulb.turn_context.read", lambda sid: ctx)
    other = str(uuid.uuid4())
    assert _send_room_message({"roomId": other, "text": "hi"}, session_id="s") == TASK_ROOM_ONLY
    assert task_room_id(key) == str(uuid.uuid5(TASK_NS, f"room:{TASK}"))


def test_router_toolset_off_every_other_turn(monkeypatch):
    monkeypatch.setattr(turn, "profile_toolsets", lambda: (["lightbulb", "lightbulb_router", "memory"], None))
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kw):
            captured.update(kw)

    import sys
    import types
    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAIAgent))
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda agent_id: {})
    monkeypatch.setattr(turn, "profile_model", lambda c: ("m", "p"))
    monkeypatch.setattr(turn, "_session_db", lambda a: None)
    monkeypatch.setattr(turn, "_credential_pool", lambda *a: None)
    monkeypatch.setattr(turn, "_capture_api_errors", lambda a: None)
    turn._construct_agent(AGENT, {}, None, f"agent:{AGENT}:lightbulb:task:{TASK}")
    assert "lightbulb_router" not in captured["enabled_toolsets"]


def test_task_ns_matches_schema():
    from tests.test_runs_v1 import _schema_path

    path = _schema_path()
    assert str(TASK_NS) == json.loads(path.read_text())["namespaces"]["task"]


@pytest.mark.parametrize("kind,args", [
    ("propose_tasks", {"tasks": [{"title": "a", "brief": "b", "budget": 9}], "ack": "x"}),  # unknown key
    ("propose_tasks", {"tasks": [{"title": 5, "brief": "b"}], "ack": "x"}),
    ("pass_through", {"task_id": "not-a-uuid"}),
    ("start_task", {"title": "t", "brief": "b", "ack": "a", "visibility": "everyone"}),
    ("reply", {"text": ["not", "text"]}),
])
def test_router_tool_validates_fields(router_turn, kind, args):
    assert "error" in json.loads(_call(kind, trigger_ids=["chat:m1"], **args))
    assert "decisions" not in turn.RUNS[RUN]


def test_router_memory_is_read_only():
    token = turn._ROUTER_TURN.set(True)
    try:
        blocked = turn.memory_pre_tool_call("memory", {"action": "add", "content": "x"})
        assert blocked["action"] == "block"
        assert turn.memory_pre_tool_call("memory", {"action": "read"}) is None
        assert turn.memory_pre_tool_call("remember", {"text": "x"})["action"] == "block"
        assert turn.memory_pre_tool_call("workspace_memory_search", {"query": "x"}) is None
    finally:
        turn._ROUTER_TURN.reset(token)
    assert turn.memory_pre_tool_call("memory", {"action": "add", "content": "x"}) is None


def test_router_remember_is_open_outside_a_router_turn():
    assert turn.memory_pre_tool_call("remember", {"text": "x"}) is None
