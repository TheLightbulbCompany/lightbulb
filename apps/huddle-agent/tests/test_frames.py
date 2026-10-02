from huddle_agent.frames import Runs, take_sentences


def test_take_sentences():
    assert take_sentences("One. Two") == ("One.", " Two")
    assert take_sentences("No end") == ("", "No end")
    assert take_sentences("Done.") == ("", "Done.")  # terminator must be followed by whitespace


# -- Runs: routed by run id and agent, no supersession --


class RunSink:
    def __init__(self):
        self.calls: list[tuple] = []

    def progress(self, run, text):
        self.calls.append(("progress", run.run_id, text))

    def say(self, run, text, final):
        self.calls.append(("say", run.run_id, text, final))

    def ended(self, run, answer):
        self.calls.append(("ended", run.run_id, answer))

    def failed(self, run):
        self.calls.append(("failed", run.run_id))

    def said(self, run_id=None):
        return [c[2] for c in self.calls if c[0] == "say" and run_id in (None, c[1])]


def rframe(runs, run_id, f, agent="fin"):
    runs.on_event({"type": "voice.run.frame", "payload": {"roomId": "r", "runId": run_id, "agentId": agent, "frame": f}})


def one_run(agent="fin", run_id="r1"):
    sink = RunSink()
    runs = Runs(sink, clock=lambda: 0.0)
    runs.started(run_id, agent, speaker="u1", request="how much?", delegation_id="d1", gen=1)
    return sink, runs


def test_runs_interleaved_agents_each_carry_their_run():
    sink = RunSink()
    runs = Runs(sink, clock=lambda: 0.0)
    runs.started("rf", "fin", speaker="u", request="a", delegation_id="d1", gen=1)
    runs.started("rg", "gro", speaker="u", request="b", delegation_id="d2", gen=2)
    rframe(runs, "rf", {"type": "tool_start", "tool": "browser"})
    rframe(runs, "rg", {"type": "chunk", "content": "Draft is ready. ", "append": True}, agent="gro")
    rframe(runs, "rf", {"type": "chunk", "content": "Paid. ", "append": True})
    rframe(runs, "rg", {"type": "run_ended", "outcome": "done"}, agent="gro")
    assert ("progress", "rf", "browser") in sink.calls
    assert sink.said("rg") == ["Draft is ready."] and sink.said("rf") == []  # rf's delta still in the window
    assert ("ended", "rg", "Draft is ready.") in sink.calls
    assert runs.working("fin") == "browser" and runs.working("gro") == ""
    assert [r.run_id for r in runs.running()] == ["rf"]


def test_runs_same_agent_both_deliver():
    sink = RunSink()
    runs = Runs(sink, clock=lambda: 0.0)
    runs.started("r1", "fin", speaker="u", request="a", delegation_id="d1", gen=1)
    runs.started("r2", "fin", speaker="u", request="b", delegation_id="d2", gen=1)
    rframe(runs, "r1", {"type": "chunk", "content": "First. ", "append": True})
    rframe(runs, "r2", {"type": "chunk", "content": "Second. ", "append": True})
    rframe(runs, "r1", {"type": "run_ended", "outcome": "done"})
    rframe(runs, "r2", {"type": "run_ended", "outcome": "done"})
    assert sink.said("r1") == ["First."] and sink.said("r2") == ["Second."]
    assert [c for c in sink.calls if c[0] == "ended"] == [("ended", "r1", "First."), ("ended", "r2", "Second.")]


def test_runs_frames_before_started_are_replayed():
    sink = RunSink()
    runs = Runs(sink, clock=lambda: 0.0)
    rframe(runs, "r1", {"type": "chunk", "content": "Quick answer. "})
    assert sink.calls == []
    run = runs.started("r1", "fin", speaker="u", request="a", delegation_id="d1", gen=3)
    assert sink.said("r1") == ["Quick answer."]
    assert run.delegation_id == "d1" and run.gen == 3 and run.speaker == "u"


def test_runs_unknown_run_with_agent_ends_with_its_answer():
    sink = RunSink()
    runs = Runs(sink, clock=lambda: 0.0)
    rframe(runs, "rx", {"type": "tool_start", "tool": "stripe"})
    rframe(runs, "rx", {"type": "chunk", "content": "It landed. $4,210", "append": True})
    assert runs.running("fin")[0].bound is False
    rframe(runs, "rx", {"type": "run_ended", "outcome": "done"})
    assert sink.calls[-1] == ("ended", "rx", "It landed. $4,210")
    assert sink.calls[-1][1] == "rx" and runs.running() == []
    runs.on_event({"type": "voice.run.frame", "payload": {"runId": "ry", "frame": {"type": "run_ended"}}})
    assert runs.running() == [] and sink.calls[-1][1] == "rx"  # no agentId: unroutable


def test_runs_queued_set_and_cleared_by_later_frame():
    sink, runs = one_run()
    rframe(runs, "r1", {"type": "run_queued"})
    assert [r.queued for r in runs.running()] == [True]
    rframe(runs, "r1", {"type": "tool_start", "tool": "browser"})
    assert [r.queued for r in runs.running()] == [False]


