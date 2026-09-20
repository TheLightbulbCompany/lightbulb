from contextlib import nullcontext

import pytest
from bridge import turn


class _FakeAgent:
    session_input_tokens, session_output_tokens = 11, 5
    session_cache_read_tokens = session_cache_write_tokens = 0

    def __init__(self, api_calls, final, **flags):
        self._result = {"api_calls": api_calls, "final_response": final, **flags}

    def run_conversation(self, **_kw):
        return self._result


def test_a_gate_outcome_omits_usage_entirely():
    body = turn.run_ended_body("r1", "agent:ceo:lightbulb:channel:x", "ignored", None, turn.ZERO_USAGE)
    assert "usage" not in body and body["outcome"] == "ignored" and body["type"] == "run.ended"


def test_every_other_outcome_carries_usage():
    body = turn.run_ended_body("r1", "k", "error", "model_unavailable", turn.ZERO_USAGE)
    assert body["usage"]["inputTokens"] == 0 and body["errorKind"] == "model_unavailable"


def test_an_error_kind_is_only_set_for_an_error_outcome():
    assert "errorKind" not in turn.run_ended_body("r1", "k", "completed", None, turn.ZERO_USAGE)


@pytest.mark.parametrize("api_calls,final,expect", [
    (0, "Sorry, I cannot reach the model right now.", ("error", "model_unavailable", "")),
    (3, "", ("error", "empty_response", "")),
    (3, "ok", ("completed", None, "ok")),
])
def test_hermes_prose_from_a_dead_model_is_never_posted(api_calls, final, expect, monkeypatch):
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _FakeAgent(api_calls, final))
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    outcome, kind, reply, usage = turn._run_hermes(
        "ceo", {"message": {"text": "hi"}, "room": {"id": "x"}, "sender": {"displayName": "Prez"},
                "participants": []}, None)
    assert (outcome, kind, reply) == expect
    assert usage["inputTokens"] == 11


def test_hermes_failed_result_is_not_completed_even_with_prose(monkeypatch):
    monkeypatch.setattr(
        turn, "_construct_agent",
        lambda *a, **k: _FakeAgent(
            3, "This model doesn't support the image content block.",
            failed=True, completed=False,
        ),
    )
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    outcome, kind, reply, _usage = turn._run_hermes(
        "ceo", {"message": {"text": "hi"}, "room": {"id": "x"}, "participants": []}, None
    )
    assert (outcome, kind, reply) == ("error", "model_unavailable", "")


def test_hermes_normalizes_profile_backends_before_construction(monkeypatch):
    order = []
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: order.append("normalize"))
    monkeypatch.setattr(turn, "_terminal_scope", lambda _agent: nullcontext())
    monkeypatch.setattr(turn, "_secret_scope", lambda _agent: nullcontext())
    monkeypatch.setattr(turn, "_hermes_home_scope", lambda _agent: nullcontext())

    def construct(*_args, **_kwargs):
        assert order == ["normalize"]
        return _FakeAgent(1, "ok")

    monkeypatch.setattr(turn, "_construct_agent", construct)
    assert turn._run_hermes("ceo", {"message": {"text": "hi"}, "room": {"id": "x"}, "participants": []}, None)[0] == "completed"


def test_native_paperclip_failed_result_is_not_completed_even_with_prose(monkeypatch):
    class Agent(_FakeAgent):
        model = "model"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    order = []
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: order.append("normalize"))
    monkeypatch.setattr(turn, "_hermes_home_scope", lambda _agent: nullcontext())
    monkeypatch.setattr(turn, "_secret_scope", lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr(turn, "_terminal_scope", lambda _agent: nullcontext())
    monkeypatch.setattr(turn, "paperclip_run_aborted", lambda _run_id: False)
    def construct(*_args, **_kwargs):
        assert order == ["normalize"]
        return Agent(3, "This model doesn't support the image content block.", failed=True, completed=False)

    monkeypatch.setattr(turn, "_construct_agent", construct)
    outcome, kind, reply, _usage = turn._run_native_paperclip(
        {"agentId": "ceo", "runId": "run-1", "sessionId": "session-1", "paperclipApiUrl": "url",
         "companyId": "company-1"},
        {"input": "hi", "instructions": "reply"},
    )
    assert (outcome, kind, reply) == ("failed", "model_unavailable", "")
