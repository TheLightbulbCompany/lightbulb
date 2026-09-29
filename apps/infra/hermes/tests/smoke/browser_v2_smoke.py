"""Pinned-Hermes contract for the shared browser's per-run CDP endpoints and lifecycle hooks.

Run by ``infra-ci.yml`` in the pinned base image with ``--network none``. This proves that with a
browser profile Hermes is in CDP mode (browser tools and browser_cdp advertised), that every CDP
consumer Hermes has -- agent-browser's session, the supervisor, browser_cdp -- resolves to THIS
turn's loopback endpoint on the multiplexer, that successful snapshots fence element refs through
the real hook shape, and that turn cleanup ends the run's browser clients.
"""
import os
import sys
import tempfile
from contextlib import nullcontext

sys.path[:0] = ["/opt/bridge", "/opt/hermes"]
os.environ["AGENTCORE_PROFILE_ID"] = "profile-smoke"

with tempfile.TemporaryDirectory() as temp:
    os.environ["HERMES_HOME"] = temp
    with open(os.path.join(temp, "config.yaml"), "w") as config:  # the managed overlay's setting
        config.write('browser:\n  backend: "off"\n')

    from bridge import browser_control, cdp_mux, sync, turn

    assert os.environ["BROWSER_CDP_URL"] == "ws://127.0.0.1:9222"
    assert os.environ["HERMES_CUA_DRIVER_CMD"] == "/opt/lightbulb/bin/lightbulb-cua"
    from agent.secret_scope import set_multiplex_active

    set_multiplex_active(False)  # no turn scope here; the bridge's boot turned the guard on
    from tools.browser_cdp_tool import _browser_cdp_check, _resolve_cdp_endpoint
    from tools.browser_tool_install import check_browser_requirements

    assert check_browser_requirements() and _browser_cdp_check()

    browser_control.install()
    lease = browser_control.begin_turn("run-1", "ceo")
    mine = f"ws://127.0.0.1:9222/devtools/browser/{cdp_mux.key_for('run-1')}"
    from tools import browser_tool_cdp, browser_tool_session

    assert browser_tool_cdp._get_cdp_override() == mine
    assert _resolve_cdp_endpoint() == mine
    assert browser_tool_session._create_session_for_key("task-1", False)["cdp_url"] == mine

    calls = []

    def post(path, body, timeout=20.0):
        calls.append((path, body, timeout))
        if path == "acquire":
            return {"generation": "g1", "cdpUrl": "wss://example.test/browser-streams/aws.browser.v1/sessions/S1/automation"}
        return {"ok": True}

    browser_control._post = post
    assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="task-1", tool_call_id="snapshot-1") is None
    browser_control.post_tool_call("browser_snapshot", status="error", tool_call_id="snapshot-1")
    assert browser_control.pre_tool_call("browser_click", {}, task_id="task-1")["action"] == "block"
    assert browser_control.pre_tool_call("browser_snapshot", {}, task_id="task-1", tool_call_id="snapshot-2") is None
    browser_control.post_tool_call("browser_snapshot", status="ok", tool_call_id="snapshot-2")
    assert browser_control.pre_tool_call("browser_click", {}, task_id="task-1") is None
    assert browser_control.session_ref() == {"browserIdentifier": "aws.browser.v1", "sessionId": "S1"}
    assert calls[0][1] == {"runId": "bridge", "agentId": "ceo"}

    profile = sync._profile_dir("ceo")
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("terminal:\n  backend: local\n", encoding="utf-8")
    (profile / ".env").write_text("", encoding="utf-8")
    reaped, closed = [], []
    browser_control._disconnect = lambda task_id: reaped.append(task_id) or True
    cdp_mux.close_run = lambda run_id, *_upstream: closed.append(run_id)

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
    turn._run_hermes("ceo", {"message": {"text": "hi"}, "room": {"id": "room"}}, None)

    assert reaped == ["task-1"] and closed == ["run-1"], (reaped, closed)
    assert browser_control._timer is not None  # the lease goes (with its save) after the grace
    browser_control._timer.cancel()

print("PASS: browser v2")
