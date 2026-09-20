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
NODE_METHODS = ("node.invoke.result", "node.invoke.progress", "node.event")
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
        self.send: asyncio.Queue[dict] = asyncio.Queue(maxsize=16)
        self.generation = ""

    async def invoke(
        self, command: str, params: dict, timeout_ms: int, *, invoke_id: str | None = None, result_grace_ms: int = 0
    ) -> dict:
        if command not in self.commands:
            raise CommandNotAllowed(command)
        if len(self.pending) >= 8 or self.send.full():
            raise NodeCapacity()
        invoke_id = invoke_id or str(uuid.uuid4())
        if invoke_id in self.pending:
            raise NodeCapacity()
        frame = invoke_request(invoke_id, self.node_id, command, params, timeout_ms)
        if len(frame["payload"]["paramsJSON"].encode()) > 16_384:
            raise NodeCapacity()
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending[invoke_id] = future
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
        for future in self.pending.values():
            if not future.done():
                future.set_exception(exc)
        self.pending.clear()
        while not self.send.empty():
            self.send.get_nowait()

    def resolve(self, frame: dict) -> dict | None:
        """Answer a node->gateway req. Returns the ack to send, or None for a non-req frame."""
        if frame.get("type") != "req":
            return None
        params = frame.get("params") or {}
        if frame.get("method") == "node.invoke.result":
            future = self.pending.get(str(params.get("id") or ""))
            if future is not None and not future.done():
                # Bound every retained field together, even when payloadJSON and
                # payload/error coexist. Check before nested decoding/resolution.
                size = len(json.dumps(params).encode())
                if size > 1024 * 1024:
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
