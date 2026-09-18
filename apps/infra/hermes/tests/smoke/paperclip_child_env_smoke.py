"""Does a native Paperclip turn's credential + run identity actually reach a SHELL?

Not collected by pytest (no ``test_`` prefix) because it only means anything inside the
pinned image: the mechanism is Hermes's, and a mock of it proves nothing. Two earlier
attempts at this leg passed their unit tests and delivered nothing -- first because
``install_profile_terminal_scope``'s ``env_overlay`` keeps only ``TERMINAL_*`` keys
(``tools/terminal_scope.py:137``), then because ``tools/env_passthrough.py`` caches the
config list per home FOR THE PROCESS LIFETIME, so whichever turn ran a shell command first
froze it. Run it from ``apps/infra/hermes``:

    docker run --rm --network none \
      -v "$PWD/bridge:/opt/bridge/bridge:ro" \
      -v "$PWD/tests/smoke/paperclip_child_env_smoke.py:/opt/bridge/smoke.py:ro" \
      --entrypoint python lightbulb/hermes-spike:m1-rc4 /opt/bridge/smoke.py

Network-free, no model call. BOTH turn orderings are exercised, because one warm microVM
serves room turns and Paperclip runs and room-first is the EXPECTED ordering:

    ceo          room turn (shell command) FIRST, then the native turn   <- the regression fence
    engineering  native turn only

Each profile has its own fake credentials; the native child must see ITS OWN six values under
either ordering, and no VALUE may reach the process env.
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

PROBE = (
    "import os; print('ENV=' + repr({k: os.environ.get(k) for k in " + repr(list(NAMES)) + "}))"
)


def child_env(label: str) -> dict:
    """Run a REAL terminal command and report what the child actually saw."""
    result = json.loads(terminal_tool(
        "python3 -c " + shlex.quote(PROBE), timeout=20,
        task_id=f"cred-smoke-{label}", session_id=f"cred-smoke-{label}",
    ))
    assert result.get("exit_code") == 0, result
    line = next(ln for ln in result["output"].splitlines() if ln.startswith("ENV="))
    return eval(line[len("ENV="):])  # noqa: S307 -- our own repr, in our own smoke


with tempfile.TemporaryDirectory() as temp:
    sync.HOME = Path(temp)
    os.environ["TERMINAL_ENV"] = "lightbulb_node"  # must be overridden to `local` per profile
    profiles = ["ceo", "engineering"]
    for profile in profiles:
        root = sync._profile_dir(profile)
        root.mkdir(parents=True)
        (root / "config.yaml").write_text("terminal:\n  backend: local\n")
        # The per-profile env dict. It carries the STATIC secret only -- the run identity is
        # per-turn, which is exactly why the NAME list cannot be derived from it alone.
        (root / ".env").write_text(f"PAPERCLIP_API_KEY=key-{profile}\nUNRELATED=nope-{profile}\n")

    def identity(profile):
        return {"runId": f"run-{profile}", "companyId": f"company-{profile}",
                "agentId": profile, "issueId": f"issue-{profile}",
                "sessionId": f"paperclip:company:company-{profile}:agent:{profile}:issue:issue-{profile}",
                "paperclipApiUrl": f"http://paperclip.internal/{profile}"}

    def expected(profile):
        return {
            "PAPERCLIP_API_KEY": f"key-{profile}",
            "PAPERCLIP_API_URL": f"http://paperclip.internal/{profile}",
            "PAPERCLIP_AGENT_ID": profile,
            "PAPERCLIP_COMPANY_ID": f"company-{profile}",
            "PAPERCLIP_RUN_ID": f"run-{profile}",
            "PAPERCLIP_TASK_ID": f"issue-{profile}",
        }

    seen_by_label = {}

    class Agent:
        def __init__(self, label):
            self.label = label

        def run_conversation(self, **kwargs):
            seen_by_label[self.label] = child_env(self.label)
            return {"api_calls": 1, "final_response": "done"}

    label = {"value": ""}

    def construct(agent_id, body, frames, session_key):
        # Keep the REAL pin_profile_defaults on the path -- it is what writes
        # terminal.env_passthrough -- then hand back a stub agent instead of a model call.
        sync.pin_profile_defaults(agent_id, os.environ.get("AUXILIARY_MODEL") or turn.MODEL)
        return Agent(label["value"])

    turn._construct_agent = construct
    turn.restore_profile = lambda _a: None
    turn.save_profile = lambda _a: None
    turn.sync.restore_root_auth = lambda: None
    turn.sync.save_root_auth = lambda: None
    turn.sync.ensure_profile = lambda _a: None
    turn.report_usage = lambda *a, **k: None

    # --- ORDERING A: a room turn runs a shell command FIRST, then the native turn -----------
    # This is the expected production ordering (one warm microVM serves both lanes) and the
    # ordering that caught the passthrough-cache freeze. The room turn's scope holds only the
    # static key, so if the NAME list were derived from the live scope it would cache without
    # the five identity names and the native turn would inherit that frozen list.
    label["value"] = "ceo-room"
    room_body = {
        "version": 1, "eventKind": "user_request", "eventId": "m1", "targetAgentId": "ceo",
        "room": {"id": "r1", "kind": "channel", "name": "general"},
        "sender": {"id": "u1", "displayName": "Human"},
        "message": {"messageId": "m1", "text": "hello", "author": "u1"},
        "participants": [{"kind": "agent", "id": "ceo", "displayName": "Ada"}],
    }
    assert turn._run_hermes("ceo", room_body, None)[0] == "completed"
    print(f"  ceo   room turn child  : {seen_by_label['ceo-room']}")

    label["value"] = "ceo"
    ident = identity("ceo")
    turn.register_paperclip_run(ident, {"input": "go", "instructions": "do"})
    assert turn._run_native_paperclip(ident, {"input": "go", "instructions": "do"})[0] == "completed"
    print(f"  ceo   native AFTER room: {seen_by_label['ceo']}")

    # --- ORDERING B: the native turn is the first to run a shell command -------------------
    label["value"] = "engineering"
    ident = identity("engineering")
    turn.register_paperclip_run(ident, {"input": "go", "instructions": "do"})
    assert turn._run_native_paperclip(ident, {"input": "go", "instructions": "do"})[0] == "completed"
    print(f"  eng   native first     : {seen_by_label['engineering']}")

    for profile in profiles:
        assert seen_by_label[profile] == expected(profile), (
            f"{profile}: got {seen_by_label[profile]}\n   want {expected(profile)}"
        )
        cfg = sync.read_profile_config(profile)
        listed = (cfg.get("terminal") or {}).get("env_passthrough") or []
        assert set(NAMES) <= set(listed), f"{profile}: config.yaml passthrough is {listed}"

    # The room turn must not see another turn's identity, and must not be handed the static
    # key of a different profile.
    assert seen_by_label["ceo-room"]["PAPERCLIP_API_KEY"] == "key-ceo"
    assert not any(seen_by_label["ceo-room"][n] for n in NAMES if n != "PAPERCLIP_API_KEY")

    # Names may persist as empty placeholders (that is how Hermes's filter finds them);
    # VALUES must never reach the process env.
    leaked = {k: os.environ[k] for k in NAMES if os.environ.get(k)}
    assert not leaked, f"values leaked into the process env: {leaked}"
    assert os.environ["TERMINAL_ENV"] == "lightbulb_node"

print("PASS: the native child sees its own six values under BOTH turn orderings, "
      "with no value in the process env")
