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


logger = logging.getLogger(__name__)


# Constants borrowed from OpenAI's Codex CLI (see module docstring).
CLIENT_ID: Final = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTH_BASE_URL: Final = "https://auth.openai.com"
USER_CODE_URL: Final = f"{AUTH_BASE_URL}/api/accounts/deviceauth/usercode"
TOKEN_POLL_URL: Final = f"{AUTH_BASE_URL}/api/accounts/deviceauth/token"
OAUTH_TOKEN_URL: Final = f"{AUTH_BASE_URL}/oauth/token"
VERIFICATION_URL: Final = f"{AUTH_BASE_URL}/codex/device"
EXCHANGE_REDIRECT_URI: Final = f"{AUTH_BASE_URL}/deviceauth/callback"


# ---------------------------------------------------------------------------
# Credential health — "is this connection still USABLE?", not just "does a row
# exist?"
#
# WHY THERE IS NO ACTIVE PROBE HERE. The obvious design is for the backend to
# validate a stored credential by spending it against ``/oauth/token`` with
# ``grant_type=refresh_token``. That is NOT safe for this provider and must
# not be added:
#
#   * OpenAI's Codex refresh tokens are ONE-TIME and ROTATE on every use.
#     Reusing a spent one returns ``refresh_token_reused`` and (per standard
#     reuse detection) burns the whole token family. This is not theoretical —
#     it is the 2026-06-23 incident that forced the per-agent auth fan-out to
#     collapse into a single shared credential file (see
#     ``workspace.pre_stage_auth_profile_store`` and its pinning test
#     ``test_ecs_manager.test_pre_stage_oauth_does_not_clear_default_agent_row``).
#   * The blob in this table is only a BOOTSTRAP SEED. Once a container has
#     imported it, the LIVE token lives in the container's auth store and has
#     rotated away from our copy. Probing with our stale seed would therefore
#     report "dead" for a perfectly healthy owner (a fail-CLOSED false
#     positive) *and* revoke the family that was working — i.e. the probe
#     would CAUSE the outage it is meant to detect.
#
# So health is OBSERVED, never polled: the only party that can legitimately
# spend the credential is the container, and it already reports the verdict on
# a failed run (``chat`` terminal ``errorMessage``). ``mark_oauth_invalid``
# records that verdict; ``has_active_oauth`` consumes it. Zero calls to OpenAI,
# zero per-poll cost.
# ---------------------------------------------------------------------------

# Value stamped on ``oauth_health`` once a definitive provider-side rejection
# has been observed. Absence of the attribute means "no bad news" — every
# legacy row and every freshly minted row reads as usable.
OAUTH_HEALTH_INVALID: Final = "invalid"

# TIER A — signals that are conclusive on their own.
#
# Two kinds, and both are somebody else's considered verdict rather than our
# guess:
#
#   * "isn't accepting your saved login" is OpenClaw's OWN ``auth_permanent``
#     copy (``src/agents/auth-profiles/failure-copy.ts``). It is emitted only
#     after upstream's ``classifyOAuthRefreshFailureReason`` has already
#     separated permanent rejections from transient ones — a 5xx refresh
#     failure gets the SOFTER ``auth`` copy instead and never reaches this
#     phrase. Keying on it inherits that classification for free. This is the
#     exact string the prod failure carried (2026-07-24).
#   * OpenAI's terminal refresh-token error codes, which appear only in its own
#     error envelope (``{"error":{"code":"refresh_token_reused"}}``) and each
#     mean the credential can never work again without a fresh sign-in.
#
# NOTE the OpenAI codes arrive on **400 OR 401** (openai/codex PR #24830 moved
# ``refresh_token_reused`` to 400 on some paths), so branching on HTTP status
# alone would be wrong. NOTE ALSO that OpenAI does not emit the RFC 6749
# ``invalid_grant`` for this flow — but OpenClaw's own permanent-reason set
# does list it, so it stays in Tier B as defence in depth.
_TERMINAL_OAUTH_SIGNALS: Final[tuple[str, ...]] = (
    "isn't accepting your saved login",
    "is not accepting your saved login",
    "refresh_token_reused",
    "refresh_token_expired",
    "refresh_token_invalidated",
    "token_invalidated",
)

# TIER B — phrases that identify the error as being about the SAVED CREDENTIAL
# rather than the model call, the tools, or the agent. From the raw refresh
# failure upstream wraps: "OAuth token refresh failed for openai: OpenAI Codex
# token refresh failed (401): {…".
_AUTH_CREDENTIAL_PHRASES: Final[tuple[str, ...]] = (
    "oauth token refresh failed",
    "token refresh failed",
    "failed to refresh token",
    "refresh token",
    "models auth login",
)

