"""The node protocol, terminated in the backend for an AgentCore workspace.

The desktop keeps its socket to the bv- relay; the bridge never opens one (spec §4.4). This
module is the frame layer: pure functions plus the in-process invoke map. Build against the
CLI node-host's contract, which is a strict superset of our Rust client's.
"""

from __future__ import annotations

import asyncio
import base64
import json
import secrets
import time
import uuid

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

PROTOCOL_VERSION = 4
SERVER_VERSION = "lightbulb-bridge/1"
TICK_INTERVAL_MS = 15_000  # we promise it in hello-ok; the CLI closes 4000 at 2x
MAX_PAYLOAD = 26_214_400  # 25 MiB — the real gateway's value; screen snapshots need it
MAX_BUFFERED_BYTES = 52_428_800
CHALLENGE_DEADLINE_S = 10.0  # the CLI's own watchdog is ~15 s; leave headroom
NODE_METHODS = ("node.invoke.result", "node.invoke.progress", "node.event", "node.pluginTools.update")
NODE_EVENTS = ("node.invoke.request", "node.invoke.cancel", "node.invoke.input", "tick")


def _b64u_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def challenge_frame() -> dict:
    """Sent unprompted the moment the socket opens — the gateway is server-first and neither
    client speaks until it lands. `ts` must be a non-negative integer for WS challenges."""
    return {
        "type": "event",
        "event": "connect.challenge",
        "payload": {"nonce": secrets.token_urlsafe(24), "ts": int(time.time() * 1000)},
    }


def tick_frame() -> dict:
    return {"type": "event", "event": "tick", "payload": {"ts": int(time.time() * 1000)}}


def hello_ok(connect_req_id: str, conn_id: str, *, tick_interval_ms: int = TICK_INTERVAL_MS) -> dict:
    """Every field is REQUIRED by HelloOkSchema; `health: {}` is legal for a hello snapshot.
    auth.deviceToken is deliberately absent — see the test."""
    return {
        "type": "res",
        "id": connect_req_id,
        "ok": True,
        "payload": {
            "type": "hello-ok",
            "protocol": PROTOCOL_VERSION,
            "server": {"version": SERVER_VERSION, "connId": conn_id},
            "features": {"methods": list(NODE_METHODS), "events": list(NODE_EVENTS)},
            "snapshot": {"presence": [], "health": {}, "stateVersion": {"presence": 0, "health": 0}, "uptimeMs": 0},
            "auth": {"role": "node", "scopes": []},
            "policy": {
                "maxPayload": MAX_PAYLOAD,
                "maxBufferedBytes": MAX_BUFFERED_BYTES,
                "tickIntervalMs": tick_interval_ms,
            },
        },
    }


def _ascii_lower(value: str) -> str:
    # trim, then ASCII-ONLY lowercase — non-ASCII codepoints are left alone (node_client.rs:645-664)
    return "".join(c.lower() if "A" <= c <= "Z" else c for c in (value or "").strip())


def device_payload_v3(params: dict, nonce: str) -> str:
    client, device = params.get("client") or {}, params.get("device") or {}
    return "|".join(
        (
            "v3",
            str(device.get("id") or ""),
            str(client.get("id") or ""),
            str(client.get("mode") or ""),
            str(params.get("role") or ""),
            ",".join(params.get("scopes") or []),
            str(device.get("signedAt") or ""),
            str((params.get("auth") or {}).get("token") or ""),
            nonce,
            _ascii_lower(str(client.get("platform") or "")),
            # The CLI node host signs client.deviceFamily ("mac"); the Rust device client sends
            # none. Reading it off `device` failed every real CLI connect with bad_signature.
            _ascii_lower(str(client.get("deviceFamily") or device.get("deviceFamily") or "")),
        )
    )


