"""``lightbulb login`` and ``lightbulb invite --agents``: the one tie between a computer on the
free ``lightbulb`` network and a Lightbulb user.

The computer proves its member key: ``challenge`` hands out a one-time nonce (``CHALLENGE_TTL_S``,
single use), and the CLI signs ``{"challenge", "key", "purpose": "lightbulb-<purpose>",
"ticket"?}`` (canonical JSON, Ed25519) with that key. A good signature consumes the nonce; only
then does a device flow start (``device_login``'s rows, ``kind: "network"``), which the
signed-in person confirms in the browser.

- ``login``: the computer is handed a sign-in proof (``network_proof``) naming the confirming
  user; nothing is stored here. A workspace's engine checks the proof's user against its
  ``members``. ``refresh`` (signed, no browser) renews a proof still in force.
- ``agents``: a workspace admin, whose own computer this is (its proof names them), adds the
  workspace to that computer's network with the once ask link the computer minted.
- ``logout`` (signed, no browser) signs the computer out everywhere (``sign_out``) before it
  deletes its proof; a later ``login`` of the same computer by the same person lifts that.

Keys, codes, nonces, links and proofs are credentials or near enough: none is ever logged.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import re
import time

from botocore.exceptions import ClientError
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from fastapi import HTTPException, Request

from core.auth import AuthContext, require_org_admin, resolve_owner_id
from core.repositories import messaging_repo
from core.services import device_login, network_proof

CHALLENGE_TTL_S = 120
KIND = "network"
PAGE = "/auth/network"
DEVICE_PURPOSES = ("login", "agents")
REFRESH_BEFORE_S = 7 * 86_400  # a proof is renewed in its last week only
KEY_RE = re.compile(r"^[a-z2-7]{52}$")  # the engine's member key: lowercase base32 of 32 bytes
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
TICKET_RE = re.compile(r"^[A-Za-z0-9_-]{1,8192}$")  # an encoded ticket is base64url
# A workspace tool call's challenge: "<unix ts> <METHOD> <path?query> <sha256(body) hex> <label>".
# The label is the engine's roster label for the calling session; no control characters.
TOOLS_CHALLENGE_RE = re.compile(r"^\d{1,12} [A-Z]{3,7} /\S{0,2048} [0-9a-f]{64} [^\x00-\x1f\x7f]{1,200}$")
TOOLS_SKEW_S = 60
_BAD_PROOF = "This computer's proof did not check out. Run the command again."
_TAKEN = "Someone else already confirmed this code."
_SIGN_IN_AGAIN = "This computer's sign-in has ended. Sign in again: lightbulb login"
_LOGIN_FIRST = "Sign this computer in as you first: run `lightbulb login` on it, then try again."


def _nonce_key(nonce: str) -> dict:
    return {"pk": f"NETNONCE#{nonce}", "sk": f"NETNONCE#{nonce}"}


def message(key: str, purpose: str, challenge: str, ticket: str | None = None) -> bytes:
    """What the computer signs: the engine's canonical JSON (sorted keys, no spaces). Every
    field is ASCII from a closed alphabet (checked before), so ``json.dumps`` matches serde."""
    body = {"challenge": challenge, "key": key, "purpose": f"lightbulb-{purpose}"}
    if ticket is not None:
        body["ticket"] = ticket
    # ensure_ascii=False: a ``tools`` challenge carries a session label, which serde writes raw.
    return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def verify(key: str, purpose: str, challenge: str, sig: str, ticket: str | None = None) -> bool:
    """Whether ``sig`` (base64url, no padding) is ``key``'s Ed25519 signature over ``message``."""
    challenge_re = TOOLS_CHALLENGE_RE if purpose == "tools" else NONCE_RE
    if not KEY_RE.match(key or "") or not challenge_re.match(challenge or ""):
        return False
    if ticket is not None and not TICKET_RE.match(ticket):
        return False
    try:
        raw = base64.b32decode(key.upper() + "====")
        signature = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
        # One spelling per signature: the tools call's one-use key hashes this text, so a
        # padded, stray-character or nonzero-trailing-bits spelling must not verify.
        if base64.urlsafe_b64encode(signature).rstrip(b"=").decode() != sig:
            return False
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
    key: str,
    purpose: str,
    nonce: str,
    sig: str,
    computer: str | None,
    ticket: str | None,
    ip: str | None = None,
    proof: str | None = None,
) -> dict:
    if purpose not in DEVICE_PURPOSES or (purpose == "agents") != (ticket is not None):
        raise HTTPException(status_code=422, detail="Unknown request. Run lightbulb update.")
    await _proven(key, purpose, nonce, sig, ticket)
    claims = network_proof.verify(proof or "", key) if purpose == "agents" else None
    if claims is not None and await is_revoked(claims["sub"], key):
        claims = None
    if purpose == "agents" and claims is None:
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
    if claims is not None:
        fields["computerUser"] = claims["sub"]  # whose computer this is, from its proof
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
        if row.get("computerUser") != ctx.user_id:
            raise HTTPException(status_code=409, detail=_LOGIN_FIRST)
    if row.get("status") == "approved":
        if row.get("approverUserId") != ctx.user_id:
            raise HTTPException(status_code=409, detail=_TAKEN)
        return {"status": "approved"}  # a retried confirm after a lost answer
    row = await device_login.claim(user_code, owner_id, ctx.user_id, kind=KIND)
    if purpose == "login":
        # Where the computer's daemon keeps its route link (the machine-relay door), so the
        # person's hosted apps can reach the network through it.
        result = {
            "name": await _name(ctx.user_id),
            # The browser's active workspace is the one this computer's workspace tools act in.
            "proof": network_proof.mint(row["computerKey"], ctx.user_id, org=ctx.org_id),
            "routeUrl": route_url(),
        }
        await unrevoke(ctx.user_id, row["computerKey"])
    else:
        await network_member.join_network(owner_id, row["ticket"])
        result = {"workspaceName": await _workspace(owner_id)}
    await device_login.finish(row, ctx.user_id, result)
    return {"status": "approved", **{k: v for k, v in result.items() if k != "proof"}}


