"""The company's Stripe account — /api/v1/company/stripe/*.

Owner-facing (Clerk) routes are paid-only and org-admin-only for anything
that mutates; the OAuth callback is public (Stripe redirects there) and is
bound to the owner via the single-use ``oauth-state`` row.
"""

from __future__ import annotations

import logging
import secrets
import time
from typing import Annotated, Literal

import stripe
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from core.auth import AuthContext, get_current_user, require_org_admin, resolve_owner_id
from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo, oauth_state_repo
from core.services import company_stripe, owner_lifecycle, provision_gate
from core.services.owner_facts import write_owner_facts
from core.services.entitlement_guards import COMPANY_STRIPE_MESSAGE, require_always_on_entitlement

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


def _status_payload(row: dict | None, projects_ready: bool | None = None) -> dict:
    row = row or {}
    account_id = row.get("company_stripe_account_id")
    return {
        "account_id": account_id,
        "source": row.get("company_stripe_source"),
        "details_submitted": bool(row.get("company_stripe_details_submitted")),
        "charges_enabled": bool(row.get("company_stripe_charges_enabled")),
        "payouts_enabled": bool(row.get("company_stripe_payouts_enabled")),
        # False only for an api_key row bound with an rk_test_/sk_test_ key;
        # the card labels that "Test mode". Connect rows follow the platform
        # key's own mode: a created account stores no flag, and defaulting it to
        # True labelled a dev sandbox account live.
        "livemode": bool(row.get("company_stripe_livemode", company_stripe.key_livemode(settings.STRIPE_SECRET_KEY)))
        if account_id
        else None,
        "dashboard_url": DASHBOARD_URL if account_id else None,
        # The frontend hides "Connect existing" and disables "Disconnect" when
        # this is false — both routes 503 without the platform client id.
        "connect_configured": bool(settings.STRIPE_CONNECT_CLIENT_ID),
        # Readiness of the account Projects actually uses
        # (`company_stripe.projects_account_of`). None = not asked (no account).
        # False shows "Finish Stripe setup", or "Add a services account" when
        # the main binding can't run Projects and no services account exists.
        "projects_ready": projects_ready,
        "services_account_id": row.get("company_stripe_projects_account_id"),
        # Public pk for Connect embedded onboarding; None ⇒ the card uses the
        # hosted Account Link instead.
        "publishable_key": settings.STRIPE_PUBLISHABLE_KEY or None,
    }


@router.get("/status")
async def status(auth: Annotated[AuthContext, Depends(get_current_user)]):
    owner_id = resolve_owner_id(auth)
    row = await billing_repo.get_by_owner_id(owner_id)
    if (row or {}).get("company_stripe_source") == "api_key":
        # No Connect relationship ⇒ no account.updated webhook ⇒ the stored
        # flags only ever reflect bind time. Refresh them on read; best-effort,
        # a Stripe hiccup serves the stored copy rather than a 502.
        row = await _refresh_api_key_status(row) or row
    return _status_payload(row, await _projects_ready(row))


async def _projects_ready(row: dict | None) -> bool | None:
    """One Stripe read per status load, only where it can matter. Best-effort:
    a hiccup answers None (card shows nothing extra), never a 502."""
    row = row or {}
    if not row.get("company_stripe_account_id"):
        return None
    account_id = company_stripe.projects_account_of(row)
    if not account_id:
        # `connected`/`api_key` with no services account: Projects can't run
        # on the main binding, so it is not ready until one is added.
        return False
    try:
        return await company_stripe.projects_ready(account_id=account_id)
    except Exception:  # noqa: BLE001 — a read-time probe must never break the card
        logger.warning("company_stripe: projects_ready read failed owner_id=%s", row.get("owner_id"))
        return None


async def _refresh_api_key_status(row: dict) -> dict | None:
    owner_id, account_id = row["owner_id"], row["company_stripe_account_id"]
    try:
        key = await company_stripe.load_api_key(owner_id)
        if not key:
            return None
        status = await company_stripe.retrieve_status(account_id=account_id, api_key=key)
    except Exception:  # noqa: BLE001 — a read-time refresh must never break the card
        logger.warning("company_stripe: api_key status refresh failed owner_id=%s", owner_id)
        return None
    return await billing_repo.set_company_stripe_status(owner_id, account_id=account_id, **status)


