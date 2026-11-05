"""The turn's memory block (wave2-M Task 3): bridge ContextVar, tagged events, private lane actor,
sync "none", Linked copy and the tasks board (Task 7). No block = today's behaviour."""
import json
import pathlib

import pytest

import bridge
from bridge import memory_ctx, runs, turn
from tests.test_memory_agentcore import MEMBER, OWNER, FakeClient, mod, provider  # noqa: F401
from tests.test_memory_plugin_recall import FakeBackend, backend, recalls, said  # noqa: F401

CASES = pathlib.Path(__file__).resolve().parents[3] / "backend" / "tests" / "fixtures" / "page_text_cases.json"
TAGS = {"taskId": "t-1", "threadId": "task-room-1", "specialist": "agent-fin", "source": "worker"}
SHARED = {"lane": "shared", "member": None, "audience": {"kind": "room", "roomId": "r1"}, "tags": TAGS,
          "sync": "events"}
PRIVATE = {"lane": "private", "member": MEMBER, "audience": {"kind": "person", "userId": MEMBER}, "tags": TAGS,
           "sync": "events"}
ROUTER = {"lane": "shared", "member": None, "audience": {"kind": "room", "roomId": "r1"}, "tags": {}, "sync": "none"}


@pytest.fixture
def block():
    def use(value):
        memory_ctx.MEMORY_CTX.set(value)
    yield use
    memory_ctx.MEMORY_CTX.set(None)


def _events(client):
    return [kw for op, kw in client.calls if op == "create"]


def test_sync_turn_sends_tags_as_event_metadata(mod, block):  # noqa: F811
    block(SHARED)
    client = FakeClient()
    provider(mod, client).sync_turn("Sam: draft it", "Done.")
    [ev] = _events(client)
    assert ev["actorId"] == OWNER
    assert ev["metadata"] == {k: {"stringValue": v} for k, v in {**TAGS, "lane": "shared"}.items()}


def test_memory_block_private_lane_writes_private_actor_without_dm_chat_type(mod, block):  # noqa: F811
    block(PRIVATE)
    client = FakeClient()
    provider(mod, client, user_id="", chat_type="group").sync_turn("brief", "Done.")
    assert _events(client)[0]["actorId"] == f"{OWNER}:{MEMBER}"


def test_shared_block_in_a_dm_never_takes_the_dm_person(mod, block):  # noqa: F811
    block(SHARED)
    client = FakeClient()
    provider(mod, client, user_id=MEMBER, chat_type="dm").sync_turn("Sam: hi", "Hi.")
    assert _events(client)[0]["actorId"] == OWNER


def test_sync_none_writes_nothing(mod, block):  # noqa: F811
    block(ROUTER)
    client = FakeClient()
    provider(mod, client).sync_turn("Sam: hi", "Hi.")
    assert client.calls == []


def test_no_memory_block_is_byte_identical_to_today(mod, backend):  # noqa: F811
    client = FakeClient()
    p = provider(mod, client, user_id=MEMBER, chat_type="dm")
    p.sync_turn("Sam: hi", "Hi.")
    [ev] = _events(client)
    assert set(ev) == {"memoryId", "actorId", "sessionId", "eventTimestamp", "payload"}
    p.prefetch(said("what is our launch date?"))
    (_, _, body, _), = recalls(backend)
    assert body == {"question": "Aden: what is our launch date?", "budget_tokens": 400, "member": MEMBER}


def test_recall_body_carries_lane_and_audience(mod, backend, block):  # noqa: F811
    block(PRIVATE)
    provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "acme"})
    (_, _, body, _), = recalls(backend)
    assert body["member"] == MEMBER and body["lane"] == "private"
    assert body["audience"] == {"kind": "person", "userId": MEMBER}


