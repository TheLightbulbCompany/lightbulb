"""Billing API endpoints — flat-fee subscription + Metronome-backed credits."""

import asyncio
import logging
from typing import Literal

import httpx
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.auth import (
    AuthContext,
    get_current_user,
    get_owner_type,
    is_org_owner_id,
    resolve_owner_id,
    require_org_admin,
)
from core.config import settings
from core.observability.metrics import put_metric, timing
from core.repositories import billing_repo, usage_repo
from core.services import clerk_admin, spend_service
from core.services.billing_service import BillingService
from core.services.container_provision import resolve_provider_choice
from core.services.stripe_webhook_handlers import STRIPE_WEBHOOK_HANDLERS
from core.services.provision_gate import (
    LIVE_SUBSCRIPTION_STATUSES,
    TRIAL_BLOCKED_STATUSES,
    is_paid_subscription,
    is_trial_blocked,
)
from core.services.usage_service import get_usage_summary
from core.billing.bedrock_pricing import get_all_rates
from core.tenancy_codes import PENDING_ORG_INVITATION
from schemas.billing import (
    BillingAccountResponse,
    PortalResponse,
    UsageSummary,
    MemberUsage,
    MyUsageResponse,
    PricingResponse,
    ModelPriceResponse,
    SpendResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()


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
        plan=(account.get("plan") or "standard") if account else "standard",
        workspace_price_usd=settings.WORKSPACE_PRICE_USD,
        ultra_price_usd=settings.ULTRA_PRICE_USD,
        subscription_source=(account.get("subscription_source") if account else None),
    )


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
            clerk_info = await _resolve_clerk_user(m["user_id"])
            return MemberUsage(
                user_id=m["user_id"],
                display_name=clerk_info.get("display_name"),
                email=clerk_info.get("email"),
                total_spend=m["total_spend_microdollars"] / 1_000_000,
                total_input_tokens=m["total_input_tokens"],
                total_output_tokens=m["total_output_tokens"],
                request_count=m["request_count"],
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
    summary="Provider-aware spend summary",
    description=(
        "Bedrock owners: month-to-date BILLED spend from Metronome with a "
        "per-model split. ChatGPT-OAuth owners: plan-quota windows. BYOK/"
        "trial/degraded: tokens_only (the panel keeps its token grid). "
        "Display-only; never errors on upstream failure."
    ),
    operation_id="get_spend",
)
async def get_spend(auth: AuthContext = Depends(get_current_user)):
    # Same org-member gating as GET /billing/usage — spend is billing-account
    # detail, not something a non-admin org member should be able to read.
    if auth.is_org_context:
        require_org_admin(auth)
    owner_id = resolve_owner_id(auth)
    # resolve_provider_choice is THE single resolver (container_provision.py)
    # — it subsumes the old raw billing-row read + None short-circuit here,
    # and additionally defaults a legacy owner with no stored provider_choice
    # to bedrock_claude (instead of silently degrading them to tokens_only),
    # and maps trial_local owners to the bedrock_trial config mode, which
    # spend_service.get_spend_summary has no branch for and so falls through
    # to tokens_only — the free tier deliberately shows no dollars.
    try:
        provider_choice, _byo_provider = await resolve_provider_choice(owner_id)
    except Exception:
        logger.warning(
            "resolve_provider_choice failed for owner %s — degrading /billing/spend to tokens_only",
            owner_id,
            exc_info=True,
        )
        return SpendResponse(mode="tokens_only")
    payload = await spend_service.get_spend_summary(owner_id, provider_choice)
    return SpendResponse(**payload)


@router.get(
    "/pricing",
    response_model=PricingResponse,
    summary="Get model pricing",
    description=(
        "Returns per-token Bedrock Claude pricing for the credit-ledger UI. "
        "Card-3 (bedrock_claude) users pay raw Bedrock prices times the credit-ledger "
        "markup (applied on deduct, not surfaced here)."
    ),
    operation_id="get_pricing",
)
async def get_pricing(
    auth: AuthContext = Depends(get_current_user),
):
    # Schema convention is USD per token; rate table is USD per million
    # tokens. Convert here so the API contract is unchanged.
    all_rates = get_all_rates()
    models = {
        model_id: ModelPriceResponse(
            input=rate["input"] / 1_000_000,
            output=rate["output"] / 1_000_000,
            cache_read=rate["cache_read"] / 1_000_000,
            cache_write=rate["cache_write"] / 1_000_000,
        )
        for model_id, rate in all_rates.items()
    }
    return PricingResponse(models=models)


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
    plan: Literal["workspace", "ultra"]


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
        result = await create_checkout(owner_id=owner_id, clerk_user_id=auth.user_id, plan=body.plan)
    except BillingServiceError as e:
        if str(e) == "checkout_in_progress":
            raise HTTPException(status_code=409, detail="checkout_in_progress") from e
        raise HTTPException(status_code=400, detail=str(e)) from e

    return result


