# Hermes on AgentCore — M3 Device commands (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Master plan:** `docs/superpowers/plans/2026-09-16-hermes-agentcore-migration.md` — read its Goal, Architecture, Global Constraints and File Structure first; every task here inherits them. **Spec:** `docs/superpowers/specs/2026-09-16-hermes-agentcore-migration-design.md`.

**Milestone:** M3 Device commands. Pass criteria: spec §6. Task numbering is local to this file.

---

## Tasks

Pass criterion (spec §6): every command in the §3 inventory works from a turn.

**Correction to the brief, verified in source.** There is no `device.mac` / `device.json` /
`device.cli` command anywhere. The Tauri client's declared set is exactly
`DESKTOP_MAC_COMMANDS` (`apps/backend/core/services/desktop_pair.py:79-86`):
`system.notify`, `screen.snapshot`, `device.info`, `device.status`, `device.health`,
`device.permissions`. `camera.list/snap/clip`, `photos.latest`, `screen.record`, `location.get`,
`notifications.list/actions` are in `NOT_IMPLEMENTED_COMMANDS`
(`apps/desktop/src-tauri/src/node_invoke.rs:39-48`) — the Mac answers them
`ok:false {code:"NOT_IMPLEMENTED"}` today. M3 ships the full catalogue through the tool and the
route so the Rust side can land later with **zero** backend or bridge change; the eight
not-yet-implemented commands surface that error verbatim to the agent.

---

### Task 1: Node command catalogue (client routing + per-command timeout)

**Files:**
- Create: `apps/backend/core/services/node_commands.py`
- Create: `apps/backend/tests/unit/services/test_node_commands.py`

**Interfaces:**
- Consumes: `desktop_pair.DESKTOP_MAC_COMMANDS`, `desktop_pair.DESKTOP_CLI_COMMANDS`,
  `desktop_pair.DESKTOP_MAC_BASE_COMMANDS` (`apps/backend/core/services/desktop_pair.py:79-162`)
- Produces: `NODE_COMMANDS: dict[str, NodeCommandSpec]`,
  `NodeCommandSpec(client_id: str, timeout_ms: int, large_payload: bool)`,
  `spec_for(command: str) -> NodeCommandSpec` (raises `KeyError` for an unknown command)

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_node_commands.py
from core.services import desktop_pair, node_commands


def test_every_declared_command_has_a_spec():
    declared = set(desktop_pair.DESKTOP_MAC_COMMANDS) | set(desktop_pair.DESKTOP_CLI_COMMANDS)
    assert declared <= set(node_commands.NODE_COMMANDS)


def test_mac_commands_route_to_the_tauri_client_and_cli_commands_to_the_node_host():
    assert node_commands.spec_for("screen.snapshot").client_id == "openclaw-macos"
    assert node_commands.spec_for("device.info").client_id == "openclaw-macos"
    assert node_commands.spec_for("system.run").client_id == "node-host"
    assert node_commands.spec_for("fs.listDir").client_id == "node-host"


def test_long_capture_commands_get_a_longer_budget_than_a_device_probe():
    assert node_commands.spec_for("device.status").timeout_ms == 10_000
    assert node_commands.spec_for("screen.record").timeout_ms == 120_000
    assert node_commands.spec_for("camera.clip").timeout_ms == 120_000


def test_image_returning_commands_are_flagged_large_payload():
    assert node_commands.spec_for("screen.snapshot").large_payload is True
    assert node_commands.spec_for("camera.snap").large_payload is True
    assert node_commands.spec_for("device.info").large_payload is False


def test_unknown_command_raises():
    import pytest

    with pytest.raises(KeyError):
        node_commands.spec_for("system.rm_rf")
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_commands.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.node_commands'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/node_commands.py
"""The node command catalogue: which desktop client serves a command, and its budget.

TWO clients answer on one Mac (research 02 §0): our Tauri client (clientId
``openclaw-macos``) owns the device surface, and the vendored upstream node host
(clientId ``node-host``) owns exec/files/browser-proxy. The bridge's ``nodes`` tool
and the Hermes terminal provider both address the backend by COMMAND, so this table
is the only place that knows which socket a command belongs to.

Timeouts are per command, not global: a device probe that hangs for 120 s is a bug,
and a 10 s cap on ``screen.record`` would abort every capture.
"""

