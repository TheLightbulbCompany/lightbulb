"""task_note: a Worker's note in its own task's room. Offered only on a super-agent Worker run (its own toolset),
the handler checks the run again, and the ids come from the turn, never the model."""
import io
import pathlib
import sys
import urllib.error

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb  # noqa: E402
from lightbulb import turn_context  # noqa: E402

from bridge import server, turn  # noqa: E402
from tests.test_runs_v1 import AUTH, CREATE, IDENTITY, REQUEST, Agent, quiet_turn, threads  # noqa: E402, F401
from tests.test_voice_call_run import env  # noqa: E402, F401 -- the _run_call harness

TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
SESSION = f"agent:ceo:lightbulb:task:{TASK}"


def _ctx(tmp_path, monkeypatch, **over):
    monkeypatch.setattr(turn_context, "_home", lambda: tmp_path)
    base = {"apiUrl": "https://api.invalid/", "ownerId": "org_test", "hookToken": "h", "agentId": "ceo",
            "runId": "run-1", "attempt": 2, "runSecret": "sec", "taskId": TASK, "mode": "worker",
            "roomId": None, "sessionKey": SESSION}
    turn_context.write(tmp_path, SESSION, {**base, **over})


def test_task_note_only_on_a_task_run(tmp_path, monkeypatch):
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))
    _ctx(tmp_path, monkeypatch, taskId=None, runSecret=None, roomId="11111111-1111-4111-8111-111111111111")
    assert lightbulb._task_note({"text": "hi"}, session_id=SESSION) == lightbulb.TASK_NOTE_ONLY
    _ctx(tmp_path, monkeypatch, mode="router")
    assert lightbulb._task_note({"text": "hi"}, session_id=SESSION) == lightbulb.TASK_NOTE_ONLY


def test_task_note_posts_the_turns_ids(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post", lambda url, token, body: calls.append((url, token, body)) or {})
    out = lightbulb._task_note({"text": " Found the PO. ", "taskId": "evil"}, session_id=SESSION)
    assert out.startswith("Posted")
    assert calls == [("https://api.invalid/api/v1/internal/runtime/org_test/notes", "h",
                      {"taskId": TASK, "runId": "run-1", "attempt": 2, "runSecret": "sec", "text": "Found the PO."})]

    def stale(*_a):
        raise urllib.error.HTTPError("u", 409, "Conflict", {}, io.BytesIO(b'{"detail":"stale_run"}'))

    monkeypatch.setattr(lightbulb, "_post", stale)
    assert lightbulb._task_note({"text": "x"}, session_id=SESSION) == lightbulb.TASK_STALE_RUN


def test_registered_under_its_own_toolset():
    src = pathlib.Path(lightbulb.__file__).read_text()
    assert 'ctx.register_tool("task_note", TASK_TOOLSET, TASK_NOTE_SCHEMA, _task_note' in src
    assert lightbulb.TASK_TOOLSET == turn.TASK_TOOLSET == "lightbulb_task"


def test_task_toolset_off_every_other_turn(monkeypatch):
    import types

    monkeypatch.setattr(turn, "profile_toolsets", lambda: (["lightbulb", "lightbulb_task", "memory"], None))
    captured = {}

    class FakeAIAgent:
        def __init__(self, **kw):
            captured.update(kw)

    monkeypatch.setitem(sys.modules, "run_agent", types.SimpleNamespace(AIAgent=FakeAIAgent))
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda agent_id: {})
    monkeypatch.setattr(turn, "profile_model", lambda c: ("m", "p"))
    monkeypatch.setattr(turn, "_session_db", lambda a: None)
    monkeypatch.setattr(turn, "_credential_pool", lambda *a: None)
    monkeypatch.setattr(turn, "_capture_api_errors", lambda a: None)
    turn._construct_agent("agent-1", {}, None, "agent:agent-1:lightbulb:room:r1")
    assert captured["enabled_toolsets"] == ["lightbulb", "memory"]


