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


def test_a_mail_event_still_needs_a_session_key_and_a_target():
    with pytest.raises(turn.IngressError, match="sessionKey"):
        turn.parse_ingress({**MAIL, "sessionKey": ""})


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
