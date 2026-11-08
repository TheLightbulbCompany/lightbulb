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


def test_propose_tasks_is_gone():
    assert "propose_tasks" not in rt.SCHEMAS  # 5 Nov 2026: requests always start; approvals live inside the task


def test_router_tool_rejects_bad_args(router_turn):
    out = _call("start_task", trigger_ids=["chat:m1"], title="t" * 201, brief="b", ack="On it")
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
    assert seen == [(k, "lightbulb_router") for k in (
        "start_task", "pass_through", "reply", "ask", "note", "ignore", "follow_link", "propose_automation")]


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
    import contextvars  # a Router build sets _ROUTER_TURN: keep it out of later tests

    key = f"agent:{AGENT}:lightbulb:task:{TASK}"
    contextvars.copy_context().run(turn._construct_agent, AGENT, {}, None, key, **turn.ROUTER_RUN_LIMITS)
    assert captured["enabled_toolsets"] == ["lightbulb_router"] and captured["skip_memory"] is True  # reaches AIAgent


def test_task_ns_matches_schema():
    from tests.test_runs_v1 import _schema_path

    path = _schema_path()
    assert str(TASK_NS) == json.loads(path.read_text())["namespaces"]["task"]


@pytest.mark.parametrize("kind,args", [
    ("start_task", {"title": 5, "brief": "b", "ack": "x"}),
    ("pass_through", {"task_id": "not-a-uuid"}),
    ("start_task", {"title": "t", "brief": "b", "ack": "a", "visibility": "everyone"}),
    ("reply", {"text": ["not", "text"]}),
])
def test_router_tool_validates_fields(router_turn, kind, args):
    assert "error" in json.loads(_call(kind, trigger_ids=["chat:m1"], **args))
    assert "decisions" not in turn.RUNS[RUN]


def test_router_never_looks_things_up():
    """Founder ruling A (8 Nov 2026, DeepSeek shared.md:5): the Router never reads memory; a Worker does."""
    assert turn.ROUTER_RUN_LIMITS["enabled_toolsets"] == [turn.ROUTER_TOOLSET]
    token = turn._ROUTER_TURN.set(True)
    try:
        for tool in ("memory", "remember", "workspace_memory_search"):
            assert turn.memory_pre_tool_call(tool, {"query": "x"})["action"] == "block"
    finally:
        turn._ROUTER_TURN.reset(token)
    assert turn.memory_pre_tool_call("workspace_memory_search", {"query": "x"}) is None


def test_start_task_records_a_question(router_turn):
    assert _call("start_task", trigger_ids=["chat:m1"], title="t", brief="b", ack="a", question=True) == "recorded"
    assert turn.RUNS[RUN]["decisions"][-1]["question"] is True


def test_router_remember_is_open_outside_a_router_turn():
    assert turn.memory_pre_tool_call("remember", {"text": "x"}) is None


def test_start_task_tells_the_router_not_to_write_approval_steps():
    """Gate run 6 (A9): the old "ask for approval inside the task" text put approval steps into briefs."""
    from plugins.lightbulb import router

    text = router.SCHEMAS["start_task"]["description"]
    assert "never add approval or confirmation steps to the brief" in text and "ask for approval" not in text


def test_request_approval_names_the_self_gating_tools_and_keeps_the_rest_gated():
    """Review #4878: only bridge/effects.py's classified calls gate themselves; every other send, spend or publish
    still needs request_approval first."""
    from plugins.lightbulb import APPROVAL_SCHEMA

    text = APPROVAL_SCHEMA["description"]
    for gated in ("lightbulb-mail.sh", "lightbulb app publish", "gh pr merge", "request_purchase", "lightbulb_network"):
        assert gated in text
    assert "OTHER send, spend or publish still needs request_approval first" in text
    for ungated in ("gog gmail send", "send_invoice", "checkout", "git push", "vercel deploy", "npm publish"):
        assert ungated in text
    assert "the specialist handles approvals" in rt.SCHEMAS["start_task"]["description"]


def test_note_records_the_fact(router_turn):
    assert _call("note", trigger_ids=["chat:m1"], fact="The levain starter dates from 2009.") == "recorded"
    assert turn.RUNS[RUN]["decisions"] == [
        {"kind": "note", "triggerIds": ["chat:m1"], "fact": "The levain starter dates from 2009."}]
    assert "error" in json.loads(_call("note", trigger_ids=["chat:m1"]))


def test_router_turns_see_their_tools_without_tool_search(monkeypatch):
    """Walk 14: Router tools sat behind tool_search (two ~3 s lookups per turn); other turns keep it."""
    import sys
    import types

    class Config:
        @classmethod
        def from_raw(cls, raw):
            return raw

    fake = types.SimpleNamespace(ToolSearchConfig=Config, load_config=lambda: {"enabled": "auto"})
    monkeypatch.setitem(sys.modules, "tools", types.SimpleNamespace(tool_search=fake))
    monkeypatch.setitem(sys.modules, "tools.tool_search", fake)
    assert turn.install_router_tool_search() and turn.install_router_tool_search()  # once, idempotent
    assert fake.load_config() == {"enabled": "auto"}
    token = turn._ROUTER_TURN.set(True)
    try:
        assert fake.load_config() == {"enabled": "off"}
    finally:
        turn._ROUTER_TURN.reset(token)
