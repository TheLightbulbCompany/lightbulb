"""The Stripe Link Agent Wallet — /api/v1/company/link-wallet/* and the agent's ask door.

Unconfigured (``link_wallet.configured()`` false): every route, the OAuth callback and the agent
door included, answers 404, so the card hides and the agent tool says purchases are not set up.

Owner routes are Clerk-authed and org-admin only (the gating ``company_policy`` uses); the owner
is the org, never the member who clicked. The agent door is hook-token-authed and can only ASK:
it returns ``{requestId, status}`` and nothing else, and a refusal carries a code, never a value.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.repositories import billing_repo, oauth_state_repo
from core.services import effect_doors, link_wallet, purchases
from core.services.container_auth import _require_hook_token

logger = logging.getLogger(__name__)


def _enabled() -> None:
    if not link_wallet.configured():
        raise HTTPException(status_code=404, detail="Not found")


router = APIRouter(dependencies=[Depends(_enabled)])
internal_router = APIRouter(dependencies=[Depends(_enabled)])


def _owner(auth: AuthContext) -> str:
    if auth.is_org_context:
        require_org_admin(auth)
    return resolve_owner_id(auth)


def _wire(exc: purchases.PurchaseRefused | link_wallet.LinkWalletError) -> HTTPException:
    status = 503 if getattr(exc, "code", "") in ("link_unavailable", "board_unavailable", "busy") else 409
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


@router.get("/status")
async def get_status(auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await link_wallet.status(_owner(auth))


@router.post("/connect")
async def connect(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = _owner(auth)
    try:
        return {"authorizeUrl": await link_wallet.start_connect(owner_id, auth.user_id)}
    except link_wallet.LinkWalletError as exc:
        raise _wire(exc) from exc


def _return(result: str) -> RedirectResponse:
    base = settings.FRONTEND_URL.rstrip("/")
    return RedirectResponse(url=f"{base}/settings?panel=connections&link_wallet={result}", status_code=302)


@router.get("/callback")
async def callback(code: str = Query(default=""), state: str = Query(default="")):
    """Public: Link redirects the browser here. The owner and PKCE verifier come from the state row."""
    rec = await oauth_state_repo.consume(state) if state else None
    if not rec or rec.get("provider") != link_wallet.PROVIDER or not code:
        return _return("error")
    try:
        landed = await link_wallet.exchange_code(rec, code)
    except link_wallet.LinkWalletError as exc:
        logger.warning("link_wallet: callback failed owner_id=%s code=%s", rec["owner_id"], exc.code)
        return _return("error")
    except Exception:  # noqa: BLE001 -- the browser must land somewhere; never echo a value
        logger.exception("link_wallet: callback crashed owner_id=%s", rec["owner_id"])
        return _return("error")
    return _return("connected" if landed else "refused")


@router.post("/disconnect")
async def disconnect(auth: Annotated[AuthContext, Depends(get_current_user)]):
    return {"disconnected": await link_wallet.disconnect(_owner(auth))}


class LimitsBody(BaseModel):
    perPurchaseCents: int = Field(ge=0)
    projectMonthlyCents: int = Field(ge=0, le=purchases.MAX_AMOUNT_CENTS * 10)
    merchants: list[str] = Field(default_factory=list, max_length=50)


@router.get("/limits")
async def get_limits(auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await billing_repo.get_purchase_limits(_owner(auth))


@router.put("/limits")
async def put_limits(body: LimitsBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = _owner(auth)
    merchants = []
    for m in body.merchants:
        try:
            merchants.append(purchases.website_logins.normalize_origin(m))
        except purchases.website_logins.LoginError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    saved = await billing_repo.set_purchase_limits(
        owner_id,
        {
            **body.model_dump(),
            "perPurchaseCents": min(body.perPurchaseCents, purchases.MAX_AMOUNT_CENTS),
            "merchants": merchants,
        },
    )
    if saved is None:
        raise HTTPException(status_code=404, detail="No billing account")
    return saved


@router.get("/purchases")
async def get_purchases(auth: Annotated[AuthContext, Depends(get_current_user)]):
    return await purchases.summary(_owner(auth))


@router.post("/purchases/{request_id}/approval-link")
async def approval_link(request_id: str, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """The "Approve in Link" click. The URL is minted here, per click; no card or row stores it."""
    try:
        return await purchases.approval_link(_owner(auth), request_id)
    except purchases.PurchaseRefused as exc:
        raise (HTTPException(status_code=404, detail="Not found") if exc.code == "not_found" else _wire(exc)) from exc
    except link_wallet.LinkWalletError as exc:
        raise _wire(exc) from exc


# ---- the agent's door (hook token) ----------------------------------------------------------


def _same_amount(a, b) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return False


class PurchaseBody(BaseModel):
    agentId: str = Field(min_length=1, max_length=128)
    merchant: str = Field(max_length=200)
    url: str = Field(max_length=2048)
    amount: float
    why: str = Field(max_length=2000)
    issueId: str = Field(max_length=128)


@internal_router.post("/{owner_id}/requests")
async def agent_request(owner_id: str, body: PurchaseBody, request: Request):
    await _require_hook_token(owner_id, request)
    try:  # a task run's purchase must be the one a person allowed (Lane Q door gating); legacy agents pass
        await effect_doors.require_intent(
            owner_id,
            body.agentId,
            "purchase",
            {"merchant": body.merchant, "url": body.url, "amount": body.amount, "why": body.why},
            # the tool's amount may be an int where the route parses a float: compare the numbers, not the JSON
            match=lambda o: (
                o.get("merchant") == body.merchant
                and o.get("url") == body.url
                and _same_amount(o.get("amount"), body.amount)
                and o.get("why") == body.why
            ),
        )
    except effect_doors.DoorRefused as exc:
        raise HTTPException(status_code=403, detail={"code": exc.code, "message": str(exc)}) from exc
    try:
        return await purchases.request_purchase(
            owner_id,
            agent_id=body.agentId,
            merchant=body.merchant,
            url=body.url,
            amount=body.amount,
            why=body.why,
            issue_id=body.issueId,
        )
    except (purchases.PurchaseRefused, link_wallet.LinkWalletError) as exc:
        raise _wire(exc) from exc
