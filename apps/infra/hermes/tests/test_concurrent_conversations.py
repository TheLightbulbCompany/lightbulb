"""Several conversations of one agent run at once in the one microVM; one conversation is still
one turn at a time (a follow-up steers it), under a microVM-wide cap."""
import json
import threading
import time
from types import SimpleNamespace

from bridge import turn
from tests import factories as f

ROOM_B = "33333333-3333-4333-8333-333333333333"


def _run_concurrently(monkeypatch, rooms, *, hold=0.1):
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def run_conversation(**_kwargs):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(hold)
        with lock:
            state["active"] -= 1
        return {"api_calls": 1, "final_response": "ack"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a: SimpleNamespace(run_conversation=run_conversation))
    threads = [
        threading.Thread(target=turn.run_turn, args=("ceo", f.ingress(
            room={"id": room, "name": f"r{i}", "kind": "channel", "revision": 1},
            run={"runId": f"run-{i}", "workspaceId": f.ROOM, "frameChannel": "", "frameEndpoint": ""})))
        for i, room in enumerate(rooms)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    return state["max"]


def test_one_agent_in_two_rooms_runs_both_at_once(home, posted, monkeypatch):
    assert _run_concurrently(monkeypatch, (f.ROOM, ROOM_B)) == 2


def test_one_room_is_still_one_turn_at_a_time(home, posted, monkeypatch):
    assert _run_concurrently(monkeypatch, (f.ROOM, f.ROOM)) == 1


def test_the_microvm_wide_cap_holds(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "_turn_slots", threading.BoundedSemaphore(2))
    rooms = [f"4444444{i}-4444-4444-8444-444444444444" for i in range(5)]
    assert _run_concurrently(monkeypatch, rooms) == 2


def test_each_slot_acquire_and_release_logs_its_cost(home, monkeypatch, caplog, capsys):
    monkeypatch.setattr(turn, "_turn_slots", threading.BoundedSemaphore(3))
    caplog.set_level("INFO", logger="bridge")
    with turn._waiting_turn_lock("ceo", "room-a", None):
        pass
    acquired, released = [r.getMessage() for r in caplog.records if "turn slot" in r.getMessage()]
    assert "acquired agent=ceo slots_in_use=1 slots_max=3 wait_ms=" in acquired
    assert "released agent=ceo slots_in_use=0 slots_max=3 held_ms=" in released
    assert "peak_rss_mb=" in acquired
    emf = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.startswith("{")]
    assert [d["bridge.turn_slot.slots_in_use"] for d in emf] == [1, 0]
    names = {m["Name"] for m in emf[0]["_aws"]["CloudWatchMetrics"][0]["Metrics"]}
    assert {"bridge.turn_slot.wait_ms", "bridge.turn_slot.peak_rss_mb"} <= names


def test_each_conversation_gets_its_own_working_directory(home):
    a = turn.conversation_dir("ceo", turn.lightbulb_session_key("ceo", f.ROOM))
    b = turn.conversation_dir("ceo", turn.lightbulb_session_key("ceo", ROOM_B))
    issue = turn.conversation_dir("ceo", "paperclip:company:c:agent:ceo:issue:../../etc")
    assert a != b and a.parent == b.parent == turn._profile_dir("ceo") / "work"
    assert a.name == f.ROOM and issue.parent == a.parent and ".." not in issue.name


def test_an_mcp_config_change_never_tears_down_a_live_turns_connections(monkeypatch):
    calls = []
    monkeypatch.setattr(turn, "_discover_mcp", lambda agent_id, alone=True: calls.append(alone))
    with turn._mcp_session("ceo"):          # a live turn holds the agent's connections
        with turn._mcp_session("ceo"):      # a second conversation discovers: must not shut down
            pass
    with turn._mcp_session("ceo"):          # alone again: free to reconnect
        pass
    assert calls == [True, False, True]


def test_discover_mcp_defers_the_shutdown_when_not_alone(monkeypatch):
    import sys

    shut = []
    fakes = {
        "tools.mcp_oauth": SimpleNamespace(suppress_interactive_oauth=lambda: __import__("contextlib").nullcontext()),
        "tools.mcp_tool_config": SimpleNamespace(_load_mcp_config=lambda: {"s": {"url": "v2"}}),
        "tools.mcp_tool_discovery": SimpleNamespace(discover_mcp_tools=lambda: None, get_mcp_status=lambda s: []),
        "tools.mcp_tool_lifecycle": SimpleNamespace(shutdown_mcp_servers=lambda scope: shut.append(scope)),
        "tools.registry": SimpleNamespace(registry=SimpleNamespace(current_scope_key=lambda: "ceo")),
    }
    for name, module in fakes.items():
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setitem(turn._MCP_FINGERPRINTS, "ceo", "old")
    turn._discover_mcp("ceo", alone=False)
    assert shut == [] and turn._MCP_FINGERPRINTS["ceo"] == "old"
    turn._discover_mcp("ceo", alone=True)
    assert shut == ["ceo"] and turn._MCP_FINGERPRINTS["ceo"] != "old"


def test_the_profile_agents_md_reaches_every_turns_system_message(home, monkeypatch):
    import sys
    import types

    seen = []
    loader = types.ModuleType("agent.prompt_builder")
    loader._load_agents_md = lambda path: seen.append(path) or (f"## AGENTS.md\n\n{(path / 'AGENTS.md').read_text()}" if (path / "AGENTS.md").exists() else "")
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.prompt_builder", loader)
    assert turn.with_profile_agents_md("ceo", "room") == "room"      # no file: unchanged
    turn._profile_dir("ceo").mkdir(parents=True, exist_ok=True)
    (turn._profile_dir("ceo") / "AGENTS.md").write_text("instructions tab")
    assert turn.with_profile_agents_md("ceo", "room") == "room\n\n## AGENTS.md\n\ninstructions tab"
    assert seen[-1] == turn._profile_dir("ceo")                        # the profile root, not work/<conv>
