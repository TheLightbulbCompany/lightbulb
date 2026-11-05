"""ChatGPT OAuth — device-code flow orchestration.

We use the public Codex CLI client_id `app_EMoamEEZ73f0CkXaXp7hrann`,
verified at github.com/openai/codex/blob/main/codex-rs/login/src/device_code_auth.rs.

OpenAI's actual `codex login --device-auth` flow:
  1. POST {base}/api/accounts/deviceauth/usercode {client_id} →
     returns {device_auth_id, user_code, interval}
  2. User opens {base}/codex/device in browser, signs in, enters user_code
  3. Backend polls POST {base}/api/accounts/deviceauth/token
     {device_auth_id, user_code} until it returns
     {authorization_code, code_challenge, code_verifier}
  4. Backend exchanges with POST {base}/oauth/token
     (grant_type=authorization_code, code, code_verifier,
      redirect_uri={base}/deviceauth/callback) → access + refresh tokens

base = https://auth.openai.com.

Spec §5.1 originally specified a generic OAuth 2.0 device-code flow at
/codex/device, which OpenAI deprecated for this client_id (~2026-04).
This module pins the actual endpoints the OpenAI CLI uses today.

auth.json shape on EFS is unchanged — only the orchestration changed.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import boto3
import httpx
from botocore.exceptions import ClientError

from core.config import settings
from core.encryption import get_fernet
from core.repositories import billing_repo
from core.services.super_agent_flags import flags_for


logger = logging.getLogger(__name__)


# Constants borrowed from OpenAI's Codex CLI (see module docstring).
CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL: Final = "https://auth.openai.com"
USER_CODE_URL: Final = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
TOKEN_POLL_URL: Final = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL: Final = f"{AUTH_BASE_URL}/oauth/token"
VERIFICATION_URL: Final = f"{AUTH_BASE_URL}/codex/device"
EXCHANGE_REDIRECT_URI: Final = f"{AUTH_BASE_URL}/deviceauth/callback"


class OAuthAlreadyActiveError(Exception):
    """Raised when start is called for a user who already has active tokens
    bound to THIS workspace. The workspace is genuinely already connected;
    callers should reuse or revoke before starting a new flow."""


class OAuthBoundElsewhereError(Exception):
    """Raised when start is called for a workspace while the user's active
    tokens are bound to a DIFFERENT workspace. This workspace is NOT connected,
    so it must not be reported as already-active — the caller surfaces a real
    error telling the user to disconnect at the other workspace first."""


class OAuthExchangeFailedError(Exception):
    """Raised when OpenAI rejects the code exchange or device-code call."""


@dataclass(frozen=True)
class DeviceCodeResponse:
    """User-facing fields shown in our UI to drive completion.

    The backend-side device_auth_id is intentionally NOT exposed —
    it stays in DDB and is used by `poll_device_code` server-side."""

    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class DevicePollResult:
    """Returned on successful poll. Tokens persisted internally.

    ``bound_owner_id`` is the workspace the session was authorized for at
    /start (read off the pending row), so the router learns what the session
    was actually for — not what the polling caller's current context is."""

    account_id: str | None
    bound_owner_id: str


# Sentinel returned while OpenAI says "still pending".
DevicePollPending: Final = object()


def _table():
    table_name = os.environ.get("OAUTH_TOKENS_TABLE") or settings.OAUTH_TOKENS_TABLE
    if not table_name:
        raise RuntimeError("OAUTH_TOKENS_TABLE is empty — backend is misconfigured.")
    return boto3.resource("dynamodb", region_name=settings.AWS_REGION).Table(table_name)