def route_url() -> str:
    from core.config import settings

    return settings.GATEWAY_RELAY_URL.rsplit("/", 1)[0] + "/network"


async def refresh(key: str, nonce: str, sig: str, proof: str) -> dict:
    """A computer renews its proof in its last week, signed, with no browser: the same user, a
    fresh ``exp``, for as long as the account is live and the person has not signed this computer
    out (``revoke``). Every refresh checks both. An expired proof is a ``login`` again."""
    await _proven(key, "refresh", nonce, sig)
    claims = network_proof.verify(proof, key)
    if claims is None or await is_revoked(claims["sub"], key):
        raise HTTPException(status_code=401, detail=_SIGN_IN_AGAIN)
    await require_live_user(claims["sub"])
    if claims["exp"] - int(time.time()) > REFRESH_BEFORE_S:
        return {"proof": proof}
    return {"proof": network_proof.mint(key, claims["sub"], auth_time=claims.get("auth_time"), org=claims.get("org"))}


# --- Remote sign-out: a person signs one of their computers out from the website ---------------


def _revoked_key(user_id: str, key: str) -> dict:
    return {"pk": f"NETREVOKED#{user_id}", "sk": key}


async def revoke(user_id: str, key: str) -> None:
    """Sign ``key`` out for ``user_id``: its proof no longer refreshes or opens a route link or a
    workspace tool call, and every workspace's engine refuses it (``revoked_keys`` in its
    sessions file). Kept while a proof minted before it could still be in force."""
    from core.dynamodb import run_in_thread

    if not KEY_RE.match(key):
        raise HTTPException(status_code=404, detail="No such computer of yours.")
    item = {**_revoked_key(user_id, key), "ttl": int(time.time()) + network_proof.LIFE_S}
    await run_in_thread(messaging_repo._table().put_item, Item=item)


async def sign_out(user_id: str, key: str) -> None:
    """Sign ``key`` out for ``user_id`` now: ``revoke``, close its route link, and republish
    the ``revoked_keys`` of each of the person's workspaces (a warm engine refuses it at once)."""
    from core.services import network_route

    await revoke(user_id, key)
    await network_route.sign_out(user_id, key)
    await _republish(user_id)


async def _republish(user_id: str) -> None:
    from core.services import clerk_admin, network_member

    for org in await clerk_admin.list_user_organizations(user_id, limit=100):
        await network_member.publish_sessions(org["id"])  # never raises


async def logout(key: str, nonce: str, sig: str, proof: str) -> dict:
    """``lightbulb logout``: the computer, signed, signs itself out as the person its proof names
    (``sign_out``). A proof that no longer checks out is refused nowhere it is not already."""
    await _proven(key, "logout", nonce, sig)
    claims = network_proof.verify(proof, key)
    if claims is not None:
        await sign_out(claims["sub"], key)
    return {"state": "signed_out"}


async def unrevoke(user_id: str, key: str) -> None:
    """A fresh ``login`` of ``key`` by ``user_id`` lifts their sign-out of it, republished."""
    from core.dynamodb import run_in_thread

    gone = await run_in_thread(
        messaging_repo._table().delete_item, Key=_revoked_key(user_id, key), ReturnValues="ALL_OLD"
    )
    if gone.get("Attributes"):
        await _republish(user_id)


async def is_revoked(user_id: str, key: str) -> bool:
    return await messaging_repo._get(**_revoked_key(user_id, key)) is not None


async def revoked_keys(user_ids: list[str]) -> list[str]:
    """The signed-out computer keys of these people, for an engine's gate."""
    rows = await asyncio.gather(*(messaging_repo._query(f"NETREVOKED#{u}") for u in user_ids))
    now = int(time.time())
    return sorted({r["sk"] for found in rows for r in found if int(r.get("ttl") or 0) > now})


