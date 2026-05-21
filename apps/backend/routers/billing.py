"""Billing API endpoints — flat-fee model with credit ledger."""

import asyncio
import logging
from typing import Literal

import httpx
import stripe
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from core.auth import AuthContext, get_current_user, get_owner_type, resolve_owner_id, require_org_admin
from core.config import settings
from core.observability.metrics import put_metric, timing
from core.repositories import billing_repo, usage_repo
from core.services import clerk_admin, credit_ledger
from core.services.billing_service import BillingService
from core.services.stripe_webhook_handlers import STRIPE_WEBHOOK_HANDLERS
from core.services.provision_gate import (
    TRIAL_BLOCKED_STATUSES,
    is_subscription_active,
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

    # is_subscribed shares the same predicate every gate site (chat,
    # provision, channel-binding) consults — see
    # ``core.services.provision_gate.is_subscription_active``. Single
    # source of truth so the response shape and the gates can never
    # disagree about whether a billing row counts as subscribed.
    is_subscribed = is_subscription_active(account)

    return BillingAccountResponse(
        is_subscribed=is_subscribed,
        current_spend=current_spend,
        lifetime_spend=lifetime_spend,
        subscription_status=subscription_status,
        trial_end=int(trial_end) if trial_end is not None else None,
        provider_choice=account.get("provider_choice") if account else None,
        byo_provider=account.get("byo_provider") if account else None,
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
# Trial signup — single endpoint that backs the ProviderPicker cards.
# ---------------------------------------------------------------------------


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


@router.post(
    "/trial-checkout",
    summary="Start a 14-day trial via Stripe Checkout",
    description=(
        "Creates a Stripe Checkout session in subscription mode with a 14-day trial. "
        "Charges $0 today; user enters card details, the Subscription is born trialing, "
        "and Stripe converts it on day 15. The chosen provider_choice is threaded into "
        "subscription metadata so the webhook handler can persist it on the user row. "
        "Returns the Checkout URL — frontend redirects the browser to it."
    ),
    operation_id="create_trial_checkout",
)
async def create_trial_checkout(
    body: TrialCheckoutRequest,
    auth: AuthContext = Depends(get_current_user),
):
    from core.services.billing_service import (
        BillingService,
        BillingServiceError,
        create_flat_fee_checkout,
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

    # Org-context users cannot pick ChatGPT OAuth:
    # OpenAI Plus terms forbid reselling, so org admins can't route their
    # teammates' prompts through their personal ChatGPT subscription; orgs
    # must use Bedrock or BYOK. Frontend ProviderPicker hides the card too,
    # but a savvy user could call this endpoint directly without server-side
    # enforcement. Reject before any Stripe Checkout creation so a denied
    # caller never gets a session URL.
    if body.provider_choice == "chatgpt_oauth" and auth.is_org_context:
        raise HTTPException(
            status_code=403,
            detail=(
                "ChatGPT OAuth is not available for organization workspaces. "
                "Use Bring-Your-Own-Key or Powered by Claude instead."
            ),
        )

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

    # Refuse a second trial-checkout for any subscription that exists,
    # regardless of state. Stripe Checkout in mode="subscription"
    # creates a NEW subscription on every successful return, so a retry
    # past the 5-minute idempotency-bucket window would charge the
    # customer twice.
    #
    # ``is_trial_blocked`` (canonical set in ``provision_gate.py``) blocks
    # any state that has ever billed or might still bill —
    # {active, trialing, past_due, canceled, incomplete, incomplete_expired,
    # unpaid, paused}. The {canceled, …} entries close the trial-gaming
    # hole from audit C3 (a user could otherwise complete a 14-day trial,
    # cancel before day 15, and immediately POST /trial-checkout again to
    # mint a new trial — ECS Fargate is recurring on us either way).
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
                "Stored sub %s not found in Stripe for owner %s — allowing fresh trial",
                sub_id,
                owner_id,
            )
        else:
            # Same expanded blocklist as the local-row path — a stale
            # canceled/incomplete subscription in Stripe is still a
            # "this account already trialed" signal (audit C3).
            if live_sub.get("status") in TRIAL_BLOCKED_STATUSES:
                raise HTTPException(
                    status_code=409,
                    detail=f"already_subscribed:{live_sub.get('status')}",
                )

    # Synchronously persist provider_choice on the billing row BEFORE
    # creating the Stripe Checkout session. Closes the race where the user
    # lands on /chat and triggers /container/provision before the
    # customer.subscription.created webhook lands (the webhook can be
    # delayed by seconds-to-minutes). The webhook handler still writes to
    # billing_repo as an idempotent backup.
    owner_type_val = account.get("owner_type", get_owner_type(auth))
    await billing_repo.set_provider_choice(
        account["owner_id"],
        provider_choice=body.provider_choice,
        byo_provider=body.byo_provider if body.provider_choice == "byo_key" else None,
        owner_type=owner_type_val,
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
            trial_days=14,
        )
    except BillingServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"checkout_url": session.url}


# ---------------------------------------------------------------------------
# Post-onboarding provider switch (POST /billing/provider)
# ---------------------------------------------------------------------------


class ChangeProviderRequest(BaseModel):
    # Same Literal as TrialCheckoutRequest. 422 on unknown values before
    # any DDB read or container side-effect.
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
            is_org_context=auth.is_org_context,
            provider_choice=body.provider_choice,
            byo_provider=body.byo_provider,
        )
    except ChangeProviderError as e:
        raise HTTPException(
            status_code=e.status_code,
            detail={"code": e.code, "message": e.message},
        ) from e


