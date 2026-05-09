"""ChatGPT OAuth endpoints.

Per spec §5.1: backend-driven device-code flow. Uses OpenAI's actual
Codex CLI endpoints (see core/services/oauth_service.py).

Frontend POSTs /start, shows the user_code + verification_uri to the
user, then polls /poll until status flips from "pending" to
"completed". On completion the backend has already pre-staged the
auth.json on EFS via the provision flow.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from core.auth import AuthContext, get_current_user
from core.containers.workspace import delete_auth_profile_store
from core.services.oauth_service import (
    DevicePollPending,
    DevicePollResult,
    OAuthAlreadyActiveError,
    OAuthExchangeFailedError,
    get_decrypted_tokens_consistent,
    poll_device_code,
    request_device_code,
    revoke_user_oauth,
)


router = APIRouter(prefix="/oauth/chatgpt", tags=["oauth"])


@router.get(
    "/status",
    summary="Whether the user has stored ChatGPT OAuth tokens",
    description=(
        "Read-only probe used by the wizard's hook on mount so the user "
        "advances cleanly past the OAuth step when tokens are already "
        "on file (e.g. they completed in another tab, or the page "
        "remounted between /poll and the next gate refresh). Returns "
        "200 with {connected, account_id}; never raises for missing-row. "
        "403 for org-context users (ChatGPT OAuth is personal-only — see "
        "memory/project_chatgpt_oauth_personal_only.md)."
    ),
)
async def status(ctx: AuthContext = Depends(get_current_user)):
    if ctx.is_org_context:
        raise HTTPException(
            status_code=403,
            detail="ChatGPT OAuth is not available for organization workspaces.",
        )
    # Strongly-consistent read so the wizard sees its own write from /poll
    # without an eventual-consistency stutter.
    tokens = await get_decrypted_tokens_consistent(user_id=ctx.user_id)
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
    # Org-context users cannot use ChatGPT OAuth — see
    # memory/project_chatgpt_oauth_personal_only.md. Belt-and-suspenders to
    # the trial-checkout block: a future "reconnect" flow that calls this
    # endpoint outside trial-checkout still won't bypass the rule.
    if ctx.is_org_context:
        raise HTTPException(
            status_code=403,
            detail="ChatGPT OAuth is not available for organization workspaces.",
        )
    try:
        result = await request_device_code(user_id=ctx.user_id)
    except OAuthAlreadyActiveError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
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
        result = await poll_device_code(user_id=ctx.user_id)
    except OAuthExchangeFailedError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    if result is DevicePollPending:
        return {"status": "pending"}
    assert isinstance(result, DevicePollResult)
    return {"status": "completed", "account_id": result.account_id}


@router.post(
    "/disconnect",
    summary="Revoke the user's stored ChatGPT OAuth tokens",
    description=(
        "Deletes the persisted OAuth row AND the EFS-staged auth-profiles.json so "
        "the container can no longer use the tokens cold. After this, /start can "
        "be called again to begin a new OAuth session."
    ),
)
async def disconnect(ctx: AuthContext = Depends(get_current_user)):
    await revoke_user_oauth(user_id=ctx.user_id)
    # Also remove the staged auth-profiles.json from EFS — without this the
    # container keeps the tokens for the rest of its lifetime even after we
    # revoke the row.
    try:
        await delete_auth_profile_store(user_id=ctx.user_id)
    except Exception:
        # Best-effort: the row deletion above is the authoritative signal.
        # EFS unlink failures shouldn't 5xx the disconnect.
        pass
    # Clear provider_choice so the next /chat visit routes the user back to
    # onboarding to pick a provider. Without this, the wizard sees
    # provider_choice=chatgpt_oauth on a billing row with no auth.json and the
    # container starts but cannot run inference.
    #
    # ChatGPT OAuth is personal-only (see memory/project_chatgpt_oauth_personal_only.md),
    # so the billing row's owner_id IS ctx.user_id.
    from core.repositories import billing_repo

    try:
        await billing_repo.clear_provider_choice(ctx.user_id)
    except Exception:
        # Best-effort. The OAuth row + EFS file are already gone; missing
        # billing_repo update just means the user has to manually re-pick.
        pass
    return {"status": "disconnected"}
