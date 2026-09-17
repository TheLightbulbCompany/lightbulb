"""`lightbulb_node`: Hermes's terminal environment provider, running a turn's shell commands
on the owner's paired Mac through the Lightbulb node route.

The bridge never opens a WebSocket (spec §4.4). One HTTP call per command on the hook-token
rail the run callbacks already use; the BACKEND holds the desktop socket and speaks the node
protocol. M3 adds the device `nodes` tool to this same plugin.

M2-R7: `agent.terminal_env_provider` is imported LAZILY, inside `_provider()` -- the provider
factory `register()` calls -- mirroring the lazy `from agent.redact import
register_redaction_patterns` in `plugins/lightbulb/__init__.py`. `LightbulbNodeProvider` below
deliberately does NOT subclass the real ABC at module scope, so this module (and its behaviour)
stays importable and testable with no Hermes image on the path; `_provider()` combines it with
the real base only when something actually registers it with Hermes, and Hermes's registration
gate is a plain `isinstance` check (`hermes_cli/plugins.py::_wrong_type`), which a dynamically
built subclass satisfies exactly like a normal one.
"""
from __future__ import annotations

import json
import os
import urllib.request

DEFAULT_TIMEOUT_S = 180
_TIMEOUT_RETURNCODE = 124   # Hermes's own sentinel (tools/environments/base.py:401-405, 575)


def _invoke(command: str, params: dict, timeout_ms: int) -> dict:
    # M2-R12: exec is not turn-scoped, so the bearer comes from the process env the boot-fetch
    # (bridge/__init__.py) writes into -- never a per-turn `.lightbulb-turn` context file.
    api = os.environ.get("ISOL8_API_URL", "").rstrip("/")
    owner, token = os.environ.get("ISOL8_OWNER_ID", ""), os.environ.get("OPENCLAW_HOOK_TOKEN", "")
    request = urllib.request.Request(
        f"{api}/api/v1/internal/node/invoke/{owner}",
        data=json.dumps({"command": command, "paramsJSON": json.dumps(params),
                         "timeoutMs": timeout_ms}).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout_ms / 1000 + 10) as res:
        return json.loads(res.read() or b"{}")


class NodeEnvironment:
    """Duck-typed: Hermes only needs execute() and cleanup(). There is no local process to
    manage, so nothing from BaseEnvironment applies."""

    def __init__(self, cwd: str, timeout: int) -> None:
        self._cwd, self._timeout = cwd, timeout or DEFAULT_TIMEOUT_S

    def execute(self, command: str, cwd: str = "", *, timeout: int | None = None, **_kw) -> dict:
        seconds = int(timeout or self._timeout)
        # No stdin: the node host hardcodes an empty input buffer, which is exactly what
        # Hermes's heredoc stdin mode exists for.
        params = {"command": ["bash", "-lc", command], "cwd": cwd or self._cwd,
                  "timeoutMs": seconds * 1000}
        try:
            answer = _invoke("system.run", params, seconds * 1000)
        except Exception as exc:  # noqa: BLE001
            return {"output": f"[node unavailable: {type(exc).__name__}]", "returncode": None}
        if not answer.get("ok"):
            error = answer.get("error") or {}
            return {"output": f"[node error {error.get('code', 'UNKNOWN')}: {error.get('message', '')}]",
                    "returncode": None}
        result = answer.get("payload") or {}
        output = f"{result.get('stdout') or ''}{result.get('stderr') or ''}"
        if result.get("error"):                      # spawn failure, not a nonzero exit
            return {"output": f"{output}[{result['error']}]", "returncode": None}
        if result.get("timedOut"):
            return {"output": f"{output}[Command timed out after {seconds}s]",
                    "returncode": _TIMEOUT_RETURNCODE}
        return {"output": output, "returncode": result.get("exitCode")}

    def cleanup(self) -> None:
        """The desktop node owns its own lifetime; there is nothing of ours on that Mac."""
        return None


class LightbulbNodeProvider:
    """Plain implementation -- deliberately not inheriting the real ABC (see module docstring).
    Implements every method/attribute `agent.terminal_env_provider.TerminalEnvironmentProvider`
    requires, so `_provider()` can turn it into a real subclass instance with no behaviour
    change, and this class is directly usable on its own for everything else."""

    name = "lightbulb_node"
    is_remote = True        # suppresses host-OS/home/cwd hints and the host Python probe
    is_container = False    # a Mac, not a sandbox: dangerous-command approvals stay ON
    cache_path_base = None
    session_isolated_when_nonpersistent = False
    # Replaces the paperclip-exec-creds plugin: never hand a spawned subprocess the gate
    # bearer or the Paperclip key.
    strip_env_keys = frozenset({"AWS_BEARER_TOKEN_BEDROCK", "PAPERCLIP_API_KEY"})

    def is_available(self) -> bool:
        return bool(os.environ.get("ISOL8_API_URL") and os.environ.get("OPENCLAW_HOOK_TOKEN"))

    def create_environment(self, *, cwd: str, timeout: int, task_id: str = "default",
                           image=None, container_config=None, **kwargs) -> NodeEnvironment:
        return NodeEnvironment(cwd, timeout)


def _provider() -> LightbulbNodeProvider:
    """The provider factory (M2-R7): lazily imports the real ABC and returns an instance of a
    class combining it with `LightbulbNodeProvider`, so Hermes's isinstance-gated registration
    (`hermes_cli/plugins.py::_wrong_type`) accepts it."""
    from agent.terminal_env_provider import TerminalEnvironmentProvider

    combined = type("LightbulbNodeProvider", (LightbulbNodeProvider, TerminalEnvironmentProvider), {})
    return combined()


def register(ctx) -> None:
    """Registration is per profile: `terminal.backend: lightbulb_node` in the agent's
    config.yaml (Task 17's config.patch can already write it) selects this provider."""
    ctx.register_terminal_environment_provider(_provider())
