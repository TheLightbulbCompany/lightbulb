"""The Lightbulb Connect sign-in issuer: 24 h EdDSA proofs naming a person (or a workspace)
and the computer key they were issued to.

The engine (lightbulb-connect ``src/proof.rs``) verifies a proof offline against its
compiled-in ``ISSUER_KEYS``: a compact JWS whose header is ``{"alg":"EdDSA","kid":…}``, whose
Ed25519 signature is over the ASCII ``header.payload``, and whose claims fit its bounds (a
name 1..=64 printable characters, at most 6 agents of 1..=32, no ``agents`` on a computer
proof, ``sub = ws:<workspace.id>`` on a workspace one). Every mint writes an audit row.

The signing seed is read at call time from the Secrets Manager secret NAMED by
``NETWORK_ISSUER_KEY_SECRET`` (``lightbulb/{env}/network/issuer_key_<kid>``, base64) and kept
5 minutes. Never log a proof.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import time
from typing import Literal

import boto3
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import HTTPException

from core.config import settings
from core.dynamodb import run_in_thread
from core.repositories import network_audit_repo
from core.services import clerk_admin, messaging
from core.services.device_login import clean_machine_name

logger = logging.getLogger(__name__)

LIFE_S = 86_400
REFRESH_GRACE_S = 7 * 86_400
REFRESH_SKEW_S = 300
MAX_TOKEN = 4096
KEY_TTL_S = 300
MAX_AGENTS = 6

# The public halves by kid, mirroring the engine's `ISSUER_KEYS` (lightbulb-connect
# `src/proof.rs`): a refresh verifies the old proof with ITS kid's key, so a p1 -> p2 rotation
# keeps old proofs refreshable; `mint` signs with the active `NETWORK_ISSUER_KID`.
# Today these are the engine's placeholder bytes (no private half exists). Replaced at the key
# ceremony: `scripts/network_keys.py` prints the values for both this table and the engine's.
ISSUER_PUBLIC_KEYS: dict[str, bytes] = {
    "p1": bytes.fromhex("3ab7efc15f6987eeccf9ae020e646e37a4d967db7449adb64d7288dcd494208b"),
    "p2": bytes.fromhex("fe78b068865a2dda3784d03d1cd9abcceeaf2f303df66d3c0030ac2c82368361"),
    "dev": bytes.fromhex("398974e26a8401d624b3fe219bc9bf44e7d1b403b4aa23a4714019f668017830"),
}

_key: tuple[float, Ed25519PrivateKey] | None = None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def canonical(value) -> bytes:
    """identity.rs ``canonical``: keys sorted recursively, no whitespace, UTF-8 unescaped."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _key_bytes(member_key: str) -> bytes | None:
    """The 32 public-key bytes a member key (52-char lowercase base32, no padding) spells."""
    if len(member_key) != 52 or member_key != member_key.lower():
        return None
    try:
        raw = base64.b32decode(member_key.upper() + "====")
    except (binascii.Error, ValueError):
        return None
    # Re-encoding must give the same text: the engine refuses non-zero trailing bits.
    return raw if base64.b32encode(raw).decode().lower().rstrip("=") == member_key else None


def member_key_ok(member_key: str) -> bool:
    return _key_bytes(member_key) is not None


def _clean(text: str | None, limit: int) -> str:
    return clean_machine_name(text)[:limit].strip()


def _read_seed() -> str:
    sm = boto3.client("secretsmanager", region_name=settings.AWS_REGION)
    return sm.get_secret_value(SecretId=settings.NETWORK_ISSUER_KEY_SECRET)["SecretString"]


async def _signing_key() -> Ed25519PrivateKey:
    global _key
    if _key is None or time.monotonic() - _key[0] > KEY_TTL_S:
        if not settings.NETWORK_ISSUER_KEY_SECRET:
            raise RuntimeError("NETWORK_ISSUER_KEY_SECRET is not configured")
        private = Ed25519PrivateKey.from_private_bytes(base64.b64decode(await run_in_thread(_read_seed)))
        kid = settings.NETWORK_ISSUER_KID
        # A stale table (the key ceremony ran, ISSUER_PUBLIC_KEYS was not updated) must fail
        # loudly here, never mint proofs that every refresh then refuses.
        if private.public_key().public_bytes_raw() != ISSUER_PUBLIC_KEYS.get(kid):
            logger.error("network: issuer seed does not match ISSUER_PUBLIC_KEYS[%s]; refusing to mint", kid)
            raise HTTPException(status_code=503, detail="Sign-in is unavailable. Try again later.")
        _key = (time.monotonic(), private)
    return _key[1]