@router.post("/create")
async def create(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Create the company account if absent and return a fresh onboarding link."""
    started_ms = int(time.time() * 1000)
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=COMPANY_STRIPE_MESSAGE)
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
        async with owner_lifecycle.revoke_fence(owner_id, started_ms):
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
        else:
            # A brand-new Stripe Express account was actually minted and bound
            # here — not the idempotent reuse above (account_id already set,
            # this whole block skipped), and not an account adopted after
            # losing a concurrent create race (the `bound is None` branch
            # above). This is the one signal that a real owner just touched
            # the live create door for the first time.
            put_metric("company_stripe.account.created")
        await _sync_status(owner_id, account_id)
        await write_owner_facts(owner_id)
    try:
        url = await company_stripe.create_company_onboarding_link(account_id=account_id)
    except stripe.error.StripeError as e:
        raise _stripe_failure(e) from e
    return {"account_id": account_id, "onboarding_url": url}


class ApiKeyBody(BaseModel):
    api_key: str


@router.post("/api-key")
async def bind_api_key(body: ApiKeyBody, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Bind the owner's OWN Stripe account by a restricted key they pasted.

    One Stripe call proves the key works and tells us whose it is; the key
    goes to Secrets Manager (backend-only — it never reaches a container)
    and the account id lands on the billing row exactly like a Connect bind,
    so every downstream reader sees an ordinary bound account. No platform
    relationship is created, which is the whole point: this is the door for
    an owner who will not (or, like us, cannot) grant Connect.
    """
    started_ms = int(time.time() * 1000)
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=COMPANY_STRIPE_MESSAGE)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_account_id"):
        raise HTTPException(status_code=409, detail="A Stripe account is already bound")
    try:
        info = await company_stripe.validate_api_key(body.api_key)
    except company_stripe.InvalidApiKey as e:
        raise HTTPException(status_code=422, detail={"code": "invalid_api_key", "message": str(e)}) from e
    except stripe.error.AuthenticationError as e:
        raise HTTPException(
            status_code=422,
            detail={"code": "invalid_api_key", "message": "Stripe rejected that key — check it was copied in full"},
        ) from e
    except stripe.error.StripeError as e:
        raise _stripe_failure(e) from e
    account_id = info["account_id"]
    # Same cross-owner fence as the OAuth callback: one Stripe account, one
    # Lightbulb owner. A key for an account another owner already bound is
    # refused without touching either row.
    existing = await billing_repo.get_by_company_stripe_account_id(account_id)
    if existing and existing.get("owner_id") != owner_id:
        raise HTTPException(
            status_code=409,
            detail={"code": "account_in_use", "message": "That Stripe account is connected to another workspace"},
        )
    # Bind FIRST: the conditional DynamoDB write is the lock. A concurrent
    # second admin loses here and never touches the secret, so the winner's
    # key cannot be deleted from under it. Only the winner stores the key;
    # until it lands, a bound row with no key reads as unbound
    # (company_stripe_reads._addressing) — "not connected", never
    # "connected but every read 401s".
    # The key is stored under the same lease, so revoke-all's unbind can never run between the
    # two writes and leave a key in the vault with no binding.
    async with owner_lifecycle.revoke_fence(owner_id, started_ms):
        bound = await billing_repo.set_company_stripe_account(owner_id, account_id=account_id, source="api_key")
        if bound is None:
            raise HTTPException(status_code=409, detail="A Stripe account is already bound")
        try:
            await company_stripe.store_api_key(owner_id, body.api_key)
        except Exception as e:  # noqa: BLE001 — the vault write failed; the binding must not outlive it
            logger.exception("company_stripe: api key store failed owner_id=%s; unbinding", owner_id)
            await billing_repo.clear_company_stripe_account(owner_id, account_id=account_id)
            raise HTTPException(
                status_code=502, detail={"code": "vault_error", "message": "Couldn't store the key. Try again."}
            ) from e
    await billing_repo.set_company_stripe_status(
        owner_id,
        account_id=account_id,
        details_submitted=info["details_submitted"],
        charges_enabled=info["charges_enabled"],
        payouts_enabled=info["payouts_enabled"],
        livemode=info["livemode"],
    )
    put_metric("company_stripe.account.api_key_bound")
    await write_owner_facts(owner_id)
    return {"account_id": account_id, "livemode": info["livemode"]}


class OnboardingTarget(BaseModel):
    target: Literal["main", "services"] = "main"


def _onboardable_account(row: dict, target: str) -> str:
    """The platform-created account a target names: the main account only when
    we created it, or the services account. Anything else can't be onboarded."""
    if target == "services":
        account_id = row.get("company_stripe_projects_account_id")
    else:
        account_id = row.get("company_stripe_account_id") if row.get("company_stripe_source") == "created" else None
    if not account_id:
        raise HTTPException(status_code=409, detail="No created Stripe account to onboard")
    return account_id


async def _ensure_projects_capability(target: str, account_id: str) -> None:
    """An account created without the developer configuration lacks it;
    request it (idempotent) so onboarding collects it. The services account is
    always created with it."""
    if target == "main" and not await company_stripe.projects_ready(account_id=account_id):
        await company_stripe.request_projects_capability(account_id=account_id)


@router.post("/onboarding-link")
async def onboarding_link(
    auth: Annotated[AuthContext, Depends(get_current_user)], body: OnboardingTarget | None = None
):
    """Hosted Account Link: the fallback where embedded onboarding can't run
    (no publishable key, or the desktop app)."""
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=COMPANY_STRIPE_MESSAGE)
    target = body.target if body else "main"
    # Consistent: the card calls this right after /services-account wrote the id.
    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    account_id = _onboardable_account(row, target)
    try:
        await _ensure_projects_capability(target, account_id)
        # Already onboarded once => this is a correction, not a first onboarding.
        # The status flags describe the main account only.
        url = await company_stripe.create_company_onboarding_link(
            account_id=account_id,
            update=target == "main" and bool(row.get("company_stripe_details_submitted")),
        )
    except stripe.error.StripeError as e:
        raise _stripe_failure(e) from e
    return {"onboarding_url": url}


