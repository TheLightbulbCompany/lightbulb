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


def test_a_turn_aborted_while_queued_never_runs(home, posted, monkeypatch):
    """Queueing widens the abort window: without a re-check after the lock, a turn aborted
    while it waits still burns a whole conversation and only then reports `aborted`."""
    ran, entered, release, queued = [], threading.Event(), threading.Event(), threading.Event()

    def run_conversation(**_kwargs):
        ran.append(1)
        entered.set()
        release.wait(10)
        return {"api_calls": 1, "final_response": "ack"}

    monkeypatch.setattr(turn, "_construct_agent",
                        lambda *args: SimpleNamespace(run_conversation=run_conversation))
    # Seeing the second turn start waiting for its room proves it is past run_turn's own
    # pre-lock abort check: it waits first to see whether the room's job is long (_side_session),
    # then on _turn_lock, and a stop must end either wait.
    real = turn._side_session
    monkeypatch.setattr(turn, "_side_session", lambda *a: (queued.set() if ran else None, real(*a))[1])

    def start(run_id):
        thread = threading.Thread(target=turn.run_turn, args=(
            "ceo", f.ingress(run={"runId": run_id, "workspaceId": f.ROOM,
                                  "frameChannel": "", "frameEndpoint": ""})))
        thread.start()
        return thread

    first = start("run-first")
    assert entered.wait(10)
    second = start("run-queued")
    assert queued.wait(10)          # second turn is inside _run_hermes, waiting on the lock
    assert turn.abort_run("run-queued") is True
    second.join(10)                 # it leaves the lock wait at once, not when the first ends
    assert not second.is_alive() and first.is_alive()
    release.set()
    first.join(10)

    assert ran == [1]               # the queued turn never reached run_conversation
    ended = {b["runId"]: b for path, b in posted if b.get("type") == "run.ended"}
    assert ended["run-queued"]["outcome"] == "aborted"
    assert ended["run-queued"]["usage"] == turn.ZERO_USAGE
    assert ended["run-first"]["outcome"] == "completed"


def test_a_turn_behind_a_long_job_is_keyed_to_its_side_session_before_anything_is_set_up(home, posted, monkeypatch):
    """Decided in run_turn, before the turn context, the Mac tool's ledger and the delegation
    bookkeeping exist: each is the side session's, so the long job keeps its own delegations."""
    room_key, seen = turn._resolve_session_key("ceo", f.ingress()), {}
    monkeypatch.setattr(turn, "LAST_TURN", {})
    monkeypatch.setattr(turn, "_start_desktop_turn", lambda _p, ctx: seen.update(desktop=ctx["sessionKey"]))
    monkeypatch.setattr(turn, "_run_hermes", lambda _a, body, _f: (
        seen.update(body=body), ("completed", None, "ok", dict(turn.ZERO_USAGE)))[1])
    with turn._turn_lock("ceo", room_key):
        turn._lock_since[("ceo", room_key)] = turn.time.monotonic() - 31
        turn.run_turn("ceo", f.ingress(run={"runId": "run-side", "workspaceId": f.ROOM,
                                             "frameChannel": "", "frameEndpoint": ""}))
    side = turn.side_session_key("ceo", "run-side")
    assert seen["desktop"] == side and seen["body"]["sessionKey"] == side
    assert seen["body"][turn.ROOM_KEY_FIELD] == room_key
    assert side in turn.LAST_TURN and room_key not in turn.LAST_TURN
    ended = [b for _p, b in posted if b.get("type") == "run.ended"]
    assert ended[0]["sessionKey"] == room_key  # the backend still knows the run by its room
