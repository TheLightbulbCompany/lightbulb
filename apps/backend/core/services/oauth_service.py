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

import asyncio
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final

import boto3
import httpx
from botocore.exceptions import ClientError

from core.config import settings
from core.encryption import get_fernet
from core.observability.metrics import put_metric
from core.repositories import billing_repo


logger = logging.getLogger(__name__)


# Constants borrowed from OpenAI's Codex CLI (see module docstring).
CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL: Final = "https://auth.openai.com"
USER_CODE_URL: Final = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
TOKEN_POLL_URL: Final = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL: Final = f"{AUTH_BASE_URL}/oauth/token"
VERIFICATION_URL: Final = f"{AUTH_BASE_URL}/codex/device"
EXCHANGE_REDIRECT_URI: Final = f"{AUTH_BASE_URL}/deviceauth/callback"

# The OpenCode coding harness authenticates with a SECOND, dedicated OAuth
# grant on the same ChatGPT account — NEVER the provider's chain. OpenAI
# refresh tokens rotate on use, so two holders of one chain race each other
# into ``refresh_token_reused`` (this burned the founders' container twice in
# June; see project_openclaw_oauth_auth_model). Independent grants are the
# supported multi-consumer model per OpenAI's own CI/CD guidance. Harness rows
# share the oauth-tokens table under a suffixed key: '#' cannot appear in a
# Clerk user id, so the namespaces cannot collide, and every legacy code path
# that reads the provider row by bare user_id is untouched.
HARNESS_ROW_SUFFIX: Final = "#harness"
PURPOSE_PROVIDER: Final = "provider"
PURPOSE_HARNESS: Final = "harness"


def _row_key(user_id: str, purpose: str) -> str:
    if purpose == PURPOSE_PROVIDER:
        return user_id
    if purpose == PURPOSE_HARNESS:
        return f"{user_id}{HARNESS_ROW_SUFFIX}"
    raise ValueError(f"Unknown OAuth purpose: {purpose!r}")


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


async def request_device_code(
    *, user_id: str, bound_owner_id: str, purpose: str = PURPOSE_PROVIDER
) -> DeviceCodeResponse:
    """Start a device-code session. Persists `device_auth_id` + `user_code`
    in DDB so the subsequent poll knows what to ask OpenAI about.

    ``purpose`` selects which token row the session writes: the provider row
    (bare ``user_id``, backs the container's model provider) or the harness
    row (``user_id#harness``, a second independent grant that backs the
    OpenCode coding harness). The two rows never share tokens — see
    ``HARNESS_ROW_SUFFIX``.

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
                "user_id": _row_key(user_id, purpose),
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
            existing = _table().get_item(Key={"user_id": _row_key(user_id, purpose)}).get("Item")
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


async def poll_device_code(*, user_id: str, purpose: str = PURPOSE_PROVIDER) -> DevicePollResult | object:
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
    row = _table().get_item(Key={"user_id": _row_key(user_id, purpose)}).get("Item")
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
    # refresh token, and for the OpenCode harness the difference between a
    # fresh process using the valid access token and it burning a rotation
    # at every start.
    expires_in = body.get("expires_in")
    if expires_in:
        token_fields["expires_at"] = int(time.time() * 1000) + int(expires_in) * 1000
    tokens_plain = json.dumps(token_fields).encode()
    encrypted = get_fernet().encrypt(tokens_plain)

    _table().update_item(
        Key={"user_id": _row_key(user_id, purpose)},
        UpdateExpression=(
            "SET #s = :ok, encrypted_tokens = :tok, account_id = :acc, bound_owner_id = :bound "
            "REMOVE device_auth_id, user_code, #i"
        ),
        ExpressionAttributeNames={"#s": "state", "#i": "interval"},
        ExpressionAttributeValues={
            ":ok": "active",
            ":tok": encrypted,
            ":acc": account_id or "",
            # The workspace this sign-in was completed for. A consumption read
            # that passes a different ``bound_to`` is rejected (see
            # get_decrypted_tokens) — one ChatGPT account ↔ one container.
            ":bound": bound_owner_id,
        },
    )
    return DevicePollResult(account_id=account_id, bound_owner_id=bound_owner_id)


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


async def get_decrypted_tokens(
    *, user_id: str, bound_to: str | None = None, purpose: str = PURPOSE_PROVIDER
) -> dict | None:
    """Decrypt and return the user's stored OAuth tokens. None if no row.

    When ``bound_to`` is given, the row is returned only if it belongs to
    that workspace (see ``_row_belongs_to``) — a row bound to a different
    owner reads as None so its tokens can't be reused by another container.
    """
    row = _table().get_item(Key={"user_id": _row_key(user_id, purpose)}).get("Item")
    if not row or row.get("state") != "active":
        return None
    if not _row_belongs_to(row, bound_to):
        return None
    plain = get_fernet().decrypt(bytes(row["encrypted_tokens"]))
    return json.loads(plain.decode())


async def has_active_oauth(
    *,
    user_id: str,
    bound_to: str | None = None,
    purpose: str = PURPOSE_PROVIDER,
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
    eventually-consistent miss right after /harness/poll's write would report
    "disconnected" while leaving both the row and the staged file behind.
    """
    row = _table().get_item(Key={"user_id": _row_key(user_id, purpose)}, ConsistentRead=consistent).get("Item")
    if not row or row.get("state") != "active":
        return False
    return _row_belongs_to(row, bound_to)


