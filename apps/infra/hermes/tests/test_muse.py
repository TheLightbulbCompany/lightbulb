import json
import sys
import types

from bridge import sync, turn, server


def test_missing_auxiliary_uses_muse(home, monkeypatch):
    (home / "profiles" / "a").mkdir()
    sync.write_profile_config(
        "a",
        {
            "model": {
                "provider": "opencode-zen",
                "default": "muse-spark-1.3",
                "base_url": "${LIGHTBULB_MUSE_BASE_URL}",
            }
        },
    )
    monkeypatch.setattr(
        sync, "_aux_defaults", lambda: {"new_task": {"provider": "auto"}}
    )
    config = sync.pin_profile_defaults("a", "bedrock-model")
    assert config["auxiliary"]["new_task"]["provider"] == "opencode-zen"
    assert config["auxiliary"]["new_task"]["base_url"] == "${LIGHTBULB_MUSE_BASE_URL}"
    assert config["auxiliary"]["new_task"]["fallback_chain"] == []
    assert config["auxiliary"]["new_task"]["api_mode"] == "codex_responses"


def test_muse_boot_bearer_is_in_profile_secret_scope(home, monkeypatch):
    captured = {}
    scope = types.ModuleType("agent.secret_scope")
    scope.build_profile_secret_scope = lambda p: {}
    scope.set_secret_scope = lambda s: captured.update(s)
    scope.reset_secret_scope = lambda t: None
    monkeypatch.setitem(sys.modules, "agent.secret_scope", scope)
    monkeypatch.setattr(turn, "_declare_child_env_names", lambda _: None)
    monkeypatch.setattr(turn, "_allow_child_env_names", lambda _: None)
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "owner-bearer")
    monkeypatch.setenv("LIGHTBULB_MUSE_BASE_URL", "https://gate.test/zen/v1")
    with turn._secret_scope("a"):
        assert captured["OPENCODE_ZEN_API_KEY"] == "owner-bearer"
        assert captured["LIGHTBULB_MUSE_BASE_URL"] == "https://gate.test/zen/v1"
    assert server._provider_credential("a", "opencode-zen") == (True, "included-muse")


def test_muse_ambient_never_calls_bedrock(monkeypatch):
    monkeypatch.setenv("LIGHTBULB_MUSE_BASE_URL", "https://gate.test/zen/v1")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "owner-bearer")

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return json.dumps(
                {
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "content": [{"type": "output_text", "text": "DISPATCH"}],
                        }
                    ],
                }
            ).encode()

    def open_request(req, timeout):
        assert req.full_url == "https://gate.test/zen/v1/responses"
        assert req.get_header("Authorization") == "Bearer owner-bearer"
        assert json.loads(req.data)["store"] is False
        return Response()

    monkeypatch.setattr(turn.urllib.request, "urlopen", open_request)
    monkeypatch.setattr(
        turn,
        "_bedrock_converse",
        lambda **_: (_ for _ in ()).throw(AssertionError("Bedrock leak")),
    )
    assert (
        turn.gate_ambient(
            {"eventKind": "room_event"}, "a", "engineer", provider="opencode-zen"
        )
        == "dispatch"
    )


def test_main_agent_uses_gate_transport_and_only_muse_catalog(monkeypatch):
    captured = {}
    module = types.ModuleType("run_agent")
    module.AIAgent = lambda **kwargs: captured.update(kwargs) or captured
    monkeypatch.setitem(sys.modules, "run_agent", module)
    monkeypatch.setattr(
        turn,
        "pin_profile_defaults",
        lambda *a: {"model": {"provider": "opencode-zen", "default": "muse-spark-1.3"}},
    )
    monkeypatch.setenv("LIGHTBULB_MUSE_BASE_URL", "https://gate.test/zen/v1")
    monkeypatch.setenv("OPENCODE_ZEN_API_KEY", "owner-bearer")
    turn._construct_agent("a", {}, None, "session")
    assert captured["api_mode"] == "codex_responses"
    assert captured["base_url"] == "https://gate.test/zen/v1"
    assert captured["api_key"] == "owner-bearer"
    assert server._provider_catalog("opencode-zen") == ["muse-spark-1.3"]
