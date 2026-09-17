"""Task 20, bridge half only. The backend half (agent_mail_delivery.py routing to the
inbound door for AgentCore rows) is lane D's."""
import pytest
from bridge import turn

MAIL = {"version": 1, "eventId": "77777777-7777-4777-8777-777777777777",
        "eventKind": "agent_mail", "targetAgentId": "ceo",
        "sessionKey": "lightbulb-email:thread-42",
        "message": {"text": "Can you look at the invoice?", "fromAddress": "a@b.com",
                    "subject": "Invoice"}}


def test_a_mail_event_needs_no_room_participants_or_run_block():
    assert turn.parse_ingress(dict(MAIL))["eventKind"] == "agent_mail"


def test_a_mail_event_with_no_session_key_derives_one_from_the_agent_and_event_id():
    """Fix round (Important 1): no derivable Message-ID -> agent_mail_delivery.py omits
    sessionKey entirely (it is `None` when thread_session_key finds nothing usable). The
    bridge used to hard-require it and 400 the whole ingress; now it derives a per-event
    session, matching the ECS behaviour of a throwaway per-message session."""
    body = {**MAIL, "sessionKey": ""}
    assert turn.parse_ingress(dict(body))["eventKind"] == "agent_mail"
    assert turn._resolve_session_key("ceo", body) == f"agent:ceo:lightbulb:agent_mail:{MAIL['eventId']}"


def test_a_mail_event_still_needs_a_target_agent():
    with pytest.raises(turn.IngressError, match="targetAgentId"):
        turn.parse_ingress({**MAIL, "targetAgentId": ""})


def test_a_mail_turn_posts_usage_and_no_run_callbacks_or_frames(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: ("completed", None, "on it", turn.ZERO_USAGE))
    monkeypatch.setattr(turn, "publish_frame", lambda *a: pytest.fail("no frames for a mail turn"))
    turn.run_turn("ceo", dict(MAIL))
    paths = [path for path, _b in posted]
    assert any(p == "usage/org_test" for p in paths)
    assert not any(p.startswith("runs/") for p in paths)


# ---- review fix (IMPORTANT 3): mail turns get their own Hermes session -----------------------
def test_mail_turns_get_their_own_hermes_session_keyed_by_the_mail_thread(home, monkeypatch):
    """Drives the REAL run_turn -> _run_hermes and mocks only _construct_agent (the seam), so
    the session_key resolution and the mail-branch system_prompt/user_message are exercised
    for real. Before this fix, session_id/gateway_session_key were both derived from
    room.get('id'), which does not exist for mail -- every mail thread collided into one
    Hermes session keyed "None:<agent>"."""
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
    turn.run_turn("ceo", dict(MAIL))

    assert seen["session_key"] == "lightbulb-email:thread-42"
    assert "room #None" not in seen["system_message"]
    assert "replying to an email" in seen["system_message"]
    assert seen["user_message"].startswith("a@b.com:")


def test_two_different_mail_threads_get_two_different_session_keys(home, monkeypatch):
    seen = []
    monkeypatch.setattr(turn, "_construct_agent",
                        lambda agent_id, body, frames, session_key: (seen.append(session_key), type(
                            "A", (), {"session_input_tokens": 0, "session_output_tokens": 0,
                                     "session_cache_read_tokens": 0, "session_cache_write_tokens": 0,
                                     "run_conversation": lambda self, **kw: {"api_calls": 1, "final_response": "x"}})())[1])
    turn.run_turn("ceo", dict(MAIL))
    turn.run_turn("ceo", {**MAIL, "sessionKey": "lightbulb-email:thread-99"})
    assert seen == ["lightbulb-email:thread-42", "lightbulb-email:thread-99"]