async def get_decrypted_tokens_consistent(
    *, user_id: str, bound_to: str | None = None, purpose: str = PURPOSE_PROVIDER
) -> dict | None:
    """Strongly-consistent variant of ``get_decrypted_tokens``.

    Used by ``GET /oauth/chatgpt/status`` so the wizard reads its own
    write from ``/poll`` without an eventual-consistency stutter. Other
    callers (gate evaluator, provisioning) keep using the eventually-
    consistent helper to avoid taxing every read path with strong reads.

    ``bound_to`` has the same cross-owner-rejection semantics as
    ``get_decrypted_tokens``.
    """
    row = _table().get_item(Key={"user_id": _row_key(user_id, purpose)}, ConsistentRead=True).get("Item")
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


async def revoke_user_oauth(*, user_id: str, purpose: str = PURPOSE_PROVIDER) -> None:
    """Delete the user's OAuth row. Caller is responsible for also
    deleting any pre-staged auth file on EFS (see workspace.py)."""
    _table().delete_item(Key={"user_id": _row_key(user_id, purpose)})


# ---------------------------------------------------------------------------
# Rotated-seed harvest
# ---------------------------------------------------------------------------
# The DDB row is a connect-time SEED: once the container imports it, the
# in-container OAuth manager rotates the refresh token in place and the DDB
# copy is spent (OpenAI refresh tokens are single-use). The live chain lives
# in the DEFAULT agent's SQLite auth store on container-LOCAL disk, reachable
# by the backend only through its Litestream S3 replica. A wipe/reprovision
# that destroys that replica and then re-stages the spent seed hands the new
# container a credential OpenAI rejects — while ``has_active_oauth`` (row
# existence) keeps reporting Connected. So the flows that destroy the replica
# harvest the rotated chain back into the row FIRST (see
# litestream_replicas.delete_replica_prefix), keeping the seed the freshest
# credential the backend has ever seen and making reprovision come back alive
# without a re-auth.

# Profile id OpenClaw builds from the staged credentials/oauth.json — provider
# key "openai", default profile (see workspace._OAUTH_PROVIDER_KEY).
_LIVE_PROFILE_ID: Final = "openai:default"
# The DEFAULT agent's auth store, relative to the owner's replica prefix. It
# is the single credential source — every other agent reads through it
# (workspace._DEFAULT_AGENT_DIR), so no other agent DB needs harvesting.
_MAIN_AGENT_DB_RELPATH: Final = "agents/main/agent/openclaw-agent.sqlite"


