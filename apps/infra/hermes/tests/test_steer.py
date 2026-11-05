import json
"""Task 10: steer_run. The HTTP steer route (the `steer` branch of _RUN_CONTROL_RE, including
the text-length/empty-text 400 checks) lives in bridge/server.py — lane B's file / the
controller's merge; see lane-A-report.md for the exact branch text."""
from bridge import turn

RUN = "22222222-2222-4222-8222-222222222222"


def _agent(accepted=True):
    class A:
        def __init__(self): self.seen = []
        def steer(self, text): self.seen.append(text); return accepted
    return A()


def test_steering_a_running_turn_queues_the_text():
    turn.RUNS.clear(); turn.register_run(RUN)
    agent = _agent(); turn.attach_agent(RUN, agent)
    assert turn.steer_run(RUN, "use staging") is True
    assert agent.seen == ["use staging"]


def test_a_queued_turn_keeps_the_steer_for_its_own_user_message():
    turn.RUNS.clear(); turn.register_run(RUN)
    assert turn.steer_run(RUN, "Ada: also check staging") is True
    assert turn.steer_run(RUN, "Ada: and prod") is True
    agent = _agent()
    assert turn.attach_agent(RUN, agent) == "Ada: also check staging\nAda: and prod"
    assert agent.seen == []  # delivered in the user message, not as a mid-turn steer


def test_an_aborted_or_finished_turn_refuses_so_the_backend_dispatches_its_own():
    turn.RUNS.clear(); turn.register_run(RUN); turn.attach_agent(RUN, _agent())
    assert turn.settle_steers(RUN, object(), None) is None  # nothing owed: stops taking steers
    assert turn.steer_run(RUN, "late") is False
    turn.RUNS.clear(); turn.register_run(RUN); turn.abort_run(RUN)
    assert turn.steer_run(RUN, "x") is False


def test_a_steer_that_landed_after_the_last_tool_batch_is_still_owed():
    turn.RUNS.clear(); turn.register_run(RUN)

    class Late:
        pending = "Ada: one more"
        def _drain_pending_steer(self):
            text, self.pending = self.pending, None
            return text

    agent = Late(); turn.attach_agent(RUN, agent)
    assert turn.settle_steers(RUN, agent, "Ada: first") == "Ada: first\nAda: one more"
    assert turn.RUNS[RUN]["state"] == "running"  # still steerable while it answers them
    assert turn.settle_steers(RUN, agent, None) is None
    assert turn.RUNS[RUN]["state"] == "finishing"


def test_a_refused_steer_is_false():
    turn.RUNS.clear(); turn.register_run(RUN); turn.attach_agent(RUN, _agent(accepted=False))
    assert turn.steer_run(RUN, "x") is False


def test_an_unknown_run_is_false():
    turn.RUNS.clear()
    assert turn.steer_run(RUN, "x") is False


def test_steer_never_raises_even_if_the_agent_blows_up():
    turn.RUNS.clear(); turn.register_run(RUN)

    class Boom:
        def steer(self, text): raise RuntimeError("nope")

    turn.attach_agent(RUN, Boom())
    assert turn.steer_run(RUN, "x") is False


