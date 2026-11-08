import bridge
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


@pytest.fixture
def silence_filter(monkeypatch):
    import sys
    import types
    rf = types.ModuleType("gateway.response_filters")
    rf.is_intentional_silence_response = lambda r: r.strip() == "[SILENT]"
    monkeypatch.setitem(sys.modules, "gateway", sys.modules.get("gateway") or types.ModuleType("gateway"))
    monkeypatch.setitem(sys.modules, "gateway.response_filters", rf)


@pytest.mark.parametrize("kind,expect", [
    ("room_event", ("completed", turn.SILENT, "")),
    ("user_request", ("error", "empty_response", "")),
])
def test_only_an_ambient_turn_may_stay_silent(kind, expect, silence_filter, monkeypatch):
    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: _FakeAgent(2, "[SILENT]"))
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    outcome, error_kind, reply, usage = turn._run_hermes(
        "ceo", {"eventKind": kind, "message": {"text": "hi"}, "room": {"id": "x"}, "participants": []}, None)
    assert (outcome, error_kind, reply) == expect
    assert usage["inputTokens"] == 11


def test_a_silent_completion_is_flagged_to_the_backend():
    body = turn.run_ended_body("r1", "k", "completed", turn.SILENT, turn.ZERO_USAGE)
    assert body["silent"] is True and "errorKind" not in body


def test_only_room_event_turns_are_offered_silence():
    assert "[SILENT]" in turn._observe_prompt("a", {"eventKind": "room_event"})
    assert "[SILENT]" not in turn._observe_prompt("a", {"eventKind": "user_request"})


def test_the_first_run_line_reaches_only_the_owners_first_message():
    welcome = [{"author": {"kind": "agent", "id": "ceo"}, "text": "Welcome", "messageSeq": 1}]
    general = {"kind": "channel", "id": turn.default_room_id(bridge.OWNER)}
    first = {"eventKind": "user_request", "sender": {"kind": "human"}, "room": general, "recentHistory": welcome}
    assert turn.FIRST_RUN_LINE in turn._observe_prompt("ceo", first)
    later = {**first, "recentHistory": [*welcome, {"author": {"kind": "human"}, "text": "hi"}]}
    assert turn.FIRST_RUN_LINE not in turn._observe_prompt("ceo", later)
    assert turn.FIRST_RUN_LINE not in turn._observe_prompt("ceo", {**first, "room": {"kind": "channel", "id": "other"}})
    assert turn.FIRST_RUN_LINE not in turn._observe_prompt("ceo", {**first, "recentHistory": []})
    trimmed = [{"author": {"kind": "agent", "id": "eng"}, "text": "status", "messageSeq": 40}]
    assert turn.FIRST_RUN_LINE not in turn._observe_prompt("ceo", {**first, "recentHistory": trimmed})


def test_with_the_switch_the_first_run_line_follows_the_welcome_into_the_lightbulb_dm():
    welcome = [{"author": {"kind": "agent", "id": "ceo"}, "text": "Welcome", "messageSeq": 1, "kind": "first_run"}]
    dm = {"eventKind": "user_request", "sender": {"kind": "human"}, "room": {"kind": "dm", "id": "dm-1"}, "recentHistory": welcome}
    assert turn.FIRST_RUN_LINE in turn._observe_prompt("ceo", dm)
    plain = [{**welcome[0], "kind": None}]
    assert turn.FIRST_RUN_LINE not in turn._observe_prompt("ceo", {**dm, "recentHistory": plain})
    # #general then holds only the team line: a teammate's first message there is no first run.
    # The owner wrote before setup finished: the welcome follows their message, and their next one is the answer.
    early = [{"author": {"kind": "human"}, "text": "hi", "messageSeq": 1}, {**welcome[0], "messageSeq": 2}]
    assert turn.FIRST_RUN_LINE in turn._observe_prompt("ceo", {**dm, "recentHistory": early})
    answered = [*early, {"author": {"kind": "human"}, "text": "acme.com", "messageSeq": 3}]
    assert turn.FIRST_RUN_LINE not in turn._observe_prompt("ceo", {**dm, "recentHistory": answered})
    team = [{**welcome[0], "kind": "team_welcome"}]
    general = {"kind": "channel", "id": turn.default_room_id(bridge.OWNER)}
    assert turn.FIRST_RUN_LINE not in turn._observe_prompt("ceo", {**dm, "room": general, "recentHistory": team})