# Retired checkout handlers remain temporarily as unregistered compatibility
# code for the webhook/provider cleanup. They are deliberately not API routes.

# Hard cap on credit-purchase amounts — a safety rail: without an upper
# bound, a compromised session could lock in an unreasonable
# charge-automatically payment against the saved card. Metronome/Stripe
# would likely reject extremes too, but defense in depth says we cap
# server-side. Raise via a code change + a pricing-tier conversation.
#
# Defined here rather than beside TopUpRequest because the subscription
# Checkout below now bundles starter credits and must enforce the SAME
# $5–$1,000 window. Two constants would let the bundled path drift into
# accepting an amount the standalone top-up rejects.
_TOP_UP_MAX_CENTS = 100_000  # $1,000.00 — single-shot prepay ceiling


class TrialCheckoutRequest(BaseModel):
    # Pydantic Literal validates at request time (422 on unknown values), so
    # the body never reaches billing_repo / Stripe with junk that would later
    # blow up ECS provisioning. byo_provider is optional even for byo_key —
    # the picker submits {provider_choice: "byo_key"} alone, then a later
    # wizard step collects byo_provider + the actual API key.
    provider_choice: Literal["chatgpt_oauth", "byo_key", "bedrock_claude"] = Field(
        ...,
        description="chatgpt_oauth | byo_key | bedrock_claude",
    )
    byo_provider: Literal["openai", "anthropic"] | None = Field(
        None,
        description=(
            "Optional. Set later via the BYO wizard step after Stripe checkout "
            "completes. The gateway/provisioner enforces a populated byo_provider "
            "(plus an actual key) as a separate downstream gate."
        ),
    )
    credits_cents: int | None = Field(
        None,
        ge=500,
        le=_TOP_UP_MAX_CENTS,
        description=(
            "Starter Claude credits to bundle into this Checkout as a one-time "
            "line item, for provider_choice=bedrock_claude only. Min $5, max "
            "$1,000 — the same window the standalone top-up enforces. Omit for "
            "the default. Ignored for the other two providers, which spend no "
            "Lightbulb credits."
        ),
    )


