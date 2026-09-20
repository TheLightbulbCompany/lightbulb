"""Runtime OAuth tokens stay in the active Hermes secret scope, outside S3."""

import json
import sys
import types
import urllib.request

from bridge import sync, turn
from tests import factories as f


def test_broker_response_uses_only_configured_server_tokens(monkeypatch):
    seen = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            assert limit <= 131072
            return json.dumps({"tokens": {"sentry": "short-access", "other": "must-not-appear"}}).encode()

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["timeout"] = timeout
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", urlopen)

    env = turn._runtime_credential_env("ceo", {"sentry": "MCP_SENTRY_ACCESS_TOKEN"})

    assert env == {"MCP_SENTRY_ACCESS_TOKEN": "short-access"}
    assert seen["url"].endswith("/api/v1/runtime/credentials/u/org_test")
    assert seen["body"] == {"agent_id": "ceo"}
    assert seen["timeout"] <= 15


def test_malformed_broker_response_cannot_abort_an_unrelated_turn(monkeypatch):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, limit):
            return b"[]"

    monkeypatch.setattr(urllib.request, "urlopen", lambda request, timeout: Response())

    assert turn._runtime_credential_env("ceo", {"sentry": "MCP_SENTRY_ACCESS_TOKEN"}) == {
        "MCP_SENTRY_ACCESS_TOKEN": ""
    }


def test_credential_is_scoped_during_construction_and_run(monkeypatch):
    sync.write_profile_config(
        "ceo",
        {
            "mcp_servers": {
                "sentry": {
                    "url": "https://mcp.sentry.dev/mcp",
                    "headers": {"Authorization": "Bearer ${MCP_SENTRY_ACCESS_TOKEN}"},
                }
            }
        },
    )
    token_dir = sync._profile_dir("ceo") / "mcp-tokens"
    token_dir.mkdir(parents=True)
    old_token = token_dir / "sentry.json"
    old_client = token_dir / "sentry.client.json"
    old_token.write_text('{"refresh_token":"old"}')
    old_client.write_text('{"client_secret":"old"}')
    monkeypatch.setattr(
        turn,
        "_runtime_credential_env",
        lambda agent, servers: {"MCP_SENTRY_ACCESS_TOKEN": "short-access"},
    )
    active = {}
    fake_agent_pkg = types.ModuleType("agent")
    fake_ss = types.ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = lambda home: {}
    fake_ss.set_secret_scope = lambda mapping: active.update(mapping) or "scope-token"
    fake_ss.reset_secret_scope = lambda token: active.clear()
    monkeypatch.setitem(sys.modules, "agent", fake_agent_pkg)
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)

    class Agent:
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kwargs):
            assert active["MCP_SENTRY_ACCESS_TOKEN"] == "short-access"
            return {"api_calls": 1, "final_response": "ok"}

    def construct(*_args):
        assert active["MCP_SENTRY_ACCESS_TOKEN"] == "short-access"
        return Agent()

    monkeypatch.setattr(turn, "_construct_agent", construct)

    assert turn._run_hermes("ceo", f.ingress(), None)[0] == "completed"
    assert active == {}
    assert not old_token.exists()
    assert not old_client.exists()


def test_paperclip_turn_gets_same_brokered_scope(monkeypatch):
    sync.write_profile_config(
        "ceo",
        {"mcp_servers": {"sentry": {"headers": {"Authorization": "Bearer ${MCP_SENTRY_ACCESS_TOKEN}"}}}},
    )
    monkeypatch.setattr(turn, "_runtime_credential_env", lambda agent, servers: {"MCP_SENTRY_ACCESS_TOKEN": "short"})
    active = {}
    fake_ss = types.ModuleType("agent.secret_scope")
    fake_ss.build_profile_secret_scope = lambda home: {}
    fake_ss.set_secret_scope = lambda mapping: active.update(mapping) or "scope-token"
    fake_ss.reset_secret_scope = lambda token: active.clear()
    monkeypatch.setitem(sys.modules, "agent", types.ModuleType("agent"))
    monkeypatch.setitem(sys.modules, "agent.secret_scope", fake_ss)

    class Agent:
        model = "m"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kwargs):
            assert active["MCP_SENTRY_ACCESS_TOKEN"] == "short"
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *_args: Agent())
    identity = {
        "runId": "r1", "companyId": "c1", "agentId": "ceo", "sessionId": "s1",
        "paperclipApiUrl": "http://paperclip.internal:3100",
    }
    request = {"input": "go", "instructions": "do"}
    turn.register_paperclip_run(identity, request)

    assert turn._run_native_paperclip(identity, request)[0] == "completed"
    assert active == {}
