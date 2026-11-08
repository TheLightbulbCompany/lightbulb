"""`session.kind == "task"` on /internal/v1/runs (spec 02 §3.3, lane V3b): the first such run
latches the VM, a later attempt of the same task resumes its transcript, the attempt is sealed
after the final save, and any other task (or a workspace-kind run) is refused."""
import pytest

from bridge import runs, server, task_mode, turn
from tests.test_paperclip_gateway import _capturing_threading
from tests.test_runs_v1 import AUTH, IDENTITY, REQUEST, Agent, quiet_turn  # noqa: F401

RUN2 = "1c6d7d4a-7b54-4c8f-ae2b-4a1a3e8d2b22"
OTHER_TASK = "8e2f3a41-5b6c-4d7e-9f0a-1b2c3d4e5f60"
OTHER_RUN = "2d7e8e5b-8c65-4d9a-bf3c-5b2b4f9e3c33"
TASK_IDENTITY = {**IDENTITY, "session": {"kind": "task"}}
CREATE = "/internal/v1/runs"


@pytest.fixture(autouse=True)
def _no_task(monkeypatch):
    monkeypatch.setattr(task_mode, "TASK", None)
    monkeypatch.setattr(server.network, "ensure_started", lambda **kw: True)


@pytest.fixture
def threads(monkeypatch):
    threading, created = _capturing_threading()
    monkeypatch.setattr(runs, "threading", threading)
    return created


def _post(identity, request=REQUEST):
    return server.handle_plugin(CREATE, "POST", AUTH, {"identity": identity, "request": request})


def _poll(identity):
    return server.handle_plugin(f"{CREATE}/{identity['runId']}", "GET", AUTH, {"identity": identity})


def test_validate_identity_accepts_task_kind_for_worker_only():
    assert runs.validate_identity(TASK_IDENTITY)["session"] == {"kind": "task"}
    router = {**TASK_IDENTITY, "mode": "router", "lane": "shared", "room": "r:0123456789abcdef",
              "sessionKey": f"agent:{IDENTITY['agentId']}:lightbulb:router:shared:r:0123456789abcdef"}
    with pytest.raises(runs.Refused) as refused:
        runs.validate_identity(router)
    assert refused.value.reply == runs.UNSUPPORTED
    with pytest.raises(runs.Refused) as refused:
        runs.validate_identity({**IDENTITY, "session": {"kind": "shared"}})
    assert refused.value.reply == runs.UNSUPPORTED


def test_task_session_latches_and_resumes(quiet_turn, threads, monkeypatch):
    sessions = []
    agent = Agent()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: (sessions.append(a[3]), agent)[1])

    code, accepted = _post(TASK_IDENTITY)
    assert code == 202 and accepted["status"] == "queued"
    assert task_mode.active() == {"taskId": IDENTITY["taskId"], "agentId": IDENTITY["agentId"],
                                  "ownerId": "org_test", "attempt": 1}
    threads[0].run_now()
    assert _poll(TASK_IDENTITY)[1]["status"] == "completed"

    second = {**TASK_IDENTITY, "runId": RUN2, "attempt": 2}
    assert _post(second)[0] == 202
    assert task_mode.active()["attempt"] == 2
    threads[-1].run_now()  # the newest captured thread is attempt 2's run (run 1 also spawned its heartbeat)
    assert _poll(second)[1]["status"] == "completed"
    assert sessions == [IDENTITY["sessionKey"]] * 2  # attempt 2 continues the task's own transcript


def test_task_session_seals_after_final_save(quiet_turn, threads, monkeypatch):
    order = []
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: (order.append("turn"), Agent())[1])
    monkeypatch.setattr(turn, "save_profile", lambda agent_id: order.append("save"))
    monkeypatch.setattr(turn.sync, "write_base_manifest", lambda agent_id, attempt: order.append(("base", attempt)))
    monkeypatch.setattr(turn.sync, "seal_attempt", lambda agent_id, task_id, attempt: order.append(("seal", agent_id, task_id, attempt)))
    identity = {**TASK_IDENTITY, "attempt": 2}
    assert _post(identity)[0] == 202
    threads[0].run_now()
    assert order == [("base", 2), "turn", "save", ("seal", IDENTITY["agentId"], IDENTITY["taskId"], 2)]


