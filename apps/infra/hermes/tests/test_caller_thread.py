"""A phone caller's thread (request principal "caller"; governance P2, the caller case, founder 8 Nov): only the
caller's booking tools and task_note, and a vend that holds no owner integration token."""
import pytest

from bridge import credentials, runs, server, turn
from plugins import lightbulb
from tests.test_runs_v1 import AGENT, AUTH, CREATE, IDENTITY, REQUEST, TASK, Agent, quiet_turn, threads  # noqa: F401

WORKER = {**IDENTITY, "runSecret": "s3cret"}


def _run(monkeypatch, threads, request):  # noqa: F811
    built, vends = {}, []
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: built.update(k) or Agent())
    monkeypatch.setattr(turn, "profile_toolsets", lambda: (["terminal", "browser", "mcp-stripe", "lightbulb",
                                                           turn.FRONT_DESK_TOOLSET], None))
    monkeypatch.setattr(turn, "runtime_mcp_env", lambda agent_id, **kw: vends.append(kw) or {})
    assert server.handle_plugin(CREATE, "POST", AUTH, {"identity": WORKER, "request": request})[0] == 202
    threads[0].run_now()
    return built, vends


def test_a_callers_thread_gets_only_the_front_desk(quiet_turn, threads, monkeypatch):  # noqa: F811
    built, vends = _run(monkeypatch, threads, {**REQUEST, "principal": "caller"})
    assert built["enabled_toolsets"] == ["lightbulb_task", "lightbulb_front_desk"]
    assert built["skip_memory"] is True  # no MEMORY.md/USER.md, no team recall or task board prefetch
    assert vends == [{"task_id": TASK, "principal": "caller"}]


def test_a_members_thread_never_gets_the_front_desk(quiet_turn, threads, monkeypatch):  # noqa: F811
    built, vends = _run(monkeypatch, threads, REQUEST)
    assert "lightbulb_front_desk" not in (built.get("enabled_toolsets") or [])
    assert "skip_memory" not in built
    assert vends == [{"task_id": TASK}]  # the backend still decides the principal from the task row


def test_an_unknown_principal_is_refused():
    with pytest.raises(runs.Refused):
        runs.validate_request({**REQUEST, "principal": "owner"})


def test_the_caller_vend_names_its_principal(monkeypatch):
    sent = []
    monkeypatch.setattr(credentials, "_post", lambda path, body, *a, **k: sent.append(body) or (200, {"tokens": {}}))
    monkeypatch.setattr(credentials, "apply", lambda *a: None)
    credentials.vend("friday", api="https://api", owner="org_test", hook_token="h", principal="caller", task_id="t1")
    assert sent == [{"agent_id": "friday", "principal": "caller", "task_id": "t1"}]


def test_front_desk_toolset_names_match():
    assert lightbulb.FRONT_DESK_TOOLSET == turn.FRONT_DESK_TOOLSET


def test_the_models_args_never_overwrite_the_runs_identity(monkeypatch):
    sent = []
    ctx = {"taskId": "t1", "runId": "r1", "attempt": 1, "runSecret": "real", "mode": "worker",
           "apiUrl": "https://api", "ownerId": "org_test", "hookToken": "h"}
    monkeypatch.setattr(lightbulb.turn_context, "read", lambda _s: ctx)
    monkeypatch.setattr(lightbulb, "_post", lambda url, tok, body: sent.append(body) or {})
    lightbulb._front_desk({"action": "my_bookings", "taskId": "other", "runSecret": "forged"})
    assert sent[0]["taskId"] == "t1" and sent[0]["runSecret"] == "real"