async def request_device_code(*, user_id: str, bound_owner_id: str) -> DeviceCodeResponse:
    """Start a device-code session. Persists `device_auth_id` + `user_code`
    in DDB so the subsequent poll knows what to ask OpenAI about.

    ``bound_owner_id`` is the workspace this sign-in will be bound to. It is
    fixed HERE, at session start, where the owner gate runs — and stamped onto
    the PENDING row. Completion (poll_device_code) reads it back off that row
    rather than re-deriving it from the polling caller's context, so switching
    Clerk contexts between start and poll cannot re-target the binding (e.g. a
    member starting a personal, ungated session and then polling in org context
    to hijack the org's connector).

    Raises OAuthAlreadyActiveError if the user already has active tokens bound
    to THIS workspace (genuinely already-connected). Raises
    OAuthBoundElsewhereError if the active tokens are bound to a DIFFERENT
    workspace — this workspace is not connected, so the caller must surface a
    real error rather than treating it as completion.
    """
    async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
        resp = await client.post(
            USER_CODE_URL,
            headers={"Content-Type": "application/json"},
            content=json.dumps({"client_id": CLIENT_ID}),
        )
    if resp.status_code >= 400:
        body_preview = resp.text[:300] if resp.text else ""
        logger.warning(
            "Device-code usercode request returned %d for user %s: %s",
            resp.status_code,
            user_id,
            body_preview,
        )
        raise OAuthExchangeFailedError(f"OpenAI device-code usercode request failed: {resp.status_code}")

    body = resp.json()
    device_auth_id = body.get("device_auth_id")
    user_code = body.get("user_code") or body.get("usercode")
    interval = int(body.get("interval", 5))
    if not device_auth_id or not user_code:
        logger.warning("Usercode response missing fields for user %s: %s", user_id, body.keys())
        raise OAuthExchangeFailedError("OpenAI usercode response missing required fields")

    try:
        _table().put_item(
            Item={
                "user_id": user_id,
                "state": "pending",
                "device_auth_id": device_auth_id,
                "user_code": user_code,
                "interval": interval,
                # The workspace this session is authorized for, fixed at start
                # (where the owner gate runs). poll_device_code reads it back
                # off this row so completion cannot re-target the binding.
                "bound_owner_id": bound_owner_id,
            },
            ConditionExpression="attribute_not_exists(user_id) OR #s <> :active",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":active": "active"},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            # The put failed because an active row exists. Whether that means
            # "already connected here" or "connected for a different workspace"
            # depends on the row's binding: an active row bound to THIS
            # workspace is genuinely already-connected (the wizard may treat
            # this as completion), but one bound elsewhere leaves the current
            # workspace unconnected and must surface a distinct error so the
            # wizard doesn't advance with no usable tokens.
            existing = _table().get_item(Key={"user_id": user_id}).get("Item")
            if existing and existing.get("state") == "active" and not _row_belongs_to(existing, bound_owner_id):
                raise OAuthBoundElsewhereError(
                    "This ChatGPT account is connected to a different workspace. "
                    "Disconnect it there before connecting here."
                ) from None
            raise OAuthAlreadyActiveError(
                f"User {user_id} already has active OAuth tokens — revoke before starting again"
            ) from None
        raise

    return DeviceCodeResponse(
        user_code=user_code,
        verification_uri=VERIFICATION_URL,
        # OpenAI's device-code spec gives a 15-minute window per the CLI
        # source. Body doesn't echo this back so we hardcode the same
        # value the CLI prints to the user.
        expires_in=15 * 60,
        interval=interval,
    )