def test_group_manners_reach_every_turn_and_the_room_question_rule_only_ambient_ones():
    ambient, addressed = (turn._observe_prompt("a", {"eventKind": k}) for k in ("room_event", "user_request"))
    for line in ("correct it once and cite your source", "phrased as \"we\"", "yes or thanks answers"):
        assert line in ambient and line in addressed
    assert "name who can" in ambient and "name who can" not in addressed


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
    monkeypatch.setattr(turn, "_terminal_scope", lambda _agent, _key: nullcontext())
    monkeypatch.setattr(turn, "_secret_scope", lambda _agent, _overlay=None: nullcontext())
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
    monkeypatch.setattr(turn, "_terminal_scope", lambda _agent, _key: nullcontext())
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


# ---- provider quota walls ----------------------------------------------------------------------
class _ApiError(Exception):
    """SDK-shaped: .status_code + .body (openai passes the inner error object, anthropic the wrapper)."""

    def __init__(self, status, body=None):
        super().__init__(f"HTTP {status}")
        self.status_code, self.body = status, body


# The real prod founder payload (openai-codex RateLimitError after Hermes's own retries).
CHATGPT_PROLITE = {"error": {"type": "usage_limit_reached", "plan_type": "prolite",
                             "resets_at": 1790565911, "resets_in_seconds": 311059}}


@pytest.mark.parametrize("exc,provider,expect", [
    (_ApiError(429, CHATGPT_PROLITE), "openai-codex", {"provider": "chatgpt", "resets_at": 1790565911}),
    (_ApiError(429, CHATGPT_PROLITE["error"]), "openai-codex", {"provider": "chatgpt", "resets_at": 1790565911}),
    (_ApiError(429, {"error": {"type": "usage_limit_reached", "resets_at": "2026-09-30T00:00:00Z"}}), "openai-codex",
     {"provider": "chatgpt", "resets_at": "2026-09-30T00:00:00Z"}),
    (_ApiError(429, {"message": "You exceeded your current quota", "type": "insufficient_quota",
                     "code": "insufficient_quota"}), "openai", {"provider": "openai"}),
    (_ApiError(429, {"type": "error", "error": {"type": "rate_limit_error",
                                                "message": "Your account has run out of credits."}}),
     "anthropic", {"provider": "anthropic"}),
    (_ApiError(400, {"type": "error", "error": {"type": "invalid_request_error", "message":
     "Your credit balance is too low to access the Anthropic API."}}), "anthropic", {"provider": "anthropic"}),
    # transient rate limits stay as they are
    (_ApiError(429, {"message": "Rate limit reached for requests", "type": "requests",
                     "code": "rate_limit_exceeded"}), "openai", None),
    (_ApiError(429, {"type": "error", "error": {"type": "rate_limit_error", "message":
     "This request would exceed your organization's rate limit of 50,000 input tokens per minute."}}),
     "anthropic", None),
    # our own gates' limit bodies are not a provider quota
    (_ApiError(429, {"error": {"type": "gate_error", "code": "trial_daily_cap",
                               "message": "Lightbulb: daily limit reached"}}), "anthropic", None),
    (_ApiError(429), "openai-codex", None),  # no body
    (RuntimeError("boom"), "", None),
    (None, "", None),
])
def test_provider_quota_classification(exc, provider, expect):
    assert turn.provider_quota(exc, provider) == expect


