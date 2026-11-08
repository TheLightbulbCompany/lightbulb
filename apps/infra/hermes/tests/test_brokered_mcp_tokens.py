import bridge
import json
from contextlib import nullcontext

from bridge import turn
from factories import ingress


class _Response:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self.payload).encode()


def test_runtime_mcp_env_vends_a_turn_scoped_bearer(monkeypatch):
    seen = {}

    def urlopen(request, timeout):
        seen["url"] = request.full_url
        seen["body"] = json.loads(request.data)
        seen["authorization"] = request.get_header("Authorization")
        seen["timeout"] = timeout
        return _Response({"tokens": {"stripe": "access-token"}, "unavailable": []})

    monkeypatch.setattr(turn.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(turn, "API", "https://api.example")
    monkeypatch.setattr(bridge, "OWNER", "org_123")
    monkeypatch.setattr(bridge, "HOOK_TOKEN", "hook-token")

    assert turn.runtime_mcp_env("agent_123") == {"MCP_STRIPE_ACCESS_TOKEN": "access-token"}
    assert seen == {
        "url": "https://api.example/api/v1/runtime/credentials/u/org_123",
        "body": {"agent_id": "agent_123"},
        "authorization": "Bearer hook-token",
        "timeout": 15,
    }


def test_hermes_turn_installs_brokered_bearer_only_in_its_secret_scope(monkeypatch):
    seen = {}

    monkeypatch.setattr(turn, "runtime_mcp_env", lambda _agent: {"MCP_STRIPE_ACCESS_TOKEN": "access-token"})
    monkeypatch.setattr(turn.sync, "ensure_profile", lambda _agent: None)
    monkeypatch.setattr(turn.sync, "normalize_profile_backends", lambda _agent: None)
    monkeypatch.setattr(turn.browser_control, "begin_turn", lambda *_args: None)
    monkeypatch.setattr(turn.browser_control, "release", lambda _lease: None)
    monkeypatch.setattr(turn, "_hermes_home_scope", lambda _agent: nullcontext())
    monkeypatch.setattr(turn, "_terminal_scope", lambda _agent, _key: nullcontext())
    monkeypatch.setattr(turn, "_desktop_active_turn", lambda *_args: nullcontext())
    monkeypatch.setattr(
        turn,
        "_brokered_mcp_scope",
        lambda overlay, _agent=None: seen.update(overlay=overlay) or nullcontext(),
    )
    monkeypatch.setattr(turn, "_secret_scope", lambda agent_id, _overlay=None: seen.update(agent_id=agent_id) or nullcontext())

    class _Agent:
        model = "test"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kwargs):
            return {"api_calls": 1, "final_response": "ok"}

    monkeypatch.setattr(turn, "_construct_agent", lambda *_args: _Agent())

    assert turn._run_hermes("ceo", ingress(), None)[0] == "completed"
    assert seen == {"agent_id": "ceo", "overlay": {"MCP_STRIPE_ACCESS_TOKEN": "access-token"}}
