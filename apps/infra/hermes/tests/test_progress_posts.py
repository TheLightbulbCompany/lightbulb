"""Mid-run progress lines: Hermes's `interim_assistant_callback`, posted where the reply will go.

Only an addressed room turn gets the callback. It posts through the reply's own POST, marked
`progress`, at most once per PROGRESS_MIN_INTERVAL_S, and never after a stop or for a marker.
"""
import bridge
import pytest
from bridge import turn
from tests.test_run_outcome import silence_filter  # noqa: F401

RUN = "run-1"
BODY = {"eventId": "evt-1", "eventKind": "user_request", "run": {"runId": RUN},
        "room": {"id": "room-1", "kind": "channel"}, "message": {"messageId": "m1", "text": "audit it"},
        "participants": []}


class _Posts(list):
    clock = None


@pytest.fixture
def posts(monkeypatch):
    """What reached the agent-messages route, and a clock a test moves by hand."""
    sent, clock = _Posts(), [1000.0]
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: sent.append((path, body)) or 200)
    monkeypatch.setattr(turn.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(turn, "RUNS", {RUN: {"aborted": False}})
    sent.clock = clock.__setitem__
    return sent


def _later(posts, seconds):
    posts.clock(0, 1000.0 + seconds)


def test_a_progress_line_goes_where_the_reply_will_go(posts):
    post = turn._progress_poster("ceo", BODY)
    _later(posts, 61)
    post("Pulled the invoices; totals next.", already_streamed=True)
    [(path, body)] = posts
    assert path == f"agent-messages/{bridge.OWNER}"
    assert body == {"eventId": "evt-1:ceo:progress:1", "roomId": "room-1", "agentId": "ceo",
                    "replyToMessageId": "m1", "threadRootMessageId": "m1", "runId": RUN,
                    "progress": True, "text": "Pulled the invoices; totals next."}


@pytest.mark.parametrize("body", [
    {**BODY, "eventKind": "room_event"},  # may still end [SILENT]: must leave nothing behind
    {**BODY, "eventKind": "agent_mail"},
    {k: v for k, v in BODY.items() if k != "run"},  # no run, no room reply
])
def test_only_an_addressed_room_turn_posts_progress(body):
    assert turn._progress_poster("ceo", body) is None


def test_at_most_one_line_per_interval_and_none_in_a_short_turn(posts):
    post = turn._progress_poster("ceo", BODY)
    post("Starting on the invoices now.")  # the run is seconds old
    _later(posts, 59)
    post("Still inside the first minute.")
    assert posts == []
    _later(posts, 60)
    post("Pulled the invoices; totals next.")
    _later(posts, 90)
    post("Too soon after the last one.")
    _later(posts, 121)
    post("Totals match; writing it up.")
    assert [b["eventId"].rsplit(":", 1)[-1] for _, b in posts] == ["1", "2"]


@pytest.mark.parametrize("text", ["**Step 21: Read step-0", "**Sleep 4 of", "Step 28: sleep 20 (2 of 4",
                                  "Running `pytest tests/unit", "See [the report"])
def test_a_line_cut_off_by_the_tool_call_is_skipped_and_keeps_the_slot(posts, text):
    post = turn._progress_poster("ceo", BODY)
    _later(posts, 61)
    post(text)
    assert posts == []
    post("**Step 22: Read step-09** (9 of 12), then `wc -w`.")  # the next whole line, same slot
    assert len(posts) == 1


@pytest.mark.parametrize("text", ["Computed 2 ** 3 = 8; validating now.", "Scanning **/*.py next.",
                                  "**Done:** 12 files written; reading them back.", "x**2 computed, plotting."])
def test_a_whole_line_with_literal_stars_still_posts(posts, text):
    post = turn._progress_poster("ceo", BODY)
    _later(posts, 61)
    post(text)
    assert len(posts) == 1


@pytest.mark.parametrize("text", ["", "   ", "On it.", None, "[SILENT]"])
def test_empty_short_and_silence_lines_are_skipped(posts, silence_filter, text):  # noqa: F811
    post = turn._progress_poster("ceo", BODY)
    _later(posts, 61)
    post(text)
    assert posts == []


def test_nothing_is_posted_after_a_stop(posts):
    post = turn._progress_poster("ceo", BODY)
    _later(posts, 61)
    turn.RUNS[RUN]["aborted"] = True
    post("Pulled the invoices; totals next.")
    assert posts == []


def test_a_failing_post_never_raises_into_hermes(posts, monkeypatch):
    monkeypatch.setattr(turn, "_post_reply", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    post = turn._progress_poster("ceo", BODY)
    _later(posts, 61)
    post("Pulled the invoices; totals next.")


class _Agent:
    session_input_tokens = session_output_tokens = 0
    session_cache_read_tokens = session_cache_write_tokens = 0
    interim_assistant_callback = None

    def run_conversation(self, **_kw):
        return {"api_calls": 1, "final_response": "ok"}


@pytest.mark.parametrize("kind,wired", [("user_request", True), ("room_event", False)])
def test_the_room_turn_sets_the_callback_on_the_agent(kind, wired, monkeypatch):
    agent = _Agent()
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: agent)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    turn._run_hermes("ceo", {**BODY, "eventKind": kind}, None)
    assert callable(agent.interim_assistant_callback) is wired


def test_the_room_prompt_asks_for_progress_and_stays_byte_stable():
    first = turn.system_prompt(BODY, "ceo")
    assert "one short line of real progress" in first and "no @mentions" in first
    assert first == turn.system_prompt({**BODY, "eventId": "evt-2", "message": {"messageId": "m2"}}, "ceo")
    assert "real progress" not in turn.system_prompt({**BODY, "eventKind": "agent_mail"}, "ceo")