async def create_trial_checkout(
    body: TrialCheckoutRequest,
    auth: AuthContext = Depends(get_current_user),
):
    from core.services.billing_service import (
        BillingService,
        BillingServiceError,
        create_flat_fee_checkout,
        normalize_starter_credits,
    )

    # Note: provider_choice and byo_provider enums are enforced by pydantic
    # Literal in TrialCheckoutRequest — invalid values produce 422 before
    # reaching this handler. byo_provider is intentionally optional even
    # for byo_key: ProvisioningStepper.ProviderPicker submits
    # {provider_choice: "byo_key"} alone, then collects byo_provider
    # (openai vs anthropic) and the actual API key in a later wizard step
    # after Stripe checkout completes. The gateway/provisioner enforces
    # "byo_key + populated byo_provider + populated key" as a separate
    # downstream gate before any agent run.

    # Tenancy invariant (Gate B): refuse a PERSONAL trial-checkout when
    # the caller has any pending org invitations. We only check the
    # personal path — an org-context call to /trial-checkout is the
    # admin subscribing the org, which can never have personal pending
    # invites by construction.
    if not auth.is_org_context:
        pending = await clerk_admin.list_pending_invitations_for_user(auth.user_id)
        if pending:
            org_name = (pending[0].get("public_organization_data") or {}).get("name") or "an organization"
            put_metric(
                "billing.trial_checkout.blocked",
                dimensions={"reason": PENDING_ORG_INVITATION},
            )
            logger.info(
                "billing.trial_checkout.blocked user=%s reason=pending_org_invitation org=%s",
                auth.user_id,
                org_name,
            )
            raise HTTPException(
                status_code=409,
                detail={
                    "code": PENDING_ORG_INVITATION,
                    "message": (
                        f"You have a pending invitation to {org_name}. Accept it before subscribing personally."
                    ),
                    "redirect_to": "/onboarding/invitations",
                },
            )

    # Org-context callers must be admins — without this gate any org member
    # could create a Stripe Checkout against the org's billing account and
    # spawn a parallel subscription.
    if auth.is_org_context:
        require_org_admin(auth)

    owner_id = resolve_owner_id(auth)
    account = await _get_billing_account(auth)
    if not account:
        owner_type = get_owner_type(auth)
        billing_service = BillingService()
        account = await billing_service.create_customer_for_owner(
            owner_id=owner_id,
            owner_type=owner_type,
            email=auth.email,
        )

    # Refuse a second checkout for any subscription that exists,
    # regardless of state. Stripe Checkout in mode="subscription"
    # creates a NEW subscription on every successful return, so a retry
    # past the 5-minute idempotency-bucket window would charge the
    # customer twice.
    #
    # ``is_trial_blocked`` (canonical set in ``provision_gate.py``) blocks
    # any state that has ever billed or might still bill —
    # {active, trialing, past_due, canceled, incomplete, incomplete_expired,
    # unpaid, paused}. The {canceled, …} entries date from audit C3, when
    # checkout carried a 14-day Stripe trial and cancel-before-conversion
    # could mint unlimited free trials; checkout now bills immediately, but
    # blocking canceled rows still prevents accidental double-subscribe.
    # Legitimate re-subscribe-after-cancel goes through customer support.
    existing_status = account.get("subscription_status")
    has_legacy_sub = bool(account.get("stripe_subscription_id"))
    if is_trial_blocked(account):
        raise HTTPException(
            status_code=409,
            detail=f"already_subscribed:{existing_status}",
        )
    # Legacy rows: stripe_subscription_id is set but subscription_status
    # hasn't been backfilled. Verify the live Stripe state instead of
    # creating a parallel subscription.
    if has_legacy_sub:
        sub_id = account["stripe_subscription_id"]
        try:
            with timing("stripe.api.latency", {"op": "subscription.retrieve"}):
                live_sub = stripe.Subscription.retrieve(sub_id)
        except stripe.error.InvalidRequestError as e:
            # "resource_missing" → Stripe forgot the sub (lost cancel
            # webhook); the local row is stale. Allow the new checkout.
            if getattr(e, "code", None) != "resource_missing":
                raise
            logger.info(
                "Stored sub %s not found in Stripe for owner %s — allowing fresh checkout",
                sub_id,
                owner_id,
            )
        else:
            # Same expanded blocklist as the local-row path — a stale
            # canceled/incomplete subscription in Stripe is still a
            # "this account already subscribed" signal (audit C3).
            if live_sub.get("status") in TRIAL_BLOCKED_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=f"already_subscribed:{live_sub.get('status')}",
                )
    elif account.get("stripe_customer_id"):
        # No stored subscription id — but the Stripe customer may still carry
        # a live subscription this row hasn't heard about: a checkout that
        # completed seconds ago with its webhook in flight, or a transient
        # `incomplete` that handle_subscription_state deliberately does not
        # persist over trial_local. Trusting the local row alone would mint a
        # second Checkout session and double-subscribe, so ask Stripe
        # directly. Match on subscription metadata owner_id — the customer is
        # shared across personal/org billing rows for the same email, and
        # another owner's subscription must not block this one.
        with timing("stripe.api.latency", {"op": "subscription.list"}):
            live_subs = stripe.Subscription.list(customer=account["stripe_customer_id"], status="all", limit=100)
        # auto_paging_iter walks has_more pages — a matching billable sub must
        # not escape the guard by sitting behind newer subs on the shared
        # customer.
        for sub in live_subs.auto_paging_iter():
            if (sub.get("metadata") or {}).get("owner_id") != owner_id:
                continue
            if sub.get("status") in LIVE_SUBSCRIPTION_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=f"already_subscribed:{sub.get('status')}",
                )

    # Single-open-session guard: at most one live Checkout session per owner.
    # A user who clicks Upgrade twice (two tabs, browser back, a different
    # provider card) must never hold two completable sessions — completing
    # both creates two charged subscriptions, and no subscription exists at
    # session-open time for the guards above to see. The marker is written by
    # create_flat_fee_checkout after every session create; the session's LIVE
    # status decides what it means, so a stale marker self-heals.
    pending_id = account.get("pending_checkout_session_id")
    if pending_id:
        pending = None
        try:
            with timing("stripe.api.latency", {"op": "checkout.session.retrieve"}):
                pending = stripe.checkout.Session.retrieve(pending_id)
        except stripe.error.InvalidRequestError:
            logger.info("Pending checkout session %s not retrievable — minting fresh", pending_id)
        if pending is not None and pending.get("status") == "open":
            # "Same intent" has to mean the same BODY, not just the same
            # provider. Provider alone was sufficient while it fully determined
            # the session; bundled starter credits made the amount a second
            # body-varying input, and this guard returns before
            # create_flat_fee_checkout runs — so its amount-aware idempotency
            # key never gets a say. An owner who opened a $1,050 session, went
            # back, and chose the $5 preset would be handed the old session:
            # charged $1,050 against a page that had just quoted $55.
            #
            # Read from the SESSION's own metadata rather than a stored column
            # so there is one source of truth (and no migration): it is the
            # same dict the grant webhook reads.
            requested_credits = (
                normalize_starter_credits(body.credits_cents) if body.provider_choice == "bedrock_claude" else None
            )
            pending_credits_raw = (pending.get("metadata") or {}).get("credits_cents")
            pending_credits = int(pending_credits_raw) if pending_credits_raw else None
            # Launch winner staleness is a third body-varying input: a session
            # minted before the owner's sheet row was flagged carries no
            # free-month discount (marker absent), and one minted WITH it goes
            # stale once the coupon is consumed. Reusing either mismatch hands
            # the owner a session at the wrong total — expire and mint fresh.
            from core.services.launch_close import winner_checkout_discount

            has_winner_marker = (pending.get("metadata") or {}).get("launch_winner_coupon") == "true"
            wants_winner_discount = bool(await winner_checkout_discount(owner_id))
            if (
                account.get("pending_checkout_provider") == body.provider_choice
                and pending_credits == requested_credits
                and has_winner_marker == wants_winner_discount
            ):
                # Same intent — send them back to the SAME hosted session.
                return {"checkout_url": pending.url}
            # Provider switch OR credit-amount change: the open session carries
            # the OLD provider in its subscription metadata (the webhook would
            # clobber the new choice) and the OLD amount in its line item, and
            # leaving it open would allow completing both. Expire it, then mint
            # fresh below.
            try:
                with timing("stripe.api.latency", {"op": "checkout.session.expire"}):
                    stripe.checkout.Session.expire(pending_id)
            except stripe.error.InvalidRequestError:
                # Thin race: the session can complete between retrieve and
                # expire (checkout finishing in another tab). Minting a
                # fresh session here would leave BOTH chargeable — re-check
                # and stop unless it genuinely expired.
                with timing("stripe.api.latency", {"op": "checkout.session.retrieve"}):
                    recheck = stripe.checkout.Session.retrieve(pending_id)
                if recheck.get("status") != "expired":
                    raise HTTPException(
                        status_code=409,
                        detail="already_subscribed:checkout_completed",
                    )
                logger.info("Pending checkout session %s expired concurrently — minting fresh", pending_id)
        elif pending is not None and pending.get("status") == "complete":
            # Paid, webhook in flight. Do not mint a parallel session; the
            # row flips active momentarily and the normal guard takes over.
            raise HTTPException(status_code=409, detail="already_subscribed:checkout_completed")
        # Falling through means the pending session is dead (switch-expired
        # just above, naturally expired, or unretrievable) and we are about to
        # mint a replacement via create_flat_fee_checkout below. We deliberately
        # do NOT force-clear the checkout slot here. An unconditional clear is
        # the double-charge ABA this fix closes: two concurrent switches could
        # each clear the other's fresh claim and both mint a completable session
        # (distinct idempotency keys → Stripe won't collapse them). The
        # replacement's own try_claim_checkout_slot handles the slot — a stale
        # claim (>90s) is reclaimed, and a genuinely live claim yields a
        # retryable 409 `checkout_in_progress`, which is correct: someone is
        # minting. A rapid self-switch within 90s of a create may see that 409
        # and retry; that is the price of not reopening the ABA.

    # A trial_local row EXISTS but carries no stripe_customer_id — the trial row
    # is written with no stripe_* fields so a brand-new signup can chat before
    # any Stripe object exists. The `if not account` branch above therefore
    # skipped customer creation for exactly the owners who need it, and
    # create_flat_fee_checkout then raised "No Stripe customer": every free user
    # who clicked Upgrade got a 400. Backfill here — AFTER the already-subscribed
    # guards, so we never mint a Stripe customer for a caller we are about to
    # refuse.
    if not account.get("stripe_customer_id"):
        owner_type = get_owner_type(auth)
        account = await BillingService().create_customer_for_owner(
            owner_id=owner_id,
            owner_type=owner_type,
            email=auth.email,
        )

    # Synchronously persist provider_choice on the billing row BEFORE
    # creating the Stripe Checkout session. Closes the race where the user
    # lands on /chat and triggers /container/provision before the
    # customer.subscription.created webhook lands (the webhook can be
    # delayed by seconds-to-minutes). The webhook handler still writes to
    # billing_repo as an idempotent backup.
    await billing_repo.set_provider_choice(
        account["owner_id"],
        provider_choice=body.provider_choice,
        byo_provider=body.byo_provider if body.provider_choice == "byo_key" else None,
        # For chatgpt_oauth the subscribing admin is the workspace owner and
        # initial connector. Later writes can't repoint it to another human:
        # /oauth/chatgpt/poll claims via a set-once conditional write, and
        # the owner check in change_provider gates re-selection.
        connector_user_id=auth.user_id if body.provider_choice == "chatgpt_oauth" else None,
    )

    try:
        session = await create_flat_fee_checkout(
            owner_id=owner_id,
            provider_choice=body.provider_choice,
            byo_provider=body.byo_provider if body.provider_choice == "byo_key" else None,
            # auth.user_id is the Clerk user actually clicking the button —
            # in org context this is the admin, NOT the org id. Threaded
            # through subscription_data.metadata so the webhook can persist
            # provider_choice on the right per-Clerk-user row.
            clerk_user_id=auth.user_id,
            # Scoped to bedrock_claude at the boundary rather than trusting the
            # body: the other two providers spend no Isol8 credits, so a client
            # sending credits_cents with them would be charged for a balance it
            # can never draw down. Pydantic already bounded the value to
            # $5–$1,000; this bounds WHO it can apply to.
            credits_cents=body.credits_cents if body.provider_choice == "bedrock_claude" else None,
        )
    except BillingServiceError as e:
        if str(e) == "checkout_in_progress":
            # Lost the per-owner checkout-slot claim to a concurrent request
            # — that request's session is (or is about to be) the one open
            # session. 409, not 400: retrying in a moment reuses it.
            raise HTTPException(status_code=409, detail="checkout_in_progress")
        raise HTTPException(status_code=400, detail=str(e))
    return {"checkout_url": session.url}


