from huddle_agent.frames import Turns, take_sentences

NAMES = {"ceo": "CEO", "fin": "Finance", "gro": "Growth"}


class FakeSink:
    def __init__(self):
        self.calls: list[tuple[str, str, str]] = []  # (kind, text, delegation_id)

    def thinking(self, text, delegation_id):
        self.calls.append(("thinking", text, delegation_id))

    def commentary(self, text, delegation_id):
        self.calls.append(("commentary", text, delegation_id))

    def spoken(self, delegation_id=None):
        return [t for k, t, d in self.calls if k == "commentary" and delegation_id in (None, d)]

    def thoughts(self):
        return [(t, d) for k, t, d in self.calls if k == "thinking"]


def frame(turns, turn_id, f):
    turns.on_event({"type": "call.turn.frame", "payload": {"callId": "c", "turnId": turn_id, "agentId": "x", "frame": f}})


def one_turn(agent="fin"):
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    assert turns.resolved("d1", "t1", agent)
    sink.calls.clear()  # drop the "is on it" line; covered separately
    return sink, turns


def test_take_sentences():
    assert take_sentences("One. Two") == ("One.", " Two")
    assert take_sentences("No end") == ("", "No end")
    assert take_sentences("Done.") == ("", "Done.")  # terminator must be followed by whitespace


def test_resolve_speaks_is_on_it():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.resolved("d1", "t1", "fin")
    assert sink.thoughts() == [("Finance is on it. Not done yet.", "d1")]


def test_unknown_agent_falls_back_to_voice_name():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.resolved("d1", "t1", "nobody")
    assert sink.thoughts() == [("CEO is on it. Not done yet.", "d1")]


def test_tool_progress_to_thinking_and_answer_to_commentary():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "tool_start", "tool": "browser"})
    frame(turns, "t1", {"type": "chunk", "content": "It landed this morning. ", "append": True})
    frame(turns, "t1", {"type": "chunk", "content": "$4,210", "append": True})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done"})
    assert ("Finance is working: browser. Not done yet.", "d1") in sink.thoughts()
    assert sink.spoken() == ["It landed this morning.", "$4,210"]


def test_drops_frames_for_superseded_turn():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.resolved("d1", "t-old", "fin")
    turns.delegated("d2", 2)
    turns.resolved("d2", "t-new", "fin")
    frame(turns, "t-old", {"type": "chunk", "content": "Old answer. ", "append": True})
    frame(turns, "t-old", {"type": "run_ended", "outcome": "done"})
    assert sink.spoken() == []
    assert not any("finished" in t for t, _ in sink.thoughts())


def test_failed_apologises():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.failed("d1")
    assert sink.calls == [("commentary", "Sorry, I couldn't reach the team for that.", "d1")]
    assert turns.resolved("d1", "t1", "fin") is False


def test_closing_answer_once_after_narration_and_tool():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "chunk", "content": "Let me check. ", "append": True})
    frame(turns, "t1", {"type": "tool_start", "tool": "browser"})
    frame(turns, "t1", {"type": "chunk", "content": "It landed this morning. ", "append": True})
    frame(turns, "t1", {"type": "chunk", "content": "It landed this morning. $4,210"})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done"})
    assert sink.spoken() == ["Let me check.", "It landed this morning.", "$4,210"]


def test_whole_closing_chunk_when_no_deltas():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "chunk", "content": "All good. Done"})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done"})
    assert sink.spoken() == ["All good.", "Done"]


def test_closing_chunk_disagreeing_speaks_nothing_new():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "chunk", "content": "First bit. ", "append": True})
    frame(turns, "t1", {"type": "chunk", "content": "Totally different answer."})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done"})
    assert sink.spoken() == ["First bit."]


def test_replays_frames_that_arrive_before_resolve():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    frame(turns, "t-early", {"type": "chunk", "content": "Quick answer. ", "append": True})
    frame(turns, "t-other", {"type": "chunk", "content": "Stray. ", "append": True})
    frame(turns, "t-early", {"type": "run_ended", "outcome": "done"})
    assert sink.spoken() == []
    assert turns.resolved("d1", "t-early", "fin")
    assert sink.spoken() == ["Quick answer."]


