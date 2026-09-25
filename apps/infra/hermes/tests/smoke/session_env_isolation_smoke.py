"""Each agent+session gets its own shell: own $HOME, and no `export` leaks between sessions.

Not collected by pytest (no ``test_`` prefix): the mechanism is Hermes's terminal environment
cache, so only the pinned image proves it. Run from ``apps/infra/hermes``::

    docker run --rm --network none \
      -v "$PWD/bridge:/opt/bridge/bridge:ro" \
      -v "$PWD/tests/smoke/session_env_isolation_smoke.py:/opt/bridge/smoke.py:ro" \
      --entrypoint python <hermes image> /opt/bridge/smoke.py

Without a bound HERMES_SESSION_KEY every turn in the process shares Hermes's ``"default"``
LocalEnvironment, whose snapshot is the FIRST shell's ``export -p``: $HOME froze at the first
profile and agent A's ``export`` showed up in agent B's commands.
"""
import json
import os
import tempfile
from pathlib import Path

from bridge import sync, turn
from tools.terminal_tool import terminal_tool


def shell(agent: str, room: str, cmd: str) -> str:
    key = turn.lightbulb_session_key(agent, room)
    with turn._hermes_home_scope(agent), turn._secret_scope(agent), turn._terminal_scope(agent, key):
        result = json.loads(terminal_tool(cmd, timeout=30))
    assert result.get("exit_code") == 0, result
    return result["output"].strip()


with tempfile.TemporaryDirectory() as root:
    sync.HOME = Path(root)
    os.environ["TERMINAL_ENV"] = "local"
    for agent in ("ceo", "engineering"):
        sync._profile_dir(agent).mkdir(parents=True)
        (sync._profile_dir(agent) / "config.yaml").write_text("terminal:\n  backend: local\n")
        sync.ensure_profile_home(agent)

    # CEO shells first and exports -- the exact ordering that used to freeze the shared snapshot.
    assert shell("ceo", "r1", "export LEAK=ceo-secret; alias leaky=true; echo $HOME") == str(
        sync._profile_dir("ceo") / "home")
    homes = {a: shell(a, "r1", "echo $HOME") for a in ("ceo", "engineering")}
    print("HOME:", homes)
    assert homes == {a: str(sync._profile_dir(a) / "home") for a in homes}, homes

    # Same session keeps its own state (a session is a shell, as before) ...
    assert shell("ceo", "r1", 'echo "${LEAK:-unset}"') == "ceo-secret"
    # ... but it is invisible to another agent and to the same agent's other room.
    for agent, room in (("engineering", "r1"), ("ceo", "r2")):
        seen = shell(agent, room, 'echo "${LEAK:-unset}"; type leaky >/dev/null 2>&1 && echo alias || echo noalias')
        print(f"{agent}/{room}:", seen.split())
        assert seen.split() == ["unset", "noalias"], (agent, room, seen)

print("PASS: per-agent $HOME; an export/alias stays in its own agent+room session")
