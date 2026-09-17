# Hermes on AgentCore — M2 Node exec via lazy attach (hard gate) (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Master plan:** `docs/superpowers/plans/2026-09-16-hermes-agentcore-migration.md` — read its Goal, Architecture, Global Constraints and File Structure first; every task here inherits them. **Spec:** `docs/superpowers/specs/2026-09-16-hermes-agentcore-migration-design.md`.

**Milestone:** M2 Node exec via lazy attach (hard gate). Pass criteria: spec §6. Task numbering is local to this file.

---

## Tasks

Spec §4.4. The desktop is unchanged — same binaries, same pairing, same `bv-` relay door. The **backend** terminates the node protocol for an AgentCore workspace, and the bridge makes one HTTP call per command. Build against the **CLI node-host** contract (`clientId node-host`, the vendored upstream `openclaw node run`), which is a strict superset of our Rust client's (report 02 §1.9).

**Backend test command, used in every M2 backend task:**

```bash
cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py -q
```

---

### Task 24: `node_broker` — the server-first handshake as pure functions (`connect.challenge`, `hello-ok`, ticks)

Implements spec §4.4 ("`gateway_relay.py` … now answers `connect.challenge` / `connect` / `hello-ok`, keeps ticking, holds the socket, and uses the paired device's declared `commands` as the allowlist") and report 02 §10's first verification step ("the `hello-ok` must match the checklist → two pure-dict unit tests").

Every field below is **required** by `HelloOkSchema` via `SnapshotSchema`/`StateVersionSchema` (report 02 §1.5, §2.2 checklist). Three non-obvious rules: `protocol` must be `4` (a higher value makes the CLI close 1012 and reconnect forever); `auth.deviceToken` must be **omitted** (present, the CLI persists it and every later reconnect authenticates with a token the backend minted instead of the device identity — `client.ts:951-963`); and `policy.tickIntervalMs` is a promise the broker then has to keep, because the CLI closes **4000 "tick timeout"** at twice that interval and refreshes its `lastTick` on *any* inbound frame (`client.ts:1288-1324`).

The connect signature is verified locally as a **cheap self-consistency check, not the gate** — the gate is `machine_auth.verify_upgrade`, already done at the WebSocket upgrade (report 02 §2.2 step 1). Note our Rust client signs with its **local** clock and never reads the challenge `ts` (`node_client.rs:682-685`) while the CLI passes the challenge ts through, so the broker must **not** require `signedAt == challenge.ts`.

**Files:**
- Create: `apps/backend/core/services/node_broker.py`
- Test: `apps/backend/tests/unit/services/test_node_broker.py`

**Interfaces:**
- Produces: `node_broker.challenge_frame() -> dict`; `node_broker.hello_ok(connect_req_id: str, conn_id: str, *, tick_interval_ms: int = 15000) -> dict`; `node_broker.tick_frame() -> dict`; `node_broker.device_payload_v3(connect_params: dict, nonce: str) -> str`; `node_broker.signature_matches(connect_params: dict, nonce: str) -> bool`; `node_broker.TICK_INTERVAL_MS`, `MAX_PAYLOAD`, `MAX_BUFFERED_BYTES`.
- Consumes: the client's `connect` req frame (`{type:"req", id, method:"connect", params:{minProtocol, maxProtocol, client:{id, displayName, version, platform, mode, instanceId}, role, scopes, caps, commands, pathEnv, auth:{token}, device:{id, publicKey, signature, signedAt, nonce}}}`, report 02 §1.3).

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_node_broker.py
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from core.services import node_broker


def _b64u(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def test_the_challenge_is_an_event_with_a_nonce_and_a_non_negative_integer_ts():
    frame = node_broker.challenge_frame()
    assert frame["type"] == "event" and frame["event"] == "connect.challenge"
    assert isinstance(frame["payload"]["nonce"], str) and frame["payload"]["nonce"].strip()
    assert isinstance(frame["payload"]["ts"], int) and frame["payload"]["ts"] >= 0


def test_hello_ok_matches_the_upstream_clients_checklist_field_for_field():
    hello = node_broker.hello_ok("req-1", "conn-1")
    assert hello["type"] == "res" and hello["id"] == "req-1" and hello["ok"] is True
    payload = hello["payload"]
    assert payload["type"] == "hello-ok" and payload["protocol"] == 4
    assert payload["server"]["version"] and payload["server"]["connId"] == "conn-1"
    assert isinstance(payload["features"]["methods"], list) and isinstance(payload["features"]["events"], list)
    assert payload["snapshot"] == {"presence": [], "health": {},
                                   "stateVersion": {"presence": 0, "health": 0}, "uptimeMs": 0}
    assert payload["auth"] == {"role": "node", "scopes": []}
    assert payload["policy"] == {"maxPayload": 26214400, "maxBufferedBytes": 52428800,
                                 "tickIntervalMs": 15000}


def test_hello_ok_never_mints_a_device_token():
    # Present, the CLI persists it and every later reconnect authenticates with OUR token
    # instead of the device identity — the Keychain path goes cold (client.ts:951-963).
    assert "deviceToken" not in node_broker.hello_ok("r", "c")["payload"]["auth"]


def test_the_v3_payload_string_is_pipe_joined_in_the_locked_order():
    params = {"client": {"id": "openclaw-macos", "mode": "node", "platform": "macOS"}, "role": "node",
              "scopes": [], "auth": {"token": "gw-tok"},
              "device": {"id": "did-abc", "signedAt": 1700000000000, "nonce": "nonce-xyz"}}
    assert node_broker.device_payload_v3(params, "nonce-xyz") == (
        "v3|did-abc|openclaw-macos|node|node||1700000000000|gw-tok|nonce-xyz|macos|")


def test_a_real_ed25519_signature_verifies_and_a_tampered_one_does_not():
    key = Ed25519PrivateKey.generate()
    raw = key.public_key().public_bytes_raw()
    params = {"client": {"id": "node-host", "mode": "node", "platform": "macos"}, "role": "node",
              "scopes": [], "auth": {"token": "t"},
              "device": {"id": "d", "signedAt": 1, "nonce": "n", "publicKey": _b64u(raw)}}
    params["device"]["signature"] = _b64u(key.sign(node_broker.device_payload_v3(params, "n").encode()))
    assert node_broker.signature_matches(params, "n") is True
    params["device"]["signature"] = _b64u(b"\x00" * 64)
    assert node_broker.signature_matches(params, "n") is False


def test_a_clock_skewed_signed_at_is_not_a_rejection():
    # Our Rust client signs with its LOCAL clock and never reads the challenge ts.
    assert node_broker.hello_ok("r", "c")  # sentinel: nothing in the broker compares signedAt to ts
    assert "signedAt" not in str(node_broker.challenge_frame())
```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py -q`
  Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.node_broker'`
- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/node_broker.py
"""The node protocol, terminated in the backend for an AgentCore workspace.

The desktop keeps its socket to the bv- relay; the bridge never opens one (spec §4.4). This
module is the frame layer: pure functions plus the in-process invoke map. Build against the
CLI node-host's contract, which is a strict superset of our Rust client's.
"""

from __future__ import annotations

import base64
import secrets
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROTOCOL_VERSION = 4
SERVER_VERSION = "lightbulb-bridge/1"
TICK_INTERVAL_MS = 15_000       # we promise it in hello-ok; the CLI closes 4000 at 2x
MAX_PAYLOAD = 26_214_400        # 25 MiB — the real gateway's value; screen snapshots need it
MAX_BUFFERED_BYTES = 52_428_800
CHALLENGE_DEADLINE_S = 10.0     # the CLI's own watchdog is ~15 s; leave headroom
NODE_METHODS = ("node.invoke.result", "node.invoke.progress", "node.event")
NODE_EVENTS = ("node.invoke.request", "node.invoke.cancel", "node.invoke.input", "tick")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def challenge_frame() -> dict:
    """Sent unprompted the moment the socket opens — the gateway is server-first and neither
    client speaks until it lands. `ts` must be a non-negative integer for WS challenges."""
    return {"type": "event", "event": "connect.challenge",
            "payload": {"nonce": secrets.token_urlsafe(24), "ts": int(time.time() * 1000)}}


def tick_frame() -> dict:
    return {"type": "event", "event": "tick", "payload": {"ts": int(time.time() * 1000)}}


def hello_ok(connect_req_id: str, conn_id: str, *, tick_interval_ms: int = TICK_INTERVAL_MS) -> dict:
    """Every field is REQUIRED by HelloOkSchema; `health: {}` is legal for a hello snapshot.
    auth.deviceToken is deliberately absent — see the test."""
    return {"type": "res", "id": connect_req_id, "ok": True, "payload": {
        "type": "hello-ok", "protocol": PROTOCOL_VERSION,
        "server": {"version": SERVER_VERSION, "connId": conn_id},
        "features": {"methods": list(NODE_METHODS), "events": list(NODE_EVENTS)},
        "snapshot": {"presence": [], "health": {},
                     "stateVersion": {"presence": 0, "health": 0}, "uptimeMs": 0},
        "auth": {"role": "node", "scopes": []},
        "policy": {"maxPayload": MAX_PAYLOAD, "maxBufferedBytes": MAX_BUFFERED_BYTES,
                   "tickIntervalMs": tick_interval_ms},
    }}


def _ascii_lower(value: str) -> str:
    # trim, then ASCII-ONLY lowercase — non-ASCII codepoints are left alone (node_client.rs:645-664)
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in (value or "").strip())