def test_dedupes_working_label():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "tool_start", "tool": "browser"})
    frame(turns, "t1", {"type": "tool_start", "tool": "browser"})
    assert len(sink.thoughts()) == 1


def test_no_result_note_after_error_apology():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "error", "message": "boom"})
    frame(turns, "t1", {"type": "run_ended", "outcome": "error"})
    assert sink.thoughts() == []
    assert sink.spoken() == ["Sorry, I couldn't reach Finance for that."]


def test_run_ended_without_speech():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "run_ended", "outcome": "done"})
    assert sink.thoughts() == [("Finance finished that request without a spoken result.", "d1")]
    assert sink.spoken() == []


def test_same_tool_twice_keeps_answer_intact():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "tool_start", "tool": "terminal"})
    frame(turns, "t1", {"type": "chunk", "content": "Looking now. Still looking. ", "append": True})
    frame(turns, "t1", {"type": "tool_start", "tool": "terminal"})
    frame(turns, "t1", {"type": "chunk", "content": "It is done. ", "append": True})
    frame(turns, "t1", {"type": "chunk", "content": "It is done. Four."})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done"})
    assert sink.spoken() == ["Looking now. Still looking.", "It is done.", "Four."]


def test_orders_deltas_by_seq_and_drops_narration_after_tool_start():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "chunk", "content": "A. ", "append": True, "seq": 1})
    frame(turns, "t1", {"type": "chunk", "content": "C. ", "append": True, "seq": 3})
    frame(turns, "t1", {"type": "chunk", "content": "B. ", "append": True, "seq": 2})
    frame(turns, "t1", {"type": "tool_start", "tool": "x", "seq": 5})
    frame(turns, "t1", {"type": "chunk", "content": "Late narration. ", "append": True, "seq": 4})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done", "seq": 6})
    assert sink.spoken() == ["A.", "B.", "C."]


def test_repairs_swapped_batch_of_five():
    sink, turns = one_turn()
    for start in (6, 1):
        for i in range(5):
            frame(turns, "t1", {"type": "chunk", "content": f"S{start + i}. ", "append": True, "seq": start + i})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done", "seq": 20})
    assert sink.spoken() == [f"S{i}." for i in range(1, 11)]


def test_reorder_window_flushes_oldest_past_ten():
    sink, turns = one_turn()
    for i in range(1, 12):  # 11 held deltas: the oldest flushes, sentence-complete
        frame(turns, "t1", {"type": "chunk", "content": f"S{i}. ", "append": True, "seq": i})
    assert sink.spoken() == ["S1."]


def test_delta_after_closing_chunk_not_spoken():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "chunk", "content": "One. Two. ", "seq": 5})
    frame(turns, "t1", {"type": "chunk", "content": "Two. ", "append": True, "seq": 4})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done", "seq": 6})
    assert sink.spoken() == ["One. Two."]


def test_closing_chunk_after_run_ended_is_final():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "run_ended", "outcome": "done", "seq": 6})
    frame(turns, "t1", {"type": "chunk", "content": "Final answer with no period", "seq": 5})
    assert sink.spoken() == ["Final answer with no period"]


def test_parallel_turns_routed_by_turn_id():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.delegated("d2", 2)
    turns.resolved("d1", "t-fin", "fin")
    turns.resolved("d2", "t-gro", "gro")
    frame(turns, "t-fin", {"type": "tool_start", "tool": "browser"})
    frame(turns, "t-gro", {"type": "chunk", "content": "Draft is ready. ", "append": True})
    frame(turns, "t-gro", {"type": "run_ended", "outcome": "done"})
    assert ("Finance is working: browser. Not done yet.", "d1") in sink.thoughts()
    assert sink.spoken("d2") == ["Draft is ready."]
    assert turns.working() == [("fin", "browser")]