async def create_team_checkout(
    auth: AuthContext = Depends(get_current_user),
):
    from core.services.billing_service import (
        BillingService,
        BillingServiceError,
        create_team_seat_checkout,
    )

    owner_id = resolve_owner_id(auth)
    # Team billing keys on the org owner_id. A personal caller has no org to
    # bill per-seat — refuse rather than mint a personal seat subscription.
    if not auth.is_org_context or not is_org_owner_id(owner_id):
        raise HTTPException(status_code=400, detail="team-checkout requires an organization context")

    # Only org admins can subscribe the org (mirrors /trial-checkout). 403.
    require_org_admin(auth)

    account = await _get_billing_account(auth)
    if not account:
        billing_service = BillingService()
        account = await billing_service.create_customer_for_owner(
            owner_id=owner_id,
            owner_type="org",
            email=auth.email,
        )

    # Refuse a second subscription for any state that has ever billed / might
    # bill — Checkout in subscription mode creates a NEW subscription on every
    # successful return. Same guard set as /trial-checkout, EXCEPT "canceled":
    # a canceled org (subscription deleted at Stripe) has no live subscription
    # and must be able to self-serve a fresh team-checkout, or it's locked out
    # forever with no path back.
    if is_trial_blocked(account) and account.get("subscription_status") != "canceled":
        raise HTTPException(
            status_code=409,
            detail=f"already_subscribed:{account.get('subscription_status')}",
        )

    # Teams are Bedrock-managed automatically — there is no provider picker in
    # the team flow, so persist the choice synchronously BEFORE Checkout (the
    # same ordering /trial-checkout uses for the personal picker). Without this
    # a poll returning from Checkout would see an active sub with no
    # provider_choice and flash the provider_required gate until the webhook
    # backup lands. create_team_seat_checkout also threads it into the
    # subscription metadata so the webhook re-asserts it idempotently.
    await billing_repo.set_provider_choice(
        owner_id,
        provider_choice="bedrock_claude",
        byo_provider=None,
    )

    member_count = await clerk_admin.get_organization_seat_count(owner_id)
    if member_count <= 0:
        # get_organization_seat_count returns 0 on any Clerk error/outage.
        # create_team_seat_checkout floors quantity at max(1, member_count),
        # so proceeding here would silently subscribe the org at a single
        # seat instead of its real size. Fail loudly instead of under-billing.
        raise HTTPException(status_code=503, detail="team_size_unavailable")
    try:
        session = await create_team_seat_checkout(owner_id=owner_id, member_count=member_count)
    except BillingServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"checkout_url": session.url}


