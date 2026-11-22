"""Billing API endpoints — flat-fee subscription + Metronome-backed credits."""

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
    is_org_owner_id,
    get_current_user,
    resolve_owner_id,
    require_org_admin,
)
from core.config import settings
from core.billing import voice_billing, voice_plan, voice_plan_checkout
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.repositories.workspace_repo import customer_ops_agent_id
from core.services import apple_iap, billing_service, openrouter_keys, os_billing, photon, text_lines, trial_cap
from core.services.billing_service import BillingService
from core.services.chat_gate import budget_block_reason
from core.services.stripe_webhook_handlers import STRIPE_WEBHOOK_HANDLERS
from core.services.provision_gate import (
    is_paid_subscription,
)
from core.services.usage_service import get_usage_summary
from schemas.billing import (
    BillingAccountResponse,
    PortalResponse,
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
    status = voice_plan.status(account)
    available = voice_plan.available()
    return {
        "ownerId": resolve_owner_id(auth),
        "status": status,
        "state": ("ready_to_enroll" if available else "configured_inactive") if status == "not_enrolled" else status,
        "endsAt": account.get("voice_plan_ending_at"),
        "available": available,
        "action": "checkout" if status == "not_enrolled" and available else None,
        "billingTimeZone": "UTC",
        "planVersion": voice_plan.PLAN_VERSION,
        "monthlyUsd": voice_billing.MONTHLY_USD,
        "includedMinutes": voice_billing.INCLUDED_MINUTES,
        "overageUsdPerMinute": voice_billing.OVERAGE_USD_PER_MINUTE,
    }


@router.post("/voice-plan/checkout")
async def checkout_voice_plan(body: VoicePlanEnrollment, auth: AuthContext = Depends(get_current_user)):
    """Accept the plan and open Stripe Checkout for the $250/month subscription (promotion codes allowed)."""
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
    except (httpx.HTTPError, stripe.error.StripeError) as exc:
        logger.warning("voice cancellation provider failure", exc_info=True)
        raise HTTPException(status_code=502, detail={"code": "voice_plan_provider_error"}) from exc


async def _get_billing_account(auth: AuthContext) -> dict | None:
    """Resolve billing account from auth context."""
    return await billing_repo.get_by_owner_id(resolve_owner_id(auth))


async def _ensure_person_trial(auth: AuthContext) -> None:
    """Before a checkout, a person's tenant gets its trial row (create-if-absent, unarmed, as the provisioning doors
    write it), so a Stripe customer joins the trial instead of replacing it. An org's row comes from its webhook."""
    owner_id = resolve_owner_id(auth)
    if not is_org_owner_id(owner_id):
        from core.services.user_provision import _ensure_trial_row

        await _ensure_trial_row(owner_id, actor_user_id=auth.user_id)


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
        "Unauthenticated. What a new business can buy, as checkout bills it. The marketing site "
        "renders /pricing from this. The voice_* fields are the voice receptionist plan (what "
        "/voice-plan bills), null while that plan cannot be bought. The retired team plan and "
        "its trial are not advertised here."
    ),
    operation_id="get_public_prices",
)
async def get_public_prices(response: Response):
    response.headers["Cache-Control"] = "public, max-age=300"
    # Nothing may advertise what checkout cannot bill: null until voice_plan.available().
    voice = voice_plan.available()
    return {
        "voice_plan_monthly_usd": voice_billing.MONTHLY_USD if voice else None,
        "voice_included_minutes": voice_billing.INCLUDED_MINUTES if voice else None,
        "voice_overage_usd_per_minute": voice_billing.OVERAGE_USD_PER_MINUTE if voice else None,
        "front_desk_price_usd": _front_desk_price_usd(),
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
        front_desk=bool(account.get("front_desk")) if account else False,
        front_desk_price_usd=_front_desk_price_usd(),
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
    # Where a web Checkout returns (fixed paths, billing_service._CHECKOUT_RETURNS):
    # the two onboarding values resume the selected provider setup after the plan is active.
    then: Literal["chatgpt", "chatgpt_onboarding", "managed_onboarding"] | None = None


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
    await _ensure_person_trial(auth)
    account = await _get_billing_account(auth)
    if apple_iap.apple_is_live(account):
        # Billed through the App Store; a web checkout would start a second subscription.
        raise HTTPException(status_code=409, detail="apple_managed")
    if not account or not account.get("stripe_customer_id"):
        try:
            await BillingService().create_customer_for_owner(
                owner_id=owner_id,
                owner_type="org" if is_org_owner_id(owner_id) else "personal",
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
    await _ensure_person_trial(auth)
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
                owner_type="org" if is_org_owner_id(owner_id) else "personal",
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


# ---------------------------------------------------------------------------
# Credit endpoints — Metronome is the only balance source
# ---------------------------------------------------------------------------


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
    # A Spark subscription is the person's, never a workspace's: its own handler, before any workspace one.
    handler = os_billing.stripe_handler(event) or STRIPE_WEBHOOK_HANDLERS.get(event_type)
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
