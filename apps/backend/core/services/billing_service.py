"""Service for Stripe billing operations — flat-fee model."""

import asyncio
import hashlib
import logging
import time
from typing import Literal

import stripe
from botocore.exceptions import ClientError

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

# Bump whenever the shared Checkout body changes; Stripe idempotency compares
# the complete request body for a reused key.
_CHECKOUT_BODY_VERSION = 6


class BillingServiceError(Exception):
    pass


class BillingService:
    async def create_customer_for_owner(
        self, owner_id: str, owner_type: str = "personal", email: str | None = None
    ) -> dict:
        """Resolve a Stripe customer for one workspace billing row only.

        A human can administer multiple workspaces, so their email is display
        data on a newly-created customer, never a cross-workspace deduplication
        key. Reuse is permitted only when this workspace row already stores a
        Stripe customer id.
        """
        existing = await billing_repo.get_by_owner_id(owner_id)
        if existing and existing.get("stripe_customer_id"):
            return existing

        with timing("stripe.api.latency", {"op": "customers.create"}):
            customer = stripe.Customer.create(
                metadata={"workspace_id": owner_id, "owner_id": owner_id, "owner_type": owner_type},
                idempotency_key=f"workspace-customer:v2:{owner_id}",
            )
        customer_id = customer.id

        if existing:
            # Backfill path (1b above) — the row already exists (trial_local
            # today), so persist onto it instead of racing a fresh
            # create_if_not_exists, which would raise AlreadyExistsError
            # against a row that's already there.
            try:
                await billing_repo.set_stripe_customer_id(owner_id, stripe_customer_id=customer_id)
            except ClientError as e:
                if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
                    raise
                winner = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
                if not winner or not winner.get("stripe_customer_id"):
                    raise BillingServiceError("customer_attach_retry") from e
                if winner["stripe_customer_id"] != customer_id:
                    with timing("stripe.api.latency", {"op": "customers.delete"}):
                        stripe.Customer.delete(customer_id, idempotency_key=f"delete_customer:{customer_id}")
                return winner
            existing["stripe_customer_id"] = customer_id
            return existing

        try:
            return await billing_repo.create_if_not_exists(
                owner_id=owner_id,
                stripe_customer_id=customer_id,
                owner_type=owner_type,
            )
        except billing_repo.AlreadyExistsError:
            # Lost the DDB race for this workspace. Delete the Stripe customer
            # created by this request and return the workspace winner.
            winner = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
            if winner and winner.get("stripe_customer_id") and winner["stripe_customer_id"] != customer_id:
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
                except Exception:
                    logger.warning("Failed to delete orphan Stripe customer %s", customer_id)
            if not winner or not winner.get("stripe_customer_id"):
                raise BillingServiceError("customer_attach_retry")
            return winner

    async def create_portal_session(self, billing_account: dict, *, return_path: str | None = None) -> str:
        """Stripe customer portal URL for this account.

        ``return_path`` overrides where Stripe sends the customer when they
        finish. The default is the web settings page, which is right for a
        browser and wrong for the app: a mobile subscriber who cancels would be
        left standing on isol8.co in Safari rather than back where they started.
        The app passes a universal-link path so iOS hands control back to it.

        It is a PATH chosen from a fixed set by the CALLER, never a URL supplied
        by the client — a client-controlled return_url on a billing endpoint is
        an open redirect wearing a Stripe badge.
        """
        owner_id = billing_account["owner_id"]
        # 5-minute time bucket — same retry-collapsing intent as checkout. The
        # path is part of the key: two callers wanting different destinations
        # inside one bucket must not be handed the same cached session.
        bucket = int(time.time() // 300)
        path = return_path or "/settings/billing"
        with timing("stripe.api.latency", {"op": "billing_portal.session.create"}):
            session = stripe.billing_portal.Session.create(
                customer=billing_account["stripe_customer_id"],
                return_url=f"{settings.FRONTEND_URL}{path}",
                idempotency_key=f"portal:{owner_id}:{path}:{bucket}",
            )
        return session.url

    async def cancel_subscription(self, billing_account: dict) -> None:
        await billing_repo.set_subscription(
            owner_id=billing_account["owner_id"],
            subscription_id=None,
            status="canceled",
            trial_end=None,
        )


async def create_workspace_checkout(*, owner_id: str, clerk_user_id: str, plan: Literal["workspace", "ultra"]) -> dict:
    """Create or change the single recurring subscription for a workspace."""
    checkout_bucket = int(time.time() // 300)
    checkout_expires_at = (checkout_bucket * 300) + 36 * 60
    price_id = settings.STRIPE_WORKSPACE_PRICE_ID if plan == "workspace" else settings.STRIPE_ULTRA_PRICE_ID
    if not price_id:
        raise BillingServiceError(f"STRIPE_{plan.upper()}_PRICE_ID not configured")

    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    account = await billing_repo.get_by_owner_id(owner_id)
    if not account or not account.get("stripe_customer_id"):
        raise BillingServiceError(f"No Stripe customer for owner_id={owner_id}")

    def validate_owned_subscription(candidate: dict, *, allow_legacy_metadata: bool = False) -> None:
        """Reject Stripe objects that cannot be proven to belong to this workspace."""
        if candidate.get("customer") != account["stripe_customer_id"]:
            raise BillingServiceError("Subscription has a different customer")
        metadata = candidate.get("metadata") or {}
        if metadata.get("owner_id") and metadata["owner_id"] != owner_id:
            raise BillingServiceError("Subscription belongs to a different workspace")
        if metadata.get("workspace_id") != owner_id and not (
            allow_legacy_metadata and metadata.get("workspace_id") is None
        ):
            raise BillingServiceError("Subscription has no matching workspace metadata")

    subscription = None
    sub_id = account.get("stripe_subscription_id")
    if sub_id:
        try:
            with timing("stripe.api.latency", {"op": "subscription.retrieve"}):
                candidate = stripe.Subscription.retrieve(sub_id)
        except stripe.error.InvalidRequestError as e:
            if getattr(e, "code", None) != "resource_missing":
                raise
        else:
            if candidate.get("status") in LIVE_SUBSCRIPTION_STATUSES:
                # A persisted row is the historical binding for pre-v2
                # subscriptions. It may lack workspace_id, but must never
                # contradict the workspace/customer recorded locally.
                validate_owned_subscription(candidate, allow_legacy_metadata=True)
                subscription = candidate

    # The local id is only a candidate. Stripe's complete customer list is the
    # authority for the one-live-subscription invariant; otherwise a stale row
    # could mask a second live subscription and let us modify the wrong one.
    with timing("stripe.api.latency", {"op": "subscription.list"}):
        subscriptions = stripe.Subscription.list(customer=account["stripe_customer_id"], status="all", limit=100)
    live = [
        candidate
        for candidate in subscriptions.auto_paging_iter()
        if candidate.get("status") in LIVE_SUBSCRIPTION_STATUSES
    ]
    if live:
        if len(live) != 1:
            raise BillingServiceError("Multiple live subscriptions require support")
        listed = live[0]
        validate_owned_subscription(
            listed,
            # Only a stored legacy binding may omit workspace_id. A list-only
            # result with missing canonical metadata is never safe to adopt.
            allow_legacy_metadata=bool(subscription and listed.get("id") == subscription.get("id")),
        )
        subscription = listed
    elif subscription is not None:
        raise BillingServiceError("Stored subscription was not found in the customer subscription list")

    if subscription is not None:
        sub_id = subscription.get("id") or sub_id
        items = subscription["items"]["data"]
        if len(items) != 1:
            raise BillingServiceError("This subscription's plan can't be changed automatically")
        item = items[0]
        if (item.get("price") or {}).get("id") == price_id:
            return {"updated": False}
        try:
            revision = str(int((subscription.get("metadata") or {}).get("workspace_plan_revision", "0")) + 1)
        except ValueError as e:
            raise BillingServiceError("Subscription has an invalid checkout revision") from e
        transition = hashlib.sha256(
            "|".join((owner_id, subscription["id"], item["id"], item["price"]["id"], price_id, revision)).encode()
        ).hexdigest()
        with timing("stripe.api.latency", {"op": "subscription.modify"}):
            stripe.Subscription.modify(
                subscription["id"],
                items=[{"id": item["id"], "price": price_id}],
                proration_behavior="create_prorations",
                # Canonical billing metadata is intentionally the only partial
                # update. It neither copies arbitrary Stripe-side fields nor
                # includes the initiating admin, so same-transition retries
                # retain an identical body/key. Empty strings delete obsolete
                # legacy provider/credit metadata in Stripe.
                metadata={
                    "workspace_id": owner_id,
                    "owner_id": owner_id,
                    "plan": plan,
                    "purpose": "workspace_subscription_v2",
                    "workspace_plan_revision": revision,
                    "provider_choice": "",
                    "byo_provider": "",
                    "credits_cents": "",
                },
                idempotency_key=f"workspace-plan-change:v2:{transition}",
            )
        return {"updated": True}

    pending_id = account.get("pending_checkout_session_id")
    if pending_id:
        try:
            with timing("stripe.api.latency", {"op": "checkout.session.retrieve"}):
                pending = stripe.checkout.Session.retrieve(pending_id)
        except stripe.error.InvalidRequestError:
            pending = None
        if pending is not None:
            if pending.get("status") == "complete":
                raise BillingServiceError("already_subscribed:checkout_completed")
            if pending.get("status") == "open":
                metadata = pending.get("metadata") or {}
                if (
                    metadata.get("purpose") == "workspace_subscription_v2"
                    and metadata.get("plan") == plan
                    and metadata.get("workspace_id") == owner_id
                    and metadata.get("owner_id") in {None, owner_id}
                    and pending.get("customer") == account["stripe_customer_id"]
                    and pending.get("mode") == "subscription"
                ):
                    return {"checkout_url": pending["url"]}
                # A corrupt or foreign marker is not ours to disclose or
                # expire. Failing closed avoids damaging another workspace.
                raise BillingServiceError("pending_checkout_invalid")

    claim_token = await billing_repo.try_claim_checkout_slot(owner_id)
    if not claim_token:
        raise BillingServiceError("checkout_in_progress")

    metadata = {
        "workspace_id": owner_id,
        "owner_id": owner_id,
        "clerk_user_id": clerk_user_id,
        "plan": plan,
        "purpose": "workspace_subscription_v2",
    }
    try:
        session = await _build_subscription_checkout(
            owner_id=owner_id,
            line_items=[{"price": price_id, "quantity": 1}],
            metadata=metadata,
            success_url=f"{settings.FRONTEND_URL}/settings?panel=billing&checkout=success",
            cancel_url=f"{settings.FRONTEND_URL}/settings?panel=billing&checkout=cancel",
            idempotency_key=(
                f"workspace_checkout:v{_CHECKOUT_BODY_VERSION}:{owner_id}:{plan}:{clerk_user_id}:{checkout_bucket}"
            ),
            expires_at=checkout_expires_at,
        )
    except Exception:
        await billing_repo.release_checkout_slot(owner_id, token=claim_token)
        raise
    try:
        await billing_repo.set_pending_checkout(owner_id, session_id=session["id"], provider_choice=None)
    except Exception:
        logger.exception("Failed to record pending workspace checkout for owner %s", owner_id)
        try:
            with timing("stripe.api.latency", {"op": "checkout.session.expire"}):
                stripe.checkout.Session.expire(session["id"])
        except Exception:
            logger.exception("Failed to expire unrecorded workspace checkout %s", session["id"])
        try:
            await billing_repo.release_checkout_slot(owner_id, token=claim_token)
        except Exception:
            logger.exception("Failed to release workspace checkout slot for owner %s", owner_id)
        raise BillingServiceError("checkout_unavailable")
    return {"checkout_url": session["url"]}


def subscription_item_on_price(items: list[dict], price_id: str) -> dict:
    """The subscription line item carrying ``price_id``, or ``{}`` if none does.

    Empty ``price_id`` yields ``{}`` so an unconfigured price can never match
    an item by accident.
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

    The launch winner discount is NOT part of ``extra``: it is read inside
    ``_build_subscription_checkout`` (the callers don't know it), which
    suffixes the key it is handed. Same reason as ``_BODY_VERSION`` below —
    a different body needs a different key.

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
    expires_at: int | None = None,
    allow_promotion_codes: bool = True,
    origin_context: str | None = None,
) -> stripe.checkout.Session:
    """Single builder for subscription-mode Stripe Checkout sessions.

    Owns the subscription metadata assembly. Workspace subscriptions use a
    single recurring item and expose manually-entered promotion codes. Stripe
    Tax is intentionally omitted until the operational readiness checklist is
    completed and finance approves enabling it.
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
        payment_method_collection="always",
        # The SAME dict on BOTH, because Stripe puts them on different objects
        # and two different webhooks read them:
        #   subscription_data.metadata → the created Subscription, which is
        #     what customer.subscription.* carries (handle_subscription_state).
        #   metadata                   → the Session itself, which is what
        #     checkout.session.completed carries (handle_checkout_session).
        # Keep Checkout and Subscription metadata aligned: Stripe emits them
        # on different webhook objects, and launch-coupon settlement reads the
        # session while subscription lifecycle reads the subscription.
        metadata=metadata,
        subscription_data=subscription_data,
        idempotency_key=idempotency_key,
        # Stripe's minimum is 30 minutes; the default is 24 hours. A short
        # expiry bounds how long an abandoned session can linger completable
        # — load-bearing for the single-open-session guard in /trial-checkout.
        # This matches _checkout_idempotency_key's five-minute bucket. Six
        # buckets plus one minute gives Stripe at least 31 minutes.
        expires_at=expires_at if expires_at is not None else (int(time.time() // 300) * 300) + 36 * 60,
    )
    if origin_context:
        # "mobile_app" switches Checkout to the UI built for app-to-web
        # purchases (Stripe: Accept payments for digital goods on iOS).
        # Omitted entirely for web callers so their session shape — and
        # therefore their idempotency body-compare — is untouched.
        kwargs["origin_context"] = origin_context
    with timing("stripe.api.latency", {"op": "checkout.session.create"}):
        session = stripe.checkout.Session.create(**kwargs)
    return session


async def _open_pending_checkout(owner_id: str) -> stripe.checkout.Session | None:
    """The owner's still-open MOBILE Checkout Session, or None if there is none.

    Reads the ``pending_checkout_session_id`` marker and asks Stripe what it
    actually is, because the marker alone cannot say: it is written at create
    time and nothing clears it when a session expires or completes. The
    session's LIVE status is the truth, so a stale marker self-heals into None.

    **The marker is shared by every checkout flow** — the flat browser checkout,
    the Ultra checkout and this one all write it — so "an open session exists"
    is a different question from "an open session for what this caller sells".
    Reusing on status alone handed the mobile caller whichever session happened
    to be open: an owner who abandoned an Ultra session ($150) and then tapped
    the mobile paywall's flat option ($50) would be sent back to the Ultra page
    and charged for line items the app never quoted. A session for anything else
    is expired and replaced, the way the web route already treats a provider or
    credit-amount switch.

    Raises ``already_subscribed:checkout_completed`` when the pending session is
    already paid. The subscription exists at Stripe but the webhook has not
    landed, so every local guard still reads "not subscribed" — minting another
    session here is precisely how an owner ends up charged twice.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    pending_id = (account or {}).get("pending_checkout_session_id")
    if not pending_id:
        return None

    try:
        with timing("stripe.api.latency", {"op": "checkout.session.retrieve"}):
            pending = stripe.checkout.Session.retrieve(pending_id)
    except stripe.error.InvalidRequestError:
        # Unretrievable (wrong mode, deleted, never existed) — dead marker.
        logger.info("Pending checkout session %s not retrievable — minting fresh", pending_id)
        return None

    status = pending.get("status")
    if status == "complete":
        raise BillingServiceError("already_subscribed:checkout_completed")
    if status != "open":
        return None

    # Read the purpose off the SESSION rather than a stored column: one source
    # of truth, no migration, and the same dict the grant webhook reads.
    purpose = (pending.get("metadata") or {}).get("purpose")
    if purpose == "mobile_web_checkout":
        # Launch winner staleness: a session minted before the owner's row
        # was flagged carries no discount (marker absent), and one minted
        # WITH the discount goes stale once the coupon is consumed. Either
        # mismatch means the cached session sells the wrong total — fall
        # through to the expire-and-mint-fresh path below.
        from core.services.launch_close import winner_checkout_discount

        has_marker = (pending.get("metadata") or {}).get("launch_winner_coupon") == "true"
        wants_discount = bool(await winner_checkout_discount(owner_id))
        if has_marker == wants_discount:
            return pending
        logger.info("Pending checkout session %s has a stale winner-discount state — expiring it", pending_id)

    # Someone else's open session. Expire it before minting ours, or both stay
    # completable and the owner can be charged twice.
    logger.info("Pending checkout session %s is for %s, not mobile — expiring it", pending_id, purpose)
    try:
        with timing("stripe.api.latency", {"op": "checkout.session.expire"}):
            stripe.checkout.Session.expire(pending_id)
    except stripe.error.InvalidRequestError:
        # Thin race: it can complete between retrieve and expire (checkout
        # finishing in a browser tab). Minting a fresh session here would leave
        # BOTH chargeable — re-check and stop unless it genuinely expired.
        with timing("stripe.api.latency", {"op": "checkout.session.retrieve"}):
            recheck = stripe.checkout.Session.retrieve(pending_id)
        if recheck.get("status") != "expired":
            raise BillingServiceError("already_subscribed:checkout_completed") from None
    return None


async def create_mobile_web_checkout(*, owner_id: str) -> stripe.checkout.Session:
    """App-to-web Checkout for the iOS paywall's "subscribe on the web" option.

    This is the link-out half of the paywall. It exists because guideline
    3.1.1(a) lets a U.S.-storefront app send customers to an external purchase
    page, and because the alternative — dropping them on
    isol8.co/settings?panel=billing — means signing in and finding a panel
    before they can pay. Stripe's iOS guide is this exact shape: create a
    Session server-side, open its URL in Safari, fulfil on the webhook.

    The client opens it in the DEFAULT BROWSER, never a WKWebView. Apple's
    wording is what binds — U.S.-storefront apps "may link out to the default
    browser ... for payment mechanisms other than in-app purchase" — and an
    embedded checkout is not linking out; it is the in-app storefront 3.1.1
    forbids.

    ``origin_context="mobile_app"`` is what makes the page the app-optimized
    variant rather than the desktop one. Apple Pay rides along automatically on
    supported devices, which is the single biggest conversion lever here.

    Carries the SAME checkout-slot claim, pending-session marker, AND
    single-open-session guard as workspace Checkout. An earlier
    version omitted them on the reasoning that the app is the only caller and
    ``is_trial_blocked`` already refuses a prior subscriber — both true and both
    irrelevant. The idempotency key is bucketed to five minutes while a session
    stays completable for about thirty-one, and ``is_trial_blocked`` reads a
    local row that says nothing until a checkout webhook lands. So an owner
    calling twice across a bucket boundary gets two live sessions, completes
    both, and ends up with two Stripe subscriptions. "The app is the only
    caller" is not a property of an authenticated HTTP endpoint.

    The version after that WROTE the marker but never READ it, which fixed
    nothing: recording an open session prevents a second one only if something
    checks for it. Hence the ``_open_pending_checkout`` call below — the same
    guard the web route runs, minus the intent-comparison, because this
    session's body never varies (one price, no credits, no provider choice), so
    an already-open session is always the right one to hand back.

    Still deliberately unlike the flat path: **no starter-credits line item**.
    Credits are a Bedrock-onboarding concern; this session sells the plan alone.

    The caller is responsible for the entitlement check. Returns the Session so
    the router can hand back ``session.url``.
    """
    if not settings.STRIPE_WORKSPACE_PRICE_ID:
        raise BillingServiceError("STRIPE_WORKSPACE_PRICE_ID not configured")

    existing = await _open_pending_checkout(owner_id)
    if existing is not None:
        return existing

    # Serializes concurrent calls for one owner; self-expires (90s) so a crashed
    # request cannot wedge them out of checking out. The router maps this to 409.
    claim_token = await billing_repo.try_claim_checkout_slot(owner_id)
    if not claim_token:
        raise BillingServiceError("checkout_in_progress")

    try:
        session = await _build_subscription_checkout(
            owner_id=owner_id,
            line_items=[{"price": settings.STRIPE_WORKSPACE_PRICE_ID, "quantity": 1}],
            metadata={"owner_id": owner_id, "purpose": "mobile_web_checkout"},
            # A universal link, so iOS hands control back to the app on success
            # and falls through to the web page when the app is not installed.
            # The matching apple-app-site-association entry is what makes it
            # deep-link rather than merely load.
            success_url=f"{settings.FRONTEND_URL}/checkout_redirect/success",
            cancel_url=f"{settings.FRONTEND_URL}/checkout_redirect/cancel",
            # Off for the same reason the credits-bundled path turns it off: a
            # promo field on a phone keyboard is friction, and nothing here
            # quotes a code.
            allow_promotion_codes=False,
            origin_context="mobile_app",
            idempotency_key=_checkout_idempotency_key("mobile_web_checkout", owner_id, "flat"),
        )
    except Exception:
        # No session exists — free the slot so an immediate retry does not wait
        # out the claim TTL. Fenced on our own token.
        await billing_repo.release_checkout_slot(owner_id, token=claim_token)
        raise

    # Record the open session. Without this, once the 90s claim lapses an
    # unrecorded session stays completable for ~31 minutes and is invisible to
    # every guard, so a retry can mint a second one and both can be completed.
    # Fail CLOSED: the caller has not seen this URL yet, so expire it and let the
    # retry mint fresh.
    try:
        await billing_repo.set_pending_checkout(owner_id, session_id=session.id, provider_choice=None)
    except Exception:
        logger.exception("Failed to record pending mobile checkout for owner %s", owner_id)
        try:
            with timing("stripe.api.latency", {"op": "checkout.session.expire"}):
                stripe.checkout.Session.expire(session.id)
        except Exception:
            logger.exception("Failed to expire orphaned mobile checkout session %s", session.id)
        await billing_repo.release_checkout_slot(owner_id, token=claim_token)
        raise BillingServiceError("could not start checkout — try again")

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
    # is what the bundled lightbulb skill points them at. The hot-swap path never
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
            # The switch turned out task-def-neutral (applied in place, no
            # restart) — clear the marker set_provider_choice stamped before
            # the hot/cold split was decided, or the NEXT wake would take a
            # pointless full provision.
            from core.repositories import container_repo as _container_repo

            await _container_repo.update_fields(owner_id, {"needs_provision": False})
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
            # Restoring the provider the container is ACTUALLY running —
            # the registered task def already matches; marking would cost a
            # pointless full provision on the next wake.
            requires_reprovision=False,
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
        TOOL_SEARCH_MODE,
        build_acp_config,
        build_provider_block,
        cheap_model_ref,
        derive_enabled_harnesses,
        heartbeat_model_ref,
        premium_model_ref,
        model_policy_block,
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
    # The image the owner's container is ACTUALLY running, not the env pin:
    # `modelPolicy` is an 8.1-only key and `agents.defaults` is strict on 7.2,
    # so writing it from the pin during a rollback/promotion-lag window would
    # crash-loop a 7.2 container on its next config reload. Same accessor
    # write_user_configs uses for the same reason.
    from core.containers import get_ecs_manager

    deployed_tag = await get_ecs_manager()._deployed_image_tag(owner_id)
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
            model_policy=model_policy_block(new_provider, default_model, deployed_tag=deployed_tag).get("modelPolicy"),
            cheap_model=cheap_model,
            heartbeat_model=heartbeat_model,
            utility_model=utility_model,
            subagent_model=subagent_model,
            llm_task_model=cheap_model,
            web_search_provider=web_search_provider,
            # Provision-time now writes toolSearch unconditionally, so the swap
            # must RE-PIN it rather than delete it. Passing None here strips the
            # key, which would drop a swapped owner straight back to carrying the
            # full tool catalog — the exact drift the unconditional setting
            # exists to prevent. Shares the provision-time constant rather than
            # repeating the literal, so the swap cannot drift to a different
            # mode than a fresh provision.
            tool_search_mode=TOOL_SEARCH_MODE,
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