def test_private_lane_worker_blocks_shared_notes(monkeypatch):
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda a: {})
    monkeypatch.setattr(turn, "profile_model", lambda c: ("m", "p"))
    monkeypatch.setattr(turn, "profile_toolsets", lambda: (None, None))
    monkeypatch.setattr(turn, "_session_db", lambda a: None)
    monkeypatch.setattr(turn, "_credential_pool", lambda *a: None)
    monkeypatch.setattr(turn, "_capture_api_errors", lambda a: None)
    import sys
    import types

    fake = types.ModuleType("run_agent")
    fake.AIAgent = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "run_agent", fake)
    add = {"action": "add", "target": "memory", "content": "x"}
    turn._construct_agent("ceo", {"sessionKey": "s", "memory": PRIVATE}, None, "s")
    assert memory_ctx.MEMORY_CTX.get() == PRIVATE
    assert turn.memory_pre_tool_call(tool_name="memory", args=add)["action"] == "block"
    turn._construct_agent("ceo", {"sessionKey": "s", "memory": SHARED}, None, "s")
    assert turn.memory_pre_tool_call(tool_name="memory", args=add) is None
    # the next turn without a block inherits nothing; a malformed block is dropped
    turn._construct_agent("ceo", {"sessionKey": "s", "memory": {**PRIVATE, "member": None}}, None, "s")
    assert memory_ctx.MEMORY_CTX.get() is None
    assert turn.memory_pre_tool_call(tool_name="memory", args=add) is None


def test_v1_run_body_passes_memory_block(monkeypatch):
    base = {"input": "brief", "instructions": ""}
    assert runs.validate_request({**base, "memory": PRIVATE})["memory"] == PRIVATE
    assert runs.validate_request(base) == base
    for bad in ({**PRIVATE, "member": None}, {**SHARED, "lane": "team"}, {**SHARED, "sync": "all"}, "x",
                {**SHARED, "member": MEMBER}):
        with pytest.raises(runs.Refused):
            runs.validate_request({**base, "memory": bad})
    seen = {}

    def construct(agent_id, body, frames, key, **kw):
        seen["body"] = body
        raise RuntimeError("stop")  # nothing past construction is under test

    monkeypatch.setattr(turn, "_construct_agent", construct)
    with pytest.raises(RuntimeError):
        turn._run_task_turn({"agentId": "ceo", "runId": "r", "sessionKey": "agent:x:lightbulb:task:y"},
                            {**base, "memory": PRIVATE}, family="task")
    assert seen.get("body", {}).get("memory") == PRIVATE


def test_page_text_fixture_parity(mod):  # noqa: F811
    if not CASES.exists():
        pytest.skip("backend fixture not in this checkout")
    for case in json.loads(CASES.read_text("utf-8")):
        assert mod._page_text(case["page"]["results"], case["page"].get("links")) == case["text"], case["name"]


def test_search_page_has_linked_section_and_link_count(mod, backend, block):  # noqa: F811
    block(SHARED)
    link = {"kind": "task", "id": "t-1", "identifier": "T-1", "title": "Acme", "status": "resolved",
            "specialist": "agent-fin", "updatedAt": "2026-11-01T00:00:00Z", "threads": []}
    turn_row = {"kind": "turn", "content": "x" * 900, "occurredAt": "2026-10-01", "status": "current"}
    backend.reply = {"results": [turn_row] * 60, "links": [link]}
    out = json.loads(provider(mod, FakeClient()).handle_tool_call("workspace_memory_search", {"query": "acme"}))
    assert out["counts"] == {"facts": 0, "turns": 60, "links": 1}
    assert len(out["page"]) <= mod._SEARCH_MAX
    assert out["page"].endswith("Linked:\nT-1 Acme — resolved 2026-11-01 · agent-fin · task:t-1")


def test_board_from_tasks_when_memory_ctx_set(mod, backend, block):  # noqa: F811
    block(SHARED)
    backend.reply = {"results": [], "text": "Tasks: T-1 Acme (working)"}
    p = provider(mod, FakeClient())
    assert "Tasks: T-1 Acme (working)" in p.prefetch(said("acme?"))
    boards = [c for c in backend.calls if "/memory/board/" in c[0]]
    assert boards and boards[0][2] == {"audience": {"kind": "room", "roomId": "r1"}}
    assert not [c for c in backend.calls if "pc.local" in c[0]]  # no Paperclip read