async def require_live_user(user_id: str) -> None:
    """The Clerk user still exists and is not banned or locked: 401 if not, 503 when Clerk cannot
    say (fail closed). A proof is renewed, and a personal key used, only for a live account."""
    from core.services import clerk_admin

    try:
        user = await clerk_admin.get_user_strict(user_id)
    except Exception as exc:  # noqa: BLE001 -- ClerkLookupError: absence and a blip look alike
        raise HTTPException(status_code=503, detail="Lightbulb could not check your account. Try again.") from exc
    if not user or user.get("banned") or user.get("locked"):
        raise HTTPException(status_code=401, detail=_SIGN_IN_AGAIN)


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


# --- The workspace tools: a signed-in computer calls Lightbulb's REST API as its person ----------


def _challenge(request: Request) -> str:
    """``X-Lightbulb-Challenge``: base64url (no padding) of the UTF-8 challenge text."""
    value = request.headers.get("X-Lightbulb-Challenge", "")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("utf-8")
    except (ValueError, UnicodeError, binascii.Error):
        return ""


def _same_target(signed: str, path: str, query: str) -> bool:
    """The signed ``<path>?<query>`` (the API base's /api/v1 included) names this request. Compared
    decoded, with the query as sorted pairs: API Gateway's HTTP API may re-encode what the engine
    sent (``+`` for ``%20``, ``/`` for ``%2F``), and a re-encoding never changes the values."""
    from urllib.parse import parse_qsl, unquote

    signed_path, _, signed_query = signed.partition("?")

    def pairs(q: str) -> list[tuple[str, str]]:
        return sorted(parse_qsl(q, keep_blank_values=True))

    return unquote(signed_path) == unquote(path) and pairs(signed_query) == pairs(query)


async def computer_person(request: Request, *, free_ok: bool = False) -> tuple[AuthContext, str]:
    """(``free_ok``: a person in no workspace is returned with ``org_id=None`` instead of a 409.)
    A signed-in computer's call (``lightbulb login``): the person its proof names, in the
    workspace chosen at login, and the engine-verified label of the calling session. Each call is signed over its
    method, path, query, body hash and a timestamp, and is good once, within ``TOOLS_SKEW_S``."""
    import hashlib

    from core.dynamodb import run_in_thread
    from core.services import clerk_admin

    proof = request.headers.get("X-Lightbulb-Proof", "")
    sig = request.headers.get("X-Lightbulb-Sig", "")
    challenge = _challenge(request)
    try:
        payload = json.loads(base64.urlsafe_b64decode(proof.split(".")[1] + "=="))
        key = payload["computer"] if isinstance(payload, dict) else ""
    except (ValueError, IndexError, KeyError, TypeError, binascii.Error):
        key = ""
    if not isinstance(key, str) or not verify(key, "tools", challenge, sig):
        raise HTTPException(status_code=401, detail=_BAD_PROOF)
    claims = network_proof.verify(proof, key)
    if claims is None or await is_revoked(claims["sub"], key):
        raise HTTPException(status_code=401, detail=_SIGN_IN_AGAIN)
    ts, method, path, body_hash, label = challenge.split(" ", 4)
    if (
        abs(int(time.time()) - int(ts)) > TOOLS_SKEW_S
        or method != request.method
        or not _same_target(path, request.url.path, request.url.query)
        or body_hash != hashlib.sha256(await request.body()).hexdigest()
    ):
        raise HTTPException(status_code=401, detail=_BAD_PROOF)
    # Good once: a captured call cannot be replayed inside its window.
    seen = hashlib.sha256(sig.encode()).hexdigest()
    try:
        await run_in_thread(
            messaging_repo._table().put_item,
            Item={"pk": f"NETTOOLS#{seen}", "sk": f"NETTOOLS#{seen}", "ttl": int(time.time()) + 2 * TOOLS_SKEW_S},
            ConditionExpression="attribute_not_exists(pk)",
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        raise HTTPException(status_code=401, detail=_BAD_PROOF) from exc
    try:
        organizations = await clerk_admin.list_user_organizations(claims["sub"], limit=100, strict=True)
    except Exception as exc:  # noqa: BLE001 -- Clerk could not say: fail closed
        raise HTTPException(status_code=503, detail="Lightbulb could not check your workspace. Try again.") from exc
    # The workspace chosen at login (the proof's ``org``), while the person is still in it. A
    # proof without one (minted before it was carried) works only for a one-workspace person.
    if claims.get("org"):
        org = next((o for o in organizations if o.get("id") == claims["org"]), None)
        if org is None:
            raise HTTPException(
                status_code=403, detail="You are no longer in the workspace this computer signed in for."
            )
    elif len(organizations) == 1:
        org = organizations[0]
    elif free_ok and not organizations:
        return AuthContext(user_id=claims["sub"]), label
    else:
        raise HTTPException(
            status_code=409,
            detail="WORKSPACE_REQUIRED: this computer's person must be in exactly one Lightbulb workspace. "
            "Sign it in again with your workspace selected: lightbulb login",
        )
    return AuthContext(user_id=claims["sub"], org_id=org["id"], org_role=org.get("role")), label


async def computer_identity(request: Request) -> tuple[AuthContext, str]:
    """``computer_person`` for routes a free (no-workspace) signed-in computer may also call."""
    return await computer_person(request, free_ok=True)
