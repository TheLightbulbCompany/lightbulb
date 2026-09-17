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
            _ascii_lower(str(device.get("deviceFamily") or "")),
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