def test_supersedes_only_same_agents_older_turn():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.resolved("d1", "t-fin-1", "fin")
    turns.delegated("dg", 2)
    turns.resolved("dg", "t-gro", "gro")
    turns.delegated("d2", 3)
    turns.resolved("d2", "t-fin-2", "fin")
    frame(turns, "t-fin-1", {"type": "chunk", "content": "Old answer. ", "append": True})
    frame(turns, "t-fin-2", {"type": "chunk", "content": "Last month landed. ", "append": True})
    frame(turns, "t-fin-2", {"type": "run_ended", "outcome": "done"})
    frame(turns, "t-gro", {"type": "chunk", "content": "Growth here. ", "append": True})
    frame(turns, "t-gro", {"type": "run_ended", "outcome": "done"})
    assert "Old answer." not in sink.spoken()
    assert sink.spoken("d2") == ["Last month landed."]
    assert sink.spoken("dg") == ["Growth here."]


def test_newer_turn_kept_when_resolves_arrive_in_reverse():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.delegated("d2", 2)
    assert turns.resolved("d2", "t-new", "fin") is True  # newer resolves first
    assert turns.resolved("d1", "t-old", "fin") is False  # late older resolve is dropped
    assert sink.thoughts() == [("Finance is on it. Not done yet.", "d2")]
    frame(turns, "t-old", {"type": "chunk", "content": "Old answer. ", "append": True})
    frame(turns, "t-new", {"type": "chunk", "content": "Last month landed. ", "append": True})
    frame(turns, "t-new", {"type": "run_ended", "outcome": "done"})
    assert sink.spoken() == ["Last month landed."]


def test_buffered_frames_of_superseded_turn_never_spoken():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.delegated("d2", 2)
    frame(turns, "t-old", {"type": "chunk", "content": "Old answer. ", "append": True})
    frame(turns, "t-old", {"type": "run_ended", "outcome": "done"})
    turns.resolved("d2", "t-new", "fin")
    assert turns.resolved("d1", "t-old", "fin") is False
    assert sink.spoken() == []


def test_commentary_chunked_at_1400_chars():
    sink, turns = one_turn()
    long = ("x" * 2000) + ". "
    frame(turns, "t1", {"type": "chunk", "content": long, "append": True})
    frame(turns, "t1", {"type": "run_ended", "outcome": "done"})
    assert [len(s) for s in sink.spoken()] == [1400, 601]


def test_ignores_other_event_types():
    sink, turns = one_turn()
    turns.on_event({"type": "room.message", "payload": {"turnId": "t1", "frame": {"type": "run_ended"}}})
    assert sink.calls == []


def test_working_clears_on_run_ended():
    sink, turns = one_turn()
    frame(turns, "t1", {"type": "tool_start", "tool": "browser"})
    assert turns.working() == [("fin", "browser")]
    frame(turns, "t1", {"type": "run_ended", "outcome": "done"})
    assert turns.working() == []


def test_failed_silent_on_turn_conflict_stops_buffering():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    turns.failed("d1", speak=False)
    frame(turns, "t1", {"type": "chunk", "content": "Late. ", "append": True})
    assert sink.calls == []
    assert turns.resolved("d1", "t1", "fin") is False
    assert turns._turns == {}


def test_frame_without_turn_id_is_buffered_never_applied():
    sink = FakeSink()
    turns = Turns(sink, NAMES, "CEO")
    turns.delegated("d1", 1)
    frame(turns, None, {"type": "chunk", "content": "Malformed. ", "append": True})
    frame(turns, None, {"type": "run_ended", "outcome": "done"})
    assert sink.calls == []
    assert turns.resolved("d1", "t1", "fin")
    frame(turns, None, {"type": "run_ended", "outcome": "done"})
    assert sink.spoken() == [] and turns.working() == []
    assert [t for t, _ in sink.thoughts()] == ["Finance is on it. Not done yet."]