# Markers that make a Tier-B phrase a DEFINITIVE rejection — the provider
# actively refused the credential, as opposed to failing to answer. Mirrors the
# keyword set upstream's ``oauth-refresh-failure.ts`` treats as permanent.
_DEFINITIVE_REJECTION_MARKERS: Final[tuple[str, ...]] = (
    "401",
    "unauthorized",
    "invalid_grant",
    "invalid_client",
    "invalid refresh token",
    "sign in again",
    "revoked",
)

# Markers that make the outcome AMBIGUOUS. A message carrying any of these is
# never treated as a dead credential even if it also matches the phrases above
# — OpenAI having a blip must not cost a working owner their onboarding. This
# is the fail-open half of the classifier, and it deliberately wins ties.
_AMBIGUOUS_FAILURE_MARKERS: Final[tuple[str, ...]] = (
    "500",
    "502",
    "503",
    "504",
    "429",
    "rate limit",
    "rate_limit",
    "timeout",
    "timed out",
    "temporarily unavailable",
    "service unavailable",
    "connection reset",
    "connection refused",
    "econnreset",
    "econnrefused",
    "etimedout",
    "network",
)

# Stored ``oauth_health_reason`` is for operators reading the row, not for
# replay — keep it bounded so a pathological error string can't bloat the item.
_HEALTH_REASON_MAX_CHARS: Final = 500