def test_runs_aborted_run_is_silent():
    sink, runs = one_run()
    rframe(runs, "r1", {"type": "chunk", "content": "Half", "append": True})
    runs.abort("r1")
    rframe(runs, "r1", {"type": "chunk", "content": " done. ", "append": True})
    rframe(runs, "r1", {"type": "run_ended", "outcome": "aborted"})
    assert sink.calls == [] and runs.running() == []


def test_runs_working_label_trimmed_to_60():
    sink, runs = one_run()
    rframe(runs, "r1", {"type": "tool_start", "tool": "x" * 80})
    assert runs.working("fin") == "x" * 60
    rframe(runs, "r1", {"type": "run_ended", "outcome": "done"})
    assert runs.working("fin") == ""


def test_runs_bind_moves_delegation():
    sink, runs = one_run()
    runs.bind("r1", None, 2)
    run = runs.running("fin")[0]
    assert run.delegation_id is None and run.gen == 2


def test_runs_error_fails_once_then_quiet():
    sink, runs = one_run()
    rframe(runs, "r1", {"type": "error", "message": "boom"})
    rframe(runs, "r1", {"type": "run_ended", "outcome": "error"})
    assert sink.calls == [("failed", "r1")] and runs.running() == []


def test_runs_stale_run_stops_running():
    now = [0.0]
    runs = Runs(RunSink(), clock=lambda: now[0])
    runs.started("r1", "fin", speaker="u", request="a", delegation_id="d1", gen=1)
    now[0] = 121.0
    assert runs.running() == []


def test_runs_queued_run_is_not_stale_at_121s():
    now = [0.0]
    runs = Runs(RunSink(), clock=lambda: now[0])
    runs.started("r1", "fin", speaker="u", request="a", delegation_id="d1", gen=1)
    rframe(runs, "r1", {"type": "run_queued"})  # behind a chat run on the room lock (F7)
    now[0] = 121.0
    assert [r.run_id for r in runs.running()] == ["r1"]
    now[0] = 1_800.0
    assert runs.running() == []


def test_runs_final_flush_and_closing_answer():
    sink, runs = one_run()
    rframe(runs, "r1", {"type": "chunk", "content": "Let me check. ", "append": True})
    rframe(runs, "r1", {"type": "tool_start", "tool": "browser"})
    rframe(runs, "r1", {"type": "chunk", "content": "It landed this morning. ", "append": True})
    rframe(runs, "r1", {"type": "chunk", "content": "It landed this morning. $4,210"})
    rframe(runs, "r1", {"type": "run_ended", "outcome": "done"})
    says = [c for c in sink.calls if c[0] == "say"]
    assert [c[2] for c in says] == ["Let me check.", "It landed this morning.", "$4,210"]
    assert says[-1][3] is True and says[0][3] is False
    assert sink.calls[-1] == ("ended", "r1", "It landed this morning. $4,210")


def test_runs_reorders_by_seq_and_drops_narration_after_tool_start():
    sink, runs = one_run()
    rframe(runs, "r1", {"type": "chunk", "content": "A. ", "append": True, "seq": 1})
    rframe(runs, "r1", {"type": "chunk", "content": "C. ", "append": True, "seq": 3})
    rframe(runs, "r1", {"type": "chunk", "content": "B. ", "append": True, "seq": 2})
    rframe(runs, "r1", {"type": "tool_start", "tool": "x", "seq": 5})
    rframe(runs, "r1", {"type": "chunk", "content": "Late narration. ", "append": True, "seq": 4})
    rframe(runs, "r1", {"type": "run_ended", "outcome": "done", "seq": 6})
    assert sink.said() == ["A.", "B.", "C."]


def test_runs_repairs_swapped_batch_and_window_flush():
    sink, runs = one_run()
    for start in (6, 1):
        for i in range(5):
            rframe(runs, "r1", {"type": "chunk", "content": f"S{start + i}. ", "append": True, "seq": start + i})
    assert sink.said() == []  # all ten held in the reorder window
    rframe(runs, "r1", {"type": "chunk", "content": "S11. ", "append": True, "seq": 11})
    assert sink.said() == ["S1."]
    rframe(runs, "r1", {"type": "run_ended", "outcome": "done", "seq": 20})
    assert sink.said() == [f"S{i}." for i in range(1, 12)]


def test_runs_closing_chunk_after_run_ended_is_final():
    sink, runs = one_run()
    rframe(runs, "r1", {"type": "run_ended", "outcome": "done", "seq": 6})
    rframe(runs, "r1", {"type": "chunk", "content": "Final answer with no period", "seq": 5})
    assert sink.said() == ["Final answer with no period"]


def test_runs_say_chunked_at_1400_chars():
    sink, runs = one_run()
    rframe(runs, "r1", {"type": "chunk", "content": ("x" * 2000) + ". ", "append": True})
    rframe(runs, "r1", {"type": "run_ended", "outcome": "done"})
    assert [len(s) for s in sink.said()] == [1400, 601]


def test_runs_ignore_old_event_type():
    sink, runs = one_run()
    runs.on_event({"type": "call.turn.frame", "payload": {"runId": "r1", "frame": {"type": "run_ended"}}})
    assert sink.calls == []