async def poll_device_code(*, user_id: str) -> DevicePollResult | object:
    """Poll OpenAI's deviceauth/token endpoint for this user's session.

    Returns DevicePollPending while the user hasn't completed sign-in
    (OpenAI returns 403/404 in that case). Returns DevicePollResult on
    success after Fernet-encrypting access+refresh tokens into DDB.
    Raises OAuthExchangeFailedError on terminal errors.

    The workspace this sign-in is bound to is NOT taken from the polling
    caller's context — it is read off the pending row's ``bound_owner_id``,
    which /start fixed (under the owner gate). Completion therefore cannot
    re-target a session: a member who started a personal, ungated session
    cannot poll in org context to bind the org's connector to themselves.

    The binding is stamped onto the completed row (and surfaced on
    DevicePollResult) so a consumption read can reject reuse by a *different*
    workspace. The oauth-tokens table keys one row per Clerk user, so without
    this a human who connected ChatGPT for an org could have the same row
    silently staged into a second, personal container — two containers sharing
    one ChatGPT account, and a disconnect in either revoking the other.
    """
    row = _table().get_item(Key={"user_id": user_id}).get("Item")
    if not row or row.get("state") != "pending":
        raise OAuthExchangeFailedError(f"No pending device-code session for user {user_id}")
    # The binding was fixed at /start. Fall back to user_id (personal
    # semantics) for any legacy pending row written before this field existed
    # — an in-flight session at deploy time completes cleanly instead of
    # erroring or binding to an empty owner.
    bound_owner_id = row.get("bound_owner_id") or user_id
    # Legacy pending rows (from the pre-Codex-CLI flow) store `device_code`
    # instead of `device_auth_id`. Same for any partially-written row that
    # doesn't have user_code. Treat both as stale — the new flow can't
    # resume them. Caller surfaces this as a clean error; user clicks
    # "Try again" which calls /start, which overwrites the pending row
    # with a fresh device_auth_id (the put_item ConditionExpression only
    # blocks state=active, not state=pending).
    device_auth_id = row.get("device_auth_id")
    user_code = row.get("user_code")
    if not device_auth_id or not user_code:
        raise OAuthExchangeFailedError(
            f"Stale OAuth session for user {user_id} (legacy row format) — click Connect ChatGPT again to restart"
        )

    # Step 1: ask OpenAI's deviceauth/token whether the user has
    # completed sign-in. While pending, OpenAI returns 403 or 404 per
    # the CLI source.
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        resp = await client.post(
            TOKEN_POLL_URL,
            headers={"Content-Type": "application/json"},
            content=json.dumps(
                {
                    "device_auth_id": device_auth_id,
                    "user_code": user_code,
                }
            ),
        )

    if resp.status_code in (403, 404):
        return DevicePollPending
    if resp.status_code >= 400:
        body_preview = resp.text[:300] if resp.text else ""
        logger.warning(
            "Device-code token poll for user %s returned %d: %s",
            user_id,
            resp.status_code,
            body_preview,
        )
        raise OAuthExchangeFailedError(f"OpenAI device-code token poll failed: {resp.status_code}")

    code_resp = resp.json()
    auth_code = code_resp.get("authorization_code")
    verifier = code_resp.get("code_verifier")
    if not auth_code or not verifier:
        logger.warning("Token poll response missing fields for user %s: %s", user_id, code_resp.keys())
        raise OAuthExchangeFailedError("OpenAI token poll response missing required fields")

    # Step 2: exchange the authorization_code for access+refresh tokens
    # against the standard OAuth /oauth/token endpoint, using the
    # PKCE verifier OpenAI just gave us.
    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        token_resp = await client.post(
            OAUTH_TOKEN_URL,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "code": auth_code,
                "code_verifier": verifier,
                "redirect_uri": EXCHANGE_REDIRECT_URI,
            },
        )

    if token_resp.status_code >= 400:
        body_preview = token_resp.text[:300] if token_resp.text else ""
        logger.warning(
            "OAuth token exchange for user %s returned %d: %s",
            user_id,
            token_resp.status_code,
            body_preview,
        )
        raise OAuthExchangeFailedError(f"OpenAI token exchange failed: {token_resp.status_code}")

    body = token_resp.json()
    access_token = body.get("access_token")
    refresh_token = body.get("refresh_token")
    if not access_token or not refresh_token:
        logger.warning("Token response missing fields for user %s: %s", user_id, body.keys())
        raise OAuthExchangeFailedError("OpenAI token response missing required fields")

    # account_id can be in the body OR encoded in the JWT id_token. Try
    # both — pi-mono and the CLI both decode the JWT for the
    # chatgpt_account_id claim under https://api.openai.com/auth.
    account_id = body.get("account_id") or _extract_account_id(access_token)

    token_fields: dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "id_token": body.get("id_token"),
        "account_id": account_id,
    }
    # Persist the access token's real expiry (ms epoch). Staged credentials
    # that omit it are treated as already-expired by their consumers, which
    # forces a refresh on first use — needless rotation of a single-use-ish
    # refresh token.
    expires_in = body.get("expires_in")
    if expires_in:
        token_fields["expires_at"] = int(time.time() * 1000) + int(expires_in) * 1000
    # Super-agent (spec 13 T1): a new connect in a workspace with the switch on is backend-custodied
    # from the start -- the backend alone refreshes it (``version`` is the CAS counter). Switch off:
    # the row is exactly today's (no custody attribute = the VM rotates it).
    backend = await flags_for(bound_owner_id)
    if backend:
        token_fields["expires_at"] = _expires_at_ms(access_token) or token_fields.get("expires_at") or 0
    tokens_plain = json.dumps(token_fields).encode()
    encrypted = get_fernet().encrypt(tokens_plain)
    values: dict[str, Any] = {
        ":ok": "active",
        ":tok": encrypted,
        ":acc": account_id or "",
        # The workspace this sign-in was completed for. A consumption read
        # that passes a different ``bound_to`` is rejected (see
        # get_decrypted_tokens) — one ChatGPT account ↔ one container.
        ":bound": bound_owner_id,
    }
    if backend:
        values.update({":backend": "backend", ":one": 1, ":exp": token_fields["expires_at"]})

    _table().update_item(
        Key={"user_id": user_id},
        UpdateExpression=(
            ("SET custody = :backend, version = :one, expires_at = :exp, " if backend else "SET ")
            + "#s = :ok, encrypted_tokens = :tok, account_id = :acc, bound_owner_id = :bound "
            "REMOVE device_auth_id, user_code, #i"
        ),
        ExpressionAttributeNames={"#s": "state", "#i": "interval"},
        ExpressionAttributeValues=values,
    )
    return DevicePollResult(account_id=account_id, bound_owner_id=bound_owner_id)


