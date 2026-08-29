"""The company's Stripe account — /api/v1/company/stripe/*.

Owner-facing (Clerk) routes are paid-only and org-admin-only for anything
that mutates; the OAuth callback is public (Stripe redirects there) and is
bound to the owner via the single-use ``oauth-state`` row.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

import httpx
import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo, oauth_state_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import company_stripe
from core.services.paperclip_admin_client import PaperclipApiError, shared_admin_client
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_openclaw_pairing import resolve_company_id_for_owner
from routers.agent_builder import _require_hook_token
from routers.teams._guards import require_always_on_entitlement

logger = logging.getLogger(__name__)
router = APIRouter()

STATE_PROVIDER = "stripe_company"
DASHBOARD_URL = "https://dashboard.stripe.com/"


def _stripe_failure(e: stripe.error.StripeError) -> HTTPException:
    logger.warning("company_stripe: Stripe call failed: %s", e)
    return HTTPException(
        status_code=502,
        detail={"code": "stripe_error", "message": str(getattr(e, "user_message", None) or e)},
    )


def _gate_admin(auth: AuthContext) -> None:
    if auth.is_org_context:
        require_org_admin(auth)


async def resolve_company_name(owner_id: str) -> str:
    """The Paperclip company name (the owner named it in Teams); falls back to
    the owner id so a Stripe account is never created with an empty name."""
    repo = PaperclipRepo(table_name="paperclip-companies")
    company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
    if not company_id:
        return owner_id
    admin = shared_admin_client()
    cookie = await get_admin_session_cookie(admin._http)
    company = await admin.get_company(session_cookie=cookie, company_id=company_id)
    return (company or {}).get("name") or owner_id


def _status_payload(row: dict | None) -> dict:
    row = row or {}
    account_id = row.get("company_stripe_account_id")
    return {
        "account_id": account_id,
        "source": row.get("company_stripe_source"),
        "details_submitted": bool(row.get("company_stripe_details_submitted")),
        "charges_enabled": bool(row.get("company_stripe_charges_enabled")),
        "payouts_enabled": bool(row.get("company_stripe_payouts_enabled")),
        "dashboard_url": DASHBOARD_URL if account_id else None,
    }


@router.get("/status")
async def status(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    row = await billing_repo.get_by_owner_id(owner_id)
    return _status_payload(row)


@router.post("/create")
async def create(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Create the company account if absent and return a fresh onboarding link."""
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if account_id and row.get("company_stripe_source") == "connected":
        raise HTTPException(status_code=409, detail="An existing Stripe account is already connected")
    if not account_id:
        company_name = await resolve_company_name(owner_id)
        try:
            account_id = await company_stripe.create_company_account(
                owner_id=owner_id, email=auth.email or "", company_name=company_name
            )
        except stripe.error.StripeError as e:
            raise _stripe_failure(e) from e
        bound = await billing_repo.set_company_stripe_account(owner_id, account_id=account_id, source="created")
        if bound is None:
            # A concurrent create bound first; use the winner. The orphan shell
            # on Stripe is an empty account with no obligations.
            row = await billing_repo.get_by_owner_id(owner_id) or {}
            account_id = row.get("company_stripe_account_id") or account_id
    try:
        url = await company_stripe.create_company_onboarding_link(account_id=account_id)
    except stripe.error.StripeError as e:
        raise _stripe_failure(e) from e
    return {"account_id": account_id, "onboarding_url": url}


@router.post("/onboarding-link")
async def onboarding_link(auth: Annotated[AuthContext, Depends(get_current_user)]):
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id or row.get("company_stripe_source") != "created":
        raise HTTPException(status_code=409, detail="No created Stripe account to onboard")
    try:
        url = await company_stripe.create_company_onboarding_link(account_id=account_id)
    except stripe.error.StripeError as e:
        raise _stripe_failure(e) from e
    return {"onboarding_url": url}


@router.get("/connect/start")
async def connect_start(auth: Annotated[AuthContext, Depends(get_current_user)]):
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        raise HTTPException(status_code=409, detail="A Stripe account is already bound")
    state = secrets.token_urlsafe(32)
    await oauth_state_repo.put(state, owner_id=owner_id, provider=STATE_PROVIDER, verifier="")
    return {"authorize_url": company_stripe.build_connect_authorize_url(state=state)}


def _return(query: str) -> RedirectResponse:
    base = settings.STRIPE_COMPANY_RETURN_URL
    sep = "&" if "?" in base else "?"
    return RedirectResponse(url=f"{base}{sep}{query}", status_code=302)


@router.get("/connect/callback")
async def connect_callback(code: str = Query(default=""), state: str = Query(default="")):
    """Public: Stripe redirects here. Owner is recovered from the state row."""
    rec = await oauth_state_repo.consume(state) if state else None
    if not rec or rec.get("provider") != STATE_PROVIDER or not code:
        return _return("stripe_error=state")
    owner_id = rec["owner_id"]
    try:
        account_id = await company_stripe.exchange_connect_code(code=code)
    except Exception:
        logger.exception("company_stripe: OAuth exchange failed owner_id=%s", owner_id)
        return _return("stripe_error=exchange")
    bound = await billing_repo.set_company_stripe_account(owner_id, account_id=account_id, source="connected")
    if bound is None:
        return _return("stripe_error=already_bound")
    # `stripe_connect`, not `stripe`: RETURN_URL already carries `?stripe=return`
    # for hosted onboarding, and a duplicate key would hide this outcome.
    return _return("stripe_connect=connected")


@router.post("/disconnect")
async def disconnect(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Revoke the platform link and unbind. The owner's account is untouched."""
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        raise HTTPException(status_code=409, detail="No Stripe account bound")
    try:
        await company_stripe.deauthorize(account_id=account_id)
    except stripe.error.StripeError as e:
        raise _stripe_failure(e) from e
    await billing_repo.clear_company_stripe_account(owner_id, account_id=account_id)
    return {"disconnected": True}


class RequestStripeBody(BaseModel):
    issue_id: str | None = None
    reason: str = ""


@router.post("/u/{owner_id}/request")
async def request_from_container(owner_id: str, body: RequestStripeBody, request: Request):
    """Container-authed (hook token): Friday asks the owner to set up Stripe."""
    await _require_hook_token(owner_id, request)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        return {"approval_id": None, "status": "connected"}
    from core.services import company_stripe_approval

    try:
        approval_id = await company_stripe_approval.request_company_stripe(
            owner_id=owner_id, issue_id=body.issue_id, reason=body.reason
        )
    except (PaperclipApiError, httpx.HTTPError) as e:
        logger.warning("company_stripe: could not file connect_stripe approval owner_id=%s: %s", owner_id, e)
        raise HTTPException(
            status_code=503,
            detail={"code": "paperclip_unavailable", "message": "Could not reach the company board; try again later"},
        ) from e
    return {"approval_id": approval_id, "status": "filed" if approval_id else "exists"}