async def mint(
    sub: str,
    name: str,
    computer: str,
    kind: Literal["computer", "workspace"],
    workspace: dict | None,
    agents: list[str] | None,
    now: int,
) -> str:
    """A compact JWS proof for ``computer`` (the member key; the hostname is never in it)."""
    name = _clean(name, 64)
    if workspace is not None:
        workspace = {"id": workspace["id"], "name": _clean(workspace.get("name"), 64)}
        if not workspace["name"]:
            workspace = None
    if not name or not member_key_ok(computer):
        raise ValueError("proof claims do not fit")
    if kind == "workspace" and (not workspace or sub != f"ws:{workspace['id']}"):
        raise ValueError("a workspace proof's sub is ws:<its workspace id>")
    if kind == "computer" and sub.startswith("ws:"):
        raise ValueError("a person is never a ws: id")
    kid = settings.NETWORK_ISSUER_KID
    claims: dict = {
        "iss": settings.NETWORK_ISSUER_URL,
        "kid": kid,
        "iat": now,
        "exp": now + LIFE_S,
        "sub": sub,
        "name": name,
        "computer": computer,
        "kind": kind,
    }
    if workspace:
        claims["workspace"] = workspace
    if kind == "workspace":
        claims["agents"] = [a for a in (_clean(a, 32) for a in agents or []) if a][:MAX_AGENTS]
    signing = f"{_b64(canonical({'alg': 'EdDSA', 'kid': kid}))}.{_b64(canonical(claims))}"
    token = f"{signing}.{_b64((await _signing_key()).sign(signing.encode()))}"
    if len(token) > MAX_TOKEN:
        raise ValueError("proof is too large")
    await network_audit_repo.put(
        "ISSUER",
        "proof",
        {"sub": sub, "computer": computer, "proof_kind": kind, "exp": claims["exp"]},
        at=now,
        item_expires_at=claims["exp"],
    )
    logger.info("network: proof minted kind=%s sub=%s computer=%s", kind, sub, computer[:12])
    return token


async def _workspace_of(user_id: str, org_id: str | None) -> dict | None:
    """``{id, name}`` when the person is still a member of ``org_id``, else None."""
    if not org_id:
        return None
    try:
        orgs = await clerk_admin.list_user_organizations(user_id, limit=100, strict=True)
    except Exception as exc:  # noqa: BLE001 -- a Clerk blip must not silently drop the workspace
        raise HTTPException(status_code=503, detail="Could not read your workspaces. Try again.") from exc
    org = next((o for o in orgs if o.get("id") == org_id), None)
    return {"id": org_id, "name": org["name"]} if org and org.get("name") else None


async def computer_proof(user_id: str, org_id: str | None, computer: str, now: int) -> str:
    """A ``kind=computer`` proof for this person, carrying ``org_id`` while they are in it."""
    name = await messaging.human_display_name(user_id)
    if name == user_id:  # the lookup failed: that is no name
        raise HTTPException(status_code=503, detail="Could not read your name. Try again.")
    return await mint(user_id, name, computer, "computer", await _workspace_of(user_id, org_id), None, now)


_REFUSED = HTTPException(status_code=401, detail="Proof refresh refused.")


async def _claims(token: str) -> dict:
    """The claims of a proof an issuer key in ``ISSUER_PUBLIC_KEYS`` signed (any age)."""
    try:
        h, p, s = token.split(".")
        header = json.loads(_unb64(h))
        public = ISSUER_PUBLIC_KEYS.get(header.get("kid")) if header.get("alg") == "EdDSA" else None
        if public is None:
            raise ValueError("unknown issuer key")
        Ed25519PublicKey.from_public_bytes(public).verify(_unb64(s), f"{h}.{p}".encode())
        return json.loads(_unb64(p))
    except (ValueError, TypeError, AttributeError, binascii.Error, InvalidSignature) as exc:
        raise _REFUSED from exc


_UNREACHABLE = "Could not check this person. Try again."


async def refresh(proof: str, at: int, sig: str, now: int) -> str:
    """A new proof for the computer holding ``proof``: signed by that computer's key over
    ``canonical({proof, at})``, ``at`` within 5 minutes, the old proof at most 7 days
    expired, and its person still in Clerk. The workspace is read again and dropped if left."""
    claims = await _claims(proof)
    key = _key_bytes(str(claims.get("computer", "")))
    if key is None or abs(now - at) > REFRESH_SKEW_S or now > int(claims["exp"]) + REFRESH_GRACE_S:
        raise _REFUSED
    try:
        Ed25519PublicKey.from_public_bytes(key).verify(_unb64(sig), canonical({"proof": proof, "at": at}))
    except (ValueError, binascii.Error, InvalidSignature) as exc:
        raise _REFUSED from exc
    sub, computer, workspace = claims["sub"], claims["computer"], claims.get("workspace") or {}
    if claims["kind"] == "workspace":
        # ponytail: carries the agents forward; the bridge re-mints with fresh ones on connect.
        try:
            org = await clerk_admin.get_organization(workspace["id"], strict=True)
        except clerk_admin.ClerkLookupError as exc:
            raise HTTPException(status_code=503, detail=_UNREACHABLE) from exc
        if not org:  # the workspace is gone
            raise _REFUSED
        ws = {"id": workspace["id"], "name": org.get("name") or workspace["name"]}
        return await mint(sub, ws["name"], computer, "workspace", ws, claims.get("agents"), now)
    try:
        user = await clerk_admin.get_user_strict(sub)
    except clerk_admin.ClerkLookupError as exc:
        raise HTTPException(status_code=503, detail=_UNREACHABLE) from exc
    if user is None or user.get("banned") or user.get("locked"):
        raise _REFUSED
    return await computer_proof(sub, workspace.get("id"), computer, now)
