"""`POST /internal/v1/curate` (spec 07 §3.5, lane V5b): the curator for one specialist, on the
workspace VM, after its learning merges; refused in task mode and while a turn is live."""
import contextlib
import sys
import types

import pytest

from bridge import server, sync, task_mode, turn

AGENT = "a9e8d7c6-b5a4-4392-8170-6f5e4d3c2b1a"
TASK = "7d1e2f30-4a5b-4c6d-8e9f-0a1b2c3d4e5f"
AUTH = {"authorization": "Bearer gw-test"}


def _call(body):
    return server.handle_plugin("/internal/v1/curate", "POST", AUTH, body)


@pytest.fixture(autouse=True)
def _no_task(monkeypatch):
    monkeypatch.setattr(task_mode, "TASK", None)
    monkeypatch.setattr(server.network, "ensure_started", lambda **kw: True)


@pytest.fixture
def curator(monkeypatch):
    """A fake `agent.curator`, and the turn's restore/ensure/save around it, all recording."""
    calls = []
    mod = types.SimpleNamespace(
        should_run_now=lambda: (calls.append("should_run_now"), True)[1],
        # Hermes's review must run under the profile lock, on this thread, with the home scope held.
        run_curator_review=lambda synchronous: calls.append(("review", synchronous, sync.profile_lock(AGENT)._is_owned())),
    )
    pkg = types.ModuleType("agent")
    pkg.curator = mod
    monkeypatch.setitem(sys.modules, "agent", pkg)
    monkeypatch.setitem(sys.modules, "agent.curator", mod)
    monkeypatch.setattr(turn, "restore_profile", lambda agent_id: calls.append("restore"))
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda agent_id: calls.append("ensure"))
    monkeypatch.setattr(turn, "_hermes_home_scope", lambda agent_id: contextlib.nullcontext())

    def save(agent_id):  # the change-only save: the seed/review wrote the curator state
        calls.append("save")
        sync._MTIMES.setdefault(agent_id, {})["skills/.curator_state"] = 1

    monkeypatch.setattr(turn, "save_profile", save)
    return calls, mod


def test_curate_endpoint_runs_under_profile_lock_and_saves(home, curator):
    calls, mod = curator
    assert _call({"agentId": AGENT}) == (200, {"ran": True, "changed": True})
    # the merged files come down (ensure) before the curator looks; the review holds the lock
    assert calls == ["restore", "ensure", "should_run_now", ("review", True, True), "save"]
    assert turn._busy == 0 and AGENT in turn.RESTORED
    calls.clear()
    mod.should_run_now = lambda: (calls.append("should_run_now"), False)[1]
    assert _call({"agentId": AGENT}) == (200, {"ran": False, "changed": False})  # not due; nothing moved
    assert calls == ["restore", "ensure", "should_run_now", "save"]


def test_curate_refused_in_task_mode(home, curator):
    calls, _ = curator
    task_mode.latch({"taskId": TASK, "agentId": AGENT, "ownerId": "org_test", "attempt": 1})
    assert _call({"agentId": AGENT}) == (409, {"error": "task_mode"})
    assert calls == []  # the curator never runs in a Worker VM (spec 07 §3.5)


def test_curate_busy_or_recycling_is_refused(home, curator):
    calls, _ = curator
    turn._busy = 1  # a turn is live
    assert _call({"agentId": AGENT}) == (409, {"error": "busy"})
    assert turn._busy == 1
    turn._busy = 0
    assert turn.hold_turns(force=True)  # a stop is pending
    try:
        assert _call({"agentId": AGENT}) == (503, {"error": "recycling"})
    finally:
        turn.release_hold()
    assert calls == [] and turn._busy == 0


def test_curate_invalid_body(home, curator):
    assert _call({}) == (400, {"error": "invalid_request"})
    assert _call({"agentId": ""}) == (400, {"error": "invalid_request"})
    assert _call("nope") == (400, {"error": "invalid_request"})
    assert server.handle_plugin("/internal/v1/curate", "GET", AUTH, {"agentId": AGENT}) == (404, {"error": "not_found", "path": "/internal/v1/curate"})