@pytest.mark.parametrize("secret,offered", [("s3", True), (None, False)])
def test_a_worker_run_enables_the_task_toolset(quiet_turn, threads, monkeypatch, secret, offered):  # noqa: F811
    monkeypatch.setattr(turn, "profile_toolsets", lambda: (["lightbulb", "lightbulb_router", "memory"], None))
    built = {}
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: built.update(k) or Agent())
    identity = {**IDENTITY, "runSecret": secret} if secret else IDENTITY
    assert server.handle_plugin(CREATE, "POST", AUTH, {"identity": identity, "request": REQUEST})[0] == 202
    threads[0].run_now()
    if offered:
        assert built["enabled_toolsets"] == ["lightbulb", "memory", "lightbulb_task"]
    else:
        assert "enabled_toolsets" not in built


# ---- gating: the tool reaches the model only on a Worker run with a runSecret ----------------------------
def test_router_run_never_gets_the_task_toolset(quiet_turn, threads, monkeypatch):  # noqa: F811
    from tests.test_runs_v1_router import IDENTITY as ROUTER, _post

    monkeypatch.setattr(turn, "profile_toolsets", lambda: (["lightbulb", "lightbulb_task", "memory"], None))
    built = {}
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: built.update(k) or Agent())
    assert _post({**ROUTER, "runSecret": "s3"})[0] == 202
    threads[0].run_now()
    assert built["enabled_toolsets"] == ["lightbulb_router"]


def test_paperclip_run_never_gets_the_task_toolset(monkeypatch):
    from tests.test_task_own_session import IDENTITY as PC, REQUEST as PC_REQUEST

    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    built = {}
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: built.update(k, called=True) or Agent())
    turn.register_paperclip_run(PC, PC_REQUEST)
    turn._run_native_paperclip({**PC, "taskId": TASK, "runSecret": "s3"}, PC_REQUEST)
    assert built.get("called") and "enabled_toolsets" not in built  # _construct_agent's own strip applies


def test_a_call_never_gets_the_task_toolset(env, home, monkeypatch):  # noqa: F811
    from tests.test_voice_call_run import IDENTITY as CALL, REQUEST as CALL_REQUEST, _frames

    monkeypatch.setattr(turn, "profile_toolsets", lambda: (["lightbulb", "lightbulb_task", "memory"], None))
    turn.register_call_run(CALL)
    assert turn._run_call(CALL, CALL_REQUEST, _frames())[0] == "completed"
    assert env["limits"]["enabled_toolsets"] == ["lightbulb", "memory", "lightbulb_voice_work"]


# ---- business_facts: a Worker reads its own business's Agent page (router-latency-2026-11-08) ----------------
def test_business_facts_on_a_worker_run_reads_the_turns_workspace(tmp_path, monkeypatch):
    _ctx(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(lightbulb, "_post",
                        lambda url, token, body: calls.append((url, token, body)) or {"facts": "Services: Cut $40."})
    assert lightbulb._business_facts({"ownerId": "org_evil"}, session_id=SESSION) == "Services: Cut $40."
    assert calls == [("https://api.invalid/api/v1/internal/runtime/org_test/business-facts", "h",
                      {"taskId": TASK, "runId": "run-1", "attempt": 2, "runSecret": "sec"})]
    monkeypatch.setattr(lightbulb, "_post", lambda *a: {"facts": ""})
    assert lightbulb._business_facts({}, session_id=SESSION) == lightbulb.NO_FACTS
    src = pathlib.Path(lightbulb.__file__).read_text()
    assert 'ctx.register_tool("business_facts", TASK_TOOLSET, BUSINESS_FACTS_SCHEMA, _business_facts' in src


def test_business_facts_only_on_a_task_run(tmp_path, monkeypatch):
    monkeypatch.setattr(lightbulb, "_post", lambda *a: pytest.fail("no HTTP call"))
    _ctx(tmp_path, monkeypatch, mode="router")
    assert "only on a Lightbulb task run" in lightbulb._business_facts({}, session_id=SESSION)