async def create_ultra_checkout(
    auth: AuthContext = Depends(get_current_user),
):
    from core.services.billing_service import BillingServiceError, create_ultra_upgrade

    # Org admins only — a billing action, so mirror /trial-checkout,
    # /team-checkout and /provider. A non-admin org member must not be able to
    # bump the org onto the $150 tier.
    if auth.is_org_context:
        require_org_admin(auth)

    owner_id = resolve_owner_id(auth)

    # Ensure a Stripe customer exists before create_ultra_upgrade may build a
    # fresh Ultra Checkout: a trial_local row carries no stripe_customer_id and
    # the checkout builder requires one. No-op for an owner already on the flat
    # sub (they already have a customer). Mirrors /trial-checkout and
    # /team-checkout, which backfill the customer here.
    account = await _get_billing_account(auth)
    if not account or not account.get("stripe_customer_id"):
        owner_type = get_owner_type(auth)
        await BillingService().create_customer_for_owner(
            owner_id=owner_id,
            owner_type=owner_type,
            email=auth.email,
        )

    try:
        result = await create_ultra_upgrade(owner_id=owner_id, clerk_user_id=auth.user_id)
    except BillingServiceError as e:
        if str(e) == "checkout_in_progress":
            # Lost the per-owner checkout-slot claim to a concurrent fresh-Ultra
            # request — that request's session is (or is about to be) the one open
            # session. 409, not 400: retrying in a moment reuses it. Mirrors
            # /trial-checkout's mapping.
            raise HTTPException(status_code=409, detail="checkout_in_progress")
        raise HTTPException(status_code=400, detail=str(e))

    # In-place swap → {"upgraded": True} (returned verbatim); a fresh checkout
    # → the Checkout Session, mapped to its URL.
    if isinstance(result, dict):
        return result
    return {"checkout_url": result.url}