def _jwt_claims(token: str) -> dict:
    """The unverified claims of a JWT (``{}`` when it is not one). OpenAI just issued it to us
    over TLS; we only read opaque fields out of it."""
    import base64

    try:
        payload = token.split(".")[1]
        claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims if isinstance(claims, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _expires_at_ms(access_token: str) -> int | None:
    exp = _jwt_claims(access_token).get("exp")
    return int(exp) * 1000 if isinstance(exp, int | float) else None


def _extract_account_id(access_token: str) -> str | None:
    """Decode the JWT and pull chatgpt_account_id from the auth claim.

    Mirrors the CLI's logic. JWT validation isn't done here — OpenAI
    just issued this token to us via TLS, so we trust it for the
    purpose of pulling out an opaque identifier.
    """
    import base64

    try:
        parts = access_token.split(".")
        if len(parts) != 3:
            return None
        # Pad payload to a multiple of 4 for base64 decode
        payload = parts[1] + "=" * (-len(parts[1]) % 4)
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
        claims = json.loads(decoded)
        auth = claims.get("https://api.openai.com/auth") or {}
        account_id = auth.get("chatgpt_account_id")
        return str(account_id) if account_id else None
    except Exception:
        logger.debug("Failed to extract account_id from JWT", exc_info=True)
        return None


def _row_belongs_to(row: Mapping[str, Any], bound_to: str | None) -> bool:
    """Whether ``bound_to`` is allowed to consume this token row.

    The oauth-tokens table keys one row per Clerk user, but a single human
    can own a personal workspace AND be the connector for an org workspace.
    ``bound_owner_id`` records which workspace claimed the row at sign-in.

    - ``bound_to is None`` — caller didn't specify a workspace; keep the
      pre-binding behavior (no check). Production consumption reads always
      pass ``bound_to``; this branch is for incidental/admin callers.
    - row has NO ``bound_owner_id`` (legacy, pre-binding) — treated as
      implicitly bound to its own ``user_id``. Legacy rows predate
      orgs-on-chatgpt entirely, so they can only legitimately back the
      personal workspace of the human who created them. A personal owner's
      ``bound_to`` equals the row's ``user_id``, so no existing personal user
      is affected; but an org consuming an unbound row (``bound_to=org_…``)
      now requires a fresh, org-bound sign-in instead of silently sharing the
      human's personal ChatGPT account.
    - row's ``bound_owner_id`` matches ``bound_to`` — the row was created
      for this workspace; admit it.
    - row's ``bound_owner_id`` differs — a DIFFERENT workspace claimed the
      sign-in; reject (treat as no tokens).
    """
    if bound_to is None:
        return True
    recorded = row.get("bound_owner_id") or row.get("user_id")
    return recorded == bound_to


async def get_decrypted_tokens(*, user_id: str, bound_to: str | None = None) -> dict | None:
    """Decrypt and return the user's stored OAuth tokens. None if no row.

    When ``bound_to`` is given, the row is returned only if it belongs to
    that workspace (see ``_row_belongs_to``) — a row bound to a different
    owner reads as None so its tokens can't be reused by another container.
    """
    row = _table().get_item(Key={"user_id": user_id}).get("Item")
    if not row or row.get("state") != "active":
        return None
    if not _row_belongs_to(row, bound_to):
        return None
    plain = get_fernet().decrypt(bytes(row["encrypted_tokens"]))
    return json.loads(plain.decode())


async def get_token_row(user_id: str, *, consistent: bool = True, bound_to: str | None = None) -> dict | None:
    """The raw token row (``custody``, ``version``, ``expires_at``, ...) with ``tokens`` decrypted
    in place of ``encrypted_tokens``. None when there is no active row for ``bound_to``."""
    row = _table().get_item(Key={"user_id": user_id}, ConsistentRead=consistent).get("Item")
    if not row or row.get("state") != "active" or not _row_belongs_to(row, bound_to):
        return None
    out = {k: v for k, v in row.items() if k != "encrypted_tokens"}
    out["tokens"] = json.loads(get_fernet().decrypt(bytes(row["encrypted_tokens"])).decode())
    for key in ("version", "expires_at"):
        if key in out:
            out[key] = int(out[key])
    return out


async def cas_update_tokens(user_id: str, *, expected_version: int, tokens: dict) -> bool:
    """Write refreshed tokens iff the row is still active, backend-custodied and at
    ``expected_version`` (the broker's conditional write). Bumps ``version``. False when the
    condition fails: another refresh won, or the row was revoked (it is never resurrected)."""
    from datetime import UTC, datetime

    expires_at = _expires_at_ms(tokens.get("access_token") or "") or int(tokens.get("expires_at") or 0)
    tokens = {**tokens, "expires_at": expires_at}
    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET encrypted_tokens = :tok, version = :next, expires_at = :exp, last_refresh = :now",
            ConditionExpression="version = :v AND custody = :backend AND #s = :active",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":tok": get_fernet().encrypt(json.dumps(tokens).encode()),
                ":next": expected_version + 1,
                ":exp": expires_at,
                ":now": datetime.now(UTC).isoformat(),
                ":v": expected_version,
                ":backend": "backend",
                ":active": "active",
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def take_custody(user_id: str, *, tokens: dict) -> bool:
    """Cutover step 4 (spec 13 §6.1): the row becomes the backend's, at ``version = 1``, holding
    ``tokens`` (the VM's newest pair). Conditional on the row being active and VM-custodied
    (no ``custody`` attribute, or ``actor``). False when the condition fails."""
    from datetime import UTC, datetime

    expires_at = _expires_at_ms(tokens.get("access_token") or "") or int(tokens.get("expires_at") or 0)
    tokens = {**tokens, "expires_at": expires_at}
    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression=(
                "SET encrypted_tokens = :tok, custody = :backend, version = :one, expires_at = :exp, last_refresh = :now"
            ),
            ConditionExpression="(attribute_not_exists(custody) OR custody = :actor) AND #s = :active",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":tok": get_fernet().encrypt(json.dumps(tokens).encode()),
                ":backend": "backend",
                ":actor": "actor",
                ":one": 1,
                ":exp": expires_at,
                ":now": datetime.now(UTC).isoformat(),
                ":active": "active",
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def set_custody_actor(user_id: str, *, expected_version: int) -> bool:
    """Rollback (or a raced cutover's undo): the VM owns the grant again. Conditional on the
    version just read, so a refresh that landed meanwhile is never overruled."""
    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET custody = :actor",
            ConditionExpression="version = :v AND #s = :active",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":actor": "actor", ":v": expected_version, ":active": "active"},
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def token_row_needs_reconnect(user_id: str, *, bound_to: str | None = None) -> bool:
    """Whether this workspace's backend-custodied grant was refused by OpenAI (the broker marked
    it): the vend then answers "reconnect", not "not brokered"."""
    row = _table().get_item(Key={"user_id": user_id}, ConsistentRead=True).get("Item")
    return bool(
        row
        and row.get("custody") == "backend"
        and row.get("state") == "reconnect_required"
        and _row_belongs_to(row, bound_to)
    )


