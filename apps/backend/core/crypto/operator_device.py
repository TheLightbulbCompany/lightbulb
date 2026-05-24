"""Operator device identity + connect-request signing for OpenClaw 4.5.

OpenClaw 4.5 replaced the flat "shared token = admin access" model with a
scoped auth system (see `src/gateway/method-scopes.ts` in the OpenClaw
reference). A client that provides only `auth.token` in its connect request
gets an empty scope set — the server clears self-declared scopes because it
can't bind them to a pre-approved identity (`message-handler.ts:438-595`).

To receive `operator.read` / `operator.write` scopes, a client must:

1. Hold an Ed25519 keypair (the "device identity").
2. Have its public key pre-registered in the container's `devices/paired.json`
   with the scopes it's allowed to claim.
3. On each connect, build the canonical v2 payload string
   (`device-auth.ts:buildDeviceAuthPayload`):

       v2|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}|{nonce}

   ...sign it with the private key, and include the signature + public key
   in the `device` field of the connect request.

Notes on the format:

- `deviceId` is the hex SHA-256 of the raw (32-byte) Ed25519 public key.
- `publicKey` is the base64url-encoded (no padding) raw public key, NOT PEM.
- `signature` is the base64url-encoded (no padding) 64-byte Ed25519 signature
  over the UTF-8 bytes of the payload string.
- `scopes` are joined by `,` (no spaces) and must exactly match what we send
  in `connectParams.scopes` — the server re-builds the payload from those
  fields and compares.
- The gateway `token` is embedded INSIDE the signed payload even though it is
  also passed as `auth.token`. That's the cryptographic binding: if either
  side of the pair (token or private key) is stolen alone, no valid signature
  can be produced.

This module deliberately has no ambient state and no I/O — all inputs and
outputs are explicit so the caller can decide how to load/store the keypair.
"""

from __future__ import annotations

import base64
import hashlib
import time
from dataclasses import dataclass
from typing import Iterable

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

# Scopes granted to the backend operator device.
#
# We need `operator.admin` because the backend proxies RPCs like
# `skills.install`, `agents.create`, `agents.update`, `agents.delete`,
# `secrets.reload`, `cron.add/update/remove`, and `sessions.patch/reset/delete`
# from the frontend to the gateway — all of which require admin scope per
# `src/gateway/method-scopes.ts` in the OpenClaw reference. The authorize
# function short-circuits on admin (`method-scopes.ts:232`), so we keep the
# read + write entries for observability ("this device was granted these
# specific scopes") but admin is what actually unblocks every method.
#
# **Ordering matters for signature verification.** OpenClaw normalizes
# device-auth scopes via `src/shared/device-auth.ts:normalizeDeviceAuthScopes`
# which calls `.toSorted()` — alphabetical order. The paired.json entry we
# write to EFS goes through that normalization path
# (`src/infra/device-pairing.ts:254`), so the persisted form is
# alphabetically sorted. When the client (us) signs the v2 connect payload,
# the server rebuilds the same payload from `connectParams.scopes` in the
# order WE sent them, and Ed25519 verifies byte-by-byte — so we must send
# the scopes in the same alphabetical order to match whatever form might
# appear anywhere in the server's auth pipeline. Hence: admin, read, write
# (alphabetical), not read-write-admin as added originally.
#
# A narrower `[read, write]` set fails the frontend's skill install
# flow ("missing scope: operator.admin") because `skills.install` is an
# admin-only method.
BACKEND_OPERATOR_SCOPES: tuple[str, ...] = (
    "operator.admin",  # skills.install, agents.create/update/delete, etc.
    "operator.read",  # health, sessions.list, status, agents.list, etc.
    "operator.write",  # chat.send, chat.abort, sessions.create/send, etc.
)

# The client.id / client.mode strings the backend identifies as. These must
# match `GATEWAY_CLIENT_IDS.GATEWAY_CLIENT` / `GATEWAY_CLIENT_MODES.BACKEND` on
# the OpenClaw side — the server uses them to classify us as a "backend" role
# for locality / trust decisions.
BACKEND_CLIENT_ID = "gateway-client"
BACKEND_CLIENT_MODE = "backend"
BACKEND_ROLE = "operator"