def _restore_replica_db(bucket: str, owner_id: str, dest_dir: str) -> str | None:
    """Blocking: ``litestream restore`` the default agent's DB replica.

    Returns the restored file's path, or None when the litestream binary is
    not installed (local dev/tests), no replica exists, or the restore fails.
    The binary is baked into the backend image pinned to the SAME version the
    fleet's containers replicate with (see apps/backend/Dockerfile).
    """
    binary = shutil.which("litestream")
    if not binary:
        logger.info("oauth seed harvest: litestream binary not installed — skipping")
        return None
    dest = os.path.join(dest_dir, "openclaw-agent.sqlite")
    replica_url = f"s3://{bucket}/{owner_id}/{_MAIN_AGENT_DB_RELPATH}"
    proc = subprocess.run(  # noqa: S603 — fixed argv, no shell
        [binary, "restore", "-if-replica-exists", "-o", dest, replica_url],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        logger.warning(
            "oauth seed harvest: litestream restore failed for %s: %s",
            owner_id,
            (proc.stderr or proc.stdout).strip()[:300],
        )
        return None
    # -if-replica-exists: no replica is a clean exit with no output file.
    return dest if os.path.exists(dest) else None


def _read_live_profile_from_db(db_path: str) -> dict | None:
    """Extract the rotated ``openai:default`` OAuth credential from a restored
    auth store DB (table ``auth_profile_store``, row ``store_key='primary'``,
    JSON cell ``store_json`` — the schema the fleet's OpenClaw writes).

    None on any miss — absent table/row/profile are all normal states for a
    non-chatgpt owner or a container that never imported a credential.
    """
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT store_json FROM auth_profile_store WHERE store_key = 'primary'").fetchone()
        finally:
            con.close()
    except sqlite3.Error as e:
        logger.info("oauth seed harvest: unreadable auth store — %s", e)
        return None
    if not row or not row[0]:
        return None
    try:
        store = json.loads(row[0])
    except (TypeError, ValueError):
        return None
    if not isinstance(store, dict):
        return None
    profile = (store.get("profiles") or {}).get(_LIVE_PROFILE_ID)
    if not isinstance(profile, dict) or profile.get("type") != "oauth":
        return None
    if not profile.get("access") or not profile.get("refresh"):
        return None
    return profile


def _live_profile_is_fresher(profile: Mapping[str, Any], stored: Mapping[str, Any]) -> bool:
    """Whether the harvested credential should replace the stored seed.

    Same refresh token = the container never rotated past the seed — nothing
    to write. Otherwise compare ``expires`` (ms epoch): both writers stamp it
    from OpenAI's expires_in at write time, so it orders the two credentials.
    A lagged replica must LOSE to a newer reconnect grant (the user re-ran the
    device-code flow while this container was down) — that grant's expires_at
    postdates anything the old chain ever wrote.
    """
    if profile.get("refresh") == stored.get("refresh_token"):
        return False
    return int(profile.get("expires") or 0) > int(stored.get("expires_at") or 0)


async def harvest_rotated_oauth_seed(*, owner_id: str) -> bool:
    """Persist the container's rotated ChatGPT tokens back to the DDB seed row.

    Called at the last moment the rotated chain is backend-reachable — right
    before the owner's Litestream replica prefix is destroyed. Callers should
    have drained the container first so litestream's final sync has landed;
    an undrained harvest still works but may read a slightly lagged chain,
    which the freshness check then discards.

    Best-effort by contract: True only when a fresher credential was written.
    Every miss (no replica, no profile, non-chatgpt owner, lost race to a
    fresh reconnect, unconfirmed task termination) is a logged no-op, not an
    error.
    """
    bucket = settings.LITESTREAM_BUCKET
    if not bucket:
        return False

    # The final Litestream sync only lands when the container EXITS — a
    # replica read while the task is still stopping can hold a chain that is
    # rotated past the seed yet already spent (its refresh token was used to
    # mint the one in the unsynced tail). Confirm actual task termination
    # (lastStatus STOPPED, not service counts — see
    # ecs_manager._tasks_fully_stopped_blocking) before trusting the replica.
    # Callers have normally drained already, so this returns on its first
    # poll; when termination cannot be confirmed, skip LOUDLY and leave the
    # seed alone — the freshness check would not catch a spent-but-newer
    # chain.
    from core.containers import get_ecs_manager

    if not await get_ecs_manager().wait_until_drained(owner_id, timeout_s=60.0):
        logger.error(
            "oauth seed harvest SKIPPED for %s — task termination unconfirmed, the replica "
            "may predate the final sync; leaving the DDB seed untouched",
            owner_id,
        )
        put_metric("oauth.seed_harvest_skipped_undrained")
        return False

    # Restore BEFORE touching DDB: the binary check is the cheap bail for
    # local/test environments, and a non-chatgpt owner's store simply has no
    # openai profile to extract.
    with tempfile.TemporaryDirectory(prefix="oauth-harvest-") as tmp:
        db_path = await asyncio.to_thread(_restore_replica_db, bucket, owner_id, tmp)
        if not db_path:
            return False
        profile = _read_live_profile_from_db(db_path)
    if not profile:
        return False

    token_owner = await resolve_token_owner(owner_id=owner_id)
    row = _table().get_item(Key={"user_id": _row_key(token_owner, PURPOSE_PROVIDER)}).get("Item")
    if not row or row.get("state") != "active" or not _row_belongs_to(row, owner_id):
        return False

    stored = json.loads(get_fernet().decrypt(bytes(row["encrypted_tokens"])).decode())
    if not _live_profile_is_fresher(profile, stored):
        return False

    # Overwrite only the credential triple; identity fields (id_token,
    # account_id, email) keep their connect-time values.
    merged = {
        **stored,
        "access_token": profile["access"],
        "refresh_token": profile["refresh"],
        "expires_at": int(profile.get("expires") or 0),
    }
    encrypted = get_fernet().encrypt(json.dumps(merged).encode())
    try:
        # CAS on the blob we read: a reconnect landing mid-harvest wrote a
        # brand-new grant that must not be clobbered by this old chain.
        _table().update_item(
            Key={"user_id": _row_key(token_owner, PURPOSE_PROVIDER)},
            UpdateExpression="SET encrypted_tokens = :tok",
            ConditionExpression="#s = :active AND encrypted_tokens = :old",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={":tok": encrypted, ":active": "active", ":old": row["encrypted_tokens"]},
        )
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            logger.info("oauth seed harvest: row changed mid-harvest for %s — keeping the newer write", owner_id)
            return False
        raise
    logger.info(
        "oauth_seed_harvested",
        extra={"owner_id": owner_id, "token_owner": token_owner, "expires_at": merged["expires_at"]},
    )
    put_metric("oauth.seed_harvested")
    return True