def is_provider_auth_failure(message: str | None) -> bool:
    """Whether a run's terminal error message is a DEFINITIVE rejection of the
    owner's saved ChatGPT credential.

    Deliberately narrow, and asymmetric on purpose — mis-classifying a healthy
    owner as needing re-auth is the expensive error, so anything we cannot
    positively call dead reads as alive.

    Two ways to qualify:

      * **Tier A** — the message carries a conclusive signal
        (``_TERMINAL_OAUTH_SIGNALS``): OpenClaw's own ``auth_permanent`` copy,
        or one of OpenAI's terminal refresh-token error codes (on a 400 or a
        401). Both are already-classified verdicts, so they stand alone.
      * **Tier B** — the message is about the saved credential
        (``_AUTH_CREDENTIAL_PHRASES``) AND carries a definitive rejection
        marker (``_DEFINITIVE_REJECTION_MARKERS``) AND carries NO ambiguity
        marker (``_AMBIGUOUS_FAILURE_MARKERS`` — 5xx, 429, timeouts,
        connection errors).

    The ambiguity veto wins ties in Tier B: "OAuth token refresh failed for
    openai … (503)" is an OpenAI outage, not a dead login, and returns False.

    Provider scoping is the CALLER's job, not this function's: the same string
    shape can come from a BYO-key OpenAI owner. Callers must confirm
    ``provider_choice == "chatgpt_oauth"`` before acting on a True.
    """
    if not message:
        return False
    text = message.lower()
    if any(signal in text for signal in _TERMINAL_OAUTH_SIGNALS):
        return True
    if not any(phrase in text for phrase in _AUTH_CREDENTIAL_PHRASES):
        return False
    if any(marker in text for marker in _AMBIGUOUS_FAILURE_MARKERS):
        return False
    return any(marker in text for marker in _DEFINITIVE_REJECTION_MARKERS)


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

    tokens_plain = json.dumps(
        {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "id_token": body.get("id_token"),
            "account_id": account_id,
        }
    ).encode()
    encrypted = get_fernet().encrypt(tokens_plain)

    _table().update_item(
        Key={"user_id": user_id},
        UpdateExpression=(
            "SET #s = :ok, encrypted_tokens = :tok, account_id = :acc, bound_owner_id = :bound "
            # A completed sign-in is valid BY CONSTRUCTION — OpenAI just issued
            # these tokens. Drop any ``invalid`` verdict a previous, now-replaced
            # credential earned, otherwise the owner would re-connect
            # successfully and still be held at the oauth_required gate.
            "REMOVE device_auth_id, user_code, #i, oauth_health, oauth_health_reason, oauth_health_at"
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


async def has_active_oauth(*, user_id: str, bound_to: str | None = None) -> bool:
    """Whether ``user_id`` has a USABLE ChatGPT connection bound to ``bound_to``
    — a connection-state check that does NOT decrypt the token blob.

    Three conditions, all answered from ONE GetItem (this runs on the ~3s
    ``/container/status`` poll path via the provision gate, so it must not grow
    a second read or any network call):

      1. an ``active`` row exists;
      2. it belongs to this workspace (``_row_belongs_to``);
      3. it has not been stamped ``oauth_health = "invalid"`` by an observed,
         definitive provider rejection (``mark_oauth_invalid``).

    Condition 3 is the fix for the class of bug where a row EXISTS but its
    credential is dead: presence used to stand in for validity, so an owner
    with a revoked/expired ChatGPT login sailed through the provision gate and
    then had every single agent run fail with an unexplained 401. Now the gate
    fires ``oauth_required`` and the owner gets the "Connect ChatGPT" card.

    Absence of ``oauth_health`` means "no bad news", so legacy rows and freshly
    minted rows are unaffected. See the module-level health section for why the
    verdict is OBSERVED from the container rather than probed from here.

    Callers that need the actual tokens use ``get_decrypted_tokens``; the gate
    only needs to know a usable connection exists, so it should not lean on the
    stored secret (which is a stale bootstrap seed once the container owns the
    live, rotating token). Keeps the connected-flag decoupled from the secret.
    """
    row = _table().get_item(Key={"user_id": user_id}).get("Item")
    if not row or row.get("state") != "active":
        return False
    if not _row_belongs_to(row, bound_to):
        return False
    return row.get("oauth_health") != OAUTH_HEALTH_INVALID


async def mark_oauth_invalid(*, user_id: str, bound_to: str | None, reason: str) -> bool:
    """Record that ``user_id``'s ChatGPT credential was DEFINITIVELY rejected,
    so the next provision-gate evaluation fires ``oauth_required``.

    This is the only writer of ``oauth_health``. It closes the loop no up-front
    check can: a token that dies AFTER onboarding (revoked in ChatGPT settings,
    refresh window elapsed) produces a failed run, and this turns that failure
    into the "Connect ChatGPT" card on the very next ~3s poll instead of an
    indefinite spinner.

    ``user_id`` is the CONNECTOR (the human who completed the device-code flow
    — ``connector_from_account``), never the polling user, and ``bound_to`` is
    the workspace whose container consumed the credential. Both are checked
    before writing, so a run failure in workspace A can never invalidate a
    credential that belongs to workspace B — the same binding rule
    ``has_active_oauth`` reads with.

    Returns True only if a stamp was actually written. False (never an
    exception) when there is no row, the row is not ``active``, the row belongs
    to a different workspace, or a concurrent re-connect flipped it — callers
    are best-effort observers on a hot path and must not care.
    """
    try:
        row = _table().get_item(Key={"user_id": user_id}).get("Item")
    except ClientError:
        logger.warning("mark_oauth_invalid: read failed for %s", user_id, exc_info=True)
        return False
    if not row or row.get("state") != "active":
        return False
    if not _row_belongs_to(row, bound_to):
        # A different workspace owns this credential; its container's failures
        # are not evidence about ours (and vice versa).
        return False
    if row.get("oauth_health") == OAUTH_HEALTH_INVALID:
        return False  # already stamped — don't churn the row on repeat failures

    try:
        _table().update_item(
            Key={"user_id": user_id},
            UpdateExpression=("SET oauth_health = :bad, oauth_health_reason = :why, oauth_health_at = :at"),
            # Re-assert ``active`` at write time: a concurrent revoke/re-connect
            # between the read above and here must win, not be overwritten with
            # a verdict about the credential it just replaced.
            ConditionExpression="attribute_exists(user_id) AND #s = :active",
            ExpressionAttributeNames={"#s": "state"},
            ExpressionAttributeValues={
                ":bad": OAUTH_HEALTH_INVALID,
                ":why": (reason or "")[:_HEALTH_REASON_MAX_CHARS],
                ":at": int(time.time()),
                ":active": "active",
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] == "ConditionalCheckFailedException":
            return False
        logger.warning("mark_oauth_invalid: write failed for %s", user_id, exc_info=True)
        return False
    logger.warning(
        "oauth_credential_invalidated",
        extra={"user_id": user_id, "bound_to": bound_to, "reason": (reason or "")[:200]},
    )
    return True


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


async def revoke_user_oauth(*, user_id: str) -> None:
    """Delete the user's OAuth row. Caller is responsible for also
    deleting any pre-staged auth file on EFS (see workspace.py)."""
    _table().delete_item(Key={"user_id": user_id})
