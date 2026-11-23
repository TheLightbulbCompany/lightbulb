"""ChatGPT OAuth endpoints.

Per spec §5.1: backend-driven device-code flow. Uses OpenAI's actual
Codex CLI endpoints (see core/services/oauth_service.py).

Frontend POSTs /start, shows the user_code + verification_uri to the
user, then polls /poll until status flips from "pending" to
"completed". On completion the backend has already pre-staged the
auth.json on EFS via the provision flow.

The connection binds to the person's workspace (one ChatGPT account ↔ one
container), recorded set-once on the billing row as
``oauth_connector_user_id``; the connector IS the Clerk user.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, resolve_owner_id
from core.repositories import billing_repo
from core.services.container_auth import (
    push_oauth_credential_to_container,
    revoke_provider_auth_in_container,
)
from core.services.oauth_service import (
    DevicePollPending,
    DevicePollResult,
    OAuthAlreadyActiveError,
    OAuthBoundElsewhereError,
    OAuthExchangeFailedError,
    get_decrypted_tokens_consistent,
    poll_device_code,
    request_device_code,
    revoke_user_oauth,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth/chatgpt", tags=["oauth"])


@router.get(
    "/status",
    summary="Whether the workspace has stored ChatGPT OAuth tokens",
    description=(
        "Read-only probe used by the wizard's hook on mount so the user "
        "advances cleanly past the OAuth step when tokens are already "
        "on file (e.g. they completed in another tab, or the page "
        "remounted between /poll and the next gate refresh). Returns "
        "200 with {connected, account_id}; never raises for missing-row. "
    ),
)
async def status(ctx: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(ctx)
    # Strongly-consistent read so the wizard sees its own write from /poll
    # without an eventual-consistency stutter. ``bound_to`` is the workspace
    # owner whose container consumes these tokens: a row the connector
    # claimed for a DIFFERENT workspace reads as disconnected here, so the
    # gate can never silently reuse it.
    tokens = await get_decrypted_tokens_consistent(user_id=ctx.user_id, bound_to=owner_id)
    return {
        "connected": tokens is not None,
        "account_id": (tokens or {}).get("account_id"),
    }


@router.post(
    "/start",
    summary="Begin a ChatGPT OAuth device-code session",
    description=(
        "Returns the user-facing fields needed to complete OAuth in the "
        "browser: user_code (typed at verification_uri) and the polling "
        "interval. Returns 409 if the user already has an active session."
    ),
)
async def start(ctx: AuthContext = Depends(get_current_user)):
    # Starting a ChatGPT sign-in needs a paid plan: 402 plan_required unless is_paid_subscription(billing row), checked
    # BEFORE any OpenAI device code is requested. Only /start is gated; /status, /poll and /disconnect, and connections
    # already made, are untouched.
    from core.services.provision_gate import is_paid_subscription

    if not is_paid_subscription(await billing_repo.get_by_owner_id(resolve_owner_id(ctx))):
        raise HTTPException(
            status_code=402,
            detail={"code": "plan_required", "message": "Signing in with ChatGPT needs the Workspace plan."},
        )
    try:
        # Bind the session to the workspace it is being started for, decided
        # HERE (the user's own id). The binding rides the pending row through to completion;
        # /poll cannot re-derive it from the polling caller's context.
        result = await request_device_code(user_id=ctx.user_id, bound_owner_id=resolve_owner_id(ctx))
    except OAuthAlreadyActiveError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except OAuthBoundElsewhereError as e:
        # NOT 409: the wizard hook treats a /start 409 as "already connected"
        # and advances, but a row bound to a different workspace means THIS
        # workspace has no usable tokens — advancing would loop the user back
        # through the provision gate. 403 with the human-readable detail routes
        # the wizard into its error branch, which renders e.message (the detail).
        raise HTTPException(status_code=403, detail=str(e)) from e
    except OAuthExchangeFailedError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    return {
        "user_code": result.user_code,
        "verification_uri": result.verification_uri,
        "expires_in": result.expires_in,
        "interval": result.interval,
    }


@router.post(
    "/poll",
    summary="Poll the device-code session for completion",
    description=(
        "Returns {status: 'pending'} until OpenAI accepts the user_code, "
        "then {status: 'completed', account_id: ...}. Frontend should poll "
        "every `interval` seconds (returned by /start)."
    ),
)
async def poll(ctx: AuthContext = Depends(get_current_user)):
    try:
        result = await poll_device_code(user_id=ctx.user_id, authorized_owner_id=resolve_owner_id(ctx))
    except OAuthBoundElsewhereError as e:
        raise HTTPException(status_code=403, detail=str(e)) from e
    except OAuthExchangeFailedError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is DevicePollPending:
        return {"status": "pending"}
    assert isinstance(result, DevicePollResult)
    # The session was started (and owner-gated) FOR this tenant at /start; the
    # caller's Clerk context at poll time is irrelevant — switching contexts
    # between start and poll cannot re-target the binding. Completing the first
    # sign-in claims the tenant's ChatGPT connection. claim_oauth_connector is
    # set-once at the repo layer (conditional write), so this can never repoint
    # a tenant whose connector is already recorded. Best-effort: a DDB blip must
    # not fail an otherwise-completed OAuth.
    try:
        await billing_repo.claim_oauth_connector(result.bound_owner_id, connector_user_id=ctx.user_id)
    except Exception:
        logger.exception("Failed to claim oauth connector for owner %s", result.bound_owner_id)
    # Push the freshly minted token into the running container. A same-provider
    # reconnect never reprovisions and never hits _hot_swap_provider, so without
    # this the container keeps its old (often spent) token while DDB holds the
    # fresh one. Staged under the container OWNER (bound_owner_id), token read
    # under the CONNECTOR (the polling caller). Best-effort: a no-op if the owner
    # has no container yet (first connect during onboarding) — provisioning
    # stages it then.
    await push_oauth_credential_to_container(owner_id=result.bound_owner_id, connector_id=ctx.user_id)
    return {"status": "completed", "account_id": result.account_id}


@router.post(
    "/disconnect",
    summary="Revoke the workspace's stored ChatGPT OAuth tokens",
    description=(
        "Deletes the persisted OAuth row AND the staged root auth.json so "
        "the runtime can no longer use the tokens. After this, /start can "
        "be called again to begin a new OAuth session."
    ),
)
async def disconnect(ctx: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(ctx)
    # The root auth.json IS the live credential, so revoke it BEFORE deleting the DDB token row
    # that gives this request retry ownership. The helper raises rather than reporting a clean
    # disconnect it could not perform.
    try:
        await revoke_provider_auth_in_container(owner_id)
    except Exception as e:
        logger.exception("authoritative ChatGPT revoke failed for owner %s", owner_id)
        raise HTTPException(
            status_code=502,
            detail="Could not remove the ChatGPT credential — try disconnecting again.",
        ) from e
    # The caller IS the connector, so revoking on ctx.user_id revokes the tokens
    # that back the container. One ChatGPT account ↔ one container.
    await revoke_user_oauth(user_id=ctx.user_id)
    # Clear provider_choice so the next /chat visit routes the owner back to
    # onboarding to pick a provider. Without this, the wizard sees
    # provider_choice=chatgpt_oauth on a billing row with no auth.json and the
    # container starts but cannot run inference. clear_provider_choice also
    # REMOVEs oauth_connector_user_id, so a later re-connect re-claims
    # ownership from scratch.
    try:
        await billing_repo.clear_provider_choice(owner_id)
    except Exception:
        # Best-effort. The OAuth row + EFS file are already gone; missing
        # billing_repo update just means the user has to manually re-pick.
        pass
    return {"status": "disconnected"}
