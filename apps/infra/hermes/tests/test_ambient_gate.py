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


def test_ignore_mapping():
    assert turn.gate_ambient(_body(), "Engineering", "engineering", converse=_converse(" ignore \n")) == "ignore"


class _Throttled(Exception):
    response = {"Error": {"Code": "ThrottlingException", "Message": "billing is not set up"}}


def test_a_failed_gate_fails_open_and_logs_why(caplog):
    """The gate always calls Bedrock whatever the owner's provider, so on an OpenRouter,
    ChatGPT or BYO workspace (or one with no credits) it can never answer. Dropping the
    turn there silenced Listening fleet-wide; the gate is a cost filter, not a fence."""

    def raises(exc):
        def boom(**kwargs):
            raise exc

        return boom

    cases = [
        (_converse("maybe"), "unparseable"),
        (_converse("DISPATCH", stop="max_tokens"), "stop_reason"),
        (raises(_Throttled()), "ThrottlingException"),
        (raises(RuntimeError("timeout")), "RuntimeError"),
    ]
    for converse, reason in cases:
        caplog.clear()
        assert turn.gate_ambient(_body(), "Engineering", "engineering", converse=converse) == "dispatch"
        assert f"ambient_gate.failed reason={reason}" in caplog.text


def test_user_requests_skip_the_gate():
    def boom(**kwargs):
        raise AssertionError("gate must not run for user_request")

    assert turn.gate_ambient(_body(kind="user_request"), "Engineering", "engineering", converse=boom) == "dispatch"


def test_an_ignored_room_event_never_reaches__run_hermes_and_emits_no_usage_or_extra_frames(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "gate_ambient", lambda *a, **k: "ignore")
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    sent = []
    monkeypatch.setattr(turn, "publish_frame",
                        lambda endpoint, channel, *events: sent.extend(e["payload"]["frame"] for e in events) or 200)
    body = f.ingress(eventKind="room_event")
    turn.run_turn("ceo", body)

    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "ignored" and "usage" not in ended
    assert not any(p.startswith("agent-messages/") for p, _b in posted)
    # Fix round (Important 2, Ruling 18): a gate outcome publishes run_ended + run_active:false
    # and NOTHING else -- no `error` frame. The client maps any `error` frame to a red "run
    # failed" state, so an agent quietly declining to respond must not look like a crash.
    assert [f["type"] for f in sent] == ["run_active", "run_ended", "run_active"]
    assert sent[-2]["outcome"] == "ignored"
    assert sent[-1]["active"] is False


def test_a_gate_error_room_event_ends_gate_error_with_no_usage(home, posted, monkeypatch):
    monkeypatch.setattr(turn, "gate_ambient", lambda *a, **k: "gate_error")
    monkeypatch.setattr(turn, "_run_hermes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run")))
    sent = []
    monkeypatch.setattr(turn, "publish_frame",
                        lambda endpoint, channel, *events: sent.extend(e["payload"]["frame"] for e in events) or 200)
    turn.run_turn("ceo", f.ingress(eventKind="room_event"))
    ended = next(b for _p, b in posted if b.get("type") == "run.ended")
    assert ended["outcome"] == "gate_error" and "usage" not in ended
    assert [f["type"] for f in sent] == ["run_active", "run_ended", "run_active"]
    assert sent[-2]["outcome"] == "gate_error"