from __future__ import annotations

from dataclasses import dataclass

from core.services.desktop_pair import DESKTOP_CLI_COMMANDS, DESKTOP_MAC_COMMANDS

MAC_CLIENT_ID = "openclaw-macos"
CLI_CLIENT_ID = "node-host"


@dataclass(frozen=True)
class NodeCommandSpec:
    client_id: str
    timeout_ms: int
    #: True when the result carries a base64 blob in ``payloadJSON`` that must never
    #: reach the model transcript verbatim (see the bridge's spill, Task 4).
    large_payload: bool = False


#: Device commands the Tauri client declares or will declare. The eight the Rust
#: client answers NOT_IMPLEMENTED today (node_invoke.rs:39-48) are listed anyway so
#: the tool + route need no change when they land.
_MAC: dict[str, tuple[int, bool]] = {
    "system.notify": (10_000, False),
    "device.info": (10_000, False),
    "device.status": (10_000, False),
    "device.health": (10_000, False),
    "device.permissions": (10_000, False),
    "screen.snapshot": (30_000, True),
    "screen.record": (120_000, True),
    "camera.list": (10_000, False),
    "camera.snap": (30_000, True),
    "camera.clip": (120_000, True),
    "photos.latest": (30_000, True),
    "location.get": (20_000, False),
    "notifications.list": (15_000, False),
    "notifications.actions": (15_000, False),
}

_CLI_DEFAULT_TIMEOUT_MS = 120_000
_CLI_OVERRIDES: dict[str, tuple[int, bool]] = {
    "system.which": (10_000, False),
    "system.execApprovals.get": (10_000, False),
    "system.execApprovals.set": (10_000, False),
    "fs.listDir": (30_000, False),
    "file.fetch": (60_000, True),
    "dir.fetch": (120_000, True),
}

NODE_COMMANDS: dict[str, NodeCommandSpec] = {
    **{c: NodeCommandSpec(MAC_CLIENT_ID, t, big) for c, (t, big) in _MAC.items()},
    **{
        c: NodeCommandSpec(CLI_CLIENT_ID, *_CLI_OVERRIDES.get(c, (_CLI_DEFAULT_TIMEOUT_MS, False)))
        for c in DESKTOP_CLI_COMMANDS
    },
}

# Fail loudly at import if the pairing grants ever declare a command this table misses:
# an unmapped command is a silent 404 at invoke time, not an error anyone can read.
_DECLARED = set(DESKTOP_MAC_COMMANDS) | set(DESKTOP_CLI_COMMANDS)
assert _DECLARED <= set(NODE_COMMANDS), f"unmapped node commands: {sorted(_DECLARED - set(NODE_COMMANDS))}"


def spec_for(command: str) -> NodeCommandSpec:
    return NODE_COMMANDS[command]
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_commands.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/node_commands.py apps/backend/tests/unit/services/test_node_commands.py
git commit -m "feat(node): command catalogue — which desktop client serves a command, and its budget

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Route an invoke to the right socket, and refuse an undeclared command

**Files:**
- Modify: `apps/backend/core/services/node_broker.py` (M2's `invoke`; add client selection +
  allowlist check before the forward)