async def mark_reconnect_required(user_id: str, *, expected_version: int) -> bool:
    """The broker's terminal refusal (``invalid_grant``, ``refresh_token_reused``, 401/403): the
    grant is dead. Conditional on the version the broker refreshed from, so a reconnect that
    landed meanwhile is never marked."""
    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression="SET #s = :rr",
            ConditionExpression="version = :v AND custody = :backend AND #s = :active",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":rr": "reconnect_required",
                ":v": expected_version,
                ":backend": "backend",
                ":active": "active",
            },
        )
        return True
    except ClientError as exc:
        if exc.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        raise


async def has_active_oauth(
    *,
    user_id: str,
    bound_to: str | None = None,
    consistent: bool = False,
) -> bool:
    """Whether an ACTIVE OAuth row exists for ``user_id`` bound to ``bound_to`` —
    a connection-state check that does NOT decrypt the token blob.

    Callers that need the actual tokens use ``get_decrypted_tokens``; the gate
    only needs to know a connection exists, so it should not lean on the stored
    secret (which is a stale bootstrap seed once the container owns the live,
    rotating token). Keeps the connected-flag decoupled from the secret.

    ``consistent=True`` forces a strongly-consistent read — required by
    callers whose answer gates a DELETE (the disconnect binding probes): an
    eventually-consistent miss right after /poll's write would report
    "disconnected" while leaving both the row and the staged file behind.
    """
    row = _table().get_item(Key={"user_id": user_id}, ConsistentRead=consistent).get("Item")
    if not row or row.get("state") != "active":
        return False
    return _row_belongs_to(row, bound_to)