# ---------------------------------------------------------------------------
# Credit ledger endpoints (Plan 2 §6.2 / §6.4)
# ---------------------------------------------------------------------------


# Hard caps on credit-purchase amounts. These exist as a safety rail
# (audit C4 / M4) — without an upper bound, a malicious or compromised
# user could (a) request a $99k off-session auto-reload that locks in a
# disputable charge, or (b) trick a phished user into pre-paying an
# unreasonable amount via Elements. Stripe will likely reject extremes
# itself, but defense in depth says we cap server-side too.
#
# Values are deliberately conservative; raise via a code change + a
# pricing-tier conversation rather than user-facing input.
_TOP_UP_MAX_CENTS = 100_000  # $1,000.00 — single-shot prepay ceiling
_AUTO_RELOAD_MAX_CENTS = 20_000  # $200.00 — recurring off-session ceiling


class TopUpRequest(BaseModel):
    amount_cents: int = Field(
        ...,
        ge=500,
        le=_TOP_UP_MAX_CENTS,
        description="Min $5 (500 cents), max $1,000 (100000 cents).",
    )


class AutoReloadRequest(BaseModel):
    enabled: bool
    threshold_cents: int | None = Field(default=None, ge=500, le=_AUTO_RELOAD_MAX_CENTS)
    amount_cents: int | None = Field(default=None, ge=500, le=_AUTO_RELOAD_MAX_CENTS)


@router.get(
    "/credits/balance",
    summary="Get the user's prepaid credit balance",
    description=("Returns the user's current prepaid Claude credit balance in microcents and dollars (formatted)."),
)
async def get_credits_balance(ctx: AuthContext = Depends(get_current_user)):
    # Credits pool at the owner level (org_id for org context, user_id for
    # personal). Keying on ctx.user_id meant org members couldn't see the
    # admin-funded balance and got blocked at chat with out_of_credits.
    balance_uc = await credit_ledger.get_balance(resolve_owner_id(ctx))
    dollars = f"{balance_uc / 1_000_000:.2f}"
    return {"balance_microcents": balance_uc, "balance_dollars": dollars}


@router.post(
    "/credits/top_up",
    summary="Start a Stripe Checkout session for a credit top-up",
    description=(
        "Creates a Stripe Checkout session in mode=payment for a one-shot "
        "credit purchase. Returns ``checkout_url`` — the frontend redirects "
        "the browser to it. Stripe handles card collection, 3DS, Apple Pay, "
        "saved cards, and promotion-code application. The credit grant "
        "happens asynchronously when Stripe fires checkout.session.completed."
    ),
    operation_id="credits_top_up_checkout",
)
async def top_up_credits(
    body: TopUpRequest,
    ctx: AuthContext = Depends(get_current_user),
):
    """Replaces the previous inline-Elements PaymentIntent flow.

    Why Checkout > Elements for our usage:
      - Native promotion-code support → internal 100%-off coupon works.
      - Auto-reload (off-session) is unaffected — that path stays
        on direct PaymentIntents because there's no UI step.
      - No client-side Stripe SDK / publishable key required.
    """
    from core.services.billing_service import (
        BillingServiceError,
        create_credit_top_up_checkout,
    )

    owner_id = resolve_owner_id(ctx)
    try:
        session = await create_credit_top_up_checkout(
            owner_id=owner_id,
            amount_cents=body.amount_cents,
        )
    except BillingServiceError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"checkout_url": session.url}


@router.put(
    "/credits/auto_reload",
    summary="Configure auto-reload",
    description=("Configure auto-reload: when balance drops below threshold_cents, charge amount_cents off-session."),
)
async def set_auto_reload(
    body: AutoReloadRequest,
    ctx: AuthContext = Depends(get_current_user),
):
    if body.enabled and (body.threshold_cents is None or body.amount_cents is None):
        raise HTTPException(
            status_code=400,
            detail="threshold_cents and amount_cents required when enabling",
        )
    await credit_ledger.set_auto_reload(
        resolve_owner_id(ctx),
        enabled=body.enabled,
        threshold_cents=body.threshold_cents,
        amount_cents=body.amount_cents,
    )
    # Return JSON (not 204) — the frontend `useApi` helper unconditionally
    # parses successful responses with .json(), so an empty body is read as
    # a parse failure and surfaces as an error in CreditsPanel.
    return {"status": "ok"}


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

    early_response = await handler(event, billing_service)
    if early_response is not None:
        return early_response
    return {"status": "ok"}
