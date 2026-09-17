"""Part C: the turn runs on the PROFILE's model, not the runtime-wide ANTHROPIC_MODEL.

Hermes stores the `model` kwarg verbatim (agent_init.py's _PASSTHROUGH_PARAMS) and never
resolves `model.default` from config.yaml itself, so the bridge has to read it.
"""
import sys
import types

from bridge import turn

ROOM = "11111111-1111-4111-8111-111111111111"


def constructed(monkeypatch, config):
    """Build an agent against `config` as the profile's config.yaml; return its kwargs."""
    seen = {}

    class FakeAgent:
        def __init__(self, **kwargs):
            seen.update(kwargs)

    fake = types.ModuleType("run_agent")
    fake.AIAgent = FakeAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake)
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda agent_id: None)
    monkeypatch.setattr(turn, "pin_profile_defaults", lambda agent_id, model: config)
    turn._construct_agent("ceo", {"room": {"id": ROOM, "kind": "channel"}}, None, "sess")
    return seen


def test_the_profiles_model_default_wins_over_the_runtime_wide_model(monkeypatch):
    seen = constructed(monkeypatch, {"model": {"default": "us.anthropic.claude-opus-4-6-v1:0",
                                               "provider": "bedrock"}})
    assert seen["model"] == "us.anthropic.claude-opus-4-6-v1:0" and seen["provider"] == "bedrock"


def test_a_non_bedrock_profile_keeps_its_own_provider(monkeypatch):
    seen = constructed(monkeypatch, {"model": {"default": "gpt-5.6-sol", "provider": "openai-codex"}})
    assert seen["model"] == "gpt-5.6-sol" and seen["provider"] == "openai-codex"


def test_a_dict_valued_default_pairs_model_and_provider(monkeypatch):
    """hermes_cli.config.split_model_config_default's other accepted shape."""
    seen = constructed(monkeypatch, {"model": {"default": {"model": "claude-x", "provider": "anthropic"}}})
    assert seen["model"] == "claude-x" and seen["provider"] == "anthropic"


def test_the_usage_report_names_the_model_the_agent_actually_ran_on(monkeypatch, home):
    class FakeAgent:
        model = "us.anthropic.claude-opus-4-6-v1:0"
        session_input_tokens, session_output_tokens = 11, 22
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kw):
            return {"final_response": "done", "api_calls": 1}

    monkeypatch.setattr(turn, "_construct_agent", lambda *a, **k: FakeAgent())
    outcome, _kind, reply, usage = turn._run_hermes("ceo", {"room": {"id": ROOM, "kind": "channel"},
                                                            "message": {"text": "hi"}}, None)
    assert (outcome, reply) == ("completed", "done")
    assert usage["model"] == FakeAgent.model and usage["inputTokens"] == 11


def test_a_profile_with_no_model_default_falls_back_to_the_runtime_model(monkeypatch):
    monkeypatch.setattr(turn, "MODEL", "us.anthropic.claude-sonnet-4-6-v1:0")
    for config in ({}, {"model": {}}, {"model": {"default": ""}}, {"model": "nonsense"}):
        seen = constructed(monkeypatch, config)
        assert seen["model"] == "us.anthropic.claude-sonnet-4-6-v1:0" and seen["provider"] == "bedrock"
