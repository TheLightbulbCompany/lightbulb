"""Sign-in proofs for the free ``lightbulb`` network: which Lightbulb user a computer is.

At ``lightbulb login`` (the device flow, after the computer signed a backend challenge with its
member key) the backend issues a compact EdDSA JWS ``{iat, exp, sub: <user id>, computer:
<member key>, auth_time, org?: <workspace id>}``, header ``{alg: EdDSA, kid}``. The computer keeps it and shows it: to a workspace
member's engine in its network ``hello`` (verified offline against the issuer key; it admits
only ``sub`` in the workspace's ``members``) and to the route link here. Nothing about the
binding is stored server-side.

The signing key is ``NETWORK_PROOF_KEY`` (Secrets Manager); its SHA-256 is the Ed25519 seed.
Its public half (``public_key``) is baked into the engine at the key ceremony; a runtime gets
it in ``LIGHTBULB_ISSUER_KEY``.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

# A proof stays valid until exp (30 d); a person signs a computer out with
# ``network_login.revoke``, which every checker (refresh, route link, tool calls, engines'
# ``revoked_keys``) honours. Removing a person from the workspace is instant via members.
LIFE_S = 30 * 86_400
MAX_TOKEN = 4096
APP_PASS_TYP = "lightbulb-app-pass"  # R61: the JWS header type of an app pass; never a sign-in proof


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _private() -> Ed25519PrivateKey:
    from core.config import settings

    secret = settings.NETWORK_PROOF_KEY
    if not secret:
        raise RuntimeError("NETWORK_PROOF_KEY is not set")
    return Ed25519PrivateKey.from_private_bytes(hashlib.sha256(secret.encode()).digest())


def kid() -> str:
    from core.config import settings

    return f"{settings.ENVIRONMENT or 'dev'}1"


def public_key() -> dict:
    """The issuer key a verifier needs: ``{kid, key}`` (base64url of the 32 raw bytes)."""
    raw = _private().public_key().public_bytes_raw()
    return {"kid": kid(), "key": _b64(raw), "env": f"{kid()}:{_b64(raw)}"}


def mint(
    member_key: str, user_id: str, now: int | None = None, auth_time: int | None = None, org: str | None = None
) -> str:
    """``auth_time``: when the person confirmed in the browser (now, for a login); a renewal
    carries the original. ``org``: the workspace active in that browser, which the workspace
    tools act in; a renewal carries it too. A proof without it (older, or a free account) is
    still good: the tools then need the person to be in exactly one workspace."""
    now = int(time.time()) if now is None else now
    claims = {
        "iat": now,
        "exp": now + LIFE_S,
        "sub": user_id,
        "computer": member_key,
        "auth_time": now if auth_time is None else auth_time,
    }
    if org:
        claims["org"] = org
    return _sign(claims)


def mint_workspace(member_key: str, org_id: str, now: int | None = None) -> str:
    """A proof that ``member_key`` is the workspace ``org_id``'s own runtime: ``sub`` and ``org`` are both
    the workspace id (an ``org_`` subject, never a person)."""
    now = int(time.time()) if now is None else now
    return _sign({"iat": now, "exp": now + LIFE_S, "sub": org_id, "org": org_id, "computer": member_key})


def _sign(claims: dict, typ: str | None = None) -> str:
    head = {"alg": "EdDSA", "kid": kid(), **({"typ": typ} if typ else {})}
    header = _b64(json.dumps(head, separators=(",", ":")).encode())
    payload = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = _private().sign(f"{header}.{payload}".encode())
    return f"{header}.{payload}.{_b64(sig)}"


def mint_app_pass(
    user_id: str, *, name: str, email: str, ws: str, room: str, app: str, now: int | None = None, life_s: int = 900
) -> str:
    """A pass to open one app (``app`` = its name, which the door compares to ``LIGHTBULB_APP``) for
    ``life_s``: a bearer credential the app's door verifies with ``public_key``. Its header ``typ`` is
    ``APP_PASS_TYP`` and it has no ``computer`` claim, so ``verify`` never takes it for a sign-in proof."""
    now = int(time.time()) if now is None else now
    return _sign(
        {
            "iat": now,
            "exp": now + life_s,
            "sub": user_id,
            "name": name,
            "email": email,
            "ws": ws,
            "room": room,
            "app": app,
        },
        typ=APP_PASS_TYP,
    )


def verify(token: str, member_key: str, now: int | None = None) -> dict | None:
    """The claims when ``token`` is our proof for ``member_key`` and in force at ``now``."""
    now = int(time.time()) if now is None else now
    try:
        if not isinstance(token, str) or len(token) > MAX_TOKEN:
            return None
        h, p, s = token.split(".")
        header = json.loads(_unb64(h))
        if header.get("alg") != "EdDSA" or header.get("kid") != kid() or header.get("typ") == APP_PASS_TYP:
            return None
        _private().public_key().verify(_unb64(s), f"{h}.{p}".encode())
        claims = json.loads(_unb64(p))
    except (ValueError, TypeError, InvalidSignature, RuntimeError):
        return None
    if (
        not isinstance(claims, dict)
        or not isinstance(claims.get("computer"), str)
        or not claims["computer"]
        or claims.get("computer") != member_key
        or not isinstance(claims.get("sub"), str)
        or not str(claims["sub"]).startswith("user_")
        or not isinstance(claims.get("exp"), int)
        or not isinstance(claims.get("iat"), int)
        or claims["iat"] > now + 300
        or now > claims["exp"]
        or not isinstance(claims.get("org", ""), str)
    ):
        return None
    return claims