def signature_matches(params: dict, nonce: str) -> bool:
    """Self-certifying (the key arrives in the same frame), so this proves the client holds
    the key it claims — NOT that the key is trusted. The real gate is machine_auth at the
    upgrade. Cheap, and it turns a silent mismatch into a loud one."""
    device = params.get("device") or {}
    try:
        public_key = Ed25519PublicKey.from_public_bytes(_b64u_decode(str(device.get("publicKey") or "")))
        public_key.verify(_b64u_decode(str(device.get("signature") or "")), device_payload_v3(params, nonce).encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


class CommandNotAllowed(Exception):
    """The paired device never declared this command — the allowlist is the paired record's
    own `commands`, which replaces the container's tools-deny flip (report 02 §2.2 step 4)."""


class NodeInvokeTimeout(Exception):
    pass


class NodeCapacity(Exception):
    """Admission must fail before enqueue when the socket is full."""


class NodeResultTooLarge(Exception):
    pass


class NodeDetached(Exception):
    """Raised for every invoke still in flight when the node's socket closes -- the relay
    calls ``NodeSession.fail_pending`` with this so a caller learns immediately that no
    answer is coming, instead of waiting out the full ``timeout_ms`` for one that never
    arrives. Task 28 maps this to ``NODE_NOT_ATTACHED``."""


def invoke_request(invoke_id: str, node_id: str, command: str, params: dict, timeout_ms: int) -> dict:
    return {
        "type": "event",
        "event": "node.invoke.request",
        "payload": {
            "id": invoke_id,
            "nodeId": node_id,
            "command": command,
            # A JSON STRING, not an object: an object is a silent no-op on the Rust client and an
            # INVALID_REQUEST on the CLI (report 02 R7).
            "paramsJSON": json.dumps(params or {}),
            "timeoutMs": int(timeout_ms),
        },
    }


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
        self.result_limits: dict[str, int] = {}
        self.send: asyncio.Queue[dict] = asyncio.Queue(maxsize=16)
        self.generation = ""
        self.catalog_version = str(uuid.uuid4())
        self.catalog: list[dict] = []

    def mcp_catalog(self) -> dict:
        return {
            "deviceId": self.node_id,
            "generation": self.generation,
            "catalogVersion": self.catalog_version,
            "untrusted": True,
            "tools": json.loads(json.dumps(self.catalog)) if "mcp.tools.call.v1" in self.commands else [],
        }

    def mcp_call(self, server: str, tool: str, version: str) -> None:
        if version != self.catalog_version or not any(
            t["server"] == server and t["tool"] == tool for t in self.mcp_catalog()["tools"]
        ):
            raise CommandNotAllowed("mcp_catalog_stale")

    def _catalog_update(self, params: dict) -> bool:
        # Replace, never merge: removed tools and malformed refreshes revoke old authority.
        self.catalog = []
        self.catalog_version = str(uuid.uuid4())
        try:
            if (
                not isinstance(params, dict)
                or set(params) != {"tools"}
                or len(json.dumps(params, allow_nan=False).encode()) > 256 * 1024
            ):
                return False
            descriptors = params["tools"]
            if not isinstance(descriptors, list) or len(descriptors) > 128:
                return False
            tools, seen = [], set()
            for item in descriptors:
                if not isinstance(item, dict):
                    return False
                if item.get("pluginId") != "node-mcp" or item.get("command") != "mcp.tools.call.v1":
                    continue  # Session catalog and other plugins have separate consent.
                mcp = item.get("mcp", {})
                if not isinstance(mcp, dict) or set(mcp) != {"server", "tool"}:
                    return False
                if any(
                    not isinstance(mcp[k], str)
                    or not 1 <= len(mcp[k]) <= 256
                    or mcp[k].strip() != mcp[k]
                    or any(ord(c) < 32 for c in mcp[k])
                    for k in mcp
                ):
                    return False
                key = (mcp["server"], mcp["tool"])
                if (
                    key in seen
                    or len(json.dumps(item).encode()) > 16 * 1024
                    or not isinstance(item.get("parameters"), dict)
                ):
                    return False
                description = item.get("description", "")
                if not isinstance(description, str):
                    return False
                seen.add(key)
                tools.append({**mcp, "description": description[:1024], "parameters": item["parameters"]})
            if "mcp.tools.call.v1" in self.commands:
                self.catalog = json.loads(json.dumps(tools))
            return True
        except (ValueError, TypeError, RecursionError):
            return False

    async def invoke(
        self,
        command: str,
        params: dict,
        timeout_ms: int,
        *,
        invoke_id: str | None = None,
        result_grace_ms: int = 0,
        browser_media: bool = False,
        file_authorized: bool = False,
    ) -> dict:
        if command not in self.commands:
            raise CommandNotAllowed(command)
        if command in {"file.fetch", "file.write", "dir.list", "dir.fetch"} and not file_authorized:
            raise CommandNotAllowed("file_policy_required")
        if browser_media and command != "browser.proxy":
            raise ValueError("media budget is private to browser proxy")
        if len(self.pending) >= 8 or self.send.full():
            raise NodeCapacity()
        invoke_id = invoke_id or str(uuid.uuid4())
        if invoke_id in self.pending:
            raise NodeCapacity()
        frame = invoke_request(invoke_id, self.node_id, command, params, timeout_ms)
        if len(frame["payload"]["paramsJSON"].encode()) > (
            768 * 1024
            if command in {"browser.proxy.upload.v1", "file.write"}
            else 32_768
            if command == "desktop.operation.approve.v1"
            else 16_384
        ):
            raise NodeCapacity()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[invoke_id] = future
        self.result_limits[invoke_id] = (
            6 * 1024 * 1024 if browser_media or command in {"file.fetch", "dir.fetch"} else 1024 * 1024
        )
        self.send.put_nowait(frame)
        interrupted = False
        try:
            return await asyncio.wait_for(future, timeout=(timeout_ms + result_grace_ms) / 1000)
        except asyncio.TimeoutError as exc:
            interrupted = True
            raise NodeInvokeTimeout(command) from exc
        except asyncio.CancelledError:
            interrupted = True
            raise
        finally:
            self.pending.pop(invoke_id, None)
            self.result_limits.pop(invoke_id, None)
            was_queued = self._drop_queued(invoke_id)
            if interrupted and not was_queued and not self.send.full():
                self.send.put_nowait(
                    {
                        "type": "event",
                        "event": "node.invoke.cancel",
                        "payload": {"invokeId": invoke_id, "nodeId": self.node_id},
                    }
                )

    def _drop_queued(self, invoke_id: str) -> bool:
        retained = []
        was_queued = False
        while not self.send.empty():
            frame = self.send.get_nowait()
            payload = frame.get("payload", {})
            was_queued |= payload.get("id") == invoke_id
            if payload.get("id") != invoke_id and payload.get("invokeId") != invoke_id:
                retained.append(frame)
        for frame in retained:
            self.send.put_nowait(frame)
        return was_queued

    def cancel(self, invoke_id: str) -> bool:
        """A queued request can be withdrawn; a sent request needs node confirmation.

        Do not resolve the waiter: the pinned CLI's cancel has no terminal ACK.
        """
        if invoke_id not in self.pending:
            return False
        self._drop_queued(invoke_id)
        if self.send.full():
            raise NodeCapacity()
        self.send.put_nowait(
            {"type": "event", "event": "node.invoke.cancel", "payload": {"invokeId": invoke_id, "nodeId": self.node_id}}
        )
        return True

    def fail_pending(self, exc: Exception) -> None:
        """The socket is gone -- nothing will ever answer an in-flight invoke. Fail every
        unfinished future with `exc` right now and clear the map, rather than letting each
        one sit until its own `timeout_ms` elapses."""
        self.catalog = []
        self.catalog_version = str(uuid.uuid4())
        for future in self.pending.values():
            if not future.done():
                future.set_exception(exc)
        self.pending.clear()
        self.result_limits.clear()
        while not self.send.empty():
            self.send.get_nowait()

    def resolve(self, frame: dict) -> dict | None:
        """Answer a node->gateway req. Returns the ack to send, or None for a non-req frame."""
        if frame.get("type") != "req":
            return None
        params = frame.get("params") or {}
        if frame.get("method") == "node.pluginTools.update":
            if self._catalog_update(params):
                return ack(str(frame.get("id") or ""))
            return {
                "type": "res",
                "id": str(frame.get("id") or ""),
                "ok": False,
                "error": {"code": "INVALID_REQUEST", "message": "Invalid or oversized tool catalog"},
            }
        if frame.get("method") == "node.invoke.result":
            future = self.pending.get(str(params.get("id") or ""))
            if future is not None and not future.done():
                # Bound every retained field together, even when payloadJSON and
                # payload/error coexist. Check before nested decoding/resolution.
                size = len(json.dumps(params).encode())
                if size > self.result_limits.get(str(params.get("id") or ""), 1024 * 1024):
                    future.set_exception(NodeResultTooLarge())
                    return ack(str(frame.get("id") or ""))
                payload = params.get("payload")
                if payload is None and isinstance(params.get("payloadJSON"), str):
                    try:
                        payload = json.loads(params["payloadJSON"])
                    except ValueError:
                        payload = None
                future.set_result(
                    {
                        "ok": bool(params.get("ok")),
                        "payload": payload,
                        **({"error": params["error"]} if params.get("error") else {}),
                    }
                )
        # progress / node.event / anything else: ack and drop. The desktop's declared surface
        # never streams, so there is nothing to implement here.
        return ack(str(frame.get("id") or ""))
