"""Task 9: the run registry + abort_run. The abort HTTP route itself lives in
bridge/server.py (lane B's file / the controller's merge) — see lane-A-report.md for the
exact route branch text these turn-side functions are meant to sit behind."""
from bridge import turn
from tests import factories as f

RUN = "22222222-2222-4222-8222-222222222222"


def test_aborting_a_queued_turn_marks_it_without_touching_an_agent():
    turn.RUNS.clear(); turn.register_run(RUN)
    assert turn.abort_run(RUN) is True
    assert turn.RUNS[RUN]["aborted"] is True and turn.RUNS[RUN]["agent"] is None


def test_aborting_a_running_turn_hard_interrupts_the_agent():
    turn.RUNS.clear(); turn.register_run(RUN)
    calls = []
    turn.attach_agent(RUN, type("A", (), {"interrupt": lambda self, **kw: calls.append(kw) or True})())
    assert turn.abort_run(RUN) is True
    assert calls == [{"hard_cancel": True, "tool_reason": "explicit stop requested"}]


def test_an_unregistered_or_already_ended_run_cannot_be_aborted():
    turn.RUNS.clear()
    assert turn.abort_run(RUN) is False
    turn.register_run(RUN)
    turn.finish_run(RUN, "completed")
    assert turn.abort_run(RUN) is False


# ---- Fix round (Important 4): bound the registry -----------------------------------------------
def test_the_registry_never_exceeds_its_cap_once_entries_finish():
    turn.RUNS.clear()
    for i in range(turn.MAX_RUNS_ENTRIES + 50):
        run_id = f"r{i}"
        turn.register_run(run_id)
        turn.finish_run(run_id, "completed")  # each call prunes if now over the cap
    assert len(turn.RUNS) <= turn.MAX_RUNS_ENTRIES


def test_pruning_never_drops_a_queued_or_running_entry():
    turn.RUNS.clear()
    turn.register_run("still-queued")
    for i in range(turn.MAX_RUNS_ENTRIES + 50):
        run_id = f"r{i}"
        turn.register_run(run_id)
        turn.finish_run(run_id, "completed")
    assert "still-queued" in turn.RUNS
    assert turn.RUNS["still-queued"]["state"] == "queued"


def test_a_turn_aborted_before_it_launched_reports_zero_usage(home, posted, monkeypatch):
    turn.RUNS.clear()
    body = f.ingress()
    turn.register_run(body["run"]["runId"])
    turn.abort_run(body["run"]["runId"])
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not launch")))
    turn.run_turn("ceo", body)
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "aborted" and ended["usage"] == turn.ZERO_USAGE


def test_a_normal_turn_still_registers_and_finishes_in_the_registry(home, posted, monkeypatch):
    turn.RUNS.clear()
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "ack", turn.ZERO_USAGE))
    body = f.ingress()
    turn.run_turn("ceo", body)
    assert turn.RUNS[body["run"]["runId"]]["state"] == "ended"
    assert turn.RUNS[body["run"]["runId"]]["outcome"] == "completed"


# ---- review fix (IMPORTANT 2): mid-flight abort ----------------------------------------------
# The pre-launch check (test above) only catches an abort that lands before _run_hermes is
# ever called. A run already IN _run_hermes when abort_run fires must also end up "aborted",
# and the tokens it already spent must not be thrown away.
def test_a_mid_flight_abort_after_the_agent_is_attached_overrides_the_outcome_but_keeps_usage(home, posted, monkeypatch):
    turn.RUNS.clear()
    body = f.ingress()
    run_id = body["run"]["runId"]
    spent_usage = {**turn.ZERO_USAGE, "inputTokens": 42}
    interrupted = []

    class FakeAgent:
        def interrupt(self, **kw):
            interrupted.append(kw)
            return True

    def fake_run_hermes(agent_id, body, frames):
        turn.attach_agent(run_id, FakeAgent())
        turn.abort_run(run_id)  # a concurrent caller stops the run while it is "running"
        return "completed", None, "partial reply that should be discarded", spent_usage

    monkeypatch.setattr(turn, "_run_hermes", fake_run_hermes)
    turn.run_turn("ceo", body)

    assert interrupted == [{"hard_cancel": True, "tool_reason": "explicit stop requested"}]
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "aborted" and ended["usage"] == spent_usage  # tokens were spent
    assert not any(p.startswith("agent-messages/") for p, _b in posted)  # the reply is discarded


def test_an_abort_landing_before_attach_agent_is_still_caught_by_the_post_check(home, posted, monkeypatch):
    """TOCTOU: abort_run can land in the queued window between run_turn's pre-launch check
    and _construct_agent/attach_agent ever running, with no live agent to interrupt. The
    post-check after _run_hermes returns must still catch it via RUNS[run_id]['aborted'],
    which abort_run sets regardless of whether an agent was attached."""
    turn.RUNS.clear()
    body = f.ingress()
    run_id = body["run"]["runId"]

    def fake_run_hermes(agent_id, body, frames):
        assert turn.RUNS[run_id]["agent"] is None  # never attached in this simulated race
        assert turn.abort_run(run_id) is True  # still succeeds with no agent to interrupt
        return "completed", None, "reply that should be discarded", turn.ZERO_USAGE

    monkeypatch.setattr(turn, "_run_hermes", fake_run_hermes)
    turn.run_turn("ceo", body)
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "aborted"
    assert not any(p.startswith("agent-messages/") for p, _b in posted)