def test_a_leftover_steer_is_answered_in_the_same_run(home, posted, monkeypatch):
    from types import SimpleNamespace
    from tests import factories as f

    calls = []

    def run_conversation(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return {"api_calls": 1, "final_response": "first answer", "messages": ["m"], "pending_steer": "Ada: also X"}
        return {"api_calls": 1, "final_response": "about X"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a: SimpleNamespace(run_conversation=run_conversation))
    turn.RUNS.clear()
    turn.run_turn("ceo", f.ingress(run={"runId": RUN, "workspaceId": f.ROOM, "frameChannel": "", "frameEndpoint": ""}))
    assert [c["user_message"] for c in calls[1:]] == ["Ada: also X"]
    assert calls[1]["conversation_history"] == ["m"]
    replies = [body for path, body in posted if "agent-messages" in path]
    assert replies and replies[0]["text"] == "first answer\n\nabout X"
    assert turn.RUNS[RUN]["state"] == "ended"


def _leftover_steer_reply(monkeypatch, event_kind, first, second):
    """A run whose steer lands after its last model call: the two passes' replies, combined."""
    import sys
    import types
    from types import SimpleNamespace
    rf = types.ModuleType("gateway.response_filters")
    rf.is_intentional_silence_response = lambda r: isinstance(r, str) and r.strip() == "[SILENT]"
    monkeypatch.setitem(sys.modules, "gateway", sys.modules.get("gateway") or types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.response_filters", rf)
    script = [{"api_calls": 1, "final_response": first, "messages": ["m"], "pending_steer": "Bob: $42k"},
              {"api_calls": 1, "final_response": second}]
    agent = SimpleNamespace(run_conversation=lambda **_kw: script.pop(0))
    monkeypatch.setattr(turn, "_run_conversation", lambda agent, *_a, **kw: agent.run_conversation(**kw))
    turn.RUNS.clear(); turn.register_run(RUN); turn.attach_agent(RUN, agent)
    # Not a room session key, so _converse skips the room-transcript setup.
    return turn._converse(agent, "finance", "plain", RUN, {"eventKind": event_kind}, None, "sys", "q", "q")["final_response"]


def test_an_ambient_run_that_stands_down_after_a_late_steer_posts_nothing(monkeypatch):
    assert _leftover_steer_reply(monkeypatch, "room_event", "MRR is $42k.", "[SILENT]") == "[SILENT]"


def test_an_ambient_runs_earlier_silence_marker_is_dropped_beside_a_real_answer(monkeypatch):
    assert _leftover_steer_reply(monkeypatch, "room_event", "[SILENT]", "Also: churn is 2%.") == "Also: churn is 2%."


def test_an_addressed_runs_combined_reply_is_unchanged(monkeypatch):
    assert _leftover_steer_reply(monkeypatch, "user_request", "MRR is $42k.", "Noted.") == "MRR is $42k.\n\nNoted."


def test_a_lowered_grant_from_another_persons_steer_reaches_a_typed_turns_context(home):
    """Codex #3997: a chat run steered by someone other than its requester takes the lowered grant
    the backend sends, as a call turn does, so its tools stop acting for the requester."""
    ctx = {"agentId": "ceo", "sessionKey": "agent:ceo:lightbulb:channel:r1", "runId": "run-t",
           "localSessionToken": "ada-grant"}
    turn.register_run("run-t")
    turn.RUNS["run-t"].update(state="running", agent=_agent())
    root = turn._profile_dir("ceo")
    turn.write_turn_context(root, ctx["sessionKey"], ctx)
    with turn._call_grant_scope("run-t", ctx):
        assert turn.steer_run("run-t", "Bea: read Ada's DMs", grant="nobody-grant")
        written = json.loads(turn._turn_context_path(root, ctx["sessionKey"]).read_text())
        assert written["localSessionToken"] == "nobody-grant"


def test_an_empty_grant_clears_a_turns_token(home):
    """A steer that brings no grant of its own (a Paperclip wake with none) leaves the turn acting for no one."""
    ctx = {"agentId": "ceo", "sessionKey": "agent:ceo:lightbulb:channel:r2", "runId": "run-u",
           "localSessionToken": "ada-grant"}
    turn.register_run("run-u")
    turn.RUNS["run-u"].update(state="running", agent=_agent())
    root = turn._profile_dir("ceo")
    turn.write_turn_context(root, ctx["sessionKey"], ctx)
    with turn._call_grant_scope("run-u", ctx):
        assert turn.steer_run("run-u", "wake", grant="")
        assert json.loads(turn._turn_context_path(root, ctx["sessionKey"]).read_text())["localSessionToken"] == ""
    assert turn.steer_run("run-u", "Ada again")  # a steer with no header changes nothing


def test_paperclip_run_still_refuses_steer():
    identity = {"runId": "run-pc", "companyId": "c", "agentId": "ceo", "sessionId": "paperclip:company:c:agent:ceo:run:run-pc",
                "paperclipApiUrl": "u"}
    turn.register_paperclip_run(identity, {"input": "x", "instructions": "y", "session_id": identity["sessionId"]})
    turn.attach_agent("run-pc", _agent())
    assert turn.steer_run("run-pc", "x") is False
    assert turn.steer_run("run-pc", "x", kind="input") is False


def test_a_room_run_refuses_an_input_steer():
    turn.RUNS.clear(); turn.register_run(RUN)
    agent = _agent(); turn.attach_agent(RUN, agent)
    assert turn.steer_run(RUN, "x", kind="input") is False and agent.seen == []
