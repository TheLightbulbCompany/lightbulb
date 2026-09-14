"""Upgrade-time authentication for the gateway relay: the authorizer Lambda's two
verifiers (`apps/infra/lambda/websocket-authorizer/index.py`), in-process.

Two credential kinds:

1. Paired desktop device JWTs (EdDSA, kind=device), verified against the
   signature-bound public key in the owner's container row.
2. Paperclip service-token JWTs (HS256, kind=paperclip_service), verified with
   ``service_token``'s signing key.

Both branches fail CLOSED, mirroring the Lambda: a paired-device entry with no
attributed member never resolves to the owner id (that would masquerade an org
id as a personal user identity), and any verification error returns None
rather than raising.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from typing import Literal

import jwt
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from core.repositories import container_repo
from core.services import service_token

DEVICE_TOKEN_MAX_LIFETIME_S = 300
NODE_CLIENT_IDS = frozenset({"node-host", "openclaw-macos"})
OPERATOR_CLIENT_ID = "paperclip-operator"


@dataclass(frozen=True)
class MachineIdentity:
    user_id: str
    org_id: str
    owner_id: str
    kind: Literal["node", "operator"]
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


def _operator(token: str, client_id: str) -> MachineIdentity | None:
    try:
        claims = service_token.verify(token)
    except (jwt.PyJWTError, RuntimeError):
        return None
    sub = claims.get("sub")
    if not sub:
        # Defense in depth below service_token.verify's own "Missing sub
        # claim" guard -- fail CLOSED (caller reports 1008) rather than a
        # KeyError the caller would report as 1011.
        return None
    return MachineIdentity(
        user_id=sub,
        org_id="",
        owner_id=sub,
        kind="operator",
        auth_kind="paperclip_service",
        client_id=client_id,
        device_id=None,
    )


async def verify_upgrade(token: str, client_id: str) -> MachineIdentity | None:
    """None on any failure; the caller closes 1008. Never raises."""
    if not token:
        return None
    if client_id in NODE_CLIENT_IDS:
        return await _device(token, client_id)
    if client_id == OPERATOR_CLIENT_ID:
        return _operator(token, client_id)
    return None
