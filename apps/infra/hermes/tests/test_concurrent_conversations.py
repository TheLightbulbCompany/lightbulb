"""Several conversations of one agent run at once in the one microVM; one conversation is still
one turn at a time (a follow-up steers it), under a microVM-wide cap."""
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


def test_each_conversation_gets_its_own_working_directory(home):
    a = turn.conversation_dir("ceo", turn.lightbulb_session_key("ceo", f.ROOM))
    b = turn.conversation_dir("ceo", turn.lightbulb_session_key("ceo", ROOM_B))
    issue = turn.conversation_dir("ceo", "paperclip:company:c:agent:ceo:issue:../../etc")
    assert a != b and a.parent == b.parent == turn._profile_dir("ceo") / "work"
    assert a.name == f.ROOM and issue.parent == a.parent and ".." not in issue.name


def test_every_conversation_folder_sees_the_profile_agents_md(home):
    profile = turn._profile_dir("ceo")
    cwd = turn.conversation_dir("ceo", turn.lightbulb_session_key("ceo", f.ROOM))
    cwd.mkdir(parents=True)
    turn.link_profile_agents_md(cwd)                      # before the profile has one: harmless
    assert not (cwd / "AGENTS.md").exists()
    (profile / "AGENTS.md").write_text("instructions tab")
    assert (cwd / "AGENTS.md").read_text() == "instructions tab"
    turn.link_profile_agents_md(cwd)                      # idempotent across turns
    repo = turn.conversation_dir("ceo", turn.lightbulb_session_key("ceo", ROOM_B))
    repo.mkdir(parents=True)
    (repo / "AGENTS.md").write_text("the repo's own")
    turn.link_profile_agents_md(repo)                     # a real file is never replaced
    assert (repo / "AGENTS.md").read_text() == "the repo's own"


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
