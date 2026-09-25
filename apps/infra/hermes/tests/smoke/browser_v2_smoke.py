"""Pinned-Hermes contract for Browser v2's generic CDP and lifecycle hooks.

Run by ``infra-ci.yml`` in the pinned base image with ``--network none``. This proves the
static loopback discovery variable survives Hermes's real credential filter, successful
snapshots fence element refs through the real hook shape, and turn cleanup still runs while
the agent's Hermes-home override is active.
"""
import os
import sys
import tempfile
from contextlib import nullcontext
from pathlib import Path

sys.path[:0] = ["/opt/bridge", "/opt/hermes"]
os.environ["AGENTCORE_PROFILE_ID"] = "profile-smoke"

with tempfile.TemporaryDirectory() as temp:
    os.environ["HERMES_HOME"] = temp

    from bridge import browser_control, sync, turn
    from hermes_constants import hermes_home_key
    from tools.browser_tool import _build_browser_env

    assert os.environ["AGENT_BROWSER_CDP"] == "http://127.0.0.1:8080"
    child_env = _build_browser_env()
    assert child_env["AGENT_BROWSER_CDP"] == "http://127.0.0.1:8080"
    assert "BROWSER_CDP_URL" not in child_env

    calls = []

    def post(path, body, timeout=20.0):
        calls.append((path, body, timeout))
        if path == "acquire":
            return {"generation": "g1", "cdpUrl": "wss://example.test/automation"}
        return {"ok": True}

    browser_control._post = post
    lease = browser_control.begin_turn("run-1", "ceo")
    assert browser_control.pre_tool_call(
        "browser_snapshot", {}, task_id="task-1", tool_call_id="snapshot-1"
    ) is None
    browser_control.post_tool_call(
        "browser_snapshot", status="error", tool_call_id="snapshot-1"
    )
    assert browser_control.pre_tool_call("browser_click", {}, task_id="task-1")["action"] == "block"
    assert browser_control.pre_tool_call(
        "browser_snapshot", {}, task_id="task-1", tool_call_id="snapshot-2"
    ) is None
    browser_control.post_tool_call(
        "browser_snapshot", status="ok", tool_call_id="snapshot-2"
    )
    assert browser_control.pre_tool_call("browser_click", {}, task_id="task-1") is None

    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    (profile / ".env").write_text("", encoding="utf-8")
    seen = []
    browser_control._disconnect = lambda task_id: seen.append((task_id, hermes_home_key())) or True
    browser_control._post = post

    class Agent:
        model = "model"
        session_input_tokens = session_output_tokens = 0
        session_cache_read_tokens = session_cache_write_tokens = 0

        def run_conversation(self, **_kwargs):
            return {"api_calls": 1, "final_response": "ok"}

    turn.sync.ensure_profile = lambda _agent: None
    turn.sync.normalize_profile_backends = lambda _agent: None
    turn._secret_scope = lambda *_args, **_kwargs: nullcontext()
    turn._terminal_scope = lambda _agent, _key: nullcontext()
    turn._construct_agent = lambda *_args, **_kwargs: Agent()
    turn.browser_control.begin_turn = lambda *_args: lease
    turn.browser_control.release = browser_control.release
    turn._run_hermes("ceo", {"message": {"text": "hi"}, "room": {"id": "room"}}, None)

    assert seen == [("task-1", str(profile))], seen
    assert calls[-1] == ("release", {"runId": "run-1", "generation": "g1"}, 190.0)

print("PASS: browser v2")
