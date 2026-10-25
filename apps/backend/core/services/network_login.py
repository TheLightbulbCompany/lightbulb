"""``lightbulb login`` / ``logout`` and ``lightbulb invite --agents``: the one tie between a
computer on the free ``lightbulb`` network and a Lightbulb user.

The computer proves its member key: ``challenge`` hands out a one-time nonce (``CHALLENGE_TTL_S``,
single use), and the CLI signs ``{"challenge", "key", "purpose": "lightbulb-<purpose>",
"ticket"?}`` (canonical JSON, Ed25519) with that key. A good signature consumes the nonce; only
then does a device flow start (``device_login``'s rows, ``kind: "network"``), which the
signed-in person confirms in the browser.

- ``login``: the key is bound to the confirming user (``network_keys_repo``); every workspace
  that user is in republishes its ``member_keys``.
- ``agents``: a workspace admin, whose own computer this is (signed in with ``login``), adds
  the workspace to that computer's network with the once ask link the computer minted.
- ``logout``: signed only (no browser), drops the binding.

Keys, codes, nonces and links are credentials or near enough: none is ever logged.
"""

from __future__ import annotations

import base64
import binascii
import json
import re
import time

from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException

from core.auth import AuthContext, require_org_admin, resolve_owner_id
from core.repositories import messaging_repo, network_keys_repo
from core.services import device_login

CHALLENGE_TTL_S = 120
KIND = "network"
PAGE = "/auth/network"
DEVICE_PURPOSES = ("login", "agents")
KEY_RE = re.compile(r"^[a-z2-7]{52}$")  # the engine's member key: lowercase base32 of 32 bytes
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{1,8192}$")  # an encoded ticket is base64url
_BAD_PROOF = "This computer's proof did not check out. Run the command again."
_RETRY = "Lightbulb could not update your workspaces just now. Run the command again."
_TAKEN = "Someone else already confirmed this code."
_LOGIN_FIRST = "Sign this computer in as you first: run `lightbulb login` on it, then try again."


def _nonce_key(nonce: str) -> dict:
    return {"pk": f"NETNONCE#{nonce}", "sk": f"NETNONCE#{nonce}"}


def message(key: str, purpose: str, challenge: str, ticket: str | None = None) -> bytes:
    """What the computer signs: the engine's canonical JSON (sorted keys, no spaces). Every
    field is ASCII from a closed alphabet (checked before), so ``json.dumps`` matches serde."""
    body = {"challenge": challenge, "key": key, "purpose": f"lightbulb-{purpose}"}
    if ticket is not None:
        body["ticket"] = ticket
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def verify(key: str, purpose: str, challenge: str, sig: str, ticket: str | None = None) -> bool:
    """Whether ``sig`` (base64url, no padding) is ``key``'s Ed25519 signature over ``message``."""
    if not KEY_RE.match(key or "") or not NONCE_RE.match(challenge or ""):
        return False
    if ticket is not None and not TICKET_RE.match(ticket):
        return False
    try:
        raw = base64.b32decode(key.upper() + "====")
        signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        Ed25519PublicKey.from_public_bytes(raw).verify(signature, message(key, purpose, challenge, ticket))
    except (InvalidSignature, ValueError, TypeError, binascii.Error):
        return False
    return True


# ponytail: challenge is unauthenticated and not rate-limited, like device_login.start: one
# TTL'd write per call. Add an API Gateway route throttle if it is ever abused for write volume.
async def challenge() -> dict:
    import secrets

    from core.dynamodb import run_in_thread

    nonce = secrets.token_urlsafe(32)
    item = {**_nonce_key(nonce), "expiresAt": int(time.time()) + CHALLENGE_TTL_S}
    await run_in_thread(messaging_repo._table().put_item, Item=item)
    return {"challenge": nonce, "expiresIn": CHALLENGE_TTL_S}