# ---- prod 2026-09-25: Stop during setup let the whole turn run -----------------------------
# The founder's Stop landed 7 s into a turn, while the profile/MCP/agent were still being set
# up. abort_run found no agent, so it only set the flag; the agent attached later was never
# interrupted and ran 14 model calls and 1m45s of browser work before the outcome said aborted.
def test_a_stop_that_landed_before_the_agent_existed_interrupts_it_on_attach():
    turn.RUNS.clear(); turn.register_run(RUN)
    assert turn.abort_run(RUN) is True
    calls = []
    turn.attach_agent(RUN, type("A", (), {"interrupt": lambda self, **kw: calls.append(kw) or True})())
    assert calls == [{"hard_cancel": True, "tool_reason": "explicit stop requested"}]


def test_a_turn_stopped_during_setup_never_starts_its_conversation(home, posted, monkeypatch):
    from types import SimpleNamespace
    body = f.ingress(run={"runId": RUN, "workspaceId": f.ROOM, "frameChannel": "", "frameEndpoint": ""})

    def construct(*_a):
        turn.abort_run(RUN)  # Stop arrives while MCP servers connect
        return SimpleNamespace(interrupt=lambda **kw: True,
                               run_conversation=lambda **kw: (_ for _ in ()).throw(AssertionError("ran")))

    monkeypatch.setattr(turn, "_construct_agent", construct)
    turn.RUNS.clear()
    turn.run_turn("ceo", body)
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "aborted"


def test_stop_interrupts_a_long_running_tool_and_the_turn_ends_aborted_promptly(home, posted, monkeypatch):
    """A running conversation blocked in a tool (a terminal command, a browser action) returns
    as soon as Hermes's interrupt fires -- the turn ends within seconds, not when the work would
    have finished. The fake stands in for Hermes: its tool waits on the interrupt, and a steer
    left behind must not start another pass."""
    import threading
    import time
    from types import SimpleNamespace

    started, interrupted = threading.Event(), threading.Event()

    def run_conversation(**_kw):
        started.set()
        assert interrupted.wait(30), "never interrupted"  # the "tool": a 30 s command
        return {"final_response": "", "interrupted": True, "pending_steer": "Ada: more"}

    agent = SimpleNamespace(run_conversation=run_conversation,
                            interrupt=lambda **kw: interrupted.set() or True)
    monkeypatch.setattr(turn, "_construct_agent", lambda *a: agent)
    turn.RUNS.clear()
    body = f.ingress(run={"runId": RUN, "workspaceId": f.ROOM, "frameChannel": "", "frameEndpoint": ""})
    worker = threading.Thread(target=turn.run_turn, args=("ceo", body))
    worker.start()
    assert started.wait(10)
    t0 = time.monotonic()
    assert turn.abort_run(RUN) is True
    worker.join(10)
    assert not worker.is_alive() and time.monotonic() - t0 < 5
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "aborted"
    assert not any(p.startswith("agent-messages/") for p, _b in posted)


def test_after_stop_only_the_closing_frames_are_published():
    sent = []
    frames = turn.FramePublisher("https://x", "ch", "room", "ceo", RUN, None,
                                 publish=lambda _e, _c, *events: sent.extend(events))
    turn.RUNS.clear(); turn.register_run(RUN)
    frames.tool_start("c1", "browser_navigate", {})
    turn.abort_run(RUN)
    frames.tool_start("c2", "browser_click", {})
    frames.delta("more text")
    frames.finished("aborted", "", None)
    assert [e["payload"]["frame"]["type"] for e in sent] == ["tool_start", "error", "run_ended", "run_active"]
    assert sent[0]["payload"]["frame"]["toolCallId"] == "c1"


def test_stop_ends_the_browser_clients_the_run_holds(monkeypatch):
    from bridge import browser_control
    reaped, closed = [], []
    monkeypatch.setattr(browser_control, "_disconnect", lambda task: reaped.append(task) or True)
    monkeypatch.setattr(browser_control.cdp_mux, "close_run", lambda run, upstream=None: closed.append(run))
    monkeypatch.setattr(browser_control, "_BROWSING", {RUN: {"t1"}, "someone-else": {"t2"}})
    monkeypatch.setattr(browser_control, "_LEASE", {})
    turn.RUNS.clear(); turn.register_run(RUN)
    turn.abort_run(RUN)
    assert reaped == ["t1"] and closed == [RUN]
    assert "someone-else" in browser_control._BROWSING  # another run's browser is not touched