def test_quota_failed_result_reports_quota_kind_and_detail(monkeypatch):
    class Agent(_FakeAgent):
        provider = "openai-codex"
        _lb_api_error = (_ApiError(429, CHATGPT_PROLITE), "HTTP 429")

    monkeypatch.setattr(turn, "_construct_agent",
                        lambda *a, **k: Agent(1, "HTTP 429", failed=True, error="HTTP 429"))
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    outcome, kind, reply, _usage = turn._run_hermes(
        "ceo", {"message": {"text": "hi"}, "room": {"id": "x"}, "participants": []}, None)
    assert (outcome, kind, reply) == ("error", "provider_quota_exhausted", "")
    body = turn.run_ended_body("r1", "k", outcome, kind, turn.ZERO_USAGE, turn._post_turn.error_detail)
    assert body["errorKind"] == "provider_quota_exhausted"
    assert body["errorDetail"] == {"provider": "chatgpt", "resets_at": 1790565911}


def test_capture_remembers_the_raw_error_hermes_summarizes():
    class Agent:
        @staticmethod
        def _summarize_api_error(error):
            return "HTTP 429"

    agent, err = Agent(), _ApiError(429, CHATGPT_PROLITE)
    turn._capture_api_errors(agent)
    assert agent._summarize_api_error(err) == "HTTP 429" and agent._lb_api_error == (err, "HTTP 429")


def test_a_stale_capture_does_not_label_a_later_unrelated_failure(monkeypatch):
    """An early attempt captured a quota wall, a retry succeeded, then the turn failed on a
    path that never summarizes (e.g. turn_response_check): that failure is not a quota wall."""
    class Agent(_FakeAgent):
        provider = "openai-codex"
        _lb_api_error = (_ApiError(429, CHATGPT_PROLITE), "HTTP 429")

    monkeypatch.setattr(turn, "_construct_agent",
                        lambda *a, **k: Agent(2, "", failed=True, error="Turn abandoned: shutting down"))
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    outcome, kind, _reply, _usage = turn._run_hermes(
        "ceo", {"message": {"text": "hi"}, "room": {"id": "x"}, "participants": []}, None)
    assert (outcome, kind) == ("error", "model_unavailable")
    assert turn._post_turn.error_detail is None


def test_error_detail_is_only_sent_with_the_quota_kind():
    assert "errorDetail" not in turn.run_ended_body(
        "r1", "k", "error", "model_unavailable", turn.ZERO_USAGE, {"provider": "openai"})


def test_a_voice_run_is_never_offered_silence():
    """Voice runs share the room key but carry no eventKind: no [SILENT] line, so no silent outcome."""
    request = {"voice": {"roomKind": "channel", "speaker": {"id": "u1", "name": "Aden"}}, "participants": []}
    body = turn._voice_body({"roomId": "room-1", "agentId": "ceo", "callId": "call-1"}, request)
    assert "eventKind" not in body and "[SILENT]" not in turn._observe_prompt("ceo", body)


def test_a_chatgpt_oauth_agent_gets_hermes_credential_pool(monkeypatch):
    """Hermes recovers an expired ChatGPT token only through the pool (ISOA-837, 2026-10-15):
    the bridge resolves it the way Hermes's own runners do and hands it to AIAgent."""
    import sys
    import types
    calls = []
    pool = object()
    fake = types.ModuleType("hermes_cli.runtime_provider")
    fake.resolve_runtime_provider = lambda **kw: calls.append(kw) or {"credential_pool": pool}
    monkeypatch.setitem(sys.modules, "hermes_cli", types.ModuleType("hermes_cli"))
    monkeypatch.setitem(sys.modules, "hermes_cli.runtime_provider", fake)
    assert turn._credential_pool("openai-codex", "gpt-5.6-sol") is pool
    assert calls == [{"requested": "openai-codex", "target_model": "gpt-5.6-sol"}]
    # The managed gate and BYO keys keep Hermes's pool-less behaviour.
    assert turn._credential_pool("custom", "m") is None and turn._credential_pool("", "") is None
    fake.resolve_runtime_provider = lambda **kw: (_ for _ in ()).throw(RuntimeError("no store"))
    assert turn._credential_pool("openai-codex", "m") is None  # never fails the turn
