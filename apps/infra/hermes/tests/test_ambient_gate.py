import json
from bridge import turn
from tests import factories as f


def _body(kind="room_event", text="anyone know if the deploy finished?"):
    return {
        "eventKind": kind,
        "targetAgentId": "agent-1",
        "room": {"id": "room-1", "name": "general", "kind": "channel"},
        "sender": {"kind": "human", "id": "user-1"},
        "message": {"id": "msg-9", "text": text, "author": {"kind": "human", "id": "user-1"}},
        "participants": [
            {"kind": "agent", "id": "agent-1", "displayName": "Engineering"},
            {"kind": "human", "id": "user-1", "displayName": "Ada"},
        ],
        "recentHistory": [
            {"messageId": f"m{i}", "text": f"line {i}", "author": {"kind": "human", "id": "user-1"}} for i in range(8)
        ],
    }


def _converse(reply, stop="end_turn"):
    calls = []

    def fake(**kwargs):
        calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": reply}]}}, "stopReason": stop}

    fake.calls = calls
    return fake


def test_dispatch_when_model_says_dispatch():
    fake = _converse("DISPATCH")
    assert turn.gate_ambient(_body(), "Engineering", "engineering", converse=fake) == "dispatch"
    req = fake.calls[0]
    assert req["inferenceConfig"] == {"maxTokens": 16, "temperature": 0}
    assert req["system"][0]["text"] == turn.AMBIENT_GATE_SYSTEM_PROMPT
    payload = json.loads(req["messages"][0]["content"][0]["text"])
    assert payload["agent"] == {"name": "Engineering", "role": "engineering"}
    assert len(payload["conversation"]) == 6  # last 5 history entries + the triggering message
    assert payload["conversation"][-1]["text"] == "anyone know if the deploy finished?"


def test_ignore_and_gate_error_mapping():
    assert turn.gate_ambient(_body(), "Engineering", "engineering", converse=_converse(" ignore \n")) == "ignore"
    assert turn.gate_ambient(_body(), "Engineering", "engineering", converse=_converse("maybe")) == "gate_error"
    assert turn.gate_ambient(_body(), "Engineering", "engineering", converse=_converse("DISPATCH", stop="max_tokens")) == "gate_error"

    def boom(**kwargs):
        raise RuntimeError("timeout")

    assert turn.gate_ambient(_body(), "Engineering", "engineering", converse=boom) == "gate_error"


def test_user_requests_skip_the_gate():
    def boom(**kwargs):
        raise AssertionError("gate must not run for user_request")

    assert turn.gate_ambient(_body(kind="user_request"), "Engineering", "engineering", converse=boom) == "dispatch"


def test_an_ignored_room_event_never_reaches__run_hermes_and_emits_no_usage_or_extra_frames(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "gate_ambient", lambda *a, **k: "ignore")
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    sent = []
    monkeypatch.setattr(turn, "publish_frame", lambda endpoint, channel, event: sent.append(event["payload"]["frame"]) or 200)
    body = f.ingress(eventKind="room_event")
    turn.run_turn("ceo", body)

    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "ignored" and "usage" not in ended
    assert not any(p.startswith("agent-messages/") for p, _b in posted)
    # Controller ruling: spec §5 has no gate exception on the frame side -- ignored/gate_error
    # flow through the SAME error/run_ended/run_active:false sequence as any other
    # non-"completed" outcome (an earlier pass on this branch suppressed run_ended here,
    # reading Task 24's "no frames beyond run_active true/false" too literally).
    assert [f["type"] for f in sent] == ["run_active", "error", "run_ended", "run_active"]
    assert sent[-2]["outcome"] == "ignored"
    assert sent[-1]["active"] is False


def test_a_gate_error_room_event_ends_gate_error_with_no_usage(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "gate_ambient", lambda *a, **k: "gate_error")
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    sent = []
    monkeypatch.setattr(turn, "publish_frame", lambda endpoint, channel, event: sent.append(event["payload"]["frame"]) or 200)
    turn.run_turn("ceo", f.ingress(eventKind="room_event"))
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "gate_error" and "usage" not in ended
    assert [f["type"] for f in sent] == ["run_active", "error", "run_ended", "run_active"]
    assert sent[-2]["outcome"] == "gate_error"