# ---------------------------------------------------------------------------
# Post-onboarding provider switch (POST /billing/provider)
# ---------------------------------------------------------------------------


class ChangeProviderRequest(BaseModel):
    # Pydantic validates choices before any DDB read or container side-effect.
    provider_choice: Literal["chatgpt_oauth", "byo_key", "bedrock_claude"] = Field(
        ...,
        description="chatgpt_oauth | byo_key | bedrock_claude",
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
        "(subscribers only — pre-subscribe users hit /billing/trial-checkout). "
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
    request_id: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Client-generated idempotency token — one per user intent, "
            "stable across retries. Feeds Metronome's Idempotency-Key so a "
            "retried POST replays the original commit instead of charging "
            "twice."
        ),
    )


class TopUpCheckoutRequest(BaseModel):
    amount_cents: int = Field(
        ...,
        ge=500,
        le=_TOP_UP_MAX_CENTS,
        description="Min $5 (500 cents), max $1,000 (100000 cents).",
    )
    enable_auto_charge: bool = Field(
        default=False,
        description=(
            "Opt in to one-click top-ups: the Checkout saves the card for "
            "off-session use (setup_future_usage) and, on successful "
            "payment, future top-ups may charge it without a redirect."
        ),
    )


def _is_exhausted(balance_microcents: int | None) -> bool:
    """Whether the owner's credits are unspendable at the bedrock-gate.

    The gate admits paid traffic only while `remaining - reserved - estimate
    > 0`, so a balance at or below one reservation buys nothing. Surfacing the
    gate's own threshold keeps the frontend from needing a copy of the
    constant. A null balance (Metronome outage / no customer) is INDETERMINATE
    and never exhausted — a false banner is worse than a late one.
    """
    from core.services.trial_cap import RESERVE_ESTIMATE_MICROCENTS

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
async def get_credits_balance(ctx: AuthContext = Depends(get_current_user)):
    # Credits pool at the owner level (org_id for org context, user_id for
    # personal). Keying on ctx.user_id meant org members couldn't see the
    # admin-funded balance and got blocked at chat with out_of_credits.
    owner_id = resolve_owner_id(ctx)
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
    account = await billing_repo.get_by_owner_id(owner_id) or {}
    return {
        "balance_microcents": balance_uc,
        "balance_dollars": f"{balance_uc / 1_000_000:.2f}" if balance_uc is not None else None,
        # Drives the frontend's top-up fork: opted-in owners get one-click
        # (card on file), everyone else routes through Stripe Checkout.
        "auto_charge_enabled": bool(account.get("credits_auto_charge")),
        "is_exhausted": _is_exhausted(balance_uc),
    }