def _b64url_nopad(data: bytes) -> str:
    """RFC 7515 base64url, no padding — matches how the OpenClaw server encodes."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_nopad_decode(s: str) -> bytes:
    """Inverse of `_b64url_nopad`. Re-pads so stdlib will accept the input."""
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


@dataclass(frozen=True)
class OperatorDeviceIdentity:
    """A persistent operator device identity.

    `device_id` and `public_key_b64` are the PUBLIC halves and are safe to
    write to DynamoDB / EFS in plaintext. `private_key_seed` is the 32-byte
    Ed25519 seed — it MUST be encrypted at rest (we use KMS).
    """

    device_id: str  # hex SHA-256 of raw public key
    public_key_b64: str  # base64url(no pad) of raw 32-byte public key
    private_key_seed: bytes  # 32-byte Ed25519 seed — NEVER persist plaintext

    def public_key(self) -> Ed25519PublicKey:
        raw = _b64url_nopad_decode(self.public_key_b64)
        return Ed25519PublicKey.from_public_bytes(raw)

    def private_key(self) -> Ed25519PrivateKey:
        return Ed25519PrivateKey.from_private_bytes(self.private_key_seed)


def generate_operator_device() -> OperatorDeviceIdentity:
    """Create a brand-new Ed25519 keypair and derive the device_id."""
    priv = Ed25519PrivateKey.generate()
    return _identity_from_private_key(priv)


def load_operator_device_from_seed(seed_bytes: bytes) -> OperatorDeviceIdentity:
    """Reconstruct an identity from the 32-byte seed (e.g. after KMS decrypt)."""
    if len(seed_bytes) != 32:
        raise ValueError(f"Ed25519 seed must be 32 bytes, got {len(seed_bytes)}")
    priv = Ed25519PrivateKey.from_private_bytes(seed_bytes)
    return _identity_from_private_key(priv)


def _identity_from_private_key(priv: Ed25519PrivateKey) -> OperatorDeviceIdentity:
    pub_bytes = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    seed = priv.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
    device_id = hashlib.sha256(pub_bytes).hexdigest()
    return OperatorDeviceIdentity(
        device_id=device_id,
        public_key_b64=_b64url_nopad(pub_bytes),
        private_key_seed=seed,
    )


def build_v2_connect_payload(
    *,
    device_id: str,
    client_id: str,
    client_mode: str,
    role: str,
    scopes: Iterable[str],
    signed_at_ms: int,
    token: str,
    nonce: str,
) -> str:
    """Build the canonical v2 device-auth payload string.

    Mirrors `buildDeviceAuthPayload` in OpenClaw
    `src/gateway/device-auth.ts:20-34`. The server rebuilds this exact string
    from the fields in the connect request and verifies our signature against
    it, so any formatting drift here breaks the handshake.

    Format: `v2|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}|{nonce}`
    """
    scopes_joined = ",".join(scopes)
    return "|".join(
        [
            "v2",
            device_id,
            client_id,
            client_mode,
            role,
            scopes_joined,
            str(signed_at_ms),
            token or "",
            nonce,
        ]
    )


def sign_connect_request(
    *,
    identity: OperatorDeviceIdentity,
    token: str,
    nonce: str,
    scopes: Iterable[str] = BACKEND_OPERATOR_SCOPES,
    client_id: str = BACKEND_CLIENT_ID,
    client_mode: str = BACKEND_CLIENT_MODE,
    role: str = BACKEND_ROLE,
    now_ms: int | None = None,
) -> dict:
    """Produce the `device` dict that goes into the connect request params.

    Returned shape matches what `handshake-auth-helpers.ts:244-281` expects:
        {
          "id":        device_id (hex sha256 of public key),
          "publicKey": base64url(raw public key),
          "signature": base64url(ed25519 signature over v2 payload),
          "signedAt":  epoch milliseconds,
          "nonce":     the nonce we received in connect.challenge,
        }
    """
    scopes_tuple = tuple(scopes)
    signed_at_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    payload = build_v2_connect_payload(
        device_id=identity.device_id,
        client_id=client_id,
        client_mode=client_mode,
        role=role,
        scopes=scopes_tuple,
        signed_at_ms=signed_at_ms,
        token=token,
        nonce=nonce,
    )
    signature_bytes = identity.private_key().sign(payload.encode("utf-8"))
    return {
        "id": identity.device_id,
        "publicKey": identity.public_key_b64,
        "signature": _b64url_nopad(signature_bytes),
        "signedAt": signed_at_ms,
        "nonce": nonce,
    }


def build_paired_operator_entry(
    identity: OperatorDeviceIdentity,
    *,
    gateway_token: str,
    scopes: Iterable[str] = BACKEND_OPERATOR_SCOPES,
    display_name: str = "isol8-backend",
    client_id: str = BACKEND_CLIENT_ID,
    client_mode: str = BACKEND_CLIENT_MODE,
) -> dict:
    """Build a single `devices/paired.json` entry for our operator device.

    Matches the `PairedDevice` shape in `src/gateway/device-auth-store.ts`.
    Written to EFS at container provision time so OpenClaw trusts the
    operator device on first boot without a pairing-approval round-trip.

    The top-level `scopes` and `approvedScopes` lists are identical because
    we grant full access at provision — there's no admin-gate for this
    device; our backend IS the admin for its own containers.
    """
    scopes_list = list(scopes)
    now_ms = int(time.time() * 1000)
    return {
        "deviceId": identity.device_id,
        "publicKey": identity.public_key_b64,
        "displayName": display_name,
        "clientId": client_id,
        "clientMode": client_mode,
        "role": "operator",
        "roles": ["operator"],
        "scopes": scopes_list,
        "approvedScopes": scopes_list,
        "createdAtMs": now_ms,
        "approvedAtMs": now_ms,
        "tokens": {
            "operator": {
                "token": gateway_token,
                "role": "operator",
                "scopes": scopes_list,
                "createdAtMs": now_ms,
            }
        },
    }