def device_payload_v3(params: dict, nonce: str) -> str:
    client, device = params.get("client") or {}, params.get("device") or {}
    return "|".join((
        "v3", str(device.get("id") or ""), str(client.get("id") or ""),
        str(client.get("mode") or ""), str(params.get("role") or ""),
        ",".join(params.get("scopes") or []), str(device.get("signedAt") or ""),
        str((params.get("auth") or {}).get("token") or ""), nonce,
        _ascii_lower(str(client.get("platform") or "")), _ascii_lower(str(device.get("deviceFamily") or "")),
    ))


def signature_matches(params: dict, nonce: str) -> bool:
    """Self-certifying (the key arrives in the same frame), so this proves the client holds
    the key it claims — NOT that the key is trusted. The real gate is machine_auth at the
    upgrade. Cheap, and it turns a silent mismatch into a loud one."""
    device = params.get("device") or {}
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_b64u_decode(str(device.get("publicKey") or "")))
        public_key.verify(_b64u_decode(str(device.get("signature") or "")),
                          device_payload_v3(params, nonce).encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False
```
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py -q`
  Expected: PASS — `6 passed`
- [ ] **Step 5: Commit**
```bash
git add apps/backend && git commit -m "feat(node): node_broker handshake frames — server-first challenge, hello-ok to the upstream checklist, v3 signature"
```

---

### Task 25: `node_broker` — `node.invoke.request` with `paramsJSON` as a **string**, result correlation by id, timeouts

Implements spec §4.4 ("`paramsJSON` as a JSON *string* on `node.invoke.request`") and report 02 §10's second verification ("one unit test asserting the emitted `node.invoke.request` payload matches `NodeInvokeRequestEventSchema` field for field"). Report 02 R7 is the failure mode: an object instead of a string gets a **silent no-op** from our Rust client (`serde_json::from_value` yields `params_json: None`) and `INVALID_REQUEST: paramsJSON required` from the CLI.

Two more rules from report 02 §1.7: a `node.invoke.result` arrives as a **`req`** and the broker must answer it with a `res` (unanswered, the CLI's pending entry suppresses its own tick watchdog), and the invoke map must be keyed by **invoke id, never node id** — `execute()` calls against one cached environment interleave by design (`/opt/hermes/tools/environments/base.py:462-464`).

**Files:**
- Modify: `apps/backend/core/services/node_broker.py` (add `NodeSession`)
- Test: `apps/backend/tests/unit/services/test_node_broker.py` (extend)

**Interfaces:**
- Produces: `node_broker.invoke_request(invoke_id, node_id, command, params: dict, timeout_ms: int) -> dict`; `node_broker.ack(req_id: str) -> dict`; class `NodeSession(node_id, commands: set[str])` with `async invoke(command, params, timeout_ms) -> dict`, `resolve(frame: dict) -> dict | None`, `pending: dict[str, asyncio.Future]`, and `CommandNotAllowed` / `NodeInvokeTimeout` exceptions.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_node_broker.py  (add)
import asyncio, json, pytest
from core.services import node_broker


def test_params_json_is_a_string_not_an_object():
    frame = node_broker.invoke_request("inv-1", "dev-1", "system.run",
                                       {"command": ["bash", "-lc", "ls"]}, 120_000)
    assert frame == {"type": "event", "event": "node.invoke.request", "payload": {
        "id": "inv-1", "nodeId": "dev-1", "command": "system.run",
        "paramsJSON": json.dumps({"command": ["bash", "-lc", "ls"]}), "timeoutMs": 120_000}}
    assert isinstance(frame["payload"]["paramsJSON"], str)


async def test_a_result_resolves_its_own_invoke_and_is_acked():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    task = asyncio.create_task(session.invoke("system.run", {"command": ["ls"]}, 5_000))
    await asyncio.sleep(0)
    invoke_id = next(iter(session.pending))
    ack = session.resolve({"type": "req", "id": "r-9", "method": "node.invoke.result",
                           "params": {"id": invoke_id, "nodeId": "dev-1", "ok": True,
                                      "payloadJSON": json.dumps({"stdout": "README.md", "exitCode": 0})}})
    assert ack == {"type": "res", "id": "r-9", "ok": True}
    assert (await task) == {"ok": True, "payload": {"stdout": "README.md", "exitCode": 0}}
    assert session.pending == {}


async def test_a_late_or_unknown_result_is_acked_and_dropped_not_raised():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    assert session.resolve({"type": "req", "id": "r-1", "method": "node.invoke.result",
                            "params": {"id": "ghost", "ok": True}}) == {"type": "res", "id": "r-1", "ok": True}


async def test_a_timeout_clears_the_pending_entry_so_the_map_cannot_grow():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    with pytest.raises(node_broker.NodeInvokeTimeout):
        await session.invoke("system.run", {"command": ["sleep", "9"]}, 10)
    assert session.pending == {}


async def test_a_command_the_device_never_declared_is_refused_before_it_reaches_the_socket():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    with pytest.raises(node_broker.CommandNotAllowed):
        await session.invoke("camera.capture", {}, 1_000)


async def test_progress_and_node_event_are_acked_and_need_no_implementation():
    session = node_broker.NodeSession("dev-1", {"system.run"})
    for method in ("node.invoke.progress", "node.event"):
        assert session.resolve({"type": "req", "id": "x", "method": method, "params": {}}) == {
            "type": "res", "id": "x", "ok": True}
```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py -q`
  Expected: FAIL with `AttributeError: module 'core.services.node_broker' has no attribute 'invoke_request'`
- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/node_broker.py  (add)
import asyncio
import json
import uuid


class CommandNotAllowed(Exception):
    """The paired device never declared this command — the allowlist is the paired record's
    own `commands`, which replaces the container's tools-deny flip (report 02 §2.2 step 4)."""


class NodeInvokeTimeout(Exception):
    pass


def invoke_request(invoke_id: str, node_id: str, command: str, params: dict, timeout_ms: int) -> dict:
    return {"type": "event", "event": "node.invoke.request", "payload": {
        "id": invoke_id, "nodeId": node_id, "command": command,
        # A JSON STRING, not an object: an object is a silent no-op on the Rust client and an
        # INVALID_REQUEST on the CLI (report 02 R7).
        "paramsJSON": json.dumps(params or {}), "timeoutMs": int(timeout_ms)}}


def ack(req_id: str) -> dict:
    """Every node->gateway `req` must get a `res`. An unanswered one keeps a pending entry
    alive on the CLI, which SUPPRESSES its tick watchdog (client.ts:1305-1311)."""
    return {"type": "res", "id": req_id, "ok": True}


class NodeSession:
    """One live desktop socket's invoke map. Keyed by INVOKE id, never node id: concurrent
    tool calls from one agent interleave by design."""

    def __init__(self, node_id: str, commands: set[str]) -> None:
        self.node_id, self.commands = node_id, set(commands)
        self.pending: dict[str, asyncio.Future] = {}
        self.send: "asyncio.Queue[dict]" = asyncio.Queue()

    async def invoke(self, command: str, params: dict, timeout_ms: int) -> dict:
        if command not in self.commands:
            raise CommandNotAllowed(command)
        invoke_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[invoke_id] = future
        await self.send.put(invoke_request(invoke_id, self.node_id, command, params, timeout_ms))
        try:
            return await asyncio.wait_for(future, timeout=timeout_ms / 1000 + 5)
        except asyncio.TimeoutError as exc:
            raise NodeInvokeTimeout(command) from exc
        finally:
            self.pending.pop(invoke_id, None)

    def resolve(self, frame: dict) -> dict | None:
        """Answer a node->gateway req. Returns the ack to send, or None for a non-req frame."""
        if frame.get("type") != "req":
            return None
        params = frame.get("params") or {}
        if frame.get("method") == "node.invoke.result":
            future = self.pending.get(str(params.get("id") or ""))
            if future is not None and not future.done():
                payload = params.get("payload")
                if payload is None and isinstance(params.get("payloadJSON"), str):
                    try:
                        payload = json.loads(params["payloadJSON"])
                    except ValueError:
                        payload = None
                future.set_result({"ok": bool(params.get("ok")), "payload": payload,
                                   **({"error": params["error"]} if params.get("error") else {})})
        # progress / node.event / anything else: ack and drop. The desktop's declared surface
        # never streams, so there is nothing to implement here.
        return ack(str(frame.get("id") or ""))
```
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_node_broker.py -q`
  Expected: PASS — `12 passed`
- [ ] **Step 5: Commit**
```bash
git add apps/backend && git commit -m "feat(node): node.invoke.request with paramsJSON as a string, result correlation by invoke id, bounded timeouts"
```

---

### Task 26: `gateway_relay.py` terminates the node protocol for an AgentCore workspace; `relay_hooks` no-ops

Implements spec §4.4's relay half. Today the relay dials `ws://{ip}:18789` (`apps/backend/routers/gateway_relay.py:135-137`), and for an AgentCore owner `resolve_running_container` returns the **sentinel string `"agentcore"`** as the IP (`core/containers/ecs_manager.py:2643-2648`) — deliberately truthy, so the relay would literally dial `ws://agentcore:18789`. That one branch is the change: instead of a second socket, the relay runs the broker.

`relay_hooks.on_node_ready` does two OpenClaw-shaped things on every hello-ok — `approve_pending_node_surface` (an RPC chain into the container's gateway) and, on the first live node, `patch_openclaw_config(..., {"tools": {"deny": TOOL_DENY_BASE}})` on EFS (`relay_hooks.py:118-127`). Neither exists on Hermes, and with the hourly reconnect the AgentCore WS door would have made, the second would have been an hourly config-patch storm against a file that does not exist. Both become no-ops for an AgentCore owner; the broker's `commands` allowlist (Task 25) replaces the deny-list flip. `publish_node_invalidation` is transport-agnostic and **stays** — the frontend still needs the `runtime.resource.invalidated` event.

**Files:**
- Modify: `apps/backend/routers/gateway_relay.py:134-250` (the `agentcore` branch)
- Modify: `apps/backend/core/services/relay_hooks.py:117-145` (guard both hooks)
- Modify: `apps/backend/core/services/desktop_pair.py` (export `declared_commands_for(owner_id, device_id) -> set[str]` reading the paired record)
- Test: `apps/backend/tests/unit/services/test_relay_hooks_agentcore.py`, `apps/backend/tests/unit/routers/test_gateway_relay_agentcore.py`

**Interfaces:**
- Produces: `gateway_relay._serve_node_protocol(websocket, identity, container) -> int` (returns the close code) — sends the challenge within `CHALLENGE_DEADLINE_S`, validates the `connect` req, answers `hello_ok`, registers the `NodeSession` in a module-level `SESSIONS: dict[owner_id, NodeSession]`, and runs three tasks: read/dispatch, drain `session.send`, tick every `TICK_INTERVAL_MS`.
- Consumes: `runtime_transport.host_for(container)`, `machine_auth.MachineIdentity`, `desktop_pair.declared_commands_for`.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_relay_hooks_agentcore.py
import pytest
from core.services import relay_hooks


async def test_an_agentcore_owner_skips_both_openclaw_shaped_hooks(monkeypatch):
    called = []
    monkeypatch.setattr(relay_hooks, "approve_pending_node_surface",
                        lambda **kw: called.append("approve"))
    monkeypatch.setattr(relay_hooks, "patch_openclaw_config", lambda *a, **kw: called.append("patch"))
    monkeypatch.setattr(relay_hooks, "_is_agentcore", lambda owner_id: True)
    published = []
    monkeypatch.setattr(relay_hooks, "publish_node_invalidation",
                        lambda owner, user: published.append(owner))
    await relay_hooks.on_node_ready("org_1", "user_1", "dev_1")
    await relay_hooks.on_node_closed("org_1", "user_1")
    assert called == []                     # no container RPC, no EFS config patch
    assert published == ["org_1", "org_1"]  # the UI still learns the node came and went


async def test_an_ecs_owner_is_unchanged(monkeypatch):
    called = []
    monkeypatch.setattr(relay_hooks, "approve_pending_node_surface", lambda **kw: called.append("approve"))
    monkeypatch.setattr(relay_hooks, "_is_agentcore", lambda owner_id: False)
    monkeypatch.setattr(relay_hooks, "publish_node_invalidation", lambda owner, user: None)
    monkeypatch.setattr(relay_hooks.ConnectionService, "count_relay_nodes", lambda self, owner: 0)
    await relay_hooks.on_node_ready("org_1", "user_1", "dev_1")
    assert called == ["approve"]
```
```python
# apps/backend/tests/unit/routers/test_gateway_relay_agentcore.py
import json, pytest
from routers import gateway_relay
from core.services import node_broker


async def test_the_relay_speaks_first_and_answers_a_valid_connect_with_hello_ok(monkeypatch):
    sent, received = [], [json.dumps({"type": "req", "id": "c-1", "method": "connect",
                                      "params": {"minProtocol": 4, "maxProtocol": 4, "role": "node",
                                                 "client": {"id": "node-host", "mode": "node",
                                                            "platform": "macos"},
                                                 "scopes": [], "commands": ["system.run"],
                                                 "auth": {}, "device": {"id": "dev-1"}}})]
    monkeypatch.setattr(node_broker, "signature_matches", lambda params, nonce: True)
    code = await gateway_relay._serve_node_protocol(
        _FakeSocket(received, sent), _identity(), {"runtime_host": "agentcore"})
    challenge = json.loads(sent[0])
    assert challenge["event"] == "connect.challenge"
    hello = json.loads(sent[1])
    assert hello["id"] == "c-1" and hello["payload"]["type"] == "hello-ok"
    assert gateway_relay.SESSIONS["org_1"].commands == {"system.run"}
    assert code == 1000


async def test_a_declared_command_outside_the_paired_record_is_dropped_from_the_allowlist(monkeypatch):
    async def _paired(owner, device):
        return {"system.run"}
    monkeypatch.setattr(gateway_relay.desktop_pair, "declared_commands_for", _paired)
    monkeypatch.setattr(node_broker, "signature_matches", lambda params, nonce: True)
    received = [json.dumps({"type": "req", "id": "c-2", "method": "connect",
                            "params": {"minProtocol": 4, "maxProtocol": 4, "role": "node",
                                       "client": {"id": "node-host", "mode": "node", "platform": "macos"},
                                       "scopes": [], "commands": ["system.run", "camera.capture"],
                                       "auth": {}, "device": {"id": "dev-1"}}})]
    await gateway_relay._serve_node_protocol(_FakeSocket(received, []), _identity(),
                                             {"runtime_host": "agentcore"})
    # The declared set is intersected with the paired record: a client that declares more
    # than the owner approved gets the intersection, never the union.
    assert gateway_relay.SESSIONS["org_1"].commands == {"system.run"}


async def test_a_missing_connect_inside_the_deadline_closes_1008(monkeypatch):
    monkeypatch.setattr(node_broker, "CHALLENGE_DEADLINE_S", 0.01)
    assert await gateway_relay._serve_node_protocol(
        _FakeSocket([], []), _identity(), {"runtime_host": "agentcore"}) == 1008
```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_relay_hooks_agentcore.py tests/unit/routers/test_gateway_relay_agentcore.py -q`
  Expected: FAIL with `AttributeError: module 'routers.gateway_relay' has no attribute '_serve_node_protocol'`
- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/relay_hooks.py  (change)
def _is_agentcore(owner_id: str) -> bool:
    container = container_repo.get_by_owner_id_sync(owner_id) or {}
    return runtime_transport.host_for(container) == "agentcore"


async def on_node_ready(owner_id: str, user_id: str, device_id: str) -> None:
    # Both of these are OpenClaw-shaped: an RPC chain into a container gateway that does not
    # exist, and a deny-list patch on an openclaw.json that does not exist. The broker's
    # `commands` allowlist replaces the deny flip (spec §4.4).
    if not _is_agentcore(owner_id):
        await approve_pending_node_surface(owner_id=owner_id, device_id=device_id)
        if await ConnectionService().count_relay_nodes(owner_id) == 1:
            await patch_openclaw_config(owner_id, {"tools": {"deny": list(TOOL_DENY_BASE)}})
    await publish_node_invalidation(owner_id, user_id)
```
  (same guard around `on_node_closed`'s `patch_openclaw_config`.)
```python
# apps/backend/routers/gateway_relay.py  (change, replacing the ws_connect at :135-137)
SESSIONS: dict[str, node_broker.NodeSession] = {}
...
    if kind == "node" and runtime_transport.host_for(container) == "agentcore":
        # No second socket: the BACKEND is the gateway for this workspace.
        close_code = await _serve_node_protocol(websocket, identity, container)
    else:
        async with ws_connect(f"ws://{ip}:{GATEWAY_PORT}", max_size=_WS_MAX_FRAME_SIZE,
                              open_timeout=10, close_timeout=5) as upstream:
            ...  # unchanged
```
```python
async def _serve_node_protocol(websocket, identity, container) -> int:
    """Server-first: challenge, connect, hello-ok, then ticks for the socket's life."""
    await websocket.accept()
    challenge = node_broker.challenge_frame()
    await websocket.send_text(json.dumps(challenge))
    try:
        frame = await asyncio.wait_for(websocket.receive_text(), timeout=node_broker.CHALLENGE_DEADLINE_S)
    except (asyncio.TimeoutError, WebSocketDisconnect):
        return 1008
    body = _parse(frame) or {}
    if body.get("method") != "connect" or body.get("type") != "req":
        return 1008
    params = body.get("params") or {}
    if not node_broker.signature_matches(params, challenge["payload"]["nonce"]):
        # The GATE is machine_auth at the upgrade; this only catches a client that does not
        # hold the key it claims, which is otherwise a silent mismatch.
        logger.warning("relay node connect signature mismatch owner=%s", identity.owner_id)
        return 1008
    allowed = set(params.get("commands") or []) & await desktop_pair.declared_commands_for(
        identity.owner_id, identity.device_id or "")
    session = node_broker.NodeSession(identity.device_id or "", allowed)
    SESSIONS[identity.owner_id] = session
    await websocket.send_text(json.dumps(node_broker.hello_ok(str(body.get("id")), str(uuid.uuid4()))))
    await relay_hooks.on_node_ready(identity.owner_id, identity.user_id, identity.device_id or "")
    try:
        await _pump(websocket, session)
        return 1000
    finally:
        SESSIONS.pop(identity.owner_id, None)
        await relay_hooks.on_node_closed(identity.owner_id, identity.user_id)


async def _pump(websocket, session) -> None:
    """Three legs: read+ack, drain outbound invokes, and tick. The tick is not optional —
    the CLI closes 4000 at twice the interval we promised in hello-ok."""
    async def reader() -> None:
        while True:
            answer = session.resolve(_parse(await websocket.receive_text()) or {})
            if answer is not None:
                await websocket.send_text(json.dumps(answer))

    async def writer() -> None:
        while True:
            await websocket.send_text(json.dumps(await session.send.get()))

    async def ticker() -> None:
        while True:
            await asyncio.sleep(node_broker.TICK_INTERVAL_MS / 1000)
            await websocket.send_text(json.dumps(node_broker.tick_frame()))

    legs = {asyncio.create_task(leg()) for leg in (reader, writer, ticker)}
    _done, pending = await asyncio.wait(legs, return_when=asyncio.FIRST_COMPLETED)
    for task in pending:
        task.cancel()
    await asyncio.gather(*legs, return_exceptions=True)
```
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_relay_hooks_agentcore.py tests/unit/routers/test_gateway_relay_agentcore.py -q`
  Expected: PASS — `5 passed`
- [ ] **Step 5: Commit**
```bash
git add apps/backend && git commit -m "feat(node): the relay terminates the node protocol for AgentCore workspaces; relay_hooks no-op there"
```

---

### Task 27: The presence row carries the accepting task's address (+ the one SG rule that makes forwarding possible)

Implements spec §4.4's cross-instance decision: "The presence row in `ws-connections` (written by the accepting task, refreshed every 60 s) gains the task's address, and the internal route forwards to it." The claim-item alternative is dropped.

**One blocker the report did not name:** backend tasks cannot reach each other today. `serviceSg` allows :8000 **only** from `albSecurityGroup` (`apps/infra/lib/stacks/service-stack.ts:305-310`). Forwarding needs one self-referencing ingress rule. That is the whole infra change.

The address comes from the task's own private IP. Fargate `awsvpc` gives each task one ENI, so `socket.gethostbyname(socket.gethostname())` is it — no metadata call, no new IAM.

**Files:**
- Modify: `apps/backend/core/services/connection_service.py:333-374` (`put_relay_presence` writes `taskAddress`; `refresh_relay_presence` re-asserts it; `get_connection` projects it)
- Create: `apps/backend/core/services/task_address.py`
- Modify: `apps/infra/lib/stacks/service-stack.ts:305-310` (self-ingress)
- Test: `apps/backend/tests/unit/services/test_connection_service_task_address.py`, `apps/infra/test/service-stack.test.ts` (extend)

**Interfaces:**
- Produces: `task_address.own_address() -> str` == `f"http://{private_ip}:8000"`, memoized.
- Produces: `ConnectionService.find_node_presence(owner_id) -> dict | None` → `{"connectionId", "taskAddress", "deviceId"}` for the owner's live `node` row.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/services/test_connection_service_task_address.py
from core.services.connection_service import ConnectionService
from core.services import task_address


def test_the_presence_row_records_the_accepting_tasks_address(monkeypatch):
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    captured = {}
    service = ConnectionService(table_name="t")
    monkeypatch.setattr(service._client, "put_item", lambda **kw: captured.update(kw))
    identity = type("I", (), {"user_id": "u", "org_id": "o", "owner_id": "org_1", "kind": "node",
                              "auth_kind": "device", "client_id": "node-host", "device_id": "dev-1"})()
    import asyncio; asyncio.run(service.put_relay_presence(connection_id="relay#1", identity=identity))
    assert captured["Item"]["taskAddress"] == {"S": "http://10.0.1.7:8000"}


def test_the_refresh_re_asserts_the_address_so_a_row_never_goes_addressless(monkeypatch):
    monkeypatch.setattr(task_address, "own_address", lambda: "http://10.0.1.7:8000")
    captured = {}
    service = ConnectionService(table_name="t")
    monkeypatch.setattr(service._client, "update_item", lambda **kw: captured.update(kw))
    import asyncio; asyncio.run(service.refresh_relay_presence("relay#1"))
    assert ":addr" in captured["ExpressionAttributeValues"]
    assert "taskAddress" in captured["UpdateExpression"]


def test_own_address_is_the_tasks_own_eni_ip(monkeypatch):
    task_address.own_address.cache_clear()
    monkeypatch.setattr(task_address.socket, "gethostbyname", lambda host: "10.0.2.9")
    assert task_address.own_address() == "http://10.0.2.9:8000"
```
```typescript
// apps/infra/test/service-stack.test.ts  (add)
it("lets backend tasks reach each other on 8000 so a node invoke can be forwarded", () => {
  // The socket is pinned to the task that accepted it; the bridge's call lands anywhere.
  template.hasResourceProperties("AWS::EC2::SecurityGroupIngress", {
    FromPort: 8000,
    ToPort: 8000,
    Description: "Peer backend task forwarding (node invoke)",
  });
});
```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_connection_service_task_address.py -q && cd ../infra && npx vitest run test/service-stack.test.ts`
  Expected: FAIL with `ModuleNotFoundError: No module named 'core.services.task_address'`, then the vitest case failing on no matching `SecurityGroupIngress`
- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/task_address.py
"""This task's own reachable address.

The desktop's WebSocket is pinned to whichever backend task accepted the upgrade, while the
bridge's node-invoke call can land on any of the 2-4. The presence row therefore records the
accepting task's address and the internal route forwards to it (spec §4.4).

Fargate awsvpc gives each task exactly one ENI, so the container's own hostname resolves to
the address peers reach it on — no metadata call, no extra IAM.
"""

import functools
import os
import socket


@functools.lru_cache(maxsize=1)
def own_address() -> str:
    return f"http://{socket.gethostbyname(socket.gethostname())}:{os.environ.get('PORT', '8000')}"
```
```python
# apps/backend/core/services/connection_service.py  (change)
            "ttl": {"N": str(now + self.RELAY_TTL_SECONDS)},
            "taskAddress": {"S": task_address.own_address()},
...
                UpdateExpression="SET #ttl = :ttl, taskAddress = :addr",
                ExpressionAttributeValues={":ttl": {"N": str(int(time.time()) + self.RELAY_TTL_SECONDS)},
                                           ":addr": {"S": task_address.own_address()}},
```
```typescript
// apps/infra/lib/stacks/service-stack.ts  (add after the ALB ingress rule)
// A desktop node's WebSocket is pinned to the task that accepted it, but the owner's Hermes
// runtime calls the node-invoke route through the ALB and lands on any task. That task
// forwards to the socket's owner using the address on the presence row — which needs the
// service to be able to reach itself.
serviceSg.addIngressRule(serviceSg, ec2.Port.tcp(8000), "Peer backend task forwarding (node invoke)");
```
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/services/test_connection_service_task_address.py -q && cd ../infra && npx cdk synth >/dev/null && npx vitest run test/service-stack.test.ts`
  Expected: PASS — `3 passed`, then vitest green. **`deploy.yml` is generated — commit the regenerated file from `npx cdk synth` in the same PR** (`infra-ci.yml` enforces this).
- [ ] **Step 5: Commit**
```bash
git add apps/backend apps/infra .github/workflows/deploy.yml && git commit -m "feat(node): presence rows carry the accepting task's address; allow peer task forwarding on 8000"
```

---

### Task 28: `POST /api/v1/internal/node/invoke/{owner_id}` — the bridge's one call, with cross-instance forwarding

Implements spec §4.4 ("`POST /api/v1/internal/node/invoke/{ownerId} {command, paramsJSON, timeoutMs}` … The backend forwards it as `node.invoke.request` on the held socket and returns the `node.invoke.result` payload"). Per the coordinator's cut it lives in `apps/backend/routers/messaging.py` beside the other hook-token internal routes (`_require_hook_token` at `:218-228`), not a new router module.

Forwarding is one hop and **never two**: the peer call carries `X-Lightbulb-Node-Forwarded: 1`, and a task that receives it with no local session returns 409 rather than bouncing it onward.

**Files:**
- Modify: `apps/backend/routers/messaging.py` (route + `NodeInvokeRequest`/`NodeInvokeResponse` schemas next to `RunEventRequest`)
- Modify: `apps/backend/core/services/messaging.py` (`invoke_node(owner_id, body, forwarded: bool) -> dict`)
- Test: `apps/backend/tests/unit/routers/test_node_invoke_route.py`

**Interfaces:**
- Consumes: `{command: str, paramsJSON: str, timeoutMs: int}` + `Authorization: Bearer <hook token>`.
- Produces: `200 {"ok": true, "payload": {...}}` / `200 {"ok": false, "error": {...}}` from the node; `404 {"detail": "no node attached"}`; `409` on a forwarded call with no local session; `504` on `NodeInvokeTimeout`; `403` when the command is not in the device's declared set.

- [ ] **Step 1: Write the failing test**
```python
# apps/backend/tests/unit/routers/test_node_invoke_route.py
import json, pytest
from core.services import node_broker
from routers import gateway_relay
from core.services import messaging as messaging_service

BODY = {"command": "system.run", "paramsJSON": json.dumps({"command": ["bash", "-lc", "ls"]}),
        "timeoutMs": 120_000}


async def test_a_local_session_answers_without_any_forward(monkeypatch):
    session = node_broker.NodeSession("dev-1", {"system.run"})
    gateway_relay.SESSIONS["org_1"] = session
    monkeypatch.setattr(session, "invoke", _resolved({"ok": True, "payload": {"stdout": "README.md"}}))
    forwarded = []
    monkeypatch.setattr(messaging_service, "_forward_node_invoke", lambda *a: forwarded.append(a))
    assert await messaging_service.invoke_node("org_1", BODY, forwarded=False) == {
        "ok": True, "payload": {"stdout": "README.md"}}
    assert forwarded == []


async def test_no_local_session_forwards_to_the_address_on_the_presence_row(monkeypatch):
    gateway_relay.SESSIONS.pop("org_1", None)
    monkeypatch.setattr(messaging_service, "_node_presence",
                        _resolved({"taskAddress": "http://10.0.1.7:8000", "connectionId": "relay#1"}))
    seen = {}
    monkeypatch.setattr(messaging_service, "_forward_node_invoke",
                        lambda address, owner, body: seen.update(address=address) or {"ok": True})
    assert (await messaging_service.invoke_node("org_1", BODY, forwarded=False))["ok"] is True
    assert seen["address"] == "http://10.0.1.7:8000"


async def test_a_forwarded_call_with_no_local_session_409s_instead_of_bouncing(monkeypatch):
    gateway_relay.SESSIONS.pop("org_1", None)
    with pytest.raises(messaging_service.ApiProblem) as exc:
        await messaging_service.invoke_node("org_1", BODY, forwarded=True)
    assert exc.value.status_code == 409


async def test_no_node_anywhere_is_404_and_an_undeclared_command_is_403(monkeypatch):
    gateway_relay.SESSIONS.pop("org_1", None)
    monkeypatch.setattr(messaging_service, "_node_presence", _resolved(None))
    with pytest.raises(messaging_service.ApiProblem) as exc:
        await messaging_service.invoke_node("org_1", BODY, forwarded=False)
    assert exc.value.status_code == 404
    gateway_relay.SESSIONS["org_1"] = node_broker.NodeSession("dev-1", {"system.which"})
    with pytest.raises(messaging_service.ApiProblem) as exc:
        await messaging_service.invoke_node("org_1", BODY, forwarded=False)
    assert exc.value.status_code == 403


async def test_a_node_timeout_becomes_a_504(monkeypatch):
    session = node_broker.NodeSession("dev-1", {"system.run"})
    gateway_relay.SESSIONS["org_1"] = session
    monkeypatch.setattr(session, "invoke", _raises(node_broker.NodeInvokeTimeout("system.run")))
    with pytest.raises(messaging_service.ApiProblem) as exc:
        await messaging_service.invoke_node("org_1", BODY, forwarded=False)
    assert exc.value.status_code == 504
```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run pytest tests/unit/routers/test_node_invoke_route.py -q`
  Expected: FAIL with `AttributeError: module 'core.services.messaging' has no attribute 'invoke_node'`
- [ ] **Step 3: Write minimal implementation**
```python
# apps/backend/core/services/messaging.py  (add)
NODE_FORWARD_HEADER = "X-Lightbulb-Node-Forwarded"
_NODE_FORWARD_TIMEOUT_S = 10.0


async def invoke_node(owner_id: str, body: dict, *, forwarded: bool) -> dict:
    """Run one node command for the owner's Hermes runtime.

    The desktop's socket lives on exactly one backend task. If it is this one, run it; if it
    is another, forward once to the address the presence row recorded. A forwarded call NEVER
    forwards again — one hop, or a 409 that the caller can retry.
    """
    from routers.gateway_relay import SESSIONS

    session = SESSIONS.get(owner_id)
    if session is None:
        if forwarded:
            raise ApiProblem("NODE_MOVED", "The node socket is no longer on this instance.",
                             status_code=409, retryable=True)
        presence = await _node_presence(owner_id)
        if not presence or not presence.get("taskAddress"):
            raise ApiProblem("NODE_NOT_ATTACHED", "No desktop node is attached.", status_code=404)
        return await _forward_node_invoke(presence["taskAddress"], owner_id, body)
    try:
        params = json.loads(body.get("paramsJSON") or "{}")
    except ValueError:
        raise ApiProblem("INVALID_REQUEST", "paramsJSON is not JSON.", status_code=400) from None
    try:
        return await session.invoke(str(body.get("command") or ""), params,
                                    int(body.get("timeoutMs") or 120_000))
    except node_broker.CommandNotAllowed:
        raise ApiProblem("COMMAND_NOT_ALLOWED", "This device did not declare that command.",
                         status_code=403) from None
    except node_broker.NodeInvokeTimeout:
        raise ApiProblem("NODE_TIMEOUT", "The node did not answer in time.",
                         status_code=504, retryable=True) from None


async def _forward_node_invoke(address: str, owner_id: str, body: dict) -> dict:
    container = await container_repo.get_by_owner_id(owner_id, consistent=True) or {}
    async with httpx.AsyncClient(timeout=_NODE_FORWARD_TIMEOUT_S) as client:
        response = await client.post(
            f"{address}/api/v1/internal/node/invoke/{owner_id}", json=body,
            headers={"Authorization": f"Bearer {container.get('hook_token') or ''}",
                     NODE_FORWARD_HEADER: "1"})
    response.raise_for_status()
    return response.json()
```
```python
# apps/backend/routers/messaging.py  (add, beside the other internal hook-token routes)
class NodeInvokeRequest(BaseModel):
    command: str = Field(min_length=1, max_length=128)
    params_json: str = Field(alias="paramsJSON", max_length=1_048_576)
    timeout_ms: int = Field(alias="timeoutMs", ge=1_000, le=600_000, default=120_000)


@router.post("/internal/node/invoke/{owner_id}")
async def invoke_node(owner_id: str, body: NodeInvokeRequest, request: Request):
    await _require_hook_token(owner_id, request)
    return await service.invoke_node(
        owner_id, body.model_dump(by_alias=True),
        forwarded=request.headers.get(service.NODE_FORWARD_HEADER) == "1")
```
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/backend && uv run pytest tests/unit/routers/test_node_invoke_route.py -q`
  Expected: PASS — `5 passed`
- [ ] **Step 5: Commit**
```bash
git add apps/backend && git commit -m "feat(node): internal node-invoke route with single-hop cross-instance forwarding"
```

---

### Task 29: The `lightbulb_node` Hermes plugin — terminal environment provider

Implements spec §4.4's bridge half. The sanctioned slot is `register_terminal_environment_provider` (`/opt/hermes/hermes_cli/plugins.py:1036-1043`); built-in names are rejected and `lightbulb_node` is free. Per the coordinator's cut this is **one** plugin — M3 adds the `nodes` device tool to this same directory.

Five facts that shape it (report 02 §3.2, verified in the image):
- The plugin object is a `TerminalEnvironmentProvider` with `is_available()` and `create_environment(...)`; the **returned** object is duck-typed — just `execute()` and `cleanup()`. No `BaseEnvironment` subclass: there is no local process to spawn.
- `execute()` returns `{"output": str, "returncode": int | None}`. **`returncode`, not `exit_code`** — the public docs are wrong. 124 = timeout with the `"[Command timed out after {n}s]"` suffix; 130 = interrupted.
- `is_remote=True` (suppresses host-OS/home/cwd hints and the host Python probe); `is_container=False`, so dangerous-command approval prompts stay ON — correct, because the Mac's exec approvals are the **user's**, not ours.
- No stdin: the node host hardcodes `input: Buffer.alloc(0)`, which is exactly what `_stdin_mode = "heredoc"` exists for.
- Resolution is ContextVar-scoped (`/opt/hermes/tools/terminal_scope.py`), so six agents in one microVM can each select a different backend concurrently.

`strip_env_keys` removes the gate bearer and the Paperclip key from every subprocess the agent spawns — the replacement for the `paperclip-exec-creds` plugin.

**Files:**
- Create: `apps/infra/hermes/plugins/lightbulb_node/plugin.yaml`
- Create: `apps/infra/hermes/plugins/lightbulb_node/__init__.py`
- Modify: `apps/infra/hermes/Dockerfile.spike` (`COPY plugins/lightbulb_node /opt/hermes/plugins/lightbulb_node`)
- Test: `apps/infra/hermes/tests/test_plugin_node.py`

**Interfaces:**
- Produces: `LightbulbNodeProvider` (`name="lightbulb_node"`, `is_remote=True`, `is_container=False`, `cache_path_base=None`, `session_isolated_when_nonpersistent=False`, `strip_env_keys={"AWS_BEARER_TOKEN_BEDROCK","PAPERCLIP_API_KEY"}`); `create_environment(cwd, timeout, task_id="default", **kwargs) -> NodeEnvironment`.
- Consumes: `POST {apiUrl}/api/v1/internal/node/invoke/{ownerId}` `{command:"system.run", paramsJSON: json({command:["bash","-lc",cmd], cwd, timeoutMs}), timeoutMs}`; bearer = the hook token from the process env (`OPENCLAW_HOOK_TOKEN`), not from a turn-context file — exec is not turn-scoped.

- [ ] **Step 1: Write the failing test**
```python
# apps/infra/hermes/tests/test_plugin_node.py
import json, sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "plugins"))
import lightbulb_node  # noqa: E402


def env(monkeypatch, result):
    monkeypatch.setattr(lightbulb_node, "_invoke", lambda command, params, timeout_ms: result)
    return lightbulb_node.LightbulbNodeProvider().create_environment(cwd="/Users/x", timeout=180)


def test_the_provider_is_remote_but_not_a_container_so_approvals_stay_on():
    provider = lightbulb_node.LightbulbNodeProvider()
    assert provider.name == "lightbulb_node"
    assert provider.is_remote is True and provider.is_container is False
    assert provider.strip_env_keys == frozenset({"AWS_BEARER_TOKEN_BEDROCK", "PAPERCLIP_API_KEY"})


def test_a_command_is_wrapped_in_bash_lc_and_sent_as_params_json(monkeypatch):
    seen = {}
    monkeypatch.setattr(lightbulb_node, "_invoke",
                        lambda command, params, timeout_ms: seen.update(
                            command=command, params=params, timeout_ms=timeout_ms) or
                        {"ok": True, "payload": {"stdout": "ok\n", "stderr": "", "exitCode": 0,
                                                 "timedOut": False, "success": True}})
    out = lightbulb_node.LightbulbNodeProvider().create_environment(
        cwd="/Users/x", timeout=180).execute("echo ok", timeout=30)
    assert seen["command"] == "system.run"
    assert seen["params"]["command"] == ["bash", "-lc", "echo ok"]
    assert seen["params"]["cwd"] == "/Users/x" and seen["params"]["timeoutMs"] == 30_000
    assert out == {"output": "ok\n", "returncode": 0}


def test_stdout_and_stderr_are_concatenated_and_a_nonzero_exit_is_a_normal_result(monkeypatch):
    out = env(monkeypatch, {"ok": True, "payload": {"stdout": "a", "stderr": "b", "exitCode": 2,
                                                    "timedOut": False, "success": False}}).execute("x")
    assert out == {"output": "ab", "returncode": 2}


def test_a_timeout_uses_hermes_sentinel_124_with_the_expected_suffix(monkeypatch):
    out = env(monkeypatch, {"ok": True, "payload": {"stdout": "partial", "stderr": "",
                                                    "timedOut": True}}).execute("sleep 99", timeout=5)
    assert out["returncode"] == 124 and out["output"].endswith("[Command timed out after 5s]")


def test_a_spawn_failure_and_a_transport_failure_both_return_returncode_none(monkeypatch):
    out = env(monkeypatch, {"ok": True, "payload": {"error": "spawn ENOENT", "stdout": "", "stderr": ""}}).execute("x")
    assert out["returncode"] is None and "spawn ENOENT" in out["output"]
    out = env(monkeypatch, {"ok": False, "error": {"code": "UNAVAILABLE", "message": "no node"}}).execute("x")
    assert out["returncode"] is None and "UNAVAILABLE" in out["output"]


def test_cleanup_never_touches_the_users_mac(monkeypatch):
    assert env(monkeypatch, {"ok": True, "payload": {}}).cleanup() is None
```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore python -m pytest tests/test_plugin_node.py -q`
  Expected: FAIL with `ModuleNotFoundError: No module named 'lightbulb_node'`
- [ ] **Step 3: Write minimal implementation**
```yaml
# apps/infra/hermes/plugins/lightbulb_node/plugin.yaml
name: lightbulb_node
version: 1.0.0
description: "Run terminal commands on the owner's paired Mac through the Lightbulb node route."
author: "Lightbulb"
kind: backend
```
```python
# apps/infra/hermes/plugins/lightbulb_node/__init__.py
"""`lightbulb_node`: Hermes's terminal tool, executed on the owner's paired Mac.

The bridge never opens a WebSocket (spec §4.4). One HTTP call per command on the hook-token
rail the run callbacks already use; the BACKEND holds the desktop socket and speaks the node
protocol. M3 adds the device `nodes` tool to this same plugin.
"""
from __future__ import annotations

import json
import os
import urllib.request

from agent.terminal_env_provider import TerminalEnvironmentProvider

DEFAULT_TIMEOUT_S = 180
_TIMEOUT_RETURNCODE = 124   # Hermes's own sentinel (tools/environments/base.py:401-405)


def _invoke(command: str, params: dict, timeout_ms: int) -> dict:
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
        # No stdin: the node host hardcodes an empty input buffer, which is what Hermes's
        # heredoc stdin mode exists for.
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


class LightbulbNodeProvider(TerminalEnvironmentProvider):
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
                           image=None, container_config=None, **kwargs):
        return NodeEnvironment(cwd, timeout)


def register(ctx) -> None:
    ctx.register_terminal_environment_provider(LightbulbNodeProvider())
```
  Selection is per profile: `terminal.backend: lightbulb_node` in the agent's `config.yaml`, which Task 17's `config.patch` can already write.
- [ ] **Step 4: Run test to verify it passes**
  Run: `cd apps/infra/hermes && uv run --no-project --with pytest --with botocore python -m pytest tests -q`
  Expected: PASS — `91 passed`
- [ ] **Step 5: Commit**
```bash
git add apps/infra/hermes && git commit -m "feat(node): lightbulb_node terminal environment provider running commands on the paired Mac"
```

---

### Task 30: Loopback proof against the **vendored upstream node host**, then the dev proof

Implements spec §6 M2's pass criteria and report 02 R6 — "a bridge tested only against the Tauri path will pass and then fail on every Mac". The CLI node-host is the real upstream client: it has a tick watchdog, protocol renegotiation, a device-token store and a 15 s post-connect deadline that our Rust client has none of. Ten minutes with no reconnect is what proves the ticks.

**Files:**
- Create: `apps/backend/tests/manual/node_host_loopback.md` (the runbook — the only file this task writes)
- Test: manual; the assertions are the checklist below.

**Interfaces:**
- Consumes: the vendored sidecar at `apps/desktop/src-tauri/bin/openclaw-host` (populated by `scripts/vendor-sidecars.sh:141-142`, not checked in) and the loopback WSS proxy shape `apps/desktop/src-tauri/src/proxy.rs:165-171`.

- [ ] **Step 1: Write the failing test** — the runbook, whose checklist starts unchecked:
```markdown
## Node-host loopback proof (M2 hard gate)

Run the REAL upstream client against a local backend. `apps/desktop/src-tauri/src/proxy.rs`
already does this shape in production; here it points at localhost.

1. Vendor the sidecar if `apps/desktop/src-tauri/bin/openclaw-host` is absent:
   `apps/desktop/src-tauri/scripts/vendor-sidecars.sh`
2. Start the backend with an AgentCore-marked container row for your dev org:
   `cd apps/backend && uv run uvicorn main:app --reload --port 8000`
   `aws dynamodb update-item --table-name lightbulb-dev-containers --profile isol8-admin \
      --key '{"owner_id":{"S":"<org>"}}' \
      --update-expression 'SET runtime_host = :h' \
      --expression-attribute-values '{":h":{"S":"agentcore"}}'`
3. Mint a device token the way the desktop does (`POST /api/v1/desktop/pair`), then run the
   sidecar against the local relay:
   `OPENCLAW_STATE_DIR=/tmp/node-loopback \
    OPENCLAW_GATEWAY_URL='ws://127.0.0.1:8000/api/v1/gateway/ws?token=<device-jwt>&clientId=node-host' \
    apps/desktop/src-tauri/bin/openclaw-host node run`
4. Drive one command through the bridge's own rail:
   `curl -sX POST http://127.0.0.1:8000/api/v1/internal/node/invoke/<org> \
     -H "Authorization: Bearer <hook-token>" -H 'Content-Type: application/json' \
     -d '{"command":"system.run","paramsJSON":"{\"command\":[\"bash\",\"-lc\",\"uname -a\"]}","timeoutMs":30000}'`

## Checklist (all must pass)
- [ ] a clean `hello-ok` — the sidecar logs no `1008`, no `1012`, no `4000`
- [ ] the `system.run` round trip returns this Mac's `uname -a` with `exitCode: 0`
- [ ] **ten minutes with no reconnect** (prove the ticks): `grep -c 'connect' /tmp/node-loopback/*.log` stays at 1
- [ ] a 2 s timeout on `sleep 30` returns `returncode: 124` with the timeout suffix
- [ ] an exec-approval prompt on the Mac is honoured (run something the Mac's policy asks about; a denial comes back as `SYSTEM_RUN_DENIED`, not a hang)
- [ ] `camera.capture` (undeclared by `node-host`) is refused **403** before it reaches the socket
- [ ] cross-instance: start a second backend on :8001, point the curl at it, and the answer still comes back (forwarded once, `X-Lightbulb-Node-Forwarded: 1` visible in the first instance's log)

## Dev proof (after the loopback passes)
- [ ] the real desktop app, paired to the dev AgentCore workspace, answers a `terminal` tool call inside a `#general` turn
- [ ] `aws logs tail /aws/bedrock-agentcore/<runtime> --profile isol8-admin` shows one `internal/node/invoke` per command and no WebSocket attempt from the microVM
```
- [ ] **Step 2: Run test to verify it fails**
  Run: `cd apps/backend && uv run uvicorn main:app --port 8000` then step 3's sidecar command
  Expected: FAIL — before Tasks 26-29 land, the sidecar logs `1008 connect challenge timeout` (the relay never speaks first) and the curl 404s
- [ ] **Step 3: Write minimal implementation** — no product code; this task is the proof. Any failure sends you back to the task that owns it: a `1008` → Task 26's challenge/deadline; a `4000` → Task 26's ticker; an `INVALID_REQUEST: paramsJSON required` → Task 25; a 409 loop → Task 28's single-hop rule.
- [ ] **Step 4: Run test to verify it passes**
  Run: the four numbered steps above, in order
  Expected: PASS — every checklist box ticked, with the ten-minute no-reconnect window observed live
- [ ] **Step 5: Commit**
```bash
git add apps/backend/tests/manual/node_host_loopback.md && git commit -m "test(node): loopback runbook proving the backend's node protocol against the vendored upstream host"
```

---