async def get_decrypted_tokens_consistent(*, user_id: str, bound_to: str | None = None) -> dict | None:
    """Strongly-consistent variant of ``get_decrypted_tokens``.

    Used by ``GET /oauth/chatgpt/status`` so the wizard reads its own
    write from ``/poll`` without an eventual-consistency stutter. Other
    callers (gate evaluator, provisioning) keep using the eventually-
    consistent helper to avoid taxing every read path with strong reads.

    ``bound_to`` has the same cross-owner-rejection semantics as
    ``get_decrypted_tokens``.
    """
    row = _table().get_item(Key={"user_id": user_id}, ConsistentRead=True).get("Item")
    if not row or row.get("state") != "active":
        return None
    if not _row_belongs_to(row, bound_to):
        return None
    plain = get_fernet().decrypt(bytes(row["encrypted_tokens"]))
    return json.loads(plain.decode())


def connector_from_account(account: dict | None, owner_id: str) -> str:
    """The Clerk user whose ChatGPT OAuth tokens back this owner's container.

    Personal owners ARE their own connector (owner_id == Clerk user_id), so
    the fallback is correct for them and for legacy rows that predate
    ``oauth_connector_user_id``. For orgs, the billing row records the
    workspace owner who completed the device-code flow. Callers that already
    hold the billing row use this directly — the provision gate runs on
    every ~3s status poll and must not burn a second read of a row it has
    in scope; callers without the row use ``resolve_token_owner``.
    """
    return (account or {}).get("oauth_connector_user_id") or owner_id


