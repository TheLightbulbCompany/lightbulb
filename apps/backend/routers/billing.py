"""Billing API endpoints — flat-fee subscription + Metronome-backed credits."""

import asyncio
import logging
import time
from typing import Literal
from uuid import UUID

import httpx
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response, Query
from pydantic import BaseModel, Field

from core.auth import (
    AuthContext,
    get_current_user,
    get_owner_type,
    resolve_owner_id,
    require_org_admin,
)
from core.config import settings
from core.billing import voice_plan, voice_plan_payments, voice_plan_checkout
from core.observability.metrics import put_metric
from core.repositories import billing_repo, usage_repo
from core.repositories.workspace_repo import customer_ops_agent_id
from core.services import apple_iap, billing_service, openrouter_keys, photon, text_lines, trial_cap
from core.services.billing_service import SEATS_INCLUDED, BillingService
from core.services.chat_gate import budget_block_reason
from core.services.stripe_webhook_handlers import STRIPE_WEBHOOK_HANDLERS
from core.services.provision_gate import (
    is_paid_subscription,
)
from core.services.usage_service import get_usage_summary
from schemas.billing import (
    BillingAccountResponse,
    PortalResponse,
    UsageSummary,
    MemberUsage,
    MyUsageResponse,
    SpendResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


class VoicePlanEnrollment(BaseModel):
    expectedOwnerId: str = Field(min_length=1, max_length=128)
    planVersion: Literal["voice-agents-v1"]
    acceptMonthlyUsd: Literal[250]
    acceptIncludedMinutes: Literal[1500]
    acceptOverageUsdPerMinute: Literal[0.35]


@router.get("/voice-plan")
async def get_voice_plan(auth: AuthContext = Depends(get_current_user)):
    account = await billing_repo.get_by_owner_id(resolve_owner_id(auth), consistent_read=True) or {}
    if account.get("metronome_voice_contract_id") and voice_plan.status(account) in {"pending_payment", "past_due"}:
        account = await voice_plan_payments.reconcile(resolve_owner_id(auth))
    status = voice_plan.status(account)
    available = voice_plan.available()
    ready = (
        bool(
            available
            and account.get("stripe_customer_id")
            and await voice_plan_payments.default_card(account["stripe_customer_id"])
        )
        if status == "not_enrolled"
        else False
    )
    action_url = next(
        (
            url
            for invoice, url in (account.get("voice_plan_invoice_actions") or {}).items()
            if (status == "pending_payment" or invoice in (account.get("voice_plan_unpaid_invoice_ids") or set()))
            and url
        ),
        None,
    )
    return {
        "ownerId": resolve_owner_id(auth),
        "status": status,
        "state": ("configured_inactive" if not available else "ready_to_enroll" if ready else "needs_payment_method")
        if status == "not_enrolled"
        else status,
        "endsAt": account.get("voice_plan_ending_at"),
        "cancelRequestedFor": account.get("voice_plan_cancel_target"),
        "available": available,
        "paymentReady": ready,
        "paymentActionUrl": action_url,
        "action": "checkout"
        if status == "not_enrolled" and available
        else "pay_invoice"
        if action_url and status in {"past_due", "pending_payment"}
        else "retry_cancellation"
        if status == "cancellation_pending"
        else None,
        "billingTimeZone": "UTC",
        "planVersion": voice_plan.PLAN_VERSION,
        "monthlyUsd": 250,
        "includedMinutes": 1500,
        "overageUsdPerMinute": 0.35,
    }


@router.post("/voice-plan")
async def enroll_voice_plan(body: VoicePlanEnrollment, auth: AuthContext = Depends(get_current_user)):
    """Admin acceptance of $250/month plus $0.35/minute after 1,500; may create a real invoice."""
    require_org_admin(auth)
    if body.expectedOwnerId != resolve_owner_id(auth):
        raise HTTPException(status_code=409, detail={"code": "workspace_changed"})
    try:
        return await voice_plan.enroll(resolve_owner_id(auth), accepted_by=auth.user_id)
    except voice_plan.VoicePlanError as exc:
        code = str(exc)
        raise HTTPException(
            status_code=503 if code == "voice_plan_unavailable" else 409, detail={"code": code}
        ) from exc
    except (httpx.HTTPError, stripe.error.StripeError) as exc:
        logger.warning("voice enrollment provider failure", exc_info=True)
        raise HTTPException(status_code=502, detail={"code": "voice_plan_provider_error"}) from exc


@router.post("/voice-plan/checkout")
async def checkout_voice_plan(body: VoicePlanEnrollment, auth: AuthContext = Depends(get_current_user)):
    """Accept the plan and collect an invoice-default card on Stripe's hosted setup page when needed."""
    require_org_admin(auth)
    if body.expectedOwnerId != resolve_owner_id(auth):
        raise HTTPException(status_code=409, detail={"code": "workspace_changed"})
    try:
        return await voice_plan_checkout.start(resolve_owner_id(auth), accepted_by=auth.user_id)
    except voice_plan.VoicePlanError as exc:
        code = str(exc)
        raise HTTPException(
            status_code=503 if code == "voice_plan_unavailable" else 409, detail={"code": code}
        ) from exc
    except (httpx.HTTPError, stripe.error.StripeError) as exc:
        raise HTTPException(status_code=502, detail={"code": "voice_plan_provider_error"}) from exc


@router.delete("/voice-plan")
async def cancel_voice_plan(
    auth: AuthContext = Depends(get_current_user), expectedOwnerId: str = Query(min_length=1, max_length=128)
):
    require_org_admin(auth)
    if expectedOwnerId != resolve_owner_id(auth):
        raise HTTPException(status_code=409, detail={"code": "workspace_changed"})
    try:
        return await voice_plan.cancel(resolve_owner_id(auth))
    except voice_plan.VoicePlanError as exc:
        raise HTTPException(status_code=409, detail={"code": str(exc)}) from exc
    except httpx.HTTPError as exc:
        logger.warning("voice cancellation provider failure", exc_info=True)
        raise HTTPException(status_code=502, detail={"code": "voice_plan_provider_error"}) from exc


async def _resolve_clerk_user(user_id: str) -> dict:
    """Resolve a Clerk user ID to display name and email."""
    if not settings.CLERK_SECRET_KEY:
        return {}
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"https://api.clerk.com/v1/users/{user_id}",
                headers={"Authorization": f"Bearer {settings.CLERK_SECRET_KEY}"},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                return {
                    "display_name": f"{data.get('first_name', '')} {data.get('last_name', '')}".strip() or None,
                    "email": (data.get("email_addresses") or [{}])[0].get("email_address"),
                }
    except Exception:
        pass
    return {}


async def _get_billing_account(auth: AuthContext) -> dict | None:
    """Resolve billing account from auth context."""
    return await billing_repo.get_by_owner_id(resolve_owner_id(auth))


# _resolve_owner_account was relocated to core/services/stripe_webhook_handlers.py
# as the public `resolve_owner_account` along with the dispatch-table refactor of
# handle_stripe_webhook (Tier 3A simplify audit). It had no callers outside the
# webhook chain, so the move is a clean relocation.


async def _block_reason(owner_id: str, account: dict | None) -> str | None:
    """The limit bar's hint must never 500 the billing page: a failed credit read reads as unblocked."""
    try:
        return await budget_block_reason(owner_id, account)
    except Exception:
        logger.warning("billing account block_reason failed owner_id=%s", owner_id, exc_info=True)
        return None


@router.get(
    "/trial-recap",
    summary="What the team did in its free trial",
    description="Day-28 recap numbers (T19). show=false outside the trial's last three days.",
    operation_id="get_trial_recap",
)
async def get_trial_recap(auth: AuthContext = Depends(get_current_user)):
    from core.services import trial_recap

    owner_id = resolve_owner_id(auth)
    account = await billing_repo.get_by_owner_id(owner_id)
    trial_end = (account or {}).get("trial_end")
    if (account or {}).get("subscription_status") != "trial_local" or not trial_recap.in_recap_window(
        int(trial_end) if trial_end is not None else None
    ):
        return {"show": False}
    return {"show": True, **await trial_recap.recap(owner_id, int(trial_end))}


def _front_desk_price_usd() -> int | None:
    """None while the Stripe price is unconfigured: nothing may advertise what checkout cannot bill."""
    return settings.FRONT_DESK_PRICE_USD if settings.STRIPE_FRONT_DESK_PRICE_ID else None


@router.get(
    "/prices",
    summary="The public plan price",
    description=(
        "Unauthenticated. The one plan's price as checkout bills it: the workspace price, the "
        "members it covers, the per-member seat price past them, and the free-trial length. "
        "The marketing site renders /pricing from this."
    ),
    operation_id="get_public_prices",
)
async def get_public_prices(response: Response):
    response.headers["Cache-Control"] = "public, max-age=300"
    return {
        "workspace_price_usd": settings.WORKSPACE_PRICE_USD,
        "included_members": SEATS_INCLUDED,
        "seat_price_usd": settings.SEAT_PRICE_USD,
        "front_desk_price_usd": _front_desk_price_usd(),
        "trial_days": billing_repo._TRIAL_LOCAL_DURATION_SECONDS // 86400,
        # The one billed copy is repo-root openrouter-pricing.json, fed in by CDK.
        "managed_markup": settings.OPENROUTER_MARKUP or None,
    }


@router.get(
    "/account",
    response_model=BillingAccountResponse,
    summary="Get billing account",
    description="Returns billing account details with real spend data.",
    operation_id="get_billing_account",
)
async def get_billing_account(
    auth: AuthContext = Depends(get_current_user),
):
    # Read-only endpoint. We do not auto-create a billing row when the caller
    # has no account — billing rows are written by the trial-subscription
    # signup flow (Plan 3 §7.1). Callers without an account are pre-signup
    # users; the response just reflects "no subscription" defaults.
    owner_id = resolve_owner_id(auth)
    account = await _get_billing_account(auth)

    # Current-period spend for analytics. Counter-only — the credit ledger
    # is the authoritative billing surface for card-3 users.
    summary = await get_usage_summary(owner_id)
    current_spend = summary["total_spend"]
    lifetime_spend = summary["lifetime_spend"]

    # Stripe-native subscription state. Both fields come from billing_repo
    # .set_subscription (set on subscription create + refreshed by the
    # customer.subscription.updated webhook). Account is None for pre-signup
    # responses → both fields stay None.
    subscription_status = account.get("subscription_status") if account else None
    trial_end = account.get("trial_end") if account else None

    # is_subscribed is the PAID predicate (``is_paid_subscription``), NOT
    # the platform-use predicate the chat/provision gates consult
    # (``is_subscription_active``). The two diverge exactly on trial_local:
    # a free-tier owner may use the platform but has not subscribed, and
    # rendering them as "Active subscription — $50/month" with no Subscribe
    # CTA (the old wiring) left them no path to pay. Gate call sites keep
    # platform-use semantics untouched.
    is_subscribed = is_paid_subscription(account)

    return BillingAccountResponse(
        is_subscribed=is_subscribed,
        current_spend=current_spend,
        lifetime_spend=lifetime_spend,
        subscription_status=subscription_status,
        trial_end=int(trial_end) if trial_end is not None else None,
        provider_choice=account.get("provider_choice") if account else None,
        byo_provider=account.get("byo_provider") if account else None,
        managed_inference_available=True,
        # A trial (or no row) has no plan: the "standard" default is only for PAID rows written
        # before the plan field existed. Reporting a trial as "standard" read as a paid Workspace.
        plan=(account.get("plan") or "standard") if account and subscription_status != "trial_local" else None,
        workspace_price_usd=settings.WORKSPACE_PRICE_USD,
        base_price_usd=settings.BASE_PRICE_USD,
        ultra_price_usd=settings.ULTRA_PRICE_USD,
        seat_price_usd=settings.SEAT_PRICE_USD,
        front_desk=bool(account.get("front_desk")) if account else False,
        front_desk_price_usd=_front_desk_price_usd(),
        included_members=SEATS_INCLUDED,
        managed_markup=settings.OPENROUTER_MARKUP or None,
        subscription_source=(account.get("subscription_source") if account else None),
        apple_iap_product_ids=list(apple_iap.SELLING_PRODUCTS) if settings.APPLE_IAP_SELLING else [],
        capped_today=bool(account) and account.get("openrouter_capped_day") == trial_cap.utc_today(),
        # Only capped owners have a daily allowance; omitted (None) when OpenRouter can't be read.
        allowance=await openrouter_keys.owner_allowance(owner_id) if trial_cap.is_managed_capped(account) else None,
        block_reason=await _block_reason(owner_id, account),
        provider_quota_until=_future_epoch(account.get("provider_quota_until") if account else None),
    )


def _future_epoch(value) -> int | None:
    return int(value) if value is not None and int(value) > time.time() else None


@router.get(
    "/usage",
    response_model=UsageSummary,
    summary="Get usage summary",
    description="Returns current period usage breakdown. Org admins see per-member usage.",
    operation_id="get_usage",
)
async def get_usage(
    auth: AuthContext = Depends(get_current_user),
):
    owner_id = resolve_owner_id(auth)
    summary = await get_usage_summary(owner_id)

    by_member: list[MemberUsage] = []

    # Org admins can see per-member breakdown
    if auth.is_org_context:
        require_org_admin(auth)
        members_raw = await usage_repo.get_member_usage(owner_id, summary["period"])

        # Resolve Clerk names in parallel
        async def _enrich(m: dict) -> MemberUsage:
            # Usage with no human behind it (agent runs, routines, webhooks) is attributed to the
            # owner id itself; Clerk has no user by that id, so name it instead of leaking org_….
            owned = m["user_id"] == owner_id
            clerk_info = {} if owned else await _resolve_clerk_user(m["user_id"])
            return MemberUsage(
                user_id=m["user_id"],
                display_name="Workspace agents & automations" if owned else clerk_info.get("display_name"),
                email=clerk_info.get("email"),
                total_spend=m["total_spend_microdollars"] / 1_000_000,
                total_input_tokens=m["total_input_tokens"],
                total_output_tokens=m["total_output_tokens"],
                request_count=m["request_count"],
                capture_prompt_tokens=m["capture_prompt_tokens"],
                capture_completion_tokens=m["capture_completion_tokens"],
            )

        by_member = await asyncio.gather(*[_enrich(m) for m in members_raw])

    return UsageSummary(
        period=summary["period"],
        total_spend=summary["total_spend"],
        total_input_tokens=summary["total_input_tokens"],
        total_output_tokens=summary["total_output_tokens"],
        total_cache_read_tokens=summary["total_cache_read_tokens"],
        total_cache_write_tokens=summary["total_cache_write_tokens"],
        request_count=summary["request_count"],
        lifetime_spend=summary["lifetime_spend"],
        by_member=list(by_member),
    )


@router.get(
    "/my-usage",
    response_model=MyUsageResponse,
    summary="Get current user's own usage",
    description="Returns the authenticated user's personal usage for the current billing period. No admin gating — any authenticated user can see their own usage.",
    operation_id="get_my_usage",
)
async def get_my_usage(
    auth: AuthContext = Depends(get_current_user),
):
    from datetime import datetime, timezone

    owner_id = resolve_owner_id(auth)
    user_id = auth.user_id

    now = datetime.now(timezone.utc)
    period = f"{now.year}-{now.month:02d}"

    member_key = f"member:{user_id}:{period}"
    m = await usage_repo.get_period_usage(owner_id, member_key) or {}

    return MyUsageResponse(
        period=period,
        total_spend=m.get("total_spend_microdollars", 0) / 1_000_000,
        total_input_tokens=m.get("total_input_tokens", 0),
        total_output_tokens=m.get("total_output_tokens", 0),
        request_count=m.get("request_count", 0),
    )


@router.get(
    "/spend",
    response_model=SpendResponse,
    summary="Spend summary",
    description="Always tokens_only: the panel keeps its token grid. Kept for the web/mobile callers.",
    operation_id="get_spend",
)
async def get_spend(auth: AuthContext = Depends(get_current_user)):
    # Same org-member gating as GET /billing/usage.
    if auth.is_org_context:
        require_org_admin(auth)
    return SpendResponse(mode="tokens_only")


@router.post(
    "/portal",
    response_model=PortalResponse,
    summary="Create customer portal session",
    description="Creates a Stripe Customer Portal session for payment management.",
    operation_id="create_portal",
)
async def create_portal(
    auth: AuthContext = Depends(get_current_user),
):
    if auth.is_org_context:
        require_org_admin(auth)

    account = await _get_billing_account(auth)
    if not account:
        raise HTTPException(status_code=404, detail="Billing account not found")

    billing_service = BillingService()
    url = await billing_service.create_portal_session(account)
    return PortalResponse(portal_url=url)


# ---------------------------------------------------------------------------
# Workspace subscription checkout.
# ---------------------------------------------------------------------------


class CheckoutRequest(BaseModel):
    # One plan (T06): Base and Ultra are retired and no longer sold. Existing
    # rows on them keep billing until moved; the webhook still reads their prices.
    plan: Literal["workspace"] = "workspace"
    return_to: Literal["web", "app"] = "web"
    # Required to change the plan of a live subscription; without it the
    # response is ``confirm_required`` with the cost and nothing is changed.
    confirm: bool = False
    # Where a web Checkout returns (fixed paths, billing_service._CHECKOUT_RETURNS): "chatgpt" = ChatGPT sign-in in
    # Settings → Model, "chatgpt_onboarding" = the onboarding ChatGPT step; absent = the billing panel.
    then: Literal["chatgpt", "chatgpt_onboarding"] | None = None


@router.post(
    "/checkout",
    summary="Start or change a workspace subscription",
    operation_id="create_workspace_checkout",
)
async def create_workspace_checkout(
    body: CheckoutRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """The only subscription Checkout entry point for an active workspace."""
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    account = await _get_billing_account(auth)
    if apple_iap.apple_is_live(account):
        # Billed through the App Store; a web checkout would start a second subscription.
        raise HTTPException(status_code=409, detail="apple_managed")
    if not account or not account.get("stripe_customer_id"):
        try:
            await BillingService().create_customer_for_owner(
                owner_id=owner_id,
                owner_type="org",
                email=auth.email,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("workspace checkout customer creation failed owner_id=%s: %s", owner_id, e)
            raise HTTPException(status_code=502, detail="Could not reach billing provider") from e

    from core.services.billing_service import BillingServiceError, create_workspace_checkout as create_checkout

    try:
        result = await create_checkout(
            owner_id=owner_id,
            clerk_user_id=auth.user_id,
            plan=body.plan,
            return_to=body.return_to,
            confirm=body.confirm,
            then=body.then,
        )
    except BillingServiceError as e:
        if str(e) == "checkout_in_progress":
            raise HTTPException(status_code=409, detail="checkout_in_progress") from e
        raise HTTPException(status_code=400, detail=str(e)) from e

    return result


class FrontDeskRequest(BaseModel):
    return_to: Literal["web", "app"] = "web"


@router.post(
    "/front-desk",
    summary="Add the Front desk add-on, or start Checkout for Team plus the add-on",
    operation_id="create_front_desk",
)
async def create_front_desk(
    body: FrontDeskRequest,
    auth: AuthContext = Depends(get_current_user),
):
    """The line itself is provisioned by the Stripe webhook, never by this click."""
    require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    account = await _get_billing_account(auth)
    if apple_iap.apple_is_live(account):
        raise HTTPException(status_code=409, detail="apple_managed")

    # The add-on needs the Customer Operations agent; refuse before any billing call.
    if not await customer_ops_agent_id(owner_id):
        raise HTTPException(status_code=409, detail="no_front_desk_agent")

    if apple_iap.stripe_is_live(account):
        outcome = await billing_service.add_front_desk_item(owner_id)
        if outcome == "already":
            # The subscription has the add-on but the line may not: a failed purchase leaves no event to retry.
            try:
                await text_lines.ensure_line(owner_id)
            except (billing_service.BillingServiceError, photon.PhotonError) as e:
                logger.warning("front desk line request failed owner_id=%s: %s", owner_id, e)
                raise HTTPException(status_code=502, detail="line_provision_failed") from e
        if outcome in ("added", "already"):
            return {"status": outcome}
        raise HTTPException(status_code=400, detail=outcome)

    if not account or not account.get("stripe_customer_id"):
        try:
            await BillingService().create_customer_for_owner(
                owner_id=owner_id,
                owner_type="org",
                email=auth.email,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("front desk customer creation failed owner_id=%s: %s", owner_id, e)
            raise HTTPException(status_code=502, detail="Could not reach billing provider") from e

    try:
        result = await billing_service.create_workspace_checkout(
            owner_id=owner_id,
            clerk_user_id=auth.user_id,
            plan="workspace",
            return_to=body.return_to,
            front_desk=True,
        )
    except billing_service.BillingServiceError as e:
        if str(e) == "checkout_in_progress":
            raise HTTPException(status_code=409, detail="checkout_in_progress") from e
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {"status": "checkout", "url": result["checkout_url"]}


# Hard cap on credit-purchase amounts — a safety rail: without an upper
# bound, a compromised session could lock in an unreasonable
# charge-automatically payment against the saved card. Metronome/Stripe
# would likely reject extremes too, but defense in depth says we cap
# server-side. Raise via a code change + a pricing-tier conversation.
#
# Defined with the top-up request so its upper bound stays an explicit payment
# safety rail independent of subscription Checkout.
_TOP_UP_MAX_CENTS = 100_000  # $1,000.00 — single-shot prepay ceiling


# ---------------------------------------------------------------------------
# Post-onboarding provider switch (POST /billing/provider)
# ---------------------------------------------------------------------------


class ChangeProviderRequest(BaseModel):
    # Pydantic validates choices before any DDB read or container side-effect.
    provider_choice: Literal["chatgpt_oauth", "byo_key", "openrouter_managed"] = Field(
        ...,
        description="chatgpt_oauth | byo_key | openrouter_managed",
    )
    byo_provider: Literal["openai", "anthropic"] | None = Field(
        None,
        description=(
            "Required when provider_choice == 'byo_key'. Switching to BYO "
            "without a previously-uploaded matching API key returns 409 — "
            "the caller must PUT /settings/keys/{provider} before this call."
        ),
    )


@router.post(
    "/provider",
    summary="Change the owner's LLM provider mid-flight",
    description=(
        "Post-onboarding provider switch. Requires an active subscription "
        "(subscribers only; workspace subscription is started at /billing/checkout). "
        "Flips provider_choice on the billing row, clears the old provider's "
        "side-effect state (OAuth tokens / BYOK Secrets Manager secrets), and "
        "re-provisions the user's container with the new provider. The "
        "container restart ends the current chat session — the frontend "
        "surfaces this as a cutover warning before posting."
    ),
    operation_id="change_provider",
)
async def change_provider(
    body: ChangeProviderRequest,
    auth: AuthContext = Depends(get_current_user),
):
    from core.services.billing_service import (
        ChangeProviderError,
        change_provider as change_provider_service,
    )

    # Org admins only — mirrors /trial-checkout. A non-admin member can't
    # blow away the org's container by clicking "Change provider".
    if auth.is_org_context:
        require_org_admin(auth)

    owner_id = resolve_owner_id(auth)
    owner_type = get_owner_type(auth)

    try:
        return await change_provider_service(
            owner_id=owner_id,
            clerk_user_id=auth.user_id,
            owner_type=owner_type,
            provider_choice=body.provider_choice,
            byo_provider=body.byo_provider,
        )
    except ChangeProviderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        ) from e


# ---------------------------------------------------------------------------
# Credit endpoints — Metronome is the only balance source
# ---------------------------------------------------------------------------


class TopUpRequest(BaseModel):
    amount_cents: int = Field(
        ...,
        ge=500,
        le=_TOP_UP_MAX_CENTS,
        description="Min $5 (500 cents), max $1,000 (100000 cents).",
    )
    request_id: UUID = Field(
        ...,
        description=(
            "Client-generated UUID — one per user intent and stable across "
            "retries. A retry replays the original Metronome commit instead "
            "of charging twice."
        ),
    )


def _is_exhausted(balance_microcents: int | None) -> bool:
    """Whether the owner's credits are unspendable at the gate.

    The gate admits paid traffic only while `remaining - reserved - estimate
    > 0`, so a balance at or below one reservation buys nothing. Surfacing the
    gate's own threshold keeps the frontend from needing a copy of the
    constant. A null balance (Metronome outage / no customer) is INDETERMINATE
    and never exhausted — a false banner is worse than a late one.
    """
    from core.services.metronome_gate import RESERVE_ESTIMATE_MICROCENTS

    if balance_microcents is None:
        return False
    return balance_microcents <= RESERVE_ESTIMATE_MICROCENTS


async def _metronome_balance_microcents(owner_id: str) -> int | None:
    """The owner's Metronome prepaid balance, in microcents.

    None when the Metronome lifecycle switch is off, on ANY failure, or
    when the owner has no Metronome customer yet — a Metronome outage (or
    an unprovisioned owner) must never 500 or slow-fail the balance
    endpoint into an error state. The synchronous gates never read this
    (they use the fast-gate projection — core/services/metronome_gate.py).
    """
    try:
        # Shared with the gate's lazy refresh — one place owns the
        # live-verified getNetBalance response shape and the cents →
        # microcents conversion.
        from core.services.metronome_gate import read_net_balance_microcents

        return await read_net_balance_microcents(owner_id)
    except Exception:
        logger.warning("Metronome balance read failed for owner %s — omitting from response", owner_id, exc_info=True)
        return None


@router.get(
    "/credits/balance",
    summary="Get the user's prepaid credit balance",
    description=(
        "Returns the owner's Metronome prepaid balance in microcents and "
        "dollars (formatted). Both fields are null when the balance is "
        "unavailable (Metronome outage, or the owner has no Metronome "
        "customer yet)."
    ),
)
async def get_credits_balance(
    request_id: UUID | None = None,
    ctx: AuthContext = Depends(get_current_user),
):
    # Credits pool at the owner level (org_id for org context, user_id for
    # personal). Keying on ctx.user_id meant org members couldn't see the
    # admin-funded balance and got blocked at chat with out_of_credits.
    owner_id = resolve_owner_id(ctx)
    from core.services.webhook_dedup import get_active_top_up, get_top_up_intent

    balance_uc = await _metronome_balance_microcents(owner_id)
    if balance_uc is not None:
        # Write the live read through the fast-gate projection and run the
        # same restore/re-enforce hook as the gate's lazy refresh: a paid-up
        # owner polling this endpoint (the onboarding wizard does, every few
        # seconds) un-blocks the provision gate immediately instead of after
        # the staleness window expires. Best-effort by contract — it never
        # raises into this endpoint.
        from core.services.metronome_gate import apply_fresh_balance

        await apply_fresh_balance(owner_id, balance_uc)
    active = await get_active_top_up(owner_id)
    if request_id is not None:
        intent = await get_top_up_intent(owner_id, str(request_id))
        if intent:
            active = {
                "request_id": intent["request_id"],
                "amount_cents": intent["amount_cents"],
                "status": (
                    active["status"]
                    if active and active.get("intent_event_id") == intent["event_id"]
                    else "requested"
                    if intent["state"] == "completed"
                    else intent["state"]
                ),
            }
        else:
            active = None
    return {
        "balance_microcents": balance_uc,
        "balance_dollars": f"{balance_uc / 1_000_000:.2f}" if balance_uc is not None else None,
        "is_exhausted": _is_exhausted(balance_uc),
        "active_top_up": (
            {
                "request_id": active["request_id"],
                "amount_cents": active["amount_cents"],
                "status": active["status"],
            }
            if active
            else None
        ),
    }


@router.post(
    "/credits/top_up",
    summary="Request a credit top-up (Metronome payment-gated commit)",
    description=(
        "Creates a PAYMENT-GATED Metronome prepaid commit. Metronome invoices "
        "the owner's saved Stripe card (charge_automatically) and releases the "
        "credits when the payment collects — there is no checkout redirect and "
        "no local balance write. The balance updates asynchronously; the gate's "
        "lazy refresh picks it up."
    ),
    operation_id="credits_top_up",
)
async def top_up_credits(
    body: TopUpRequest,
    ctx: AuthContext = Depends(get_current_user),
):
    import httpx

    from core.billing.metronome_client import MetronomeError
    from core.billing.metronome_funding import FundingError, request_top_up
    from core.services.webhook_dedup import TopUpIntentConflict

    require_org_admin(ctx)
    owner_id = resolve_owner_id(ctx)
    try:
        await request_top_up(owner_id, body.amount_cents, request_id=str(body.request_id))
    except TopUpIntentConflict as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    except FundingError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except (MetronomeError, httpx.HTTPError) as e:
        # A provider-side rejection/outage is not the caller's fault and must
        # not surface as an unhandled 500 — money endpoints answer precisely.
        logger.exception("Metronome top-up failed for owner %s", owner_id)
        raise HTTPException(status_code=502, detail="billing provider error") from e

    # Pull the fresh balance through the gate right after the commit so a
    # depleted owner who buys credits is re-armed immediately. Restore is
    # pull-based (no payment-success webhook reaches us), so it otherwise waits
    # for the next balance poll or chat-gate refresh — both race the owner's
    # FIRST send after buying credits, which then fails AccessDenied (observed
    # live 2026-06-11).
    #
    # RESTORE-ONLY here: act ONLY on a confirmed-POSITIVE balance. A ≤0 read in
    # this path is the gated commit not yet released (Stripe collection is
    # async) — NOT a depleted owner — so handing it to apply_fresh_balance
    # (which re-asserts the revoke on ≤0) would wrongly REVOKE someone who just
    # paid. A top-up never depletes, so there is nothing to enforce here; the
    # normal lazy refresh (chat / balance poll) re-arms once the credits land.
    # Best-effort: apply_fresh_balance never raises.
    try:
        from core.services.metronome_gate import apply_fresh_balance, read_net_balance_microcents

        balance_uc = await read_net_balance_microcents(owner_id)
        if balance_uc is not None and balance_uc > 0:
            await apply_fresh_balance(owner_id, balance_uc)
    except Exception:
        logger.warning("Post-top-up balance refresh failed for owner %s", owner_id, exc_info=True)

    return {"status": "requested", "amount_cents": body.amount_cents}


@router.post(
    "/webhooks/stripe",
    summary="Handle Stripe webhooks",
    description="Processes Stripe webhook events for subscription lifecycle.",
    operation_id="handle_stripe_webhook",
    include_in_schema=False,
)
async def handle_stripe_webhook(
    request: Request,
):
    """Handle Stripe webhook events. No Clerk auth — uses Stripe signature."""
    body = await request.body()
    sig = request.headers.get("stripe-signature", "")

    try:
        event = stripe.Webhook.construct_event(body, sig, settings.STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        put_metric("stripe.webhook.sig_fail")
        logger.error("Stripe webhook signature verification failed: %s", e)
        raise HTTPException(status_code=400, detail="Invalid signature")

    # Dedup check — Stripe replays webhooks on any non-2xx and on its own
    # at-least-once delivery insurance. The local import keeps the helper's
    # boto3 client out of cold-start until first use.
    from core.services.webhook_dedup import (
        WebhookDedupResult,
        record_event_or_skip,
    )

    dedup = await record_event_or_skip(event["id"], source="stripe")
    if dedup is WebhookDedupResult.ALREADY_SEEN:
        put_metric(
            "stripe.webhook.dedup_skipped",
            dimensions={"event_type": event["type"]},
        )
        return Response(status_code=200)

    event_type = event["type"]
    put_metric("stripe.webhook.received", dimensions={"event_type": event_type})

    billing_service = BillingService()
    handler = STRIPE_WEBHOOK_HANDLERS.get(event_type)
    if handler is None:
        return {"status": "ok"}

    try:
        early_response = await handler(event, billing_service)
    except Exception:
        # The dedup row was recorded BEFORE dispatch, so a handler failure
        # would otherwise eat the event permanently: the 500 makes Stripe
        # redeliver, but the retry hits ALREADY_SEEN and is acked without
        # processing. For a credit-top-up grant that means money collected
        # and credits never granted. Roll the dedup row back so the
        # redelivery gets to re-attempt — handlers with side effects are
        # idempotent (the Metronome grant keys on the Checkout session id),
        # so a partial first attempt replays safely. Same pattern as the
        # Metronome webhook's dedup rollback.
        from core.services.webhook_dedup import delete_event

        await delete_event(event["id"])
        raise
    if early_response is not None:
        return early_response
    return {"status": "ok"}
