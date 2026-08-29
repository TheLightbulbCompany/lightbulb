"""The company's Stripe account — /api/v1/company/stripe/*.

Owner-facing (Clerk) routes are paid-only and org-admin-only for anything
that mutates; the OAuth callback is public (Stripe redirects there) and is
bound to the owner via the single-use ``oauth-state`` row.
"""

from __future__ import annotations

import logging
import secrets
from typing import Annotated

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo, oauth_state_repo
from core.services import company_stripe, company_stripe_approval, provision_gate
from core.services.owner_facts import write_owner_facts
from routers.agent_builder import _require_hook_token
from routers.company_board import file_card
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


def _require_connect_configured() -> None:
    """Both OAuth doors — "connect existing" and its exit, deauthorize — are
    unusable without the platform client id. Without this check `connect/start`
    would mint a state row and hand the owner an authorize URL with an empty
    `client_id` (Stripe errors on arrival), and `disconnect` would call
    `OAuth.deauthorize` with no client id, get a Stripe error, and leave the
    row bound with no way out. Fail closed, before either side effect."""
    if not settings.STRIPE_CONNECT_CLIENT_ID:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "stripe_connect_not_configured",
                "message": "Stripe Connect isn't configured for this environment yet",
            },
        )


async def _sync_status(owner_id: str, account_id: str) -> None:
    """Pull the account's real status right after a bind so the page is
    correct before any webhook lands — a connected existing account is
    usually already fully enabled. Best-effort: a Stripe failure here must
    not fail the bind, and ``account.updated`` catches up either way."""
    try:
        status = await company_stripe.retrieve_status(account_id=account_id)
    except stripe.error.StripeError as e:
        logger.warning("company_stripe: status fetch failed for %s: %s", account_id, e)
        return
    await billing_repo.set_company_stripe_status(owner_id, account_id=account_id, **status)


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
        # The frontend hides "Connect existing" and disables "Disconnect" when
        # this is false — both routes 503 without the platform client id.
        "connect_configured": bool(settings.STRIPE_CONNECT_CLIENT_ID),
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
        company_name = await company_stripe.resolve_company_name(owner_id)
        try:
            account_id = await company_stripe.create_company_account(
                owner_id=owner_id, email=auth.email or "", company_name=company_name
            )
        except stripe.error.StripeError as e:
            raise _stripe_failure(e) from e
        bound = await billing_repo.set_company_stripe_account(owner_id, account_id=account_id, source="created")
        if bound is None:
            # None means EITHER a concurrent create bound first OR there is no
            # billing row at all — the condition covers both. A consistent
            # re-read tells them apart: an account id means a winner to adopt;
            # nothing means the row is missing, and returning the orphan
            # shell's onboarding link would hand the owner an account nothing
            # will ever be able to look up again.
            row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
            winner = row.get("company_stripe_account_id")
            if not winner:
                logger.error("company_stripe: no billing row to bind acct owner_id=%s", owner_id)
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "no_billing_row",
                        "message": "No billing account for this owner; finish signing up first",
                    },
                )
            account_id = winner
        await _sync_status(owner_id, account_id)
        await write_owner_facts(owner_id)
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
    _require_connect_configured()
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        raise HTTPException(status_code=409, detail="A Stripe account is already bound")
    state = secrets.token_urlsafe(32)
    await oauth_state_repo.put(state, owner_id=owner_id, provider=STATE_PROVIDER, verifier="")
    return {"authorize_url": company_stripe.build_connect_authorize_url(state=state)}


async def _release(account_id: str) -> None:
    """Hand back an authorization we are not going to use. Best-effort: the
    redirect must happen either way, or the owner is left on a blank page."""
    try:
        await company_stripe.deauthorize(account_id=account_id)
    except Exception:  # noqa: BLE001 — the redirect must still happen
        logger.exception("company_stripe: could not release %s", account_id)


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
    # The plan is re-checked HERE, not just at connect/start: the state row
    # outlives the click, so a subscription can lapse between the two.
    entitled = provision_gate.has_always_on_entitlement(await billing_repo.get_by_owner_id(owner_id))
    try:
        account_id = await company_stripe.exchange_connect_code(code=code)
    except Exception:
        logger.exception("company_stripe: OAuth exchange failed owner_id=%s", owner_id)
        return _return("stripe_error=exchange")
    if not entitled:
        logger.warning("company_stripe: callback for an unentitled owner_id=%s; releasing", owner_id)
        await _release(account_id)
        return _return("stripe_error=plan")
    # Cross-owner uniqueness. The GSI read is best-effort against a truly
    # concurrent connect (a global claim would need a claim table), but it
    # catches the real case: the same Stripe account offered to two owners.
    # No deauthorize — that link belongs to the OTHER owner and revoking it
    # would disconnect them.
    existing = await billing_repo.get_by_company_stripe_account_id(account_id)
    if existing and existing.get("owner_id") != owner_id:
        logger.warning(
            "company_stripe: %s is already bound to owner_id=%s; refusing owner_id=%s",
            account_id,
            existing.get("owner_id"),
            owner_id,
        )
        return _return("stripe_error=account_in_use")
    bound = await billing_repo.set_company_stripe_account(owner_id, account_id=account_id, source="connected")
    if bound is None:
        # We hold an authorization on an account we are not going to use.
        # Hand it back rather than leaving the platform attached to it.
        await _release(account_id)
        return _return("stripe_error=already_bound")
    await _sync_status(owner_id, account_id)
    await write_owner_facts(owner_id)
    # `stripe_connect`, not `stripe`: RETURN_URL already carries `?stripe=return`
    # for hosted onboarding, and a duplicate key would hide this outcome.
    return _return("stripe_connect=connected")


@router.post("/disconnect")
async def disconnect(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Revoke the platform link and unbind. The owner's account is untouched."""
    _require_connect_configured()
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
    await write_owner_facts(owner_id)
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
    approval_id = await file_card(
        owner_id,
        company_stripe_approval.request_company_stripe(owner_id=owner_id, issue_id=body.issue_id, reason=body.reason),
    )
    # "no_company": the owner has no Paperclip company yet, so there is no
    # board to file against — not a duplicate, not a failure.
    return {"approval_id": approval_id, "status": "filed" if approval_id else "no_company"}
