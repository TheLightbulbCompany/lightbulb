"""ChatGPT OAuth endpoints.

Per spec §5.1: backend-driven device-code flow. Uses OpenAI's actual
Codex CLI endpoints (see core/services/oauth_service.py).

Frontend POSTs /start, shows the user_code + verification_uri to the
user, then polls /poll until status flips from "pending" to
"completed". On completion the backend has already pre-staged the
auth.json on EFS via the provision flow.

ChatGPT OAuth is available to both personal owners and org owners. The
connection binds to the workspace OWNER (one ChatGPT account ↔ one
container), recorded set-once on the billing row as
``oauth_connector_user_id``. For personal owners the connector IS the
Clerk user; for orgs it is the admin who completed the device-code flow.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user, is_org_owner_id, require_org_admin, resolve_owner_id
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
    caller_may_manage_connector,
    get_decrypted_tokens_consistent,
    poll_device_code,
    request_device_code,
    resolve_token_owner,
    revoke_user_oauth,
)


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/oauth/chatgpt", tags=["oauth"])


async def _require_org_oauth_owner(ctx: AuthContext) -> None:
    """403 unless the caller may manage the org's ChatGPT connection.

    The connection is bound to the workspace OWNER (one ChatGPT account ↔
    one container, set-once). An org row with no recorded owner yet admits
    any admin — that first sign-in claims ownership (legacy-row backfill
    and the normal first-connect path are the same flow).
    """
    require_org_admin(ctx)
    row = await billing_repo.get_by_owner_id(resolve_owner_id(ctx))
    if not caller_may_manage_connector(row, ctx.user_id):
        # Phantom-connector self-heal: a recorded id naming no Clerk user
        # wedges every real admin here with no recovery path. Clear it (CAS
        # on the exact phantom value) and let this caller's normal flow
        # proceed; a real owner keeps their 403 below.
        from core.services.oauth_service import maybe_clear_phantom_connector

        owner_id = resolve_owner_id(ctx)
        if await maybe_clear_phantom_connector(owner_id, row):
            return
        raise HTTPException(
            status_code=403,
            detail="Only the workspace owner who connected ChatGPT can manage this connection.",
        )


@router.get(
    "/status",
    summary="Whether the workspace has stored ChatGPT OAuth tokens",
    description=(
        "Read-only probe used by the wizard's hook on mount so the user "
        "advances cleanly past the OAuth step when tokens are already "
        "on file (e.g. they completed in another tab, or the page "
        "remounted between /poll and the next gate refresh). Returns "
        "200 with {connected, account_id}; never raises for missing-row. "
        "In org context this resolves the WORKSPACE's connection (bound to "
        "the recorded owner), so any org member sees the same answer."
    ),
)
async def status(ctx: AuthContext = Depends(get_current_user)):
    # Org context resolves the connection bound to the workspace owner — any
    # member reading /status sees the shared container's connection, not their
    # own (a member never completes the device-code flow). Personal owners ARE
    # their own connector, so the resolution is a no-op for them.
    owner_id = resolve_owner_id(ctx)
    token_owner = await resolve_token_owner(owner_id=owner_id) if ctx.is_org_context else ctx.user_id
    # Strongly-consistent read so the wizard sees its own write from /poll
    # without an eventual-consistency stutter. ``bound_to`` is the workspace
    # owner whose container consumes these tokens: a row the connector
    # claimed for a DIFFERENT workspace (e.g. they connected for an org, then
    # opened a personal workspace under the same Clerk user) reads as
    # disconnected here, so the personal gate can never silently reuse it.
    tokens = await get_decrypted_tokens_consistent(user_id=token_owner, bound_to=owner_id)
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
        "interval. Returns 409 if the user already has an active session. "
        "In org context, only the workspace owner who connected (or an "
        "admin on a not-yet-claimed workspace) may start a session."
    ),
)
async def start(ctx: AuthContext = Depends(get_current_user)):
    if ctx.is_org_context:
        await _require_org_oauth_owner(ctx)
    # ChatGPT sign-in comes with a paid plan (the same rule /billing/provider applies). Refuse BEFORE any OpenAI
    # device code is requested, so a free workspace never runs the whole sign-in only to be told to pick a plan.
    from core.services.provision_gate import is_paid_subscription

    if not is_paid_subscription(await billing_repo.get_by_owner_id(resolve_owner_id(ctx))):
        raise HTTPException(
            status_code=402,
            detail={"code": "plan_required", "message": "Signing in with ChatGPT needs the Workspace plan."},
        )
    try:
        # Bind the session to the workspace it is being started for, decided
        # HERE under the owner gate (org_id in org context, the user's own id
        # personally). The binding rides the pending row through to completion;
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
    # No owner gate here (unlike /start and /disconnect), and none is needed:
    # the binding AND the authorization are both fixed at /start (which IS
    # owner-gated) and ride the pending row through to completion. /poll can
    # only complete what /start authorized — it never re-derives the workspace
    # from the polling caller's current Clerk context. poll_device_code also
    # 502s unless a pending row exists for ctx.user_id, which only /start can
    # create.
    try:
        result = await poll_device_code(user_id=ctx.user_id)
    except OAuthExchangeFailedError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is DevicePollPending:
        return {"status": "pending"}
    assert isinstance(result, DevicePollResult)
    if is_org_owner_id(result.bound_owner_id):
        # The session was started (and owner-gated) FOR this org at /start; the
        # caller's Clerk context at poll time is irrelevant — switching contexts
        # between start and poll cannot re-target the binding or let an ungated
        # personal session claim an org. Completing the first sign-in claims
        # workspace ownership of the ChatGPT connection. claim_oauth_connector
        # is set-once at the repo layer (conditional write), so this can never
        # repoint an org whose owner is already recorded. Best-effort: a DDB
        # blip must not fail an otherwise-completed OAuth.
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
        "be called again to begin a new OAuth session. In org context, only the "
        "workspace owner who connected may disconnect."
    ),
)
async def disconnect(ctx: AuthContext = Depends(get_current_user)):
    if ctx.is_org_context:
        await _require_org_oauth_owner(ctx)
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
    # The owner gate above guarantees the caller IS the recorded connector in
    # org context, so revoking on ctx.user_id revokes the tokens that back the
    # container. One ChatGPT account ↔ one container, so this fully revokes for
    # both personal and org owners.
    await revoke_user_oauth(user_id=ctx.user_id)
    # Clear provider_choice so the next /chat visit routes the owner back to
    # onboarding to pick a provider. Without this, the wizard sees
    # provider_choice=chatgpt_oauth on a billing row with no auth.json and the
    # container starts but cannot run inference. clear_provider_choice also
    # REMOVEs oauth_connector_user_id, so a later re-connect re-claims
    # ownership from scratch — the disconnect is total for personal and org
    # owners alike.
    try:
        await billing_repo.clear_provider_choice(owner_id)
    except Exception:
        # Best-effort. The OAuth row + EFS file are already gone; missing
        # billing_repo update just means the user has to manually re-pick.
        pass
    return {"status": "disconnected"}