async def _consume(nonce: str) -> bool:
    """Single use: the one delete of a live nonce wins; a replay, or an expired one, loses."""
    from core.dynamodb import run_in_thread

    try:
        await run_in_thread(
            messaging_repo._table().delete_item,
            Key=_nonce_key(nonce),
            ConditionExpression="attribute_exists(pk) AND expiresAt > :now",
            ExpressionAttributeValues={":now": int(time.time())},
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return False
    return True


async def _proven(key: str, purpose: str, nonce: str, sig: str, ticket: str | None = None) -> None:
    """The signature first (so nobody spends a nonce they cannot sign for), then the nonce."""
    if not verify(key, purpose, nonce, sig, ticket) or not await _consume(nonce):
        raise HTTPException(status_code=401, detail=_BAD_PROOF)


async def start(
    key: str, purpose: str, nonce: str, sig: str, computer: str | None, ticket: str | None, ip: str | None = None
) -> dict:
    if purpose not in DEVICE_PURPOSES or (purpose == "agents") != (ticket is not None):
        raise HTTPException(status_code=422, detail="Unknown request. Run lightbulb update.")
    await _proven(key, purpose, nonce, sig, ticket)
    if purpose == "agents" and not await network_keys_repo.get(key):
        raise HTTPException(status_code=409, detail=_LOGIN_FIRST)
    fields = {
        "kind": KIND,
        "purpose": purpose,
        "computerKey": key,
        "machineName": device_login.clean_machine_name(computer),
        # Shown on the confirm page beside the name, which the computer chose itself.
        "startedAt": int(time.time()),
        "sourceIp": (ip or "")[:64],
    }
    if ticket is not None:
        fields["ticket"] = ticket
    return await device_login.create(fields, PAGE, code_in_url=False)


async def describe(user_code: str) -> dict:
    row = await device_login.live_row(user_code, KIND)
    return {
        "purpose": row["purpose"],
        "computer": row.get("machineName") or "",
        "startedAt": row.get("startedAt"),
        "ip": row.get("sourceIp") or "",
    }


async def approve(ctx: AuthContext, user_code: str) -> dict:
    """The signed-in person confirms. ``login`` binds the computer to them; ``agents`` (an admin
    of the active workspace, on a computer already theirs) sends the workspace to join."""
    from core.services import network_member

    row = await device_login.live_row(user_code, KIND)
    purpose = row["purpose"]
    owner_id = ""
    if purpose == "agents":
        require_org_admin(ctx)
        owner_id = resolve_owner_id(ctx)
        bound = await network_keys_repo.get(row["computerKey"])
        if not bound or bound.get("user_id") != ctx.user_id:
            raise HTTPException(status_code=409, detail=_LOGIN_FIRST)
    if row.get("status") == "approved":
        if row.get("approverUserId") != ctx.user_id:
            raise HTTPException(status_code=409, detail=_TAKEN)
        return {"status": "approved"}  # a retried confirm after a lost answer
    row = await device_login.claim(user_code, owner_id, ctx.user_id, kind=KIND)
    if purpose == "login":
        key = row["computerKey"]
        old = await network_keys_repo.get(key)
        # Resolved before the binding moves: a Clerk error now is a 503 the person retries, not a
        # gate left open in a workspace nobody republished.
        owners = await _workspaces(ctx.user_id, (old or {}).get("user_id", ""))
        await network_keys_repo.put(key, ctx.user_id, row.get("machineName") or "")
        await _republish(owners, extra=(key,))
        result = {"name": await _name(ctx.user_id)}
    else:
        await network_member.join_network(owner_id, row["ticket"])
        result = {"workspaceName": await _workspace(owner_id)}
    await device_login.finish(row, ctx.user_id, result)
    return {"status": "approved", **result}


async def _workspaces(*user_ids: str) -> list[str]:
    from core.services import network_member

    try:
        return await network_member.workspaces_of(*user_ids)
    except Exception as exc:  # noqa: BLE001 -- Clerk could not say; nothing has changed yet
        raise HTTPException(status_code=503, detail=_RETRY) from exc


async def _republish(owners: list[str], **delta) -> None:
    from core.services import network_member

    try:
        await network_member.republish(owners, **delta)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=_RETRY) from exc


async def logout(key: str, nonce: str, sig: str) -> None:
    """The binding goes last: a failed republish leaves it in place, so running ``lightbulb
    logout`` again republishes everything again."""
    await _proven(key, "logout", nonce, sig)
    bound = await network_keys_repo.get(key)
    if bound:
        await _republish(await _workspaces(bound["user_id"]), exclude=(key,))
        await network_keys_repo.delete(key)


async def _name(user_id: str) -> str | None:
    from core.services.messaging import human_display_name

    try:
        name = await human_display_name(user_id)
    except Exception:  # noqa: BLE001 -- a label never blocks the login
        return None
    return None if name == user_id else name


async def _workspace(owner_id: str) -> str | None:
    from core.services import clerk_admin

    try:
        return ((await clerk_admin.get_organization(owner_id)) or {}).get("name") or None
    except Exception:  # noqa: BLE001
        return None