def test_workspace_run_does_not_seal_or_latch(quiet_turn, threads, monkeypatch):
    order = []
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: Agent())
    monkeypatch.setattr(turn.sync, "write_base_manifest", lambda *a: order.append("base"))
    monkeypatch.setattr(turn.sync, "seal_attempt", lambda *a: order.append("seal"))
    assert _post(IDENTITY)[0] == 202
    threads[0].run_now()
    assert order == [] and task_mode.active() is None


def test_task_poll_and_steer_latch_a_fresh_vm(monkeypatch):
    """#4769 review: a park ended the process; the backend's next poll lands on a FRESH microVM
    and must latch before the record read (else it looks in the specialist's .runs/, answers 404,
    and the attempt is re-dispatched after every park). A workspace-kind poll never latches."""
    seen = []

    def status(run_id, identity, *, kind):
        seen.append(task_mode.active())
        return {"run_id": run_id, "status": "completed", "session_id": identity["sessionKey"]}

    monkeypatch.setattr(turn, "durable_run_status", status)
    monkeypatch.setattr(turn, "steer_run", lambda *a, **k: True)
    assert _poll(IDENTITY)[0] == 200 and task_mode.active() is None
    assert _poll(TASK_IDENTITY)[0] == 200
    monkeypatch.setattr(task_mode, "TASK", None)
    body = {"identity": TASK_IDENTITY, "text": "go on", "kind": "user"}
    assert server.handle_plugin(f"{CREATE}/{IDENTITY['runId']}/steer", "POST", AUTH, body)[0] == 200
    latched = {"taskId": IDENTITY["taskId"], "agentId": IDENTITY["agentId"], "ownerId": "org_test", "attempt": 1}
    assert seen == [None, latched, latched]


def test_second_task_on_task_vm_refused(threads):
    assert _post(TASK_IDENTITY)[0] == 202
    other = {**TASK_IDENTITY, "runId": OTHER_RUN, "taskId": OTHER_TASK,
             "sessionKey": f"agent:{IDENTITY['agentId']}:lightbulb:task:{OTHER_TASK}"}
    assert _post(other) == (409, {"error": "task_mode"})
    assert _poll(other) == (409, {"error": "task_mode"})
    assert server.handle_plugin(f"{CREATE}/{OTHER_RUN}/stop", "POST", AUTH, {"identity": other}) == (409, {"error": "task_mode"})
    # A workspace-kind run on a latched VM would restore and write the specialist profile: refused.
    assert _post({**IDENTITY, "runId": OTHER_RUN}) == (409, {"error": "task_mode"})
    assert len(threads) == 1


def test_a_callers_task_run_latches_a_fresh_vm_and_never_starts_the_network(quiet_turn, threads, monkeypatch):
    """A workspace on task sessions creates a caller's run on a task VM: the task-VM gate admits caller-runs and,
    like a task run, it starts no network member there."""
    started = []
    monkeypatch.setattr(server.network, "ensure_started", lambda **kw: started.append(kw) or True)
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: Agent())
    body = {"identity": TASK_IDENTITY, "request": {**REQUEST, "principal": "caller"}}
    code, accepted = server.handle_plugin(runs.CALLER_RUNS_PATH, "POST", AUTH, body)
    assert code == 202 and accepted["status"] == "queued"
    assert task_mode.active()["taskId"] == IDENTITY["taskId"]
    assert started == []
    # and once latched, the same create is still admitted (not refused as "not a task run")
    assert server.handle_plugin(runs.CALLER_RUNS_PATH, "POST", AUTH, body)[0] in (200, 202)
