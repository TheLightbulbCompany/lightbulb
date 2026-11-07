"""Upgrade-time device authentication for the desktop node relay.

Paired desktop device JWTs (EdDSA, kind=device) are verified against the
signature-bound public key in the owner's container row. Service tokens are
not accepted by this WebSocket endpoint.

A paired-device entry with no attributed member fails closed rather than
masquerading the owner id as a personal user identity.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.repositories import container_repo

DEVICE_TOKEN_MAX_LIFETIME_S = 300
NODE_CLIENT_IDS = frozenset({"node-host", "openclaw-macos"})


@dataclass(frozen=True)
class MachineIdentity:
    user_id: str
    org_id: str
    owner_id: str
    kind: Literal["node"]
    auth_kind: str
    client_id: str
    device_id: str | None


async def _device(token: str, client_id: str) -> MachineIdentity | None:
    try:
        unverified = jwt.decode(token, options={"verify_signature": False})
    except jwt.PyJWTError:
        return None
    device_id, owner_id = unverified.get("deviceId"), unverified.get("owner")
    if not isinstance(device_id, str) or not isinstance(owner_id, str) or not device_id or not owner_id:
        return None
    row = await container_repo.get_by_owner_id(owner_id, consistent=True)
    entry = ((row or {}).get("paired_devices") or {}).get(device_id)
    if not entry or not entry.get("publicKey"):
        return None  # not paired / revoked
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.urlsafe_b64decode(entry["publicKey"] + "=="))
        claims = jwt.decode(
            token,
            key=pub,
            algorithms=["EdDSA"],
            options={"require": ["exp", "iat", "deviceId", "owner"]},
            leeway=30,
        )
    except (jwt.PyJWTError, ValueError):
        return None
    if claims.get("kind") != "device" or claims["exp"] - claims["iat"] > DEVICE_TOKEN_MAX_LIFETIME_S:
        return None
    user_id = entry.get("userId")
    if not user_id:
        # Fail CLOSED — an entry with no attributed member must never be
        # allowed as the owner (index.py:228-234). The sole writer
        # (desktop_pair.py) always sets userId.
        return None
    return MachineIdentity(
        user_id=user_id,
        org_id="" if owner_id == user_id else owner_id,
        owner_id=owner_id,
        kind="node",
        auth_kind="device",
        client_id=client_id,
        device_id=device_id,
    )


async def verify_upgrade(token: str, client_id: str) -> MachineIdentity | None:
    """None on any failure; the caller closes 1008. Never raises."""
    if not token:
        return None
    if client_id in NODE_CLIENT_IDS:
        return await _device(token, client_id)
    return None
