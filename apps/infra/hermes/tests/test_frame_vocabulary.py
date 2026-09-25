from bridge import turn

ROOM, RUN = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def publisher():
    sent = []
    p = turn.FramePublisher(
        "https://e.invalid", "/ws/c", ROOM, "ceo", RUN, None,
        # varargs: a publish carries 1..FRAME_BATCH events since streaming deltas batch
        # `seq` has its own ordering tests (test_frames.py); these compare the vocabulary only.
        publish=lambda endpoint, channel, *events: sent.extend(
            {k: v for k, v in e["payload"]["frame"].items() if k != "seq"} for e in events) or 200)
    return p, sent


def test_a_completed_run_brackets_with_run_active_and_ends_with_a_full_text_resync():
    p, sent = publisher()
    p.started()
    p.tool_start("call_1", "terminal", {"command": "ls"})
    p.tool_complete("call_1", "terminal", {"command": "ls"}, "README.md")
    p.finished("completed", "Here is the listing.", None)
    assert [f["type"] for f in sent] == [
        "run_active", "tool_start", "tool_end", "chunk", "done", "run_ended", "run_active"]
    assert sent[0]["active"] is True and sent[-1]["active"] is False
    assert sent[3] == {"type": "chunk", "content": "Here is the listing.", "runId": RUN, "agentId": "ceo"}
    assert sent[5]["outcome"] == "done"
    # ToolResultBlock[], never a bare string: the shared client validator drops a string result
    assert sent[2]["result"] == [{"type": "text", "text": "README.md"}]


def test_a_huge_tool_result_is_capped_so_the_frame_still_publishes():
    p, sent = publisher()
    p.tool_complete("call_3", "terminal", {}, "x" * 500_000)
    assert len(sent[0]["result"][0]["text"]) == turn.TOOL_RESULT_CHARS


def test_a_failing_tool_result_becomes_tool_error():
    p, sent = publisher()
    p.tool_complete("call_2", "terminal", {}, {"error": "command not found"})
    assert sent[0]["type"] == "tool_error" and sent[0]["toolCallId"] == "call_2"


def test_an_aborted_run_emits_the_cancelled_error_pair_and_no_done():
    p, sent = publisher()
    p.finished("aborted", "partial", None)
    assert [f["type"] for f in sent] == ["error", "run_ended", "run_active"]
    assert sent[0]["message"] == "Agent run was cancelled"
    assert sent[1]["outcome"] == "aborted"


def test_a_non_tool_progress_event_becomes_agent_activity_and_tool_events_are_never_doubled():
    p, sent = publisher()
    p.activity("compaction", "_context", "compressing", None)
    assert sent[0] == {"type": "agent_activity", "activity": "compaction", "title": "_context",
                       "detail": {"preview": "compressing"}, "runId": RUN, "agentId": "ceo"}
    p.activity("tool.started", "terminal", "ls", {})
    assert len(sent) == 1