async def resolve_token_owner(*, owner_id: str) -> str:
    """``connector_from_account`` for callers that don't hold the billing row."""
    row = await billing_repo.get_by_owner_id(owner_id)
    return connector_from_account(row, owner_id)


def caller_may_manage_connector(account: dict | None, caller_user_id: str) -> bool:
    """Whether this caller may bind/rebind the owner's ChatGPT connection.

    The connection belongs to the workspace owner who connected it
    (``oauth_connector_user_id``): once recorded, only that human may
    manage or re-select it. An unclaimed row (no recorded connector —
    first connect, or a legacy row predating the field) admits any
    caller; the surrounding endpoint/service decides who reaches this
    check (org-admin gates etc.). Deliberately NOT built on
    ``connector_from_account``: its owner_id fallback would make an
    unclaimed org row look owned by the org id and wrongly reject the
    first claimer.
    """
    recorded = (account or {}).get("oauth_connector_user_id")
    return not recorded or recorded == caller_user_id


async def maybe_clear_phantom_connector(owner_id: str, account: dict | None) -> bool:
    """Clear a recorded connector that names no existing Clerk user.

    Phantom-connector recovery (issue #2864 follow-up): checkout metadata
    once recorded an unverified ``clerk_user_id`` onto billing rows, and the
    id can dangle — no such Clerk user, ever. The provision gate then waits
    on a ghost (permanent ``oauth_required``) while ``/start`` and the
    provider switch 403 every real human, and the set-once claim can never
    repoint. This lets those two already-admin-gated doors self-heal: verify
    the recorded id via Clerk, and CAS-remove it when Clerk 404s so the
    caller's normal first-connect flow proceeds as unclaimed.

    Fail-CLOSED on uncertainty: a Clerk blip, missing key, or any non-404
    returns False without touching the row — a legitimate owner keeps their
    claim. The CAS additionally refuses when the field already changed under
    us, so a concurrent real claim is never clobbered. Returns True only
    when this call cleared the phantom.
    """
    from core.observability.metrics import put_metric
    from core.services import clerk_admin

    recorded = (account or {}).get("oauth_connector_user_id")
    if not recorded:
        return False
    try:
        user = await clerk_admin.get_user_strict(recorded)
    except clerk_admin.ClerkLookupError:
        logger.warning(
            "phantom-connector check inconclusive for owner %s — leaving connector in place",
            owner_id,
        )
        return False
    if user is not None:
        return False
    cleared = await billing_repo.clear_phantom_oauth_connector(owner_id, expected_user_id=recorded)
    if cleared:
        put_metric("billing.oauth_connector.phantom_cleared")
        logger.info("cleared phantom oauth connector %s for owner %s", recorded, owner_id)
    return cleared


async def revoke_user_oauth(*, user_id: str) -> None:
    """Delete the user's OAuth row. The staged profile copy is the caller's job
    (``hermes_profiles.unstage_codex_auth``)."""
    _table().delete_item(Key={"user_id": user_id})
