"""Service for Stripe billing operations — flat-fee model."""

import asyncio
import hashlib
import logging
import os
import time
from typing import Literal

import stripe

from core.config import settings
from core.observability.metrics import put_metric, timing
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

ProviderChoice = Literal["chatgpt_oauth", "byo_key", "bedrock_claude"]
ByoProvider = Literal["openai", "anthropic"]


class ChangeProviderError(Exception):
    """Raised when a post-onboarding provider switch is rejected.

    ``code`` is a short machine-readable string the router maps to an
    HTTP status; ``message`` is the user-facing detail.
    """

    def __init__(self, code: str, message: str, *, status_code: int = 400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


stripe.api_key = settings.STRIPE_SECRET_KEY

FRONTEND_URL = os.getenv(
    "FRONTEND_URL", settings.cors_origins_list[0] if settings.cors_origins_list else "http://localhost:3000"
)

# Starter Claude credits bundled into the bedrock_claude subscription Checkout
# as a one-time line item. The owner picks the amount; this is only the
# preselected default when they don't. Deliberately constants and not env vars:
# the resulting total is quoted to the customer BEFORE payment, so a value that
# drifted per environment would make the disclosure false in one of them.
#
# The min/max mirror the standalone top-up window (routers/billing.py
# `_TOP_UP_MAX_CENTS`, `ge=500`) — the same money buying the same balance
# should not have two different limits depending on which door it came
# through. Kept as literals here rather than imported to avoid a service →
# router import; the router's Pydantic bounds are the front-line check and
# these are the backstop for non-router callers.
BEDROCK_STARTER_CREDITS_CENTS = 2000
MIN_STARTER_CREDITS_CENTS = 500  # $5 — matches TrialCheckoutRequest ge=500
MAX_STARTER_CREDITS_CENTS = 100_000  # $1,000 — matches _TOP_UP_MAX_CENTS


# Bumped whenever the Checkout Session request body shape changes — see
# _checkout_idempotency_key. v2 = top-level `metadata` + the optional credits
# line item + conditional allow_promotion_codes.
_CHECKOUT_BODY_VERSION = 2


def normalize_starter_credits(credits_cents: int | None) -> int | None:
    """The ONE definition of "what starter amount does this request mean".

    ``None`` in, ``None`` out — absent means no credits line item, never a
    silent default (see create_flat_fee_checkout). Any supplied value is
    clamped into the $5–$1,000 window.

    Explicitly `is None`, not truthiness: ``credits_cents or DEFAULT`` treated
    a deliberate ``0`` as "not supplied" and substituted $20, charging money
    nobody asked for — and the clamp that would have caught the zero never saw
    it.

    Shared with the router's open-session guard on purpose. That guard decides
    whether an already-open Checkout still matches what the customer is asking
    for now, which is only answerable against the same normalized value this
    function feeds into the line item; two copies of the arithmetic would let
    "same amount" and "same session" disagree.
    """
    if credits_cents is None:
        return None
    return min(max(credits_cents, MIN_STARTER_CREDITS_CENTS), MAX_STARTER_CREDITS_CENTS)


class BillingServiceError(Exception):
    pass


class BillingService:
    async def create_customer_for_owner(
        self, owner_id: str, owner_type: str = "personal", email: str | None = None
    ) -> dict:
        """Resolve a Stripe customer for ``owner_id``, keyed by ``email``.

        Email is the system-of-record idempotency key for Stripe-side identity:
        a single human (one verified email in Clerk) maps to a single Stripe
        Customer, regardless of how many billing entities they own (personal
        account, multiple orgs as admin, etc.). DDB ``billing-accounts`` rows
        are still per-owner_id, so multiple rows can share one
        ``stripe_customer_id``.

        Resolution order:
          1. Existing DDB row for this owner_id WITH a ``stripe_customer_id``
             → return it (no Stripe call).
          1b. Existing DDB row WITHOUT a ``stripe_customer_id`` — the
              card-less ``trial_local`` row (``create_trial_local_if_absent``
              deliberately writes no ``stripe_*`` fields) is the only row
              that reaches this today — falls through to steps 2-3 below to
              mint/reuse a customer, then BACKFILLS it onto the existing row
              via ``billing_repo.set_stripe_customer_id`` rather than
              attempting a fresh ``create_if_not_exists`` (which would raise
              ``AlreadyExistsError`` against a row that's already there).
              Without this, ``create_flat_fee_checkout`` always sees no
              Stripe customer and raises — every free-tier user hits an
              HTTP 400 on Upgrade.
          2. ``stripe.Customer.list(email=...)`` → reuse the most recent match.
             ``list`` is exact-match and immediately consistent (unlike
             ``search``, which has indexing lag).
          3. No match → ``stripe.Customer.create``.

        Webhook handlers must resolve owner via subscription metadata
        (``owner_id``) since the GSI on ``stripe_customer_id`` is no longer
        unique. See ``handle_stripe_webhook`` in ``routers/billing.py``.
        """
        existing = await billing_repo.get_by_owner_id(owner_id)
        if existing and existing.get("stripe_customer_id"):
            return existing

        # Normalize so a single human typing/signing in with mixed case
        # ("User@Example.com" vs "user@example.com") still maps to one
        # Stripe customer. Stripe's Customer.list email filter is exact
        # match, so without normalization we'd silently create duplicates.
        normalized_email = email.strip().lower() if email else None

        customer_id: str | None = None
        created_locally = False
        if normalized_email:
            with timing("stripe.api.latency", {"op": "customers.list"}):
                matches = stripe.Customer.list(email=normalized_email, limit=1)
            if matches and matches.data:
                customer_id = matches.data[0].id
                put_metric("stripe.customer.email_dedup", dimensions={"result": "reuse"})
                logger.info(
                    "Reusing Stripe customer %s for owner_id=%s (email=%s)",
                    customer_id,
                    owner_id,
                    normalized_email,
                )

        if customer_id is None:
            with timing("stripe.api.latency", {"op": "customers.create"}):
                customer = stripe.Customer.create(
                    email=normalized_email,
                    metadata={"owner_id": owner_id, "owner_type": owner_type},
                )
            customer_id = customer.id
            created_locally = True
            put_metric("stripe.customer.email_dedup", dimensions={"result": "create"})

        if existing:
            # Backfill path (1b above) — the row already exists (trial_local
            # today), so persist onto it instead of racing a fresh
            # create_if_not_exists, which would raise AlreadyExistsError
            # against a row that's already there.
            await billing_repo.set_stripe_customer_id(owner_id, stripe_customer_id=customer_id)
            existing["stripe_customer_id"] = customer_id
            return existing

        try:
            return await billing_repo.create_if_not_exists(
                owner_id=owner_id,
                stripe_customer_id=customer_id,
                owner_type=owner_type,
            )
        except billing_repo.AlreadyExistsError:
            # Lost the DDB race for this owner_id. If we took the create
            # path, two concurrent callers may have both missed the
            # email-list lookup and each minted a fresh Stripe customer —
            # the loser's customer is now orphaned (no DDB row points at
            # it). Delete it so the email→customer invariant holds.
            winner = await billing_repo.get_by_owner_id(owner_id)
            if (
                created_locally
                and winner
                and winner.get("stripe_customer_id")
                and winner["stripe_customer_id"] != customer_id
            ):
                logger.info(
                    "Lost DDB race for owner_id=%s; deleting orphan Stripe customer %s (winner=%s)",
                    owner_id,
                    customer_id,
                    winner["stripe_customer_id"],
                )
                try:
                    with timing("stripe.api.latency", {"op": "customers.delete"}):
                        stripe.Customer.delete(
                            customer_id,
                            idempotency_key=f"delete_customer:{customer_id}",
                        )
                    put_metric("stripe.customer.orphan_delete", dimensions={"result": "ok"})
                except Exception:
                    put_metric("stripe.customer.orphan_delete", dimensions={"result": "error"})
                    logger.warning("Failed to delete orphan Stripe customer %s", customer_id)
            return winner

    async def create_portal_session(self, billing_account: dict) -> str:
        owner_id = billing_account["owner_id"]
        # 5-minute time bucket — same retry-collapsing intent as checkout.
        bucket = int(time.time() // 300)
        with timing("stripe.api.latency", {"op": "billing_portal.session.create"}):
            session = stripe.billing_portal.Session.create(
                customer=billing_account["stripe_customer_id"],
                return_url=f"{FRONTEND_URL}/settings/billing",
                idempotency_key=f"portal:{owner_id}:{bucket}",
            )
        return session.url

    async def cancel_subscription(self, billing_account: dict) -> None:
        # clear_seat_fields=True: cancellation must also wipe
        # subscription_item_id + seat_count, not just the subscription
        # id/status -- otherwise a stale subscription_item_id survives
        # cancellation and is_seat_subscription (routers/billing.py) keeps
        # reporting a per-seat subscription that no longer exists.
        await billing_repo.set_subscription(
            owner_id=billing_account["owner_id"],
            subscription_id=None,
            status="canceled",
            trial_end=None,
            clear_seat_fields=True,
        )


async def create_flat_fee_checkout(
    *,
    owner_id: str,
    provider_choice: str | None = None,
    byo_provider: str | None = None,
    clerk_user_id: str | None = None,
    credits_cents: int | None = None,
) -> stripe.checkout.Session:
    """Create a Stripe Checkout session on the single flat-fee price.

    Used by the flat-fee onboarding wizard (frontend cards 1, 2, 3 — all three
    pay the same monthly fee against ``STRIPE_FLAT_PRICE_ID``). The card is
    charged for the first month at checkout — there is no Stripe trial;
    no-card evaluation happens on the ``trial_local`` free tier before the
    user ever reaches this flow.

    ``provider_choice``, ``clerk_user_id`` and ``owner_id`` are threaded into
    ``subscription_data.metadata`` so the ``customer.subscription.*`` webhook
    can resolve the owner unambiguously. ``owner_id`` matters because the
    Stripe customer is shared across personal/org billing rows for the same
    email — the GSI on ``stripe_customer_id`` is no longer 1:1.

    Conventions (mirrors Plan 1 Stripe Tax setup):
      - ``automatic_tax={"enabled": True}`` — Stripe computes sales tax/VAT.
      - ``customer_update={"address": "auto"}`` — required when automatic_tax
        is enabled so Checkout can persist the collected billing address back
        onto the existing Stripe customer.
      - ``allow_promotion_codes=True`` — surfaces the promo-code field so
        internal coupons (e.g. 100%-off launch codes) can be applied.
      - ``idempotency_key`` bucketed to a 5-minute window: collapses
        rapid duplicate clicks (and "open Stripe Checkout in two tabs"
        style retries) to a single Checkout Session, so a user can't
        accidentally complete two parallel subscriptions before the
        first ``customer.subscription.created`` webhook lands and the
        guard in ``create_trial_checkout`` kicks in. Stripe's
        idempotency does a body-shape compare, so a config change
        (e.g. ``allow_promotion_codes`` flipping on) naturally
        bypasses the cached response within the same bucket.
    """
    if not settings.STRIPE_FLAT_PRICE_ID:
        raise BillingServiceError("STRIPE_FLAT_PRICE_ID not configured")

    # Serialize session creation per owner. Concurrent requests with
    # different provider choices carry different idempotency keys, so
    # without the claim both would mint distinct completable sessions
    # before either pending-checkout marker lands. Self-expires (90s) so a
    # crashed request can't wedge the owner; the router maps this error to
    # a 409.
    if not await billing_repo.try_claim_checkout_slot(owner_id):
        raise BillingServiceError("checkout_in_progress")

    metadata: dict = {"owner_id": owner_id}
    if provider_choice:
        metadata["provider_choice"] = provider_choice
    if byo_provider:
        metadata["byo_provider"] = byo_provider
    if clerk_user_id:
        metadata["clerk_user_id"] = clerk_user_id

    # Bedrock is the only path provision_gate will not start a container for
    # until credits are funded (Layer 3, `credits_required`), so its Checkout
    # carries the starter credits as a ONE-TIME line item beside the recurring
    # flat price: one session, one card entry, the full total disclosed before
    # payment.
    #
    # This replaces a second Checkout that fired AFTER the subscription was
    # already paid for — the owner reached CreditsStep having bought a
    # container that would not boot, which is a refund, not a conversion. It
    # is also strictly cheaper: one combined charge costs 2.9% + $0.30, where
    # the two separate charges paid the fixed $0.30 twice.
    #
    # CreditsStep needs no change to match — it already calls onComplete()
    # when the balance is spendable, so a funded balance makes it self-skip.
    #
    # `credits_cents` is the owner's chosen starter amount, already bounded to
    # $5–$1,000 by TrialCheckoutRequest and scoped to bedrock_claude by the
    # router. It is re-normalized here anyway: this function is importable and
    # a future caller that skips the router must not be able to mint a
    # $0-or-$1M line item.
    #
    # EXPLICIT-ONLY, deliberately: no credits_cents means no credits line item,
    # never a silent default. The amount is quoted to the customer before they
    # pay, and only a caller that renders the chooser knows what was quoted. A
    # cached frontend bundle, the mobile app, or any client predating this
    # change posts {provider_choice: "bedrock_claude"} alone — defaulting there
    # would charge $70 against a UI that displayed $50. Such a caller gets the
    # pre-existing behaviour instead: $50 now, credits later via CreditsStep.
    line_items: list[dict] = [{"price": settings.STRIPE_FLAT_PRICE_ID, "quantity": 1}]
    starter_cents = normalize_starter_credits(credits_cents) if provider_choice == "bedrock_claude" else None
    if starter_cents is not None:
        line_items.append(
            {
                "price_data": {
                    "currency": "usd",
                    "unit_amount": starter_cents,
                    "product_data": {
                        "name": "Claude credits",
                        "description": "Prepaid Claude inference balance.",
                    },
                },
                "quantity": 1,
            }
        )
        # The webhook grants off metadata, never off the line items.
        metadata["credits_cents"] = str(starter_cents)

    try:
        session = await _build_subscription_checkout(
            owner_id=owner_id,
            line_items=line_items,
            metadata=metadata,
            # `credits=bundled` tells the returning wizard the balance is
            # already paid for and in flight, so CreditsStep shows a "landing
            # shortly" state instead of a live second purchase form. Without
            # it CreditsStep only self-skips once the balance READS spendable,
            # which it does not at the instant the browser gets back from
            # Stripe — the grant is still travelling through the webhook — so
            # the owner who just paid to avoid a second step was shown one.
            success_url=(
                f"{FRONTEND_URL}/chat?checkout=success"
                + (f"&provider={provider_choice}" if provider_choice else "")
                + ("&credits=bundled" if starter_cents is not None else "")
            ),
            cancel_url=f"{FRONTEND_URL}/?checkout=cancel",
            # Promotion codes are OFF once credits ride along. The standalone
            # top-up deliberately grants the REQUESTED amount under a coupon
            # ("us absorbing the cost") — safe there, because that session
            # contains nothing but credits the owner chose. Here one
            # subscription-intended code would also discount up to $1,000 of
            # prepaid balance while the webhook still grants the full metadata
            # amount. Re-enable only with a coupon restricted via `applies_to`
            # to the subscription product.
            allow_promotion_codes=starter_cents is None,
            # The amount is part of the key, not just the provider. The key is
            # bucketed to 5 minutes to collapse double-clicks; without the
            # amount, an owner who picks $20, backs out, and picks $100 inside
            # that window reuses the key with a different body — Stripe rejects
            # the mismatch and the second, larger purchase simply fails.
            idempotency_key=_checkout_idempotency_key(
                "flat_checkout",
                owner_id,
                f"{provider_choice or '_'}:{starter_cents or '_'}",
            ),
        )
    except Exception:
        # No session exists — free the slot so the user's immediate retry
        # doesn't wait out the claim TTL.
        await billing_repo.release_checkout_slot(owner_id)
        raise
    # The marker is load-bearing: after the 90s claim TTL lapses, an
    # UNRECORDED open session is invisible to every guard while staying
    # completable until its ~31-minute expiry — a retry could mint a second
    # session and both could be completed. Fail CLOSED: the user has not
    # seen this session's URL yet, so expire it, free the slot, and let the
    # retry mint fresh.
    try:
        await billing_repo.set_pending_checkout(owner_id, session_id=session.id, provider_choice=provider_choice)
    except Exception:
        logger.exception("Failed to record pending checkout session for owner %s", owner_id)
        try:
            with timing("stripe.api.latency", {"op": "checkout.session.expire"}):
                stripe.checkout.Session.expire(session.id)
        except Exception:
            # Session floats until expires_at — bounded, and nobody holds
            # its URL.
            logger.exception("Failed to expire unrecorded checkout session %s", session.id)
        try:
            await billing_repo.release_checkout_slot(owner_id)
        except Exception:
            # Claim self-expires in 90s; the retry just waits it out.
            logger.exception("Failed to release checkout slot for owner %s", owner_id)
        raise BillingServiceError("checkout_unavailable")
    return session


def subscription_item_on_price(items: list[dict], price_id: str) -> dict:
    """The subscription line item carrying ``price_id``, or ``{}`` if none does.

    THE single way to answer "which item is priced X?". A Teams subscription
    carries two items (flat base + per-seat) and Stripe does not guarantee
    their order, so any positional read is a latent billing bug — picking the
    base item as the seat item makes sync_team_seat_quantity drive the base
    quantity. Empty ``price_id`` yields ``{}`` so an unconfigured price can
    never match an item by accident.
    """
    if not price_id:
        return {}
    return next((item for item in items if (item.get("price") or {}).get("id") == price_id), {})


def _checkout_idempotency_key(flow: str, owner_id: str, extra: str | None = None) -> str:
    """The single idempotency-key shape for subscription Checkout sessions.

    5-minute time bucket collapses rapid duplicate clicks / multi-tab retries
    to one session before the customer.subscription.created webhook + the
    duplicate-sub guard catch up. ``flow`` namespaces flat vs team; ``extra``
    carries the flat path's provider_choice so a config change bypasses the
    cached response naturally.

    ``_BODY_VERSION`` exists because Stripe compares the REQUEST BODY against a
    stored idempotency key and rejects a mismatch rather than replaying. Any
    change to the session kwargs — the top-level ``metadata`` this file now
    sends on every subscription checkout, for instance — makes an old task's
    key and a new task's body disagree for the length of a rolling deploy, and
    a customer's retry fails inside that window. Bump this whenever the body
    shape changes; it costs one wasted bucket and nothing else.
    """
    bucket = int(time.time() // 300)
    suffix = f":{extra}" if extra is not None else ""
    return f"{flow}:v{_CHECKOUT_BODY_VERSION}:{owner_id}{suffix}:{bucket}"


async def _build_subscription_checkout(
    *,
    owner_id: str,
    line_items: list[dict],
    metadata: dict,
    success_url: str,
    cancel_url: str,
    idempotency_key: str,
    allow_promotion_codes: bool = True,
) -> stripe.checkout.Session:
    """Single builder for subscription-mode Stripe Checkout sessions.

    Owns the session kwargs, the automatic_tax / customer_update block, and
    the subscription_data metadata assembly so neither the flat (personal)
    nor the team path copies them. Callers pass a list so the team path can
    charge a flat base alongside the per-seat item. Single-element lists are
    the norm; price-id existence guards stay in each caller because the
    config key differs.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not account.get("stripe_customer_id"):
        raise BillingServiceError(f"No Stripe customer for owner_id={owner_id}")

    subscription_data: dict = {"metadata": metadata}

    kwargs: dict = dict(
        customer=account["stripe_customer_id"],
        mode="subscription",
        line_items=line_items,
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=allow_promotion_codes,
        automatic_tax={"enabled": True},
        customer_update={"address": "auto"},
        # The SAME dict on BOTH, because Stripe puts them on different objects
        # and two different webhooks read them:
        #   subscription_data.metadata → the created Subscription, which is
        #     what customer.subscription.* carries (handle_subscription_state).
        #   metadata                   → the Session itself, which is what
        #     checkout.session.completed carries (handle_checkout_session).
        # Only subscription_data was set here, so any Session-reading handler
        # saw {}. That was invisible until the bedrock_claude Checkout started
        # bundling starter credits: the one-time line item was charged and
        # `credits_cents` never reached the grant, so the customer paid for a
        # balance that was never funded. The standalone top-up path never hit
        # this because it calls Session.create directly with top-level
        # metadata — which is exactly why it worked and this did not.
        metadata=metadata,
        subscription_data=subscription_data,
        idempotency_key=idempotency_key,
        # Stripe's minimum is 30 minutes; the default is 24 hours. A short
        # expiry bounds how long an abandoned session can linger completable
        # — load-bearing for the single-open-session guard in /trial-checkout.
        expires_at=int(time.time()) + 31 * 60,
    )
    with timing("stripe.api.latency", {"op": "checkout.session.create"}):
        session = stripe.checkout.Session.create(**kwargs)
    return session


async def create_team_seat_checkout(*, owner_id: str, member_count: int) -> stripe.checkout.Session:
    """Teams Checkout: flat base at quantity 1 plus per-seat at the seat count.

    Threads ``purpose="team_seat"`` into subscription metadata so the
    customer.subscription.* webhook records the subscription item id + seat
    count via set_team_subscription. The BASE quantity is always 1; the SEAT
    quantity is floored at 1 so a transient member-count read of 0 never
    creates a zero-quantity subscription.
    """
    if not settings.STRIPE_TEAM_SEAT_PRICE_ID:
        raise BillingServiceError("STRIPE_TEAM_SEAT_PRICE_ID not configured")
    if not settings.STRIPE_TEAM_BASE_PRICE_ID:
        raise BillingServiceError("STRIPE_TEAM_BASE_PRICE_ID not configured")
    if settings.STRIPE_TEAM_BASE_PRICE_ID == settings.STRIPE_TEAM_SEAT_PRICE_ID:
        # Identical ids would make subscription_item_on_price return the same
        # item for both lookups — reintroducing, through config, exactly the
        # positional ambiguity this whole flow exists to eliminate.
        raise BillingServiceError("STRIPE_TEAM_BASE_PRICE_ID and STRIPE_TEAM_SEAT_PRICE_ID must differ")

    quantity = max(1, member_count)
    return await _build_subscription_checkout(
        owner_id=owner_id,
        # Flat base + per-seat, in that order. NOTHING may select these by
        # position — the webhook picks the seat item by price id precisely
        # because Stripe does not guarantee line-item order.
        line_items=[
            {"price": settings.STRIPE_TEAM_BASE_PRICE_ID, "quantity": 1},
            {"price": settings.STRIPE_TEAM_SEAT_PRICE_ID, "quantity": quantity},
        ],
        # Teams are Bedrock-managed automatically — there is no provider picker
        # in the team flow. Thread provider_choice into the subscription
        # metadata so the customer.subscription.* webhook persists it as the
        # idempotent backup when the subscription first goes live (the
        # metadata is a checkout-time snapshot, so live→live events never
        # re-apply it over a later change_provider switch), mirroring how
        # /trial-checkout threads the personal picker's choice. The router
        # also writes it synchronously before this call so a poll returning
        # from Checkout never flashes provider_required.
        metadata={"owner_id": owner_id, "purpose": "team_seat", "provider_choice": "bedrock_claude"},
        success_url=f"{FRONTEND_URL}/chat?checkout=success",
        cancel_url=f"{FRONTEND_URL}/?checkout=cancel",
        # Seat count rides in the key. Seats now move on INVITE — seconds
        # before the admin clicks Subscribe — so invite, open checkout,
        # abandon, invite again, Subscribe can all land inside one 5-minute
        # bucket. Without the quantity, that resends the same key with
        # different line items and Stripe either errors or replays the stale
        # session at the old seat count.
        idempotency_key=_checkout_idempotency_key("team_checkout", owner_id, str(quantity)),
    )


async def sync_team_seat_quantity(owner_id: str) -> int:
    """Set the team subscription's seat quantity to the live billable seat
    count (accepted members PLUS pending invitations — "no free members").

    The SINGLE writer of seat quantity — called from the membership webhooks,
    the reconcile safety-net, and post-checkout. Absolute set (no delta math),
    idempotent. Returns the resulting seat count.

    No-ops (returns the stored seat_count, or 0) when:
      - there's no active subscription, or
      - there's no ``subscription_item_id`` (grandfathered non-seat sub, or a
        team sub whose creation webhook hasn't landed yet), or
      - the live count already equals the stored count.

    Stripe errors are logged and swallowed (returns the stored count) so a
    caller in a webhook path still 200s; the reconcile net heals the drift.
    """
    from core.services import clerk_admin
    from core.services.provision_gate import is_subscription_active

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not is_subscription_active(account):
        return 0

    item_id = account.get("subscription_item_id")
    if not item_id:
        # Grandfathered / not-yet-webhooked — never touch a non-seat sub.
        return int(account.get("seat_count") or 0)

    current = int(account.get("seat_count") or 0)
    # Returns 0 when the count isn't known for certain, which the guard below
    # treats as "don't act" — an uncertain count must never move the Stripe
    # quantity. See get_organization_seat_count.
    member_count = await clerk_admin.get_organization_seat_count(owner_id)
    if member_count <= 0 or member_count == current:
        # 0 = Clerk read failed/stubbed; don't zero a real subscription.
        return current

    try:
        with timing("stripe.api.latency", {"op": "subscription_item.modify"}):
            stripe.SubscriptionItem.modify(
                item_id,
                quantity=member_count,
                proration_behavior="create_prorations",
            )
    except Exception:
        logger.exception("sync_team_seat_quantity: Stripe modify failed for owner %s", owner_id)
        return current

    await billing_repo.set_team_subscription(
        owner_id=owner_id,
        subscription_id=account.get("stripe_subscription_id"),
        subscription_item_id=item_id,
        status=account.get("subscription_status"),
        seat_count=member_count,
    )

    # Keep the agent-facing facts file's seat count current on the
    # membership-change path — without this, agents keep quoting the old
    # count until an unrelated webhook/reprovision rewrites the file. The
    # just-written count rides as an override. Internally best-effort.
    from core.services.owner_facts import write_owner_facts

    await write_owner_facts(owner_id, account_overrides={"seat_count": member_count})

    return member_count


async def create_ultra_upgrade(
    *,
    owner_id: str,
    clerk_user_id: str | None = None,
) -> dict | stripe.checkout.Session:
    """Upgrade an owner to the Ultra Pro tier.

    Ultra is *only* a container size ($150 base instead of $50) — it swaps a
    price, never the shape of the subscription. Two paths, picked by whether
    the owner already holds a LIVE subscription:

    - **In-place item swap** — an owner already on a live subscription
      (``stripe_subscription_id`` present + a LIVE ``subscription_status`` —
      any of ``LIVE_SUBSCRIPTION_STATUSES``, i.e. active/trialing but also
      past_due/unpaid/paused/incomplete) is upgraded by swapping ONE item's
      price to ``STRIPE_ULTRA_PRICE_ID`` on the SAME Stripe subscription,
      prorated. Keeps ONE subscription (no cancel + re-subscribe), so the
      owner is charged only the prorated difference and a still-billable
      (e.g. past_due) owner never gets a SECOND parallel subscription.

      A **personal** sub has a single item — that item is swapped. A
      **team** sub carries two items (flat base + per-seat, price ids
      ``STRIPE_TEAM_BASE_PRICE_ID`` / ``STRIPE_TEAM_SEAT_PRICE_ID``); only the
      BASE item is swapped to the Ultra price — the seat item's id and
      quantity are never touched, so a 4-seat Ultra team pays
      $150 + 4×$20/mo. Item selection is always by price id
      (``subscription_item_on_price``), never position — Stripe does not
      guarantee line-item order. A subscription whose shape isn't exactly one
      of those two (an unrecognised multi-item sub, a seat-only sub with no
      base item, etc.) is refused rather than repriced blind. Returns
      ``{"upgraded": True}``.

    - **Fresh checkout** — an owner with no live subscription (``trial_local``
      / canceled / never subscribed — no item to swap) gets a
      subscription-mode Checkout session on the Ultra price via the shared
      builder. Returns the Session; the caller redirects to ``session.url``.
      This branch always mints a single-item, seat-less Ultra subscription, so
      an **org** with no live subscription is refused up front — it must buy
      its team plan (base + seat) first, or it would end up paying $150/mo for
      zero seats with no seat item for ``sync_team_seat_quantity`` to drive.

    ``trial_local`` counts as "active" for platform use but carries no
    ``stripe_subscription_id``, so it correctly takes the fresh-checkout
    branch. A terminal status (``canceled``/``incomplete_expired``) never
    swaps in place even if a stale ``stripe_subscription_id`` lingers — it
    falls through to a fresh checkout rather than modifying a dead sub.

    The fresh-checkout branch serializes behind the same per-owner claim as
    ``create_flat_fee_checkout`` (no duplicate concurrent sessions) and threads
    the account's existing ``provider_choice`` into the session metadata so a
    trial_local / no-sub owner doesn't hit the provider_required gate after
    paying.

    The durable plan/tier write + container resize happen in the
    ``customer.subscription.*`` webhook (Task 4), NOT here — this only
    initiates the Stripe-side change. The webhook derives the Ultra tier from
    the resulting subscription's price and resizes.
    """
    if not settings.STRIPE_ULTRA_PRICE_ID:
        raise BillingServiceError("STRIPE_ULTRA_PRICE_ID not configured")

    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    account = await billing_repo.get_by_owner_id(owner_id)

    sub_id = account.get("stripe_subscription_id") if account else None
    status = account.get("subscription_status") if account else None

    # An org with no live subscription must buy its team plan first. The
    # fresh-checkout branch below emits a single-item Ultra subscription with
    # NO seat item — correct for a personal owner, but it would leave an org
    # paying $150/mo for zero seats and with no item for
    # sync_team_seat_quantity to drive.
    from core.auth import is_org_owner_id

    if is_org_owner_id(owner_id) and not (sub_id and status in LIVE_SUBSCRIPTION_STATUSES):
        raise BillingServiceError("Subscribe your team before upgrading to Ultra Pro")

    if sub_id and status in LIVE_SUBSCRIPTION_STATUSES:
        with timing("stripe.api.latency", {"op": "subscription.retrieve"}):
            sub = stripe.Subscription.retrieve(sub_id)
        items_data = sub["items"]["data"]

        seat_item = subscription_item_on_price(items_data, settings.STRIPE_TEAM_SEAT_PRICE_ID)
        base_item = subscription_item_on_price(items_data, settings.STRIPE_TEAM_BASE_PRICE_ID)
        ultra_item = subscription_item_on_price(items_data, settings.STRIPE_ULTRA_PRICE_ID)
        is_team_sub = bool(seat_item)

        # Which item carries the tier? A team's tier lives on its BASE item
        # (either the standard base, or the ultra price once upgraded); a
        # personal sub's tier is its only item. Never by position — Stripe does
        # not guarantee line-item order.
        if is_team_sub:
            target_item = base_item or ultra_item
            recognised = len(items_data) == 2 and bool(target_item)
        else:
            target_item = items_data[0] if len(items_data) == 1 else {}
            recognised = len(items_data) == 1

        # Defence in depth: never reprice an item on a subscription whose shape
        # we cannot fully explain (an unknown add-on, a coupon item, a
        # hand-edited sub). Recognised shapes are exactly: one item (personal),
        # or base+seat (team).
        if not recognised or not target_item:
            raise BillingServiceError("This subscription's plan can't be changed automatically")

        item_id = target_item["id"]
        with timing("stripe.api.latency", {"op": "subscription.modify"}):
            stripe.Subscription.modify(
                sub_id,
                items=[{"id": item_id, "price": settings.STRIPE_ULTRA_PRICE_ID}],
                proration_behavior="create_prorations",
                # Scope the key to the CURRENT subscription item id: a genuine
                # double-click retries the same item and collapses to one write,
                # while a later downgrade-then-re-upgrade lands on a NEW item id
                # (Stripe mints a fresh item across resubscribe) and gets a fresh
                # key — so the real re-upgrade re-swaps instead of returning
                # Stripe's cached first response.
                idempotency_key=f"ultra-upgrade-{owner_id}-{item_id}",
            )
        return {"upgraded": True}

    # No live subscription to swap — start a fresh Ultra subscription via
    # Checkout. Serialize session creation behind the same per-owner claim as
    # create_flat_fee_checkout so a trial/canceled owner can't open duplicate
    # concurrent checkout sessions (distinct idempotency keys would otherwise
    # each mint a completable session before either pending-checkout marker
    # lands). Self-expires (90s); the router maps this error to a 409.
    if not await billing_repo.try_claim_checkout_slot(owner_id):
        raise BillingServiceError("checkout_in_progress")

    # ``purpose="ultra"`` rides the subscription metadata so the webhook records
    # the Ultra tier when the subscription first goes live; ``clerk_user_id`` is
    # threaded through the same as the flat path so the webhook can resolve the
    # right per-Clerk-user row.
    metadata: dict = {"owner_id": owner_id, "purpose": "ultra"}
    if clerk_user_id:
        metadata["clerk_user_id"] = clerk_user_id
    # Seed the account's existing provider_choice into the checkout metadata
    # (mirrors create_flat_fee_checkout): without it a trial_local / no-sub owner
    # returns from Checkout and hits the provider_required gate. Omitted when the
    # account has none — never invent one.
    provider_choice = account.get("provider_choice") if account else None
    byo_provider = account.get("byo_provider") if account else None
    if provider_choice:
        metadata["provider_choice"] = provider_choice
    if byo_provider:
        metadata["byo_provider"] = byo_provider

    try:
        session = await _build_subscription_checkout(
            owner_id=owner_id,
            line_items=[{"price": settings.STRIPE_ULTRA_PRICE_ID, "quantity": 1}],
            metadata=metadata,
            success_url=(
                f"{FRONTEND_URL}/chat?checkout=success" + (f"&provider={provider_choice}" if provider_choice else "")
            ),
            cancel_url=f"{FRONTEND_URL}/?checkout=cancel",
            idempotency_key=_checkout_idempotency_key("ultra_checkout", owner_id),
        )
    except Exception:
        # No session exists — free the slot so an immediate retry doesn't wait
        # out the claim TTL.
        await billing_repo.release_checkout_slot(owner_id)
        raise
    # Fail CLOSED on a marker-write failure (mirrors create_flat_fee_checkout):
    # an unrecorded open session is invisible to every guard once the claim TTL
    # lapses, yet stays completable until its ~31-minute expiry. The user has not
    # seen this session's URL yet, so expire it, free the slot, and let the retry
    # mint fresh.
    try:
        await billing_repo.set_pending_checkout(owner_id, session_id=session.id, provider_choice=provider_choice)
    except Exception:
        logger.exception("Failed to record pending checkout session for owner %s", owner_id)
        try:
            with timing("stripe.api.latency", {"op": "checkout.session.expire"}):
                stripe.checkout.Session.expire(session.id)
        except Exception:
            logger.exception("Failed to expire unrecorded checkout session %s", session.id)
        try:
            await billing_repo.release_checkout_slot(owner_id)
        except Exception:
            logger.exception("Failed to release checkout slot for owner %s", owner_id)
        raise BillingServiceError("checkout_unavailable")
    return session


async def create_credit_top_up_checkout(
    *,
    owner_id: str,
    amount_cents: int,
    enable_auto_charge: bool = False,
) -> stripe.checkout.Session:
    """Create a Stripe Checkout session for a one-shot Claude-credit top-up.

    EXPLICIT payment is the default credits-purchase path: the customer
    sees the amount and their card on a Stripe-hosted page and actively
    confirms — no implicit card-on-file charge. The credits grant happens
    asynchronously when Stripe fires ``checkout.session.completed`` with
    metadata ``{"purpose": "credit_top_up", ...}``: the webhook creates an
    UNGATED Metronome commit (the money is already collected here, so
    Metronome must never invoice it a second time — see
    metronome_funding.grant_prepaid_credits).

    ``enable_auto_charge`` is the customer's opt-in to one-click top-ups:
    it sets ``payment_intent_data.setup_future_usage="off_session"`` —
    Stripe's documented consent artifact for charging the saved card
    off-session later — and rides the session metadata so the webhook
    flips the billing row's ``credits_auto_charge`` flag only after this
    consenting payment actually succeeds. Without it the session saves
    nothing and later top-ups keep routing through Checkout.

    Conventions match :func:`create_flat_fee_checkout`:
      - ``allow_promotion_codes`` (internal 100%-off coupons),
        ``automatic_tax``, ``customer_update.address=auto``.
      - ``idempotency_key`` includes a per-request nonce: legitimate repeat
        top-ups for the same amount must produce distinct sessions, so
        bucketing on owner_id+amount would silently collapse a second
        purchase onto the first.

    ``owner_id`` is the org_id in org context, the user_id in personal
    context — the credits pool at the owner level.
    """
    if amount_cents < 500:
        raise BillingServiceError("Minimum top-up is $5 (500 cents)")

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not account.get("stripe_customer_id"):
        raise BillingServiceError(f"No Stripe customer for owner_id={owner_id}")

    # Take money ONLY when the completion webhook can actually grant: mirror
    # grant_prepaid_credits' prerequisites (Metronome customer + contract on
    # the billing row, credits product configured) BEFORE creating a payable
    # session. Without this, a not-yet-provisioned owner pays on Stripe and
    # the grant then 500s/retries until provisioning lands — collected money
    # in limbo.
    if not settings.METRONOME_CREDITS_PRODUCT_ID:
        raise BillingServiceError("Credits are not configured for this environment")

    # Provision on demand rather than assuming something else already did.
    # This block used to just assert the ids were present, on the reasoning
    # that "provisioning re-runs on every status poll" — which is FALSE for a
    # personal owner with a running container: ``ensure_user_ready`` (the only
    # other caller of ensure_metronome_customer) is re-dispatched on a running
    # box only for org context or a failed Teams seat. A trial→paid converted
    # owner therefore sat permanently un-provisioned and got "try again in a
    # moment" forever — on the exact screen the credits gate sends them to.
    # Ensuring here makes buying credits self-healing, so a missed webhook
    # provision can never strand a paying customer with no way to pay us.
    # Idempotent and a no-op once the ids are on the row.
    if not account.get("metronome_customer_id") or not account.get("metronome_contract_id"):
        from core.billing.metronome_provisioning import ensure_metronome_customer

        provisioning_failed = False
        try:
            await ensure_metronome_customer(owner_id=owner_id)
        except Exception:
            provisioning_failed = True
            put_metric("metronome.provision.error", dimensions={"source": "credit_top_up"})
            logger.exception("On-demand Metronome provisioning failed for owner %s", owner_id)
        # Strongly consistent: we just wrote these ids and are about to decide
        # whether to take the customer's money on the strength of them.
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True) or account
        if provisioning_failed and (
            not account.get("metronome_customer_id") or not account.get("metronome_contract_id")
        ):
            # Do NOT tell the buyer "try again in a moment" here. Provisioning
            # raised, so retrying hits the same failure every time — the old
            # message sent them into an infinite retry loop with nothing
            # surfaced to them or to support saying it could never work. The
            # transient wording below is reserved for the case where
            # provisioning ran WITHOUT raising and the ids still aren't
            # visible, which a moment genuinely can fix.
            raise BillingServiceError(
                "We couldn't finish setting up billing for this account. "
                "Please contact support@isol8.co — retrying won't clear this."
            )
    if not account.get("metronome_customer_id") or not account.get("metronome_contract_id"):
        raise BillingServiceError("Credits aren't ready for this account yet — try again in a moment")

    # Per-request nonce — see docstring. Hashed to keep the key short.
    nonce = hashlib.sha1(os.urandom(16)).hexdigest()[:12]

    payment_intent_data: dict = {
        "metadata": {
            "purpose": "credit_top_up",
            "owner_id": owner_id,
        },
    }
    if enable_auto_charge:
        payment_intent_data["setup_future_usage"] = "off_session"

    with timing("stripe.api.latency", {"op": "checkout.session.create"}):
        session = stripe.checkout.Session.create(
            customer=account["stripe_customer_id"],
            mode="payment",
            line_items=[
                {
                    "price_data": {
                        "currency": "usd",
                        "unit_amount": amount_cents,
                        "product_data": {
                            "name": "Claude credits",
                            "description": "Prepaid Claude inference balance.",
                        },
                    },
                    "quantity": 1,
                }
            ],
            success_url=f"{FRONTEND_URL}/chat?credits=success",
            cancel_url=f"{FRONTEND_URL}/chat?credits=cancel",
            allow_promotion_codes=True,
            automatic_tax={"enabled": True},
            customer_update={"address": "auto"},
            payment_intent_data=payment_intent_data,
            metadata={
                "purpose": "credit_top_up",
                "owner_id": owner_id,
                "amount_cents": str(amount_cents),
                "auto_charge_opt_in": "true" if enable_auto_charge else "false",
            },
            idempotency_key=f"credit_checkout:{owner_id}:{nonce}",
        )
    return session


# ---------------------------------------------------------------------------
# Module-level admin wrappers
# ---------------------------------------------------------------------------
#
# Used by routers/admin.py — operate by Clerk user_id rather than billing_account
# dict so the admin router doesn't have to load the row first. Each function
# loads billing_repo + calls Stripe + updates DDB. Errors raise BillingServiceError.


async def cancel_subscription_for_owner(user_id: str) -> dict:
    """Admin: cancel a user's Stripe subscription. Idempotent on no-sub."""
    billing = await billing_repo.get_by_owner_id(user_id)
    if not billing:
        return {"status": "no_billing_account"}
    sub_id = billing.get("stripe_subscription_id")
    if not sub_id:
        return {"status": "no_subscription"}
    try:
        stripe.Subscription.delete(
            sub_id,
            idempotency_key=f"sub_cancel:{sub_id}",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_cancel_failed: {e}")
    await BillingService().cancel_subscription(billing)
    return {"status": "cancelled", "subscription_id": sub_id}


def _is_stripe_missing(e: Exception) -> bool:
    """True when a Stripe API error means "already gone" — an
    InvalidRequestError with code="resource_missing", or a bare 404 status.

    Checked via duck-typed attribute access rather than
    `isinstance(e, stripe.InvalidRequestError)` on purpose: this module's
    existing tests patch `core.services.billing_service.stripe` wholesale
    with a MagicMock (see test_billing_service.py), which would turn
    `stripe.InvalidRequestError` into a non-exception MagicMock attribute —
    an `except stripe.InvalidRequestError` clause would then raise TypeError
    the moment any exception is actually thrown. Duck-typing sidesteps that
    regardless of how `stripe` is mocked in a given test.
    """
    if getattr(e, "code", None) == "resource_missing":
        return True
    return getattr(e, "http_status", None) == 404


async def delete_stripe_customer(owner_id: str) -> bool:
    """Admin Account-Wipe teardown step 10
    (docs/superpowers/specs/2026-07-14-admin-account-wipe-design.md):
    cancel the owner's Stripe subscription (if any) then delete their
    Stripe customer.

    No-ops (returns False) when the billing row is missing or carries no
    stripe_customer_id — trial_local owners have neither, and this lets the
    wipe orchestrator call it unconditionally. Returns True only when it
    actually deleted a Stripe customer.

    Treats "already gone" (Stripe resource_missing / 404) as success at
    each step, so this is safe to re-run after a partial wipe failure.
    Any other Stripe failure raises BillingServiceError, mirroring
    cancel_subscription_for_owner / pause_subscription_for_owner above.
    """
    billing = await billing_repo.get_by_owner_id(owner_id)
    if not billing or not billing.get("stripe_customer_id"):
        return False

    sub_id = billing.get("stripe_subscription_id")
    if sub_id:
        try:
            stripe.Subscription.delete(
                sub_id,
                idempotency_key=f"wipe_sub_cancel:{sub_id}",
            )
        except Exception as e:  # noqa: BLE001
            if not _is_stripe_missing(e):
                raise BillingServiceError(f"stripe_wipe_cancel_failed: {e}")

    customer_id = billing["stripe_customer_id"]
    try:
        stripe.Customer.delete(
            customer_id,
            idempotency_key=f"wipe_customer_delete:{customer_id}",
        )
    except Exception as e:  # noqa: BLE001
        if not _is_stripe_missing(e):
            raise BillingServiceError(f"stripe_wipe_delete_failed: {e}")

    return True


async def pause_subscription_for_owner(user_id: str) -> dict:
    """Admin: pause Stripe subscription billing (mark_uncollectible)."""
    billing = await billing_repo.get_by_owner_id(user_id)
    if not billing or not billing.get("stripe_subscription_id"):
        return {"status": "no_subscription"}
    sub_id = billing["stripe_subscription_id"]
    try:
        stripe.Subscription.modify(
            sub_id,
            pause_collection={"behavior": "mark_uncollectible"},
            idempotency_key=f"sub_modify:{sub_id}:pause",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_pause_failed: {e}")
    return {"status": "paused", "subscription_id": sub_id}


async def issue_credit_for_owner(user_id: str, *, amount_cents: int, reason: str) -> dict:
    """Admin: credit the user's Stripe customer balance by amount_cents.

    Stripe applies negative balance amounts as a credit toward future invoices.
    """
    billing = await billing_repo.get_by_owner_id(user_id)
    if not billing or not billing.get("stripe_customer_id"):
        return {"status": "no_customer"}
    customer_id = billing["stripe_customer_id"]
    # No admin_action_id is threaded through here yet, so derive a stable
    # hash from (amount, reason). Two genuinely-different credits with the
    # same reason but different amounts get different keys; a retry of the
    # same logical credit collapses to one Stripe write.
    reason_hash = hashlib.sha256(f"{amount_cents}:{reason}".encode("utf-8")).hexdigest()[:16]
    try:
        txn = stripe.Customer.create_balance_transaction(
            customer_id,
            amount=-abs(amount_cents),
            currency="usd",
            description=reason,
            idempotency_key=f"balance_tx:{customer_id}:{reason_hash}",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_credit_failed: {e}")
    return {"status": "credited", "transaction_id": txn["id"], "amount_cents": amount_cents}


async def mark_invoice_resolved(user_id: str, invoice_id: str) -> dict:
    """Admin: mark a Stripe invoice as paid out-of-band (e.g. wire transfer)."""
    try:
        invoice = stripe.Invoice.pay(
            invoice_id,
            paid_out_of_band=True,
            idempotency_key=f"invoice_pay:{invoice_id}",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_invoice_pay_failed: {e}")
    return {"status": "resolved", "invoice_id": invoice_id, "stripe_status": invoice.get("status")}


# ---------------------------------------------------------------------------
# Post-onboarding provider switch (POST /billing/provider)
# ---------------------------------------------------------------------------
#
# Mid-flight provider changes have to do three things atomically:
#   1. Flip provider_choice (+ byo_provider) on the billing row.
#   2. Clear the *old* provider's side-effect state (OAuth tokens, BYOK
#      Secrets Manager secrets) so a future re-switch doesn't read stale
#      credentials. Cleared AFTER the new state is committed — partial
#      failure leaves the owner with an extra row to garbage-collect, never
#      an owner stranded with no provider.
#   3. Re-provision the container. The new provider needs different
#      task-def secrets (BYOK env vars) or different EFS pre-staging
#      (chatgpt_oauth auth-profiles.json), neither of which a Track-1
#      openclaw.json patch alone can achieve. Re-provisioning ends the
#      current chat session — the frontend surfaces this as an explicit
#      cutover warning before calling the endpoint.


async def change_provider(
    *,
    owner_id: str,
    clerk_user_id: str,
    owner_type: str,
    provider_choice: ProviderChoice,
    byo_provider: ByoProvider | None = None,
) -> dict:
    """Apply a post-onboarding provider switch for an existing subscriber.

    Args:
        owner_id: org_id for org context, user_id for personal. Pins the
            billing row + the container scope (re-provision target).
        clerk_user_id: The signed-in human's Clerk user_id. OAuth tokens and
            BYOK keys live keyed on this calling user. For an org switching
            INTO chatgpt_oauth the caller must BE the workspace owner (the
            recorded ``oauth_connector_user_id``, or claim it if unset) — one
            ChatGPT account ↔ one container.
        owner_type: ``"personal"`` or ``"org"``. Drives the org workspace-owner
            check on a switch into chatgpt_oauth, and the leaving-chatgpt
            cleanup (which key to revoke vs. which EFS file to delete).
        provider_choice: Target provider.
        byo_provider: Required when ``provider_choice == "byo_key"``; one of
            "openai" or "anthropic".

    Returns:
        ``{"status": "ok"|"noop", "provider_choice": ..., "byo_provider": ...}``.
        ``noop`` means the requested state matched the current billing-row
        state and no work was done.

    Raises:
        ChangeProviderError: with a code + status_code the router maps to HTTP.
    """
    # Subscription must exist and be currently usable. The post-onboarding
    # switch endpoint is NOT a substitute for /billing/trial-checkout —
    # if there's no subscription we route the caller back through the
    # signup picker rather than mutating provider state on a dead row.
    from core.services.provision_gate import is_subscription_active

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account:
        raise ChangeProviderError(
            "no_billing_account",
            "No billing account. Subscribe via /onboarding first.",
            status_code=404,
        )
    # A no-card local-trial row converts to a paid plan through
    # /billing/trial-checkout, where the provider is chosen at checkout — NOT
    # through this post-onboarding switch. trial_local counts as "active" for
    # is_subscription_active, so guard it explicitly here (before any Stripe /
    # reprovision work) rather than let it fall through onto the meta box.
    if account.get("subscription_status") == "trial_local":
        raise ChangeProviderError(
            "use_trial_checkout",
            "Pick a plan first — provider selection happens at checkout for free-tier accounts.",
            status_code=409,
        )
    if not is_subscription_active(account):
        raise ChangeProviderError(
            "subscription_inactive",
            "Subscription is not active. Manage it in /settings/billing.",
            status_code=409,
        )

    current_choice = account.get("provider_choice")
    current_byo = account.get("byo_provider")
    next_byo = byo_provider if provider_choice == "byo_key" else None

    # The org owner whose tokens back this workspace (None for rows that
    # were never on chatgpt_oauth and for pre-connector legacy rows).
    # Captured BEFORE the provider write below REMOVEs/overwrites it —
    # cleanup and the owner check both need the recorded identity.
    recorded_connector = account.get("oauth_connector_user_id")

    # One ChatGPT account ↔ one container, bound to the workspace OWNER:
    # only the recorded owner may (re)select chatgpt_oauth for an org. A
    # legacy org row with no recorded owner is claimed by the first admin
    # to switch (the only sane backfill for rows that predate the field).
    from core.services.oauth_service import caller_may_manage_connector

    if (
        provider_choice == "chatgpt_oauth"
        and owner_type == "org"
        and not caller_may_manage_connector(account, clerk_user_id)
    ):
        raise ChangeProviderError(
            "not_workspace_owner",
            "Only the workspace owner who connected ChatGPT can select this provider.",
            status_code=403,
        )

    # Fast-path no-op. Returning early instead of re-provisioning prevents
    # an idle "change provider" click from blowing away the user's chat
    # session for no functional change. byo_key with the same byo_provider
    # is treated as a no-op too — different byo_provider DOES re-provision
    # because the env-var secret reference changes.
    if current_choice == provider_choice and current_byo == next_byo:
        return {
            "status": "noop",
            "provider_choice": provider_choice,
            "byo_provider": next_byo,
        }

    # Prerequisite check for the *new* provider. Done before the DDB write
    # so a missing prerequisite returns 409 with the chosen state still
    # consistent — the caller can complete the prerequisite step in the UI
    # (OAuth device flow / paste API key) and retry without our row in an
    # intermediate state.
    if provider_choice == "chatgpt_oauth":
        from core.services import oauth_service

        # The connector's row must be bound to THIS workspace (owner_id). A row
        # the same human claimed for a different workspace reads as missing —
        # one ChatGPT account ↔ one container, so we don't let a switch borrow
        # tokens that back another container.
        tokens = await oauth_service.get_decrypted_tokens(user_id=clerk_user_id, bound_to=owner_id)
        if not tokens:
            raise ChangeProviderError(
                "oauth_required",
                "Complete the ChatGPT sign-in step before switching to ChatGPT OAuth.",
                status_code=409,
            )
    elif provider_choice == "byo_key":
        if byo_provider not in ("openai", "anthropic"):
            raise ChangeProviderError(
                "byo_provider_required",
                "byo_provider must be 'openai' or 'anthropic' when switching to BYO key.",
                status_code=422,
            )
        from core.services.key_service import get_secret_arn

        # Scoped to owner_id, NOT clerk_user_id. BYOK secrets are stored
        # per-OWNER: every route in routers/settings_keys.py (list/set/delete)
        # keys them on resolve_owner_id(auth), and the provisioner reads them
        # back the same way (ecs_manager._build_byo_secrets_for_user). For a
        # personal owner the two ids are identical, which is why this went
        # unnoticed — but for an ORG they diverge (org_... vs user_...), so an
        # org admin who had just saved a key hit a spurious `byo_key_missing`
        # here and could never complete the switch.
        #
        # Contrast the chatgpt_oauth branch above, which correctly uses BOTH:
        # OAuth tokens are per-HUMAN bound to a workspace, so they need the
        # clerk id AND owner_id. A BYOK secret has no such per-human identity.
        if not await get_secret_arn(owner_id, byo_provider):
            raise ChangeProviderError(
                "byo_key_missing",
                f"Save your {byo_provider.title()} API key before switching to BYO key.",
                status_code=409,
            )
    # bedrock_claude: no prerequisite. The credits gate fires later via
    # provision_gate.evaluate_provision_gate — we don't pre-block here so
    # owners with zero balance can still pick Bedrock + see the structured
    # gate UI prompting them to top up.

    # Commit the new provider state. Done BEFORE side-effect cleanup so a
    # crash mid-cleanup leaves the owner in a coherent state (new provider
    # + a bit of stale OAuth/BYOK data, which the next sweep can reclaim).
    await billing_repo.set_provider_choice(
        owner_id,
        provider_choice=provider_choice,
        byo_provider=next_byo,
        connector_user_id=clerk_user_id if provider_choice == "chatgpt_oauth" else None,
    )

    # Keep the agents' view of plan/provider current — the owner-facts file
    # is what the bundled isol8 skill points them at. The hot-swap path never
    # reaches write_user_configs (which owns the write on the cold path), so
    # this explicit write is what keeps the file fresh there; on the cold
    # path it is a cheap redundant write that also covers a redeploy dying
    # before write_user_configs. The just-committed values ride as overrides
    # — same-process pinning that outranks any row read. Internally
    # best-effort.
    from core.services.owner_facts import write_owner_facts

    await write_owner_facts(
        owner_id,
        account_overrides={"provider_choice": provider_choice, "byo_provider": next_byo},
    )

    # Side-effect cleanup: drop the OLD provider's credentials so a future
    # switch back doesn't reuse stale state. Each branch is best-effort —
    # a failure here is logged but does not roll back the billing-row
    # update. The provider switch is the user-visible commit point; orphan
    # secrets/tokens are an operator-sweep concern.
    if current_choice == "chatgpt_oauth" and provider_choice != "chatgpt_oauth":
        from core.containers.workspace import delete_auth_profile_store
        from core.services.oauth_service import revoke_user_oauth

        # One ChatGPT account ↔ one container: the owner's token row serves
        # nothing but this workspace, so leaving chatgpt_oauth fully revokes
        # it — identical for personal and org. Switching back later requires
        # a fresh sign-in by design (never reuse stale credentials).
        try:
            # Tokens are keyed on the human who connected (recorded_connector);
            # for legacy/personal rows with no recorded owner, clerk_user_id IS
            # that human (personal owner_id == user_id).
            await revoke_user_oauth(user_id=recorded_connector or clerk_user_id)
        except Exception:
            logger.exception(
                "change_provider: revoke_user_oauth failed for connector=%s",
                recorded_connector or clerk_user_id,
            )
        try:
            # EFS auth file is keyed on the CONTAINER OWNER (org_id for
            # orgs), not the human who connected.
            await delete_auth_profile_store(user_id=owner_id)
        except Exception:
            logger.exception(
                "change_provider: delete_auth_profile_store failed for owner=%s",
                owner_id,
            )
        # The coding-harness grant leaves with the provider: after the switch
        # the new config stops advertising opencode and the management card is
        # hidden, so a surviving row + auth.json would be an unmanageable live
        # credential. Switching back later re-offers the grant in the wizard.
        # FILE first, row second (same order as the disconnect endpoints): the
        # file is the live credential, so if the unlink fails the row must
        # survive with it — row-and-file stay consistent and a later
        # disconnect/wipe can still find and clean the grant.
        from core.containers.workspace import delete_opencode_auth
        from core.services.oauth_service import PURPOSE_HARNESS

        try:
            await delete_opencode_auth(user_id=owner_id)
        except Exception:
            logger.exception(
                "change_provider: delete_opencode_auth failed for owner=%s — keeping the harness row",
                owner_id,
            )
        else:
            try:
                await revoke_user_oauth(user_id=recorded_connector or clerk_user_id, purpose=PURPOSE_HARNESS)
            except Exception:
                logger.exception(
                    "change_provider: harness revoke_user_oauth failed for connector=%s",
                    recorded_connector or clerk_user_id,
                )
        # On 6.6 the deleted EFS seed isn't what the running container reads —
        # the credential it imported lives in its SQLite store. Tell the live
        # gateway to drop it via models.authLogout so switch-away revokes the
        # token now (best-effort; no-op when the container is cold).
        from core.services.container_auth import revoke_provider_auth_in_container

        await revoke_provider_auth_in_container(owner_id)

    if current_choice == "byo_key" and provider_choice != "byo_key" and current_byo in ("openai", "anthropic"):
        from core.services.key_service import KeyService

        try:
            await KeyService().delete_key(clerk_user_id, current_byo)
        except Exception:
            logger.exception(
                "change_provider: delete_key failed for clerk_user_id=%s provider=%s",
                clerk_user_id,
                current_byo,
            )

    # Apply the swap to the running container.
    #
    # Two paths, picked by whether either side of the transition uses BYO:
    #
    # - **Cold path** (BYO involved on either side) — re-register the ECS
    #   task definition and force a new deployment. Required because BYOK
    #   injects ``OPENAI_API_KEY`` / ``ANTHROPIC_API_KEY`` via task-def
    #   ``secrets: [{valueFrom: <SecretsManagerARN>}]`` resolved at task
    #   start; ECS does not let you mutate a running task's env vars.
    #   Ends the user's current chat session.
    #
    # - **Hot path** (Bedrock ↔ ChatGPT OAuth only) — Track-1 patch the
    #   three provider-block subtrees in openclaw.json (handled atomically
    #   by ``config_patcher.replace_provider_block``) and, for a switch
    #   *into* chatgpt_oauth, also pre-stage ``auth-profiles.json`` to EFS
    #   *before* the patch so chokidar's reload reads it in place.
    #   OpenClaw classifies all three keys as ``kind: "hot"`` in
    #   ``gateway/config-reload-plan.ts`` (``restart-heartbeat`` +
    #   ``reload-plugins``), so the gateway swaps providers without
    #   restarting. No task-def env-var change → no container restart →
    #   the chat session stays alive.
    #
    # On the hot path, if the owner has no ``openclaw.json`` on EFS yet
    # (legacy / un-provisioned owner), ``replace_provider_block`` raises
    # ``ConfigPatchError`` and we fall through to the cold path so the
    # file gets written from scratch by ``provision_user_container``.
    involves_byo = current_choice == "byo_key" or provider_choice == "byo_key"

    # A switch INTO bedrock_claude must take the COLD path: the per-owner
    # scoped task role is attached only by a task-def re-register
    # (provision_user_container, Task 9), which the hot path never does. The
    # config builder now fails closed on bedrock without aip_arns, so the hot
    # path cannot synthesize a bedrock provider block anyway.
    targets_bedrock = provider_choice == "bedrock_claude"

    if not involves_byo and not targets_bedrock:
        try:
            await _hot_swap_provider(
                owner_id=owner_id,
                clerk_user_id=clerk_user_id,
                new_provider=provider_choice,
            )
            put_metric(
                "billing.provider.changed",
                dimensions={
                    "from": current_choice or "none",
                    "to": provider_choice,
                    "path": "hot",
                },
            )
            return {
                "status": "ok",
                "provider_choice": provider_choice,
                "byo_provider": next_byo,
                "restart": False,
            }
        except _HotSwapUnavailableError:
            # Owner has no openclaw.json on EFS yet — fall through to the
            # cold path. logger inside _hot_swap_provider already recorded
            # the reason.
            pass

    put_metric(
        "billing.provider.changed",
        dimensions={
            "from": current_choice or "none",
            "to": provider_choice,
            "path": "cold",
        },
    )

    # Stop-first (F1). A bare dispatch_provision call's FIRST act is
    # the single-flight provision claim, whose ConditionExpression admits
    # only no-row/error/stopped/stale-provisioning — a "running" row is NOT
    # claimable, so the dispatch loses the claim and stands down silently:
    # provision_user_container (the only branch that re-registers the task
    # def + rewrites openclaw.json) is never reached, yet we'd still tell
    # the caller ``restart: True``. reprovision_for_user fixes this by
    # scaling the service to 0 and waiting for it to drain BEFORE
    # dispatching dispatch_provision, so the dispatch lands on the
    # scale-from-zero branch instead of racing (and losing to) the still-
    # running row. Mirrors the admin container-reprovision action
    # (routers/admin.py) — see EcsManager.reprovision_for_user's docstring.
    #
    # Split across the request boundary: the stop is awaited (one fast ECS
    # call — fail-loud, and it guarantees the old-provider container is
    # already draining by the time we answer ``restart: True``), but the
    # drain-wait + provision dispatch run as a background task —
    # ``_wait_for_service_drained`` polls for up to 90s, which would hang
    # the ``/billing/provider`` request past ALB/client timeouts and, if
    # the handler were cancelled mid-drain, strand the owner stopped with
    # no provision dispatched. Once the row is "stopped", the sequence is
    # self-healing: even if this task dies, the next /container/status
    # poll's wake path wins the claim and applies the committed provider.
    # (stop_user_service treats a missing ECS service as idempotent
    # success, so a subscribed-but-never-provisioned owner falls through
    # to dispatch_provision's create-from-scratch path.)
    #
    # Accepted race — a status-poll wake DURING the drain window: the
    # poll's retryable-state dispatch can claim the stopped row before
    # the drain-only task's delayed dispatch does. Benign by design: the
    # single-flight lease admits exactly one executor (the loser stands
    # down), both executors would apply the SAME committed provider, and
    # task overlap on EFS is fenced one layer down —
    # SINGLE_WRITER_DEPLOYMENT_CONFIG (maximumPercent=100) keeps ECS from
    # starting the new task until the draining one has fully stopped, so
    # the drain-wait is defense-in-depth, not the single-writer fence.
    # The same window has always existed on the admin reprovision path.
    # Load-bearing detail: the task below must NOT re-stop (see
    # drain_then_dispatch_provision) or it would clobber the poll's claim
    # and break exactly this guarantee.
    from core.containers import get_ecs_manager

    manager = get_ecs_manager()
    stop_issued = False
    try:
        # Fence FIRST, stop SECOND — the order is load-bearing. A
        # status-poll wake that claimed BEFORE the provider commit
        # resolved the OLD provider and would keep passing its leased
        # checkpoints, finish with the old config, and leave our delayed
        # dispatch standing down — restart: True with the switch silently
        # unapplied. takeover_provision supersedes that lease so the
        # stale run stops at its next checkpoint (the same deliberate-
        # door fencing conversion, /recover, and admin reprovision use —
        # the cold provider change is the same class of door). Taking
        # over BEFORE the stop also closes the ordering race the other
        # way around: with stop-then-takeover, a poll could claim the
        # freshly-exposed stopped row in the await gap, the takeover
        # would preserve its "provisioning" status, and our dispatch
        # couldn't claim — wedging the row until the stale-lease timeout.
        # Takeover-first leaves no such gap: a poll can't claim a
        # still-running row, and once the stop lands our dispatch always
        # claims the stopped row (stopped is claimable regardless of the
        # takeover's fresh lease). Guarded on row existence: with NO row
        # there is no in-flight run to fence, and takeover on an absent
        # row would CREATE it as provisioning with a fresh lease — which
        # the dispatch's own claim then refuses, breaking the
        # create-from-scratch path for never-provisioned owners.
        from core.repositories import container_repo as _container_repo

        # Stop ONLY when a row exists (whose lease we just took over). With no
        # row there is nothing to stop, and issuing the stop anyway opens a
        # TOCTOU: a concurrent /container/status poll can win a first-time
        # provision (creating the row) between this check and the stop, and
        # stop_user_service's update_status would then stamp that
        # freshly-claimed row "stopped" — it is conditioned on the row EXISTING,
        # not on the lease, so being a scoped write does not save it here —
        # resurrecting the double-provision class this path exists to prevent.
        # The no-row case falls straight through to the drain-only dispatch,
        # whose try_claim_provision creates-from-scratch (or stands down behind
        # the poll's claim) under the single-flight lease.
        if await _container_repo.get_by_owner_id(owner_id) is not None:
            await _container_repo.takeover_provision(owner_id)
            stop_issued = True
            await manager.stop_user_service(owner_id, reason="reprovision")
    except Exception as e:
        # Any stop-path failure, not just the wrapped ECS call
        # (EcsManagerError): stop_user_service's row writes after the ECS
        # call (update_status / stamp_stop_reason) propagate raw DDB
        # exceptions, and skipping the revert for those would leave the
        # provider row committed with no dispatch and no convergence path
        # — the exact poisoned-no-op state the revert exists to prevent.
        # A genuine ECS failure (throttle, cluster outage — not-found is
        # already idempotent success inside stop_user_service). The new
        # provider is committed but the container was NOT stopped, so
        # nothing will converge it: a running row is neither poll-retryable
        # nor claimable, and a retry of this call would hit the no-op
        # fast-path above (row already matches the request) and dispatch
        # nothing. REVERT the commit so the retry re-runs the full switch
        # instead of silently no-oping. The old provider's credentials may
        # already be gone (the cleanup block above is fire-and-forget by
        # design); that fork is user-recoverable — the provision gate /
        # prerequisite checks drive re-auth — whereas a poisoned no-op is
        # not. A legacy row with no prior provider_choice can't be written
        # back (the repo validates the value); its retry-noop exposure
        # requires that compound (never-chosen provider AND ECS failing)
        # so it is accepted.
        #
        # Partial-stop shape: a raw (non-EcsManagerError) exception means
        # the ECS call SUCCEEDED and only the row write after it failed —
        # the service is scaling to 0 while the row still says "running",
        # which neither the poll nor a claim can heal. Best-effort
        # re-stamp "stopped" so the wake path restores the (reverted) old
        # provider. An EcsManagerError means the stop was NOT applied, so
        # no stamp (stamping a genuinely-running container would trigger
        # a pointless restart cycle on a transient ECS throttle). If the
        # re-stamp also fails (the same DDB that just failed),
        # convergence still exists via /recover's no-IP branch; a fuller
        # partial-stop state machine is not worth its weight for a
        # compound transient.
        from core.containers.ecs_manager import EcsManagerError
        from core.repositories import container_repo

        # stop_issued gates the stamp: a failure BEFORE the stop call (the
        # takeover fence's row read/write) must not mark a genuinely-
        # running container "stopped" — that would trigger a pointless
        # poll-wake restart cycle on a transient DDB blip.
        if stop_issued and not isinstance(e, EcsManagerError):
            try:
                await container_repo.update_status(owner_id, "stopped")
            except Exception:  # noqa: BLE001 — best-effort by design
                logger.warning("change_provider: best-effort stopped re-stamp failed for %s", owner_id)
        # Restore the pre-commit provider so a retry re-runs the full switch
        # instead of hitting the no-op fast path. A LEGACY row carried no
        # provider_choice (current_choice is None) and the repo rejects a None
        # write, so restore the resolved legacy default "bedrock_claude" -- the
        # exact value resolve_provider_choice (container_provision.py) derives
        # for a None row, i.e. the provider the container was actually running.
        # This both preserves the effective state AND un-no-ops a
        # switch-to-non-bedrock retry (row now explicitly bedrock_claude, which
        # differs from the requested provider). current_byo stays None for a
        # legacy row.
        restore_choice = current_choice or "bedrock_claude"
        await billing_repo.set_provider_choice(
            owner_id,
            provider_choice=restore_choice,
            byo_provider=current_byo,
            connector_user_id=recorded_connector if restore_choice == "chatgpt_oauth" else None,
        )
        await write_owner_facts(
            owner_id,
            account_overrides={"provider_choice": restore_choice, "byo_provider": current_byo},
        )
        raise ChangeProviderError(
            "reprovision_stop_failed",
            "Could not restart the container to apply the new provider. No changes were made — try again.",
            status_code=502,
        ) from e
    # Drain-only continuation — deliberately NOT reprovision_for_user,
    # whose leading re-stop would clobber a poll-claimed "provisioning"
    # row back to "stopped" and let a second executor claim it (see
    # drain_then_dispatch_provision's docstring).
    asyncio.create_task(manager.drain_then_dispatch_provision(owner_id))

    return {
        "status": "ok",
        "provider_choice": provider_choice,
        "byo_provider": next_byo,
        "restart": True,
    }


class _HotSwapUnavailableError(Exception):
    """Internal — signals change_provider to fall back to the cold path."""


async def _hot_swap_provider(
    *,
    owner_id: str,
    clerk_user_id: str,
    new_provider: ProviderChoice,
) -> None:
    """Apply a Bedrock ↔ ChatGPT-OAuth provider switch hot, no container restart.

    Step order matters:
      1. If switching *to* chatgpt_oauth, write ``auth-profiles.json``
         to EFS FIRST. Once we patch openclaw.json (step 2) chokidar fires
         a reload and the openai provider initializes — it needs the
         auth file already in place.
      2. Atomically swap the three openclaw.json subtrees via
         ``replace_provider_block``. OpenClaw's reload-plan picks up the
         change as ``kind: "hot"``.

    Switching *away* from chatgpt_oauth has the symmetric cleanup
    (``revoke_user_oauth`` + ``delete_auth_profile_store``) but that runs
    in the caller's side-effect cleanup block earlier — not here — so the
    stale auth file is already gone before we patch.

    Raises ``_HotSwapUnavailableError`` if the owner has no openclaw.json
    on EFS yet (legacy / un-provisioned), signaling the caller to fall
    back to the cold path.
    """
    from core.containers.config import (
        build_acp_config,
        build_provider_block,
        cheap_model_ref,
        derive_enabled_harnesses,
        heartbeat_model_ref,
        premium_model_ref,
    )
    from core.services.config_patcher import ConfigPatchError, replace_provider_block

    if new_provider == "chatgpt_oauth":
        # We already gated on tokens existing in the prerequisite check at
        # the top of change_provider. Re-read so the file we write reflects
        # whatever rotation may have happened concurrently.
        from core.containers.workspace import (
            clear_default_agent_auth_profile_row,
            pre_stage_auth_profile_store,
        )
        from core.services import oauth_service

        # READ the tokens under the switcher (the new connector): tokens live
        # keyed on the human who completed the device-code flow, which IS the
        # clerk_user_id we just recorded as oauth_connector_user_id. Pass
        # ``bound_to=owner_id`` so we only read a row this connector claimed for
        # THIS workspace — one ChatGPT account ↔ one container.
        tokens = await oauth_service.get_decrypted_tokens(user_id=clerk_user_id, bound_to=owner_id)
        if not tokens:
            # Lost the tokens between the prerequisite check and now (very
            # rare — concurrent disconnect). Bail out to the cold path so
            # the user lands on the oauth_required gate next instead of a
            # silently misconfigured container.
            logger.warning(
                "change_provider hot path: tokens disappeared between gate and pre-stage for user=%s",
                clerk_user_id,
            )
            raise _HotSwapUnavailableError("oauth_tokens_missing")
        # WRITE the auth file under the CONTAINER OWNER (org_id for orgs): the
        # EFS workspace + auth-profiles.json are owner-scoped, not keyed on the
        # human connector. For personal owners owner_id == clerk_user_id.
        await pre_stage_auth_profile_store(user_id=owner_id, oauth_tokens=tokens)
        # Staging oauth.json alone is not enough on an ALREADY-RUNNING 6.6
        # container: its per-agent SQLite auth store imports oauth.json ONLY when
        # the store row is absent, and a prior session (or authLogout-on-
        # disconnect) leaves an empty ``{"profiles":{}}`` row that shadows the
        # import. Delete the default agent's row so the freshly staged credential
        # re-imports on the next run — without this, a switch/reconnect into
        # chatgpt_oauth keeps using the old (often spent) token. See
        # project_openclaw_oauth_auth_model. Best-effort: a fresh container has no
        # row (no-op) and the EFS file alone still seeds a cold boot. On the local-sqlite
        # local-SQLite layout the row lives on container-local disk — the clear
        # returns "restart_required" after writing a boot sentinel, and only a
        # task replacement applies it, so the swap cannot stay hot: fall back
        # to the cold path, whose restart contract the frontend already
        # handles. Switching AWAY stays hot (models.authLogout needs no clear).
        cleared = await clear_default_agent_auth_profile_row(user_id=owner_id)
        if cleared == "restart_required":
            raise _HotSwapUnavailableError("auth_clear_requires_restart")

    providers, default_model, plugin_entries = build_provider_block(
        provider_choice=new_provider,
        user_id=clerk_user_id,
        byo_provider=None,  # hot path never carries byo_key
    )
    # The CEO (Friday) runs on the premium tier via a per-agent override; the
    # hot swap must re-pin (or clear) it so it tracks the new provider. None on
    # flat-rate paths (chatgpt_oauth), which drops any stale override.
    ceo_model = premium_model_ref(new_provider, None)
    # Compaction/memory-flush aux overrides track cheap_model_ref, which is
    # deliberately None on chatgpt_oauth (Codex native compaction rejects an
    # OpenClaw-side summarizer override — a pinned mini would make compaction
    # fail and the session overflow), so the swap CLEARS those slots there.
    cheap_model = cheap_model_ref(new_provider, None)
    # The heartbeat/utility lane is different in kind: both are ordinary
    # completions the OAuth token CAN serve on the mini tier, so they re-pin
    # via heartbeat_model_ref (mini on chatgpt_oauth) instead of being cleared
    # with the compaction slots. Subagent spawns re-pin to the new standard
    # tier so children never inherit a stale ref (or Friday's premium pin);
    # llm-task follows cheap_model (enable-only when None). All four mirror
    # what build_openclaw_config_dict writes at provision time.
    heartbeat_model = heartbeat_model_ref(new_provider, None)
    utility_model = heartbeat_model
    subagent_model = default_model["primary"]
    # EVERY provider path pins the keyless default. This used to leave
    # chatgpt_oauth unpinned so Codex-native hosted search could engage, but
    # that assumed those owners run OpenClaw's Codex runtime — we never set
    # `agents.defaults.embeddedHarness.runtime`, so they run the EMBEDDED
    # runtime, which has no hosted search. Passing None here did not "let
    # Codex take over", it DELETED a working pin and left the owner with no
    # web search at all. Mirrors build_openclaw_config_dict.
    web_search_provider = "parallel-free"

    # The coding-harness set follows the provider (bedrock → claude,
    # chatgpt_oauth → opencode), so the swap must replace the acp block and
    # re-seed the acpx entry — the patcher's plugins.entries rebuild would
    # otherwise drop acpx entirely and leave the old provider's ACP policy.
    harnesses = derive_enabled_harnesses(new_provider, None)
    if harnesses:
        acp_block, acpx_entry = build_acp_config(harnesses)
    else:
        # Defensive: no hot-path provider derives empty today. Disable
        # rather than leave the old policy advertising unauthable spawns.
        acp_block, acpx_entry = {"enabled": False}, None

    try:
        await replace_provider_block(
            owner_id,
            providers=providers,
            default_model=default_model,
            plugin_entries=plugin_entries,
            ceo_model=ceo_model,
            cheap_model=cheap_model,
            heartbeat_model=heartbeat_model,
            utility_model=utility_model,
            subagent_model=subagent_model,
            llm_task_model=cheap_model,
            web_search_provider=web_search_provider,
            # Provision-time never writes toolSearch for chatgpt_oauth (the
            # hot path's only target) — delete any stale embedded-runtime key.
            tool_search_mode=None,
            acp_block=acp_block,
            acpx_entry=acpx_entry,
        )
    except ConfigPatchError as e:
        logger.info(
            "change_provider hot path: openclaw.json missing for owner=%s (%s) — falling back to cold path",
            owner_id,
            e,
        )
        raise _HotSwapUnavailableError("config_missing") from e
