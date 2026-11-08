"""Worker runs heartbeat the backend and post their terminal status once (wave-2 O Task 4)."""
import io
import json
import urllib.error

import pytest

from bridge import runs, server
REAL_POST_EVENT = runs._post_event  # captured at import, before conftest's recorder replaces it

from tests.test_runs_v1 import BODY, CREATE, AUTH, IDENTITY, RUN, Agent, quiet_turn, threads  # noqa: F401


@pytest.fixture
def real_poster(monkeypatch):
    monkeypatch.setattr(runs, "_post_event", REAL_POST_EVENT)
    calls = []

    class Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(req, timeout):
        calls.append((req.full_url, json.loads(req.data)))
        code = responses.pop(0) if responses else 200
        if code != 200:
            raise urllib.error.HTTPError(req.full_url, code, "x", {}, io.BytesIO(b""))
        return Resp()

    responses: list[int] = []
    monkeypatch.setattr(runs.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(runs.time, "sleep", lambda s: None)
    return calls, responses


def test_terminal_event_posted_once_and_retried(real_poster):
    calls, responses = real_poster
    responses.extend([503])
    assert runs._post_event(IDENTITY, {"kind": "terminal", "status": "completed"}) == 200
    assert len(calls) == 2  # the 503 was retried
    url, body = calls[-1]
    assert url.endswith(f"/api/v1/internal/runtime/org_test/runs/{RUN}/events")
    assert body["taskId"] == IDENTITY["taskId"] and body["attempt"] == 1 and body["kind"] == "terminal"


def test_4xx_is_not_retried(real_poster):
    calls, responses = real_poster
    responses.extend([403])
    assert runs._post_event(IDENTITY, {"kind": "heartbeat", "status": "running"}) == 403
    assert len(calls) == 1


def test_worker_run_posts_terminal_after_the_turn(quiet_turn, threads, monkeypatch, run_events):  # noqa: F811
    monkeypatch.setattr(runs.turn, "_construct_agent", lambda *a, **k: Agent("done"))
    assert server.handle_plugin(CREATE, "POST", AUTH, BODY)[0] == 202
    target = threads[0]
    target.run_now()
    terminal = [b for _, b in run_events if b["kind"] == "terminal"]
    assert len(terminal) == 1 and terminal[0]["status"] == "completed" and terminal[0]["output"] == "done"
    # the heartbeat thread was started with this run's identity
    assert any(t.target is runs._heartbeats for t in threads)


def test_heartbeat_event_every_interval(quiet_turn, monkeypatch, run_events):  # noqa: F811
    from threading import Event

    statuses = iter([{"status": "running", "recentTools": [{"name": "web"}]}, {"status": "completed"}])
    monkeypatch.setattr(runs.turn, "durable_run_status", lambda *a, **k: next(statuses))
    monkeypatch.setattr(runs, "HEARTBEAT_EVERY_S", 0.01)
    runs._heartbeats(IDENTITY, Event())
    assert run_events == [(IDENTITY, {"kind": "heartbeat", "status": "running", "recentTools": [{"name": "web"}], "credentialsDue": True})]


def test_terminal_posted_before_the_turn_ends(quiet_turn, threads, monkeypatch, run_events):  # noqa: F811
    order = []
    monkeypatch.setattr(runs.turn, "_construct_agent", lambda *a, **k: Agent("x" * 70000))
    monkeypatch.setattr(runs.turn, "_end_turn", lambda: order.append("end_turn"))
    real = runs._post_event
    monkeypatch.setattr(runs, "_post_event", lambda i, b: order.append(b["kind"]) or real(i, b))
    server.handle_plugin(CREATE, "POST", AUTH, {**BODY, "identity": {**IDENTITY, "runSecret": "s3cr3t"}})
    threads[0].run_now()
    assert order == ["terminal", "end_turn"]
    sent = [b for _, b in run_events if b["kind"] == "terminal"][0]
    assert len(sent["output"]) == runs.MAX_EVENT_OUTPUT


def test_event_carries_the_run_secret(real_poster):
    calls, _ = real_poster
    runs._post_event({**IDENTITY, "runSecret": "s3cr3t"}, {"kind": "heartbeat", "status": "running"})
    assert calls[0][1]["runSecret"] == "s3cr3t"


def test_run_secret_never_lands_in_the_run_record(quiet_turn, threads, monkeypatch, run_events):  # noqa: F811
    server.handle_plugin(CREATE, "POST", AUTH, {**BODY, "identity": {**IDENTITY, "runSecret": "s3cr3t"}})
    assert "runSecret" not in runs.turn.RUNS[IDENTITY["runId"]]["identity"]
    assert threads[0].args[0]["runSecret"] == "s3cr3t"  # the thread's copy posts it (test above)


def test_terminal_carries_the_results_files_and_no_media_line(quiet_turn, threads, monkeypatch, run_events):  # noqa: F811
    monkeypatch.setattr(runs.turn, "_construct_agent", lambda *a, **k: Agent("Done.\nMEDIA:/x/agenda.docx"))
    seen = []
    monkeypatch.setattr(runs.turn, "task_result_media", lambda agent, task, out: seen.append((agent, task)) or (
        "Done.", [{"attachmentId": "a1", "fileName": "agenda.docx", "contentType": "application/pdf"}]))
    server.handle_plugin(CREATE, "POST", AUTH, {**BODY, "identity": {**IDENTITY, "runSecret": "s3cr3t"}})
    threads[0].run_now()
    sent = [b for _, b in run_events if b["kind"] == "terminal"][0]
    assert seen == [(IDENTITY["agentId"], IDENTITY["taskId"])]
    assert sent["output"] == "Done." and sent["attachments"][0]["fileName"] == "agenda.docx"
