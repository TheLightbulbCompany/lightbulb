"""Final-review fix round, Critical 3: paperclip_run is a second non-room event kind.

routers/messaging.py::post_paperclip_run (Task 21) invented `eventKind: "paperclip_run"` but
nothing ever taught the bridge to accept it -- turn.EVENT_KINDS only had "agent_mail" for the
non-room case, so parse_ingress raised on every Paperclip-triggered run. Mirrors
test_agent_mail.py's shape for the sibling non-room kind."""
import pytest
from bridge import turn

RUN = {
    "version": 1,
    "eventId": "run_123",
    "eventKind": "paperclip_run",
    "targetAgentId": "agent_ceo",
    "sessionKey": "agent:agent_ceo:paperclip:run_123",
    "message": {"text": "New comment on your task"},
}


def test_a_paperclip_run_event_needs_no_room_participants_or_run_block():
    assert turn.parse_ingress(dict(RUN))["eventKind"] == "paperclip_run"


def test_a_paperclip_run_event_still_needs_a_target_agent():
    with pytest.raises(turn.IngressError, match="targetAgentId"):
        turn.parse_ingress({**RUN, "targetAgentId": ""})


def test_a_paperclip_run_turn_posts_usage_and_no_run_callbacks_or_frames(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "on it", turn.ZERO_USAGE))
    monkeypatch.setattr(turn, "publish_frame", lambda *a: pytest.fail("no frames for a paperclip_run turn"))
    turn.run_turn("agent_ceo", dict(RUN))
    paths = [path for path, _b in posted]
    assert any(p == "usage/org_test" for p in paths)
    assert not any(p.startswith("runs/") for p in paths)


def test_the_system_prompt_never_renders_room_hashNone_for_a_paperclip_run():
    prompt = turn.system_prompt(dict(RUN), "agent_ceo")
    assert "room #None" not in prompt
    assert "Paperclip run" in prompt


def test_the_user_message_is_the_trigger_text_with_no_question_mark_prefix():
    assert turn._turn_user_message(dict(RUN)) == "New comment on your task"


def test_a_paperclip_run_gets_its_own_hermes_session_keyed_by_the_run(home, monkeypatch):
    seen = {}

    class _FakeAgent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **kw):
            seen["user_message"] = kw["user_message"]
            seen["system_message"] = kw["system_message"]
            return {"api_calls": 1, "final_response": "ack"}

    def fake_construct_agent(agent_id, body, frames, session_key):
        seen["session_key"] = session_key
        return _FakeAgent()

    monkeypatch.setattr(turn, "_construct_agent", fake_construct_agent)
    turn.run_turn("agent_ceo", dict(RUN))

    assert seen["session_key"] == "agent:agent_ceo:paperclip:run_123"
    assert "room #None" not in seen["system_message"]
    assert seen["user_message"] == "New comment on your task"
