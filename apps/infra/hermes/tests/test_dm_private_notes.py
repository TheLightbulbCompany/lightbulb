"""A private 1:1 DM cannot write the agent's shared notes (memories/USER.md, MEMORY.md).

Hermes injects those files into every session of the agent, so a fact saved in one person's DM
was recited to another person in theirs (dev 10-10). The bridge marks the turn at agent
construction; the lightbulb plugin's pre_tool_call hook refuses add/replace on that turn.
"""
import contextvars
import json
import sys
import types

import pytest

from bridge import turn

PERSON, OTHER = "user_2abcA", "user_9zzzB"


def run_turn(monkeypatch, body, call):
    """Construct the turn's agent with the REAL _construct_agent (fake AIAgent), then run `call`
    in that turn's context -- the context Hermes copies into its tool workers."""
    fake = types.ModuleType("run_agent")
    fake.AIAgent = lambda **kw: types.SimpleNamespace(**kw)
    monkeypatch.setitem(sys.modules, "run_agent", fake)
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda agent_id: {"model": {"default": "m"}})
    monkeypatch.setattr(turn, "_restore_review_counters", lambda agent: None)
    monkeypatch.setattr(turn, "_capture_api_errors", lambda agent: None)

    def in_turn():
        turn._construct_agent("ceo", body, None, "sess")
        return call()

    return contextvars.copy_context().run(in_turn)


def memory_call(home, args):
    """Hermes's dispatch contract (agent_runtime_helpers.invoke_tool): a pre_tool_call block
    returns {"error": message} and the memory tool never runs; otherwise it writes the note."""
    block = turn.memory_pre_tool_call(tool_name="memory", args=args, session_id="s", task_id="t")
    if block and block.get("action") == "block":
        return json.dumps({"error": block["message"]})
    target = home / "memories" / ("USER.md" if args.get("target") == "user" else "MEMORY.md")
    target.parent.mkdir(exist_ok=True)
    contents = [args.get("content")] + [op.get("content") for op in args.get("operations") or []]
    target.write_text("\n".join(c for c in contents if c), encoding="utf-8")
    return json.dumps({"success": True})


def body(kind, sender):
    return {"room": {"id": "r1", "kind": kind}, "sender": {"id": sender}}


ADD_USER = {"action": "add", "target": "user", "content": "locker code is 7319-9680"}
REPLACE_MEMORY = {"action": "replace", "target": "memory", "old_text": "x", "content": "locker code is 7319-9680"}
BATCH_USER = {"target": "user", "operations": [{"action": "remove", "old_text": "old"},
                                               {"action": "add", "content": "locker code is 7319-9680"}]}


@pytest.mark.parametrize("args", [ADD_USER, REPLACE_MEMORY, BATCH_USER])
def test_a_private_dm_cannot_write_shared_notes(tmp_path, monkeypatch, args):
    out = run_turn(monkeypatch, body("dm", PERSON), lambda: memory_call(tmp_path, args))
    assert json.loads(out)["error"] == turn.PRIVATE_DM_NOTES
    assert "remembered privately" in turn.PRIVATE_DM_NOTES
    assert not (tmp_path / "memories").exists()


def test_a_private_dm_may_still_remove_a_shared_note(monkeypatch):
    args = {"action": "remove", "target": "user", "old_text": "stale"}
    assert run_turn(monkeypatch, body("dm", PERSON),
                    lambda: turn.memory_pre_tool_call(tool_name="memory", args=args)) is None


@pytest.mark.parametrize("kind,sender", [("channel", PERSON), ("group-dm", PERSON), ("dm", "ceo-agent"),
                                         ("dm", "__system__"), ("dm", "")])
def test_every_other_turn_still_writes(tmp_path, monkeypatch, kind, sender):
    out = run_turn(monkeypatch, body(kind, sender), lambda: memory_call(tmp_path, ADD_USER))
    assert json.loads(out) == {"success": True}
    assert "7319-9680" in (tmp_path / "memories" / "USER.md").read_text()


def test_the_flag_is_per_construction_not_sticky(tmp_path, monkeypatch):
    """One microVM serves a DM and a channel of the same agent: the next construction resets it."""
    def dm_then_channel():
        turn._construct_agent("ceo", body("dm", PERSON), None, "a")
        turn._construct_agent("ceo", body("channel", OTHER), None, "b")
        return memory_call(tmp_path, ADD_USER)

    assert json.loads(run_turn(monkeypatch, body("dm", PERSON), dm_then_channel)) == {"success": True}


def test_other_tools_pass_untouched(monkeypatch):
    assert run_turn(monkeypatch, body("dm", PERSON),
                    lambda: turn.memory_pre_tool_call(tool_name="terminal", args={"command": "ls"})) is None


def test_the_plugin_registers_the_hook(monkeypatch):
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1] / "plugins"))
    import lightbulb

    monkeypatch.setattr(lightbulb, "_register_redaction_default", lambda: 0)
    monkeypatch.setattr(lightbulb, "_scope_session_search", lambda: None)
    hooks = []

    class Ctx:
        def register_hook(self, name, fn):
            hooks.append((name, fn))

        def register_tool(self, *_a, **_k):
            pass

    lightbulb.register(Ctx())
    assert ("pre_tool_call", turn.memory_pre_tool_call) in hooks
