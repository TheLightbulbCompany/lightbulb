"""Does a native Paperclip turn's credential + run identity actually reach a SHELL?

Not collected by pytest (no ``test_`` prefix) because it only means anything inside the
pinned image: the mechanism is Hermes's, and a mock of it proves nothing. An earlier
attempt at this leg passed a mocked unit test while delivering ZERO of the six variables,
because ``install_profile_terminal_scope``'s ``env_overlay`` keeps only ``TERMINAL_*`` keys
(``tools/terminal_scope.py:137``). Run it from ``apps/infra/hermes``:

    docker run --rm --network none \
      -v "$PWD/bridge:/opt/bridge/bridge:ro" \
      -v "$PWD/tests/smoke/paperclip_child_env_smoke.py:/opt/bridge/smoke.py:ro" \
      --entrypoint python lightbulb/hermes-spike:m1-rc4 /opt/bridge/smoke.py

Network-free, no model call. Two profiles with DIFFERENT fake credentials each run a real
terminal command that prints the six variables; each must see its OWN values, the pinned
``terminal.env_passthrough`` must list them, and no VALUE may reach the process env.
"""
import json
import os
import shlex
import tempfile
from pathlib import Path

from bridge import sync, turn
from tools.terminal_tool import terminal_tool

NAMES = ("PAPERCLIP_API_KEY", "PAPERCLIP_API_URL", "PAPERCLIP_AGENT_ID",
         "PAPERCLIP_COMPANY_ID", "PAPERCLIP_RUN_ID", "PAPERCLIP_TASK_ID")

with tempfile.TemporaryDirectory() as temp:
    sync.HOME = Path(temp)
    os.environ["TERMINAL_ENV"] = "lightbulb_node"  # must be overridden to `local` per profile
    profiles = ["ceo", "engineering"]
    for profile in profiles:
        root = sync._profile_dir(profile)
        root.mkdir(parents=True)
        (root / "config.yaml").write_text("terminal:\n  backend: local\n")
        # The ONE per-profile env dict. Its keys become the passthrough list; its values
        # live in the secret scope. Nothing here is PAPERCLIP-specific to the mechanism.
        (root / ".env").write_text(f"PAPERCLIP_API_KEY=key-{profile}\nUNRELATED=nope-{profile}\n")

    def identity(profile):
        return {"runId": f"run-{profile}", "companyId": f"company-{profile}",
                "agentId": profile, "issueId": f"issue-{profile}",
                "sessionId": f"paperclip:company:company-{profile}:agent:{profile}:issue:issue-{profile}",
                "paperclipApiUrl": f"http://paperclip.internal/{profile}"}

    class Agent:
        def __init__(self, profile):
            self.profile = profile

        def run_conversation(self, **kwargs):
            code = (
                "import os; "
                "print('ENV=' + repr({k: os.environ.get(k) for k in "
                f"{list(NAMES)!r}"
                "}))"
            )
            result = json.loads(terminal_tool(
                "python3 -c " + shlex.quote(code), timeout=20,
                task_id=f"cred-smoke-{self.profile}", session_id=f"cred-smoke-{self.profile}",
            ))
            assert result.get("exit_code") == 0, result
            line = next(ln for ln in result["output"].splitlines() if ln.startswith("ENV="))
            seen = eval(line[len("ENV="):])  # noqa: S307 -- our own repr, in our own smoke
            expected = {
                "PAPERCLIP_API_KEY": f"key-{self.profile}",
                "PAPERCLIP_API_URL": f"http://paperclip.internal/{self.profile}",
                "PAPERCLIP_AGENT_ID": self.profile,
                "PAPERCLIP_COMPANY_ID": f"company-{self.profile}",
                "PAPERCLIP_RUN_ID": f"run-{self.profile}",
                "PAPERCLIP_TASK_ID": f"issue-{self.profile}",
            }
            assert seen == expected, f"{self.profile}: got {seen}\n   want {expected}"
            print(f"  {self.profile}: all six present and profile-scoped")
            return {"api_calls": 1, "final_response": "done"}

    turn._construct_agent = _orig = turn._construct_agent

    def construct(agent_id, body, frames, session_key):
        # Keep the REAL pin_profile_defaults on the path -- it is what writes
        # terminal.env_passthrough -- then hand back a stub agent instead of a model call.
        sync.pin_profile_defaults(agent_id, os.environ.get("AUXILIARY_MODEL") or turn.MODEL)
        return Agent(agent_id)

    turn._construct_agent = construct
    turn.restore_profile = lambda _a: None
    turn.save_profile = lambda _a: None
    turn.sync.restore_root_auth = lambda: None
    turn.sync.save_root_auth = lambda: None
    turn.sync.ensure_profile = lambda _a: None
    turn.report_usage = lambda *a, **k: None

    for profile in profiles:
        ident = identity(profile)
        request = {"input": "go", "instructions": "do"}
        turn.register_paperclip_run(ident, request)
        outcome = turn._run_native_paperclip(ident, request)[0]
        assert outcome == "completed", outcome
        cfg = sync.read_profile_config(profile)
        listed = (cfg.get("terminal") or {}).get("env_passthrough") or []
        assert set(NAMES) <= set(listed), f"{profile}: config.yaml passthrough is {listed}"

    # Names may persist as empty placeholders (that is how Hermes's filter finds them);
    # VALUES must never reach the process env.
    leaked = {k: os.environ[k] for k in NAMES if os.environ.get(k)}
    assert not leaked, f"values leaked into the process env: {leaked}"
    assert os.environ["TERMINAL_ENV"] == "lightbulb_node"

print("PASS: six vars reach a real terminal child, per profile, with no value in the process env")
