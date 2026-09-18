"""Hazard 1 of the local-exec persistence report: one agent = one working tree = one turn at a
time.

Two turns for the SAME agent in different rooms run concurrently in one process (the FIFO
group is room+agent, see sync.py's ``_save_lock`` comment). With ``terminal.backend: local``
both turns' shell commands now run in the IDENTICAL profile directory -- a concurrent ``git
commit`` and ``npm test`` in one working tree. The pinned Hermes image does not serialize this
for us: ``tools/terminal_scope.py`` is a ContextVar policy projection, and the only locks in
``tools/terminal_tool.py`` / ``tools/environments/local.py`` guard sandbox creation, the env
mutation and the session->cwd map, never execution.
"""
import threading
import time
from types import SimpleNamespace

from bridge import turn
from tests import factories as f


def _run_both(monkeypatch, agent_ids):
    """Run one turn per entry in ``agent_ids`` concurrently through the REAL ``_run_hermes``
    (only ``_construct_agent`` is replaced, as test_terminal_scope.py does), and report the
    high-water mark of turns executing at once -- the shape test_sync.py's save-serialization
    test already uses."""
    state = {"active": 0, "max": 0}
    lock = threading.Lock()

    def run_conversation(**_kwargs):
        with lock:
            state["active"] += 1
            state["max"] = max(state["max"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return {"api_calls": 1, "final_response": "ack"}

    monkeypatch.setattr(turn, "_construct_agent",
                        lambda *args: SimpleNamespace(run_conversation=run_conversation))
    threads = [
        threading.Thread(
            target=turn.run_turn,
            args=(agent_id, f.ingress(run={"runId": f"run-{i}", "workspaceId": f.ROOM,
                                           "frameChannel": "", "frameEndpoint": ""})),
        )
        for i, agent_id in enumerate(agent_ids)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    return state["max"]


def test_two_turns_for_one_agent_never_run_at_the_same_time(home, posted, monkeypatch):
    assert _run_both(monkeypatch, ("ceo", "ceo")) == 1


def test_two_agents_still_run_concurrently(home, posted, monkeypatch):
    """The lock is per agent, not global: the Engineering agent must not queue behind the CEO
    -- they have different profile dirs, so there is nothing to serialize."""
    assert _run_both(monkeypatch, ("ceo", "engineering")) == 2