- Modify: `apps/backend/tests/unit/services/test_node_broker.py` (M2's file; append)

**Interfaces:**
- Consumes: `node_commands.spec_for`, the paired-device record's declared `commands`
  (`container_repo` row `paired_devices[deviceId]["commands"]`), M2's presence rows
- Produces: `invoke(owner_id, *, command, params_json, timeout_ms=None, client_id=None)` — when
  `timeout_ms` is None it uses the catalogue's budget; raises `NodeCommandNotAllowed` when no live
  device declared the command

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_node_broker.py  (append)
import pytest

from core.services import node_broker


@pytest.mark.asyncio
async def test_invoke_selects_the_socket_by_command_family(monkeypatch):
    seen: dict = {}

    async def fake_forward(owner_id, *, client_id, command, params_json, timeout_ms):
        seen.update(client_id=client_id, command=command, timeout_ms=timeout_ms)
        return {"id": "i1", "nodeId": "n1", "ok": True, "payloadJSON": "{}"}

    monkeypatch.setattr(node_broker, "_forward_to_socket", fake_forward)
    monkeypatch.setattr(
        node_broker, "_declared_commands",
        lambda owner_id, client_id: {"screen.snapshot", "system.run"},
    )

    await node_broker.invoke("org_a", command="screen.snapshot", params_json="{}")
    assert seen == {"client_id": "openclaw-macos", "command": "screen.snapshot", "timeout_ms": 30_000}

    await node_broker.invoke("org_a", command="system.run", params_json='{"command":["bash","-lc","ls"]}')
    assert seen["client_id"] == "node-host"
    assert seen["timeout_ms"] == 120_000


@pytest.mark.asyncio
async def test_invoke_refuses_a_command_the_paired_device_never_declared(monkeypatch):
    monkeypatch.setattr(node_broker, "_declared_commands", lambda owner_id, client_id: {"device.info"})
    with pytest.raises(node_broker.NodeCommandNotAllowed):
        await node_broker.invoke("org_a", command="camera.snap", params_json="{}")


@pytest.mark.asyncio
async def test_invoke_rejects_an_unknown_command_without_touching_a_socket(monkeypatch):
    monkeypatch.setattr(node_broker, "_declared_commands", lambda owner_id, client_id: {"device.info"})
    with pytest.raises(node_broker.NodeCommandNotAllowed):
        await node_broker.invoke("org_a", command="system.rm_rf", params_json="{}")
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py -q -k "selects_the_socket or refuses or unknown_command"`
Expected: FAIL with `AttributeError: module 'core.services.node_broker' has no attribute 'NodeCommandNotAllowed'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/node_broker.py  (add near the top)
from core.services.node_commands import spec_for


class NodeCommandNotAllowed(Exception):
    """The command is unknown, or no live paired device declared it.

    Spec §4.4: the paired device's own declared ``commands`` list is the allowlist —
    there is no ``paired.json`` inside the microVM and no ``tools.deny`` flip
    (relay_hooks.on_node_ready is a no-op for AgentCore owners).
    """


# ... inside invoke(), replacing the direct forward:
async def invoke(
    owner_id: str,
    *,
    command: str,
    params_json: str,
    timeout_ms: int | None = None,
    client_id: str | None = None,
) -> dict:
    try:
        spec = spec_for(command)
    except KeyError as exc:
        raise NodeCommandNotAllowed(f"unknown node command: {command}") from exc
    target = client_id or spec.client_id
    if command not in _declared_commands(owner_id, target):
        raise NodeCommandNotAllowed(f"{command} is not granted on this workspace's paired device")
    return await _forward_to_socket(
        owner_id,
        client_id=target,
        command=command,
        params_json=params_json,
        timeout_ms=timeout_ms or spec.timeout_ms,
    )
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py -q`
Expected: PASS (M2's cases plus the 3 new ones)

- [ ] **Step 5: Commit**
```bash
git add apps/backend/core/services/node_broker.py apps/backend/tests/unit/services/test_node_broker.py
git commit -m "feat(node): route an invoke by command family and refuse an undeclared command

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: The `nodes` Hermes tool (same plugin as the exec provider)

**Files:**
- Create: `apps/infra/hermes/plugins/lightbulb_node/tool_nodes.py`
- Modify: `apps/infra/hermes/plugins/lightbulb_node/__init__.py` (M2's `register(ctx)`; add one line)
- Create: `apps/infra/hermes/tests/conftest.py` (only if M2 did not create it)
- Create: `apps/infra/hermes/tests/test_tool_nodes.py`

**Interfaces:**
- Consumes: `ctx.register_tool(name, toolset, schema, handler, is_async=False, description=..., emoji=...)`
  (`/opt/hermes/hermes_cli/plugins.py:460-503`); the hook-token rail
  `POST {ISOL8_API_URL}/api/v1/internal/node/invoke/{ownerId}`
- Produces: tool `nodes`, toolset `terminal`, schema
  `{"command": str, "params": object (optional), "timeoutMs": int (optional)}`;
  returns a JSON string `{"ok": bool, "payload"?: any, "error"?: {"code","message"}}`

- [ ] **Step 1: Write the failing test**
```python
# apps/infra/hermes/tests/test_tool_nodes.py
import json

from plugins.lightbulb_node import tool_nodes


def test_schema_declares_command_required_and_params_optional():
    schema = tool_nodes.NODES_TOOL_SCHEMA
    assert schema["type"] == "object"
    assert schema["required"] == ["command"]
    assert set(schema["properties"]) == {"command", "params", "timeoutMs"}


def test_handler_posts_the_command_and_returns_the_payload(monkeypatch):
    calls = []

    def fake_post(path, body, timeout_s):
        calls.append((path, body, timeout_s))
        return 200, {"ok": True, "payloadJSON": json.dumps({"battery": 88})}

    monkeypatch.setattr(tool_nodes, "_post_node_invoke", fake_post)
    out = json.loads(tool_nodes.nodes_handler(command="device.status"))
    assert out == {"ok": True, "payload": {"battery": 88}}
    path, body, timeout_s = calls[0]
    assert path.endswith("/api/v1/internal/node/invoke/org_a")
    assert body == {"command": "device.status", "paramsJSON": "{}", "timeoutMs": None}
    assert timeout_s == tool_nodes.DEFAULT_HTTP_TIMEOUT_S


def test_handler_serialises_params_as_a_json_STRING(monkeypatch):
    body_seen = {}
    monkeypatch.setattr(
        tool_nodes, "_post_node_invoke",
        lambda path, body, timeout_s: (body_seen.update(body) or (200, {"ok": True, "payloadJSON": "{}"})),
    )
    tool_nodes.nodes_handler(command="system.notify", params={"title": "hi", "body": "there"})
    assert body_seen["paramsJSON"] == '{"title": "hi", "body": "there"}'


def test_handler_surfaces_a_node_error_verbatim(monkeypatch):
    monkeypatch.setattr(
        tool_nodes, "_post_node_invoke",
        lambda path, body, timeout_s: (
            200,
            {"ok": False, "error": {"code": "NOT_IMPLEMENTED", "message": "camera.snap is not yet supported"}},
        ),
    )
    out = json.loads(tool_nodes.nodes_handler(command="camera.snap"))
    assert out == {"ok": False, "error": {"code": "NOT_IMPLEMENTED", "message": "camera.snap is not yet supported"}}


def test_handler_maps_a_backend_403_to_a_readable_denial(monkeypatch):
    monkeypatch.setattr(tool_nodes, "_post_node_invoke", lambda path, body, timeout_s: (403, {}))
    out = json.loads(tool_nodes.nodes_handler(command="camera.snap"))
    assert out["ok"] is False and out["error"]["code"] == "NOT_ALLOWED"


def test_handler_maps_no_paired_device_to_a_readable_error(monkeypatch):
    monkeypatch.setattr(tool_nodes, "_post_node_invoke", lambda path, body, timeout_s: (409, {}))
    out = json.loads(tool_nodes.nodes_handler(command="device.info"))
    assert out["error"]["code"] == "NO_NODE"
    assert "desktop app" in out["error"]["message"]
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest ../infra/hermes/tests/test_tool_nodes.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'plugins.lightbulb_node.tool_nodes'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/infra/hermes/tests/conftest.py  (create only if absent)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
```
```python
# apps/infra/hermes/plugins/lightbulb_node/tool_nodes.py
"""The ``nodes`` tool — one desktop device command per call, over the hook-token rail.

Spec §4.4: the bridge NEVER opens a WebSocket. Device commands are one HTTP call to
``POST /api/v1/internal/node/invoke/{ownerId}``; the backend holds the desktop's socket,
forwards ``node.invoke.request`` and returns the ``node.invoke.result`` params.

``paramsJSON`` is a JSON **string**, not an object — both node clients ``JSON.parse`` it
(research 02 §1.6). Getting this wrong is a silent INVALID_PARAMS on the Mac.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

API = os.environ.get("ISOL8_API_URL", "").rstrip("/")
OWNER = os.environ.get("ISOL8_OWNER_ID", "")
HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "")

#: The route's own budget is per command (backend catalogue); this is the transport ceiling
#: above the longest of them (camera.clip / screen.record at 120 s) plus relay slack.
DEFAULT_HTTP_TIMEOUT_S = 150

NODES_TOOL_SCHEMA = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": (
                "Device command on the owner's paired Mac: system.notify, screen.snapshot, "
                "screen.record, camera.list, camera.snap, camera.clip, photos.latest, "
                "location.get, notifications.list, notifications.actions, device.info, "
                "device.status, device.health, device.permissions."
            ),
        },
        "params": {"type": "object", "description": "Command parameters, e.g. {\"title\":..,\"body\":..}."},
        "timeoutMs": {"type": "integer", "description": "Override the command's default budget."},
    },
    "required": ["command"],
}

_HTTP_ERRORS = {
    403: ("NOT_ALLOWED", "That command is not granted on this workspace's paired device."),
    409: ("NO_NODE", "No paired Mac is connected. Ask the owner to open the Lightbulb desktop app."),
    504: ("TIMEOUT", "The Mac did not answer in time."),
}


def _post_node_invoke(path: str, body: dict, timeout_s: int) -> tuple[int, dict]:
    req = urllib.request.Request(
        path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {HOOK_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as res:
            return res.status, json.loads(res.read() or b"{}")
    except urllib.error.HTTPError as exc:
        return exc.code, {}
    except Exception:
        return 0, {}


def _fail(code: str, message: str) -> str:
    return json.dumps({"ok": False, "error": {"code": code, "message": message}})


def nodes_handler(command: str, params: dict | None = None, timeoutMs: int | None = None, **_: object) -> str:
    status, result = _post_node_invoke(
        f"{API}/api/v1/internal/node/invoke/{OWNER}",
        {"command": command, "paramsJSON": json.dumps(params or {}), "timeoutMs": timeoutMs},
        DEFAULT_HTTP_TIMEOUT_S,
    )
    if status != 200:
        code, message = _HTTP_ERRORS.get(status, ("NODE_UNAVAILABLE", f"The node route answered {status}."))
        return _fail(code, message)
    if not result.get("ok"):
        return json.dumps({"ok": False, "error": result.get("error") or {"code": "UNKNOWN", "message": ""}})
    raw = result.get("payloadJSON")
    payload = json.loads(raw) if isinstance(raw, str) and raw else result.get("payload")
    return json.dumps({"ok": True, "payload": payload})


def register_nodes_tool(ctx) -> None:
    ctx.register_tool(
        name="nodes",
        toolset="terminal",
        schema=NODES_TOOL_SCHEMA,
        handler=nodes_handler,
        description="Run a device command on the owner's paired Mac (screen, camera, notify, device).",
        emoji="💻",
    )
```
```python
# apps/infra/hermes/plugins/lightbulb_node/__init__.py  (inside register(ctx), after the provider)
    from .tool_nodes import register_nodes_tool

    register_nodes_tool(ctx)
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest ../infra/hermes/tests/test_tool_nodes.py -q`
Expected: PASS (6 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/infra/hermes/plugins/lightbulb_node/tool_nodes.py apps/infra/hermes/plugins/lightbulb_node/__init__.py apps/infra/hermes/tests/
git commit -m "feat(hermes): nodes tool — device commands over the hook-token invoke rail

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Spill a large payload to the profile instead of the transcript

**Files:**
- Modify: `apps/infra/hermes/plugins/lightbulb_node/tool_nodes.py` (`nodes_handler` result branch)
- Modify: `apps/infra/hermes/tests/test_tool_nodes.py` (append)

**Interfaces:**
- Consumes: the Mac's screen payload `{"ok":true,"format","size","mimeType","base64","sha256"}`
  (`apps/desktop/src-tauri/src/screen.rs:80-89`)
- Produces: `{"ok": true, "payload": {"path", "bytes", "mimeType", "sha256", ...}}` with `base64`
  removed and the decoded bytes written to `$HERMES_HOME/media/<sha256[:16]>.<ext>`

- [ ] **Step 1: Write the failing test**
```python
# apps/infra/hermes/tests/test_tool_nodes.py  (append)
import base64
import hashlib
import json
import os


def test_a_base64_screenshot_is_written_to_the_profile_not_the_transcript(monkeypatch, tmp_path):
    raw = b"\x89PNG\r\n\x1a\n" + b"x" * 400_000
    digest = hashlib.sha256(raw).hexdigest()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        tool_nodes, "_post_node_invoke",
        lambda path, body, timeout_s: (
            200,
            {"ok": True, "payloadJSON": json.dumps({
                "ok": True, "format": "png", "size": len(raw),
                "mimeType": "image/png", "base64": base64.b64encode(raw).decode(), "sha256": digest,
            })},
        ),
    )
    out = json.loads(tool_nodes.nodes_handler(command="screen.snapshot"))
    payload = out["payload"]
    assert "base64" not in payload
    assert payload["bytes"] == len(raw)
    assert payload["mimeType"] == "image/png"
    written = tmp_path / "media" / f"{digest[:16]}.png"
    assert written.read_bytes() == raw
    assert payload["path"] == str(written)
    # The whole tool result must stay small enough to sit in a transcript.
    assert len(json.dumps(out)) < 2_000


def test_a_small_payload_is_returned_inline(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(
        tool_nodes, "_post_node_invoke",
        lambda path, body, timeout_s: (200, {"ok": True, "payloadJSON": json.dumps({"ok": True, "battery": 88})}),
    )
    out = json.loads(tool_nodes.nodes_handler(command="device.status"))
    assert out["payload"] == {"ok": True, "battery": 88}
    assert not (tmp_path / "media").exists()
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest ../infra/hermes/tests/test_tool_nodes.py -q -k base64`
Expected: FAIL with `KeyError: 'bytes'` (the handler still returns the raw `base64` payload)

- [ ] **Step 3: Write minimal implementation**
```python
# apps/infra/hermes/plugins/lightbulb_node/tool_nodes.py  (add, and call from nodes_handler)
import base64 as _b64
import hashlib
from pathlib import Path

_EXT_BY_MIME = {"image/png": "png", "image/jpeg": "jpg", "video/quicktime": "mov", "video/mp4": "mp4"}


def _spill_base64(payload: dict) -> dict:
    """Write a base64 blob to the profile and hand the model a path, not a megabyte.

    The HTTP route carries the blob whole (spec §4.4 — no 32/64 KB frame cap on this
    transport); the transcript is what cannot hold it. ``sha256`` is the Mac's own
    (screen.rs:77-79), so a repeat capture of an unchanged screen reuses one file.
    """
    blob = payload.get("base64")
    if not isinstance(blob, str) or not blob:
        return payload
    raw = _b64.b64decode(blob)
    digest = payload.get("sha256") or hashlib.sha256(raw).hexdigest()
    ext = _EXT_BY_MIME.get(payload.get("mimeType", ""), "bin")
    media = Path(os.environ.get("HERMES_HOME", "/opt/data")) / "media"
    media.mkdir(parents=True, exist_ok=True)
    target = media / f"{digest[:16]}.{ext}"
    target.write_bytes(raw)
    out = {k: v for k, v in payload.items() if k != "base64"}
    out.update(path=str(target), bytes=len(raw), sha256=digest)
    return out
```
```python
# ...and in nodes_handler, replace the success return with:
    payload = json.loads(raw) if isinstance(raw, str) and raw else result.get("payload")
    if isinstance(payload, dict):
        payload = _spill_base64(payload)
    return json.dumps({"ok": True, "payload": payload})
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest ../infra/hermes/tests/test_tool_nodes.py -q`
Expected: PASS (8 passed)

- [ ] **Step 5: Commit**
```bash
git add apps/infra/hermes/plugins/lightbulb_node/tool_nodes.py apps/infra/hermes/tests/test_tool_nodes.py
git commit -m "feat(hermes): spill node image/video payloads to the profile, hand the model a path

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Honour the per-command timeout end to end (route → socket → 504)

**Files:**
- Modify: `apps/backend/routers/messaging.py` (M2's route: accept `timeoutMs: int | None`,
  map `NodeCommandNotAllowed` → 403, no-device → 409, socket timeout → 504)
- Modify: `apps/backend/tests/unit/routers/test_node_invoke_route.py` (M2's file; append)

**Interfaces:**
- Consumes: `node_broker.invoke`, `node_broker.NodeCommandNotAllowed`, `node_broker.NoPairedNode`
- Produces: `200 {ok, payloadJSON?|payload?, error?}` / `403` / `409` / `504`

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/routers/test_node_invoke_route.py  (append)
import asyncio

import pytest

from core.services import node_broker


def test_route_maps_a_disallowed_command_to_403(client, hook_headers, monkeypatch):
    async def boom(*a, **k):
        raise node_broker.NodeCommandNotAllowed("camera.snap is not granted")

    monkeypatch.setattr(node_broker, "invoke", boom)
    r = client.post(
        "/api/v1/internal/node/invoke/org_a",
        json={"command": "camera.snap", "paramsJSON": "{}"},
        headers=hook_headers,
    )
    assert r.status_code == 403


def test_route_maps_no_paired_device_to_409(client, hook_headers, monkeypatch):
    async def boom(*a, **k):
        raise node_broker.NoPairedNode("no live node")

    monkeypatch.setattr(node_broker, "invoke", boom)
    r = client.post(
        "/api/v1/internal/node/invoke/org_a",
        json={"command": "device.info", "paramsJSON": "{}"},
        headers=hook_headers,
    )
    assert r.status_code == 409


def test_route_maps_a_socket_timeout_to_504(client, hook_headers, monkeypatch):
    async def boom(*a, **k):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(node_broker, "invoke", boom)
    r = client.post(
        "/api/v1/internal/node/invoke/org_a",
        json={"command": "screen.record", "paramsJSON": "{}"},
        headers=hook_headers,
    )
    assert r.status_code == 504


def test_route_passes_an_explicit_timeout_through(client, hook_headers, monkeypatch):
    seen = {}

    async def spy(owner_id, *, command, params_json, timeout_ms=None, client_id=None):
        seen.update(timeout_ms=timeout_ms)
        return {"ok": True, "payloadJSON": "{}"}

    monkeypatch.setattr(node_broker, "invoke", spy)
    client.post(
        "/api/v1/internal/node/invoke/org_a",
        json={"command": "system.run", "paramsJSON": "{}", "timeoutMs": 9000},
        headers=hook_headers,
    )
    assert seen["timeout_ms"] == 9000
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/routers/test_node_invoke_route.py -q -k "403 or 409 or 504 or timeout_through"`
Expected: FAIL with `assert 500 == 403` (the route has no exception mapping yet)

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/routers/messaging.py
import asyncio

from fastapi import HTTPException, Request

from core.services import node_broker


@router.post("/internal/node/invoke/{owner_id}")
async def node_invoke(owner_id: str, body: NodeInvokeRequest, request: Request):
    await _require_hook_token(owner_id, request)
    try:
        return await node_broker.invoke(
            owner_id,
            command=body.command,
            params_json=body.paramsJSON,
            timeout_ms=body.timeoutMs,
        )
    except node_broker.NodeCommandNotAllowed as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except node_broker.NoPairedNode as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except asyncio.TimeoutError as exc:
        raise HTTPException(status_code=504, detail="the node did not answer in time") from exc
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/routers/test_node_invoke_route.py -q`
Expected: PASS

- [ ] **Step 5: Commit**
```bash
git add apps/backend/routers/messaging.py apps/backend/tests/unit/routers/test_node_invoke_route.py
git commit -m "feat(node): per-command timeout and readable 403/409/504 on the invoke route

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Dev proof — every device command from a real turn

**Files:**
- Create: `apps/backend/scripts/node_device_smoke.py` (re-runnable ops tool; stays)
- Modify: `.superpowers/sdd/2026-09-16-hermes-agentcore-migration/progress.md` (record the run)

**Interfaces:**
- Consumes: `node_commands.NODE_COMMANDS`, `node_broker.invoke`
- Produces: one line per command — `OK` / `NOT_IMPLEMENTED` / `DENIED` / `TIMEOUT`, exit 1 if any
  command in `DESKTOP_MAC_COMMANDS` is not `OK`

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/scripts/test_node_device_smoke.py
import pytest

from scripts import node_device_smoke


def test_classify_reads_each_outcome():
    assert node_device_smoke.classify({"ok": True, "payloadJSON": "{}"}) == "OK"
    assert node_device_smoke.classify({"ok": False, "error": {"code": "NOT_IMPLEMENTED"}}) == "NOT_IMPLEMENTED"


def test_exit_code_is_1_when_a_declared_mac_command_fails():
    results = {c: "OK" for c in node_device_smoke.REQUIRED}
    assert node_device_smoke.exit_code(results) == 0
    results["screen.snapshot"] = "TIMEOUT"
    assert node_device_smoke.exit_code(results) == 1


def test_a_not_implemented_command_outside_the_required_set_does_not_fail_the_run():
    results = {c: "OK" for c in node_device_smoke.REQUIRED} | {"camera.snap": "NOT_IMPLEMENTED"}
    assert node_device_smoke.exit_code(results) == 0
```

- [ ] **Step 2: Run test to verify it fails**
Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_node_device_smoke.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.node_device_smoke'`

- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/scripts/node_device_smoke.py
"""Run every device command against an owner's paired Mac and report per command.

Re-runnable ops tool (CLAUDE.md keeps these): the M3 gate is "every command in the
inventory works from a turn", and the eight the Rust client has not implemented yet
must read NOT_IMPLEMENTED, never TIMEOUT — a timeout means the routing is wrong.

    cd apps/backend && uv run python -m scripts.node_device_smoke org_XXX
"""

from __future__ import annotations

import asyncio
import sys

from core.services.desktop_pair import DESKTOP_MAC_COMMANDS
from core.services.node_commands import MAC_CLIENT_ID, NODE_COMMANDS

REQUIRED = tuple(DESKTOP_MAC_COMMANDS)

_PARAMS: dict[str, dict] = {
    "system.notify": {"title": "Lightbulb", "body": "node smoke test"},
    "screen.snapshot": {"format": "jpg"},
}


def classify(result: dict) -> str:
    if result.get("ok"):
        return "OK"
    return (result.get("error") or {}).get("code") or "ERROR"


def exit_code(results: dict[str, str]) -> int:
    return 0 if all(results.get(c) == "OK" for c in REQUIRED) else 1


async def main(owner_id: str) -> int:
    import json

    from core.services import node_broker

    results: dict[str, str] = {}
    for command, spec in NODE_COMMANDS.items():
        if spec.client_id != MAC_CLIENT_ID:
            continue
        try:
            result = await node_broker.invoke(
                owner_id, command=command, params_json=json.dumps(_PARAMS.get(command, {}))
            )
            results[command] = classify(result)
        except asyncio.TimeoutError:
            results[command] = "TIMEOUT"
        except node_broker.NodeCommandNotAllowed:
            results[command] = "DENIED"
        print(f"{command:<24} {results[command]}")
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1])))
```

- [ ] **Step 4: Run test to verify it passes**
Run: `cd apps/backend && uv run pytest tests/unit/scripts/test_node_device_smoke.py -q`
Expected: PASS (3 passed)

Then the dev proof itself, against the founder Mac with the desktop app open:
Run: `cd apps/backend && uv run python -m scripts.node_device_smoke org_3JCfoyOVF0AjDYjOnIZz5cK8XJL`
Expected: `system.notify OK`, `screen.snapshot OK`, `device.info OK`, `device.status OK`,
`device.health OK`, `device.permissions OK`, the other eight `NOT_IMPLEMENTED`, exit 0 —
plus a visible macOS banner and a file under the profile's `media/`.

- [ ] **Step 5: Commit**
```bash
git add apps/backend/scripts/node_device_smoke.py apps/backend/tests/unit/scripts/test_node_device_smoke.py .superpowers/sdd/2026-09-16-hermes-agentcore-migration/progress.md
git commit -m "test(node): device-command smoke tool + M3 dev proof on the founder Mac

Co-Authored-By: Claude Opus 5 (1M context) <noreply@anthropic.com>"
```

---