@router.post(
    "/credits/top_up_checkout",
    summary="Start a credit top-up via Stripe Checkout (the default purchase path)",
    description=(
        "Creates a Stripe-hosted Checkout session for the requested amount — "
        "the customer sees and confirms the payment explicitly. On completion "
        "the webhook grants the credits as an UNGATED Metronome commit "
        "(instant release, no second invoice). With enable_auto_charge the "
        "session also captures off-session card consent and, once paid, "
        "enables one-click top-ups for this owner."
    ),
    operation_id="credits_top_up_checkout",
)
async def top_up_credits_checkout(
    body: TopUpCheckoutRequest,
    ctx: AuthContext = Depends(get_current_user),
):
    from core.services.billing_service import BillingServiceError, create_credit_top_up_checkout

    owner_id = resolve_owner_id(ctx)
    try:
        session = await create_credit_top_up_checkout(
            owner_id=owner_id,
            amount_cents=body.amount_cents,
            enable_auto_charge=body.enable_auto_charge,
        )
    except BillingServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except stripe.error.StripeError as e:
        logger.exception("Credit checkout session create failed for owner %s", owner_id)
        raise HTTPException(status_code=502, detail="billing provider error") from e
    return {"checkout_url": session.url}


@router.post(
    "/credits/auto_charge/disable",
    summary="Disable one-click top-ups",
    description=(
        "Turns off the owner's one-click top-up consent. Re-enabling "
        "requires a fresh Checkout with the opt-in box ticked — that "
        "checkout is where the off-session card consent is captured, so "
        "there is deliberately NO enable endpoint."
    ),
    operation_id="credits_auto_charge_disable",
)
async def disable_credits_auto_charge(ctx: AuthContext = Depends(get_current_user)):
    owner_id = resolve_owner_id(ctx)
    await billing_repo.set_credits_auto_charge(owner_id, enabled=False)
    return {"auto_charge_enabled": False}


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

    owner_id = resolve_owner_id(ctx)

    # One-click top-ups charge the saved card with no confirmation step, so
    # they require the owner's standing consent — granted only via a prior
    # Checkout where they ticked the opt-in box (which carried Stripe's
    # setup_future_usage=off_session consent). Everyone else routes through
    # POST /credits/top_up_checkout. 409 (not 400): the request is
    # well-formed, the owner's state just doesn't allow this path.
    account = await billing_repo.get_by_owner_id(owner_id) or {}
    if not account.get("credits_auto_charge"):
        raise HTTPException(
            status_code=409,
            detail={
                "code": "auto_charge_not_enabled",
                "message": "One-click top-ups are not enabled — use the checkout flow.",
            },
        )

    # Namespace the client token with the owner id: the raw request_id is
    # attacker-chosen, and an un-namespaced key would let one user replay or
    # collide another user's in-flight commit at Metronome.
    idempotency_key = f"topup-{owner_id}-{body.request_id}" if body.request_id else None
    try:
        await request_top_up(owner_id, body.amount_cents, idempotency_key=idempotency_key)
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