@router.post("/services-account")
async def services_account(auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Create the platform-owned account Stripe Projects runs on for an owner
    whose main binding can't (see `company_stripe.projects_account_of`). The
    main binding is untouched. The card then onboards it with
    `POST /account-session {target: "services"}`."""
    started_ms = int(time.time() * 1000)
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=COMPANY_STRIPE_MESSAGE)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    if row.get("company_stripe_projects_account_id"):
        return {"account_id": row["company_stripe_projects_account_id"]}
    if not row.get("company_stripe_account_id") or row.get("company_stripe_source") == "created":
        raise HTTPException(
            status_code=409,
            detail={"code": "services_account_not_needed", "message": "This Stripe account runs services itself"},
        )
    company_name = await company_stripe.resolve_company_name(owner_id)
    try:
        account_id = await company_stripe.create_company_account(
            owner_id=owner_id, email=auth.email or "", company_name=company_name, kind="company_services"
        )
    except stripe.error.StripeError as e:
        raise _stripe_failure(e) from e
    async with owner_lifecycle.revoke_fence(owner_id, started_ms):
        bound = await billing_repo.set_company_stripe_projects_account(owner_id, account_id=account_id)
    if bound is None:
        # A concurrent create won (adopt it), or the main binding went away.
        row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
        winner = row.get("company_stripe_projects_account_id")
        if not winner:
            raise HTTPException(status_code=409, detail="No Stripe account bound")
        return {"account_id": winner}
    put_metric("company_stripe.services_account.created")
    return {"account_id": account_id}


@router.post("/account-session")
async def account_session(body: OnboardingTarget, auth: Annotated[AuthContext, Depends(get_current_user)]):
    """Client secret for Connect embedded onboarding. Only for accounts the
    platform created: the main account when its source is `created`, or the
    services account."""
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=COMPANY_STRIPE_MESSAGE)
    if not settings.STRIPE_PUBLISHABLE_KEY:
        raise HTTPException(
            status_code=409,
            detail={"code": "embedded_onboarding_unavailable", "message": "Embedded onboarding isn't configured"},
        )
    # Consistent: the card calls this right after /services-account wrote the id.
    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or {}
    account_id = _onboardable_account(row, body.target)
    try:
        await _ensure_projects_capability(body.target, account_id)
        client_secret = await company_stripe.create_account_session(account_id=account_id)
    except stripe.error.StripeError as e:
        raise _stripe_failure(e) from e
    return {"client_secret": client_secret, "publishable_key": settings.STRIPE_PUBLISHABLE_KEY}


@router.get("/connect/start")
async def connect_start(auth: Annotated[AuthContext, Depends(get_current_user)]):
    _require_connect_configured()
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    await require_always_on_entitlement(owner_id, message=COMPANY_STRIPE_MESSAGE)
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
    # Revoke everything reads the binding under this lease: a bind either lands before that read
    # (and is released) or finds the mark here.
    try:
        async with owner_lifecycle.owner_lifecycle_guard(owner_id):
            if await owner_lifecycle.revoked_since(owner_id, rec):
                await _release(account_id)
                return _return("stripe_error=revoked")
            bound = await billing_repo.set_company_stripe_account(owner_id, account_id=account_id, source="connected")
    except owner_lifecycle.OwnerLifecycleBusyError:
        await _release(account_id)
        return _return("stripe_error=busy")
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
    """Revoke the platform link and unbind. The owner's account is untouched.

    The unbind is unconditional. A Stripe error used to 502 with the row left
    bound, which is unrecoverable for a `created` account: it was minted
    through Accounts v2 and was never an OAuth grant, so Stripe can answer
    `invalid_client` ("isn't connected to your application") on every retry
    and the owner could never let go. `stripe_released` reports whether the
    revoke was confirmed instead of hiding it.
    """
    _gate_admin(auth)
    owner_id = resolve_owner_id(auth)
    row = await billing_repo.get_by_owner_id(owner_id) or {}
    account_id = row.get("company_stripe_account_id")
    if not account_id:
        raise HTTPException(status_code=409, detail="No Stripe account bound")
    # An api_key row has no platform grant to revoke, so the client id is
    # irrelevant to it; Connect rows still fail closed without one.
    if row.get("company_stripe_source") != "api_key":
        _require_connect_configured()
    released = await company_stripe.release_and_unbind(
        owner_id=owner_id, account_id=account_id, source=row.get("company_stripe_source")
    )
    if row.get("company_stripe_source") == "api_key" and not released:
        # The vault delete failed and the binding was deliberately KEPT so this
        # can be retried. Saying "disconnected" here would be a lie.
        raise HTTPException(
            status_code=502,
            detail={
                "code": "key_delete_failed",
                "message": "Couldn't remove your key from our vault — nothing was disconnected. Try again.",
            },
        )
    await write_owner_facts(owner_id)
    return {"disconnected": True, "stripe_released": released}
