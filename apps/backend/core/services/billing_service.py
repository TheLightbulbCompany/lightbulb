"""Service for Stripe billing operations — flat-fee model."""

import hashlib
import logging
import time
from typing import Literal

import stripe
from botocore.exceptions import ClientError

from core.config import settings
from core.observability.metrics import timing
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

ProviderChoice = Literal["chatgpt_oauth", "byo_key", "openrouter_managed"]
ByoProvider = Literal["openai", "anthropic"]


stripe.api_key = settings.STRIPE_SECRET_KEY

# Bump whenever the shared Checkout body changes; Stripe idempotency compares
# the complete request body for a reused key.
_CHECKOUT_BODY_VERSION = 9

# Stripe refuses a trial_end less than 48 hours out; an hour of margin for the bucket.
_STRIPE_MIN_TRIAL_S = 49 * 3600
# Founding teams' price lock (owner ruling 2026-10-13: two years).
_PRICE_LOCK_DAYS = 730


class BillingServiceError(Exception):
    pass


class BillingService:
    async def create_customer_for_owner(self, owner_id: str, email: str | None = None) -> dict:
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
                metadata={"workspace_id": owner_id, "owner_id": owner_id},
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


#: Where a web Checkout started for ChatGPT returns (`then`): ChatGPT sign-in in Settings → Model, or the onboarding
#: wizard's ChatGPT step (ProvisioningStepper keeps ?provider=). Fixed paths: `then` is an allowlisted Literal.
_CHECKOUT_RETURNS = {
    "chatgpt": "/settings?panel=billing&action=connect-chatgpt&checkout=success",
    "chatgpt_onboarding": "/chat?provider=chatgpt_oauth&checkout=success",
    "managed_onboarding": "/chat?provider=openrouter_managed&checkout=success",
}


async def create_workspace_checkout(
    *,
    owner_id: str,
    clerk_user_id: str,
    plan: Literal["workspace"],
    return_to: Literal["web", "app"] = "web",
    confirm: bool = False,
    front_desk: bool = False,
    then: Literal["chatgpt", "chatgpt_onboarding", "managed_onboarding"] | None = None,
) -> dict:
    """Create or change the single recurring subscription for a workspace.

    ``front_desk`` adds the texting add-on as a third item, charged now.

    Changing the plan of a LIVE subscription needs ``confirm=True``. Without it
    nothing is changed and the result is ``confirm_required`` plus what the
    change will cost, so no client can move a paid plan in one unconfirmed call.
    """
    checkout_bucket = int(time.time() // 300)
    checkout_expires_at = (checkout_bucket * 300) + 36 * 60
    # One plan (T06): Base and Ultra are retired and never sold again.
    if plan != "workspace":
        raise BillingServiceError("plan_retired")
    price_id = settings.STRIPE_WORKSPACE_PRICE_ID
    if not price_id:
        raise BillingServiceError("STRIPE_WORKSPACE_PRICE_ID not configured")
    if front_desk and not settings.STRIPE_FRONT_DESK_PRICE_ID:
        raise BillingServiceError("front_desk_price_unconfigured")

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
        if front_desk:
            # The in-place plan change below would silently drop the add-on.
            raise BillingServiceError("already_subscribed")
        sub_id = subscription.get("id") or sub_id
        items = list(subscription["items"]["data"])
        if len(items) != 1:
            raise BillingServiceError("This subscription's plan can't be changed automatically")
        item = items[0]
        if (item.get("price") or {}).get("id") == price_id:
            return {"updated": False}
        if not confirm:
            # Nothing is charged at the swap (create_prorations below): the
            # prorated difference lands on the next invoice. The client states
            # that and resends with confirm=True.
            return {"updated": False, "confirm_required": True}
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

    # Pending-session lifecycle: reuse, supersede or mint the workspace's one
    # pending Checkout session. One claim serializes every read and write of
    # the marker, and the marker is re-read strongly consistent UNDER it, so no
    # request acts on a marker another is replacing. The claim is released on
    # every exit; holding it past a recorded marker would 409 an owner who
    # cancels Checkout and picks another plan within the TTL.
    #
    # Stripe's session `status` is the state machine: open = "payment
    # processing has not started", complete = "payment processing may still be
    # in progress", expired = "no further processing will occur". We get here
    # only when the customer has NO live subscription.
    # - open, ours, same plan  -> return its URL (double click / second tab).
    # - open, ours, other plan -> expire it, then mint: the newest explicit plan
    #   choice wins, since two open sessions could complete into two charged
    #   subscriptions. `expire` only succeeds on an open session, so one
    #   already submitted in another tab is never cancelled; we 409 instead.
    #   `return_to` (web/app) is a second reuse key alongside plan: a session
    #   minted for one is never handed back for the other, and a mismatch
    #   takes this same expire-then-mint path.
    # - open, not ours         -> fail closed; not ours to expire.
    # - complete, its subscription live -> it completed after the list above,
    #   or is an ACH debit still settling: never supersede it.
    # - complete, subscription gone (canceled / incomplete_expired), or
    #   expired -> mint fresh; the new marker retires the stale id. A complete
    #   marker used to raise forever, since nothing clears the field.
    claim_token = await billing_repo.try_claim_checkout_slot(owner_id)
    if not claim_token:
        raise BillingServiceError("checkout_in_progress")
    try:
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
        pending_id = account.get("pending_checkout_session_id")
        pending = _retrieve_checkout_session(pending_id) if pending_id else None
        if pending is not None and pending.get("status") == "complete":
            if (pending.get("subscription") or {}).get("status") in LIVE_SUBSCRIPTION_STATUSES:
                raise BillingServiceError("already_subscribed:checkout_completed")
            logger.info("Superseding completed checkout %s with no live subscription owner=%s", pending_id, owner_id)
        elif pending is not None and pending.get("status") == "open":
            metadata = pending.get("metadata") or {}
            if not (
                metadata.get("purpose") == "workspace_subscription_v2"
                and metadata.get("workspace_id") == owner_id
                and metadata.get("owner_id") in {None, owner_id}
                and pending.get("customer") == account["stripe_customer_id"]
                and pending.get("mode") == "subscription"
            ):
                # A corrupt or foreign marker is not ours to disclose or
                # expire. Failing closed avoids damaging another workspace.
                raise BillingServiceError("pending_checkout_invalid")
            if (
                metadata.get("plan") == plan
                and (metadata.get("return_to") or "web") == return_to
                # A plain-Team session is never handed to a front-desk click, nor the reverse.
                and (metadata.get("front_desk") or "0") == str(int(front_desk))
                # Provider setup is part of the authenticated user's checkout intent:
                # don't return a managed selection through a pending ChatGPT URL.
                and (metadata.get("then") or None) == then
            ):
                return {"checkout_url": pending["url"]}
            _expire_open_checkout_session(pending_id)

        metadata = {
            "workspace_id": owner_id,
            "owner_id": owner_id,
            "clerk_user_id": clerk_user_id,
            "plan": plan,
            "purpose": "workspace_subscription_v2",
            "return_to": return_to,
            "front_desk": str(int(front_desk)),
        }
        if then:
            metadata["then"] = then
        bucket_start = checkout_bucket * 300
        # The add-on costs $250 (FRONT_DESK_PRICE_USD) the moment it exists: no Stripe trial with it.
        trial_end = None if front_desk else _checkout_trial_end(account, bucket_start)
        if account.get("founding_team"):
            metadata["price_lock_until"] = price_lock_until(bucket_start)
        if return_to == "app":
            # Stripe's app-to-web flow: the app opens this in Safari; these URLs are
            # claimed by the iOS app's universal link (apple-app-site-association
            # /checkout_redirect/*) and fall back to the web page of the same path.
            urls = {
                "success_url": f"{settings.FRONTEND_URL}/checkout_redirect/success",
                "cancel_url": f"{settings.FRONTEND_URL}/checkout_redirect/cancel",
                "origin_context": "mobile_app",
            }
        else:
            urls = {
                "success_url": f"{settings.FRONTEND_URL}{_CHECKOUT_RETURNS.get(then or '', '/settings?panel=billing&checkout=success')}",
                "cancel_url": f"{settings.FRONTEND_URL}/settings?panel=billing&checkout=cancel",
            }
        line_items = [{"price": price_id, "quantity": 1}]
        if front_desk:
            line_items.append({"price": settings.STRIPE_FRONT_DESK_PRICE_ID, "quantity": 1})
        session = await _build_subscription_checkout(
            owner_id=owner_id,
            line_items=line_items,
            metadata=metadata,
            trial_end=trial_end,
            # No typed codes: internal coupons are applied by a founder in the Dashboard.
            allow_promotion_codes=False,
            **urls,
            idempotency_key=(
                f"workspace_checkout:v{_CHECKOUT_BODY_VERSION}:{owner_id}:{plan}:{clerk_user_id}:{checkout_bucket}"
                # A superseded session keys a new bucket: otherwise
                # Workspace -> Ultra -> Workspace inside one bucket replays the
                # first (now expired) session.
                f":{pending_id or ''}"
                # The trial end is part of the body.
                f":{trial_end or ''}"
                # Web and app sessions differ in body (return URLs, origin_context);
                # sharing a key would make Stripe reject the second as a mismatch.
                f":{return_to}"
                # The add-on changes the body (a third item, no trial).
                f":fd{int(front_desk)}"
                # A ChatGPT return changes the success URL; empty keeps every existing key unchanged.
                f"{':' + then if then else ''}"
            ),
            expires_at=checkout_expires_at,
        )
        try:
            await billing_repo.set_pending_checkout(owner_id, session_id=session["id"])
        except Exception:
            logger.exception("Failed to record pending workspace checkout for owner %s", owner_id)
            try:
                with timing("stripe.api.latency", {"op": "checkout.session.expire"}):
                    stripe.checkout.Session.expire(session["id"])
            except Exception:
                logger.exception("Failed to expire unrecorded workspace checkout %s", session["id"])
            raise BillingServiceError("checkout_unavailable")
        return {"checkout_url": session["url"]}
    finally:
        try:
            await billing_repo.release_checkout_slot(owner_id, token=claim_token)
        except Exception:
            logger.exception("Failed to release workspace checkout slot for owner %s", owner_id)


def _checkout_trial_end(account: dict, now_s: int) -> int | None:
    """Card now, first charge on the workspace's own day 31 (T05).

    A ``trial_local`` workspace checks out into a Stripe trial ending where its
    free trial does, so adding the card early never shortens the free month. An
    unarmed trial (workspace not ready yet) gets the full 30 days from now. Near
    the end the trial is stretched to Stripe's 48-hour minimum rather than
    charging early. An expired trial, or any other row, is billed at once.
    ``now_s`` is the checkout bucket start, so retries send an identical body.
    """
    if account.get("subscription_status") != "trial_local":
        return None
    end = account.get("trial_end")
    end = int(end) if end is not None else now_s + billing_repo._TRIAL_LOCAL_DURATION_SECONDS
    if end <= now_s:
        return None
    return max(end, now_s + _STRIPE_MIN_TRIAL_S)


def price_lock_until(now_s: int) -> str:
    """The founding-team price lock date (ISO date), two years from ``now_s``."""
    import datetime as dt

    return (dt.datetime.fromtimestamp(now_s, dt.UTC) + dt.timedelta(days=_PRICE_LOCK_DAYS)).date().isoformat()


async def mark_founding_team(owner_id: str) -> dict:
    """Admin: mark a workspace as a founding team (two-year price lock).

    Its next Checkout carries ``price_lock_until`` in subscription metadata; a
    subscription that is already live is stamped now. An existing lock is kept,
    never moved. Any future price change must skip a subscription whose lock
    has not passed.
    """
    account = await billing_repo.get_by_owner_id(owner_id)
    if not account:
        return {"status": "no_billing_account"}
    await billing_repo.set_founding_team(owner_id)
    sub_id = account.get("stripe_subscription_id")
    if not sub_id:
        return {"status": "marked"}
    with timing("stripe.api.latency", {"op": "subscription.retrieve"}):
        subscription = stripe.Subscription.retrieve(sub_id)
    lock = (subscription.get("metadata") or {}).get("price_lock_until")
    if not lock:
        lock = price_lock_until(int(time.time()))
        with timing("stripe.api.latency", {"op": "subscription.modify"}):
            stripe.Subscription.modify(sub_id, metadata={"price_lock_until": lock})
    return {"status": "marked", "subscription_id": sub_id, "price_lock_until": lock}


def _retrieve_checkout_session(session_id: str):
    """The Checkout session with its subscription expanded, or None when Stripe has no such session."""
    try:
        with timing("stripe.api.latency", {"op": "checkout.session.retrieve"}):
            return stripe.checkout.Session.retrieve(session_id, expand=["subscription"])
    except stripe.error.InvalidRequestError:
        return None


def _expire_open_checkout_session(session_id: str) -> None:
    """Expire an open Checkout session; ``checkout_in_progress`` when it is no longer open."""
    try:
        with timing("stripe.api.latency", {"op": "checkout.session.expire"}):
            stripe.checkout.Session.expire(session_id)
    except stripe.error.StripeError as e:
        # No longer open: submitted in another tab, or it just expired.
        # Re-evaluate on retry rather than guess.
        raise BillingServiceError("checkout_in_progress") from e


async def retire_pending_checkout_for_apple(owner_id: str, account: dict | None) -> None:
    """Before an App Store purchase starts or is linked, make sure no Stripe Checkout can
    complete into a SECOND live subscription for the workspace.

    Takes the same checkout slot as :func:`create_workspace_checkout`, so a session
    cannot be minted or reused underneath this. With the claim held, the workspace's
    pending session is resolved against Stripe:

    - open -> expired. ``checkout_in_progress`` if Stripe refuses (it was submitted
      in the meantime), so the caller retries against the settled state.
    - complete, subscription live -> ``stripe_managed``: Stripe already bills it.
    - complete with a dead subscription, expired, or unknown to Stripe -> nothing to do.

    Raises BillingServiceError("checkout_in_progress" | "stripe_managed").
    """
    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    if not (account or {}).get("pending_checkout_session_id"):
        return
    claim_token = await billing_repo.try_claim_checkout_slot(owner_id)
    if not claim_token:
        raise BillingServiceError("checkout_in_progress")
    try:
        account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
        pending_id = (account or {}).get("pending_checkout_session_id")
        pending = _retrieve_checkout_session(pending_id) if pending_id else None
        if pending is None:
            return
        if pending.get("status") == "open":
            _expire_open_checkout_session(pending_id)
            logger.info("Expired open checkout %s for an App Store purchase owner=%s", pending_id, owner_id)
        elif (
            pending.get("status") == "complete"
            and (pending.get("subscription") or {}).get("status") in LIVE_SUBSCRIPTION_STATUSES
        ):
            raise BillingServiceError("stripe_managed")
    finally:
        try:
            await billing_repo.release_checkout_slot(owner_id, token=claim_token)
        except Exception:
            logger.exception("Failed to release workspace checkout slot for owner %s", owner_id)


def subscription_item_on_price(items: list[dict], price_id: str) -> dict:
    """The subscription line item carrying ``price_id``, or ``{}`` if none does.

    Empty ``price_id`` yields ``{}`` so an unconfigured price can never match
    an item by accident.
    """
    if not price_id:
        return {}
    return next((item for item in items if (item.get("price") or {}).get("id") == price_id), {})


def _is_front_desk_item(item: dict) -> bool:
    price_id = settings.STRIPE_FRONT_DESK_PRICE_ID
    return bool(price_id) and (item.get("price") or {}).get("id") == price_id


async def add_front_desk_item(owner_id: str) -> str:
    """Add the Front desk item to a live Stripe subscription, prorated.

    Returns ``added | already | not_live | front_desk_price_unconfigured``.
    Removal is Stripe's (billing portal / dashboard); the webhook releases the line.
    """
    from core.services.apple_iap import stripe_is_live

    price = settings.STRIPE_FRONT_DESK_PRICE_ID
    if not price:
        return "front_desk_price_unconfigured"
    account = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    sub_id = (account or {}).get("stripe_subscription_id")
    if not sub_id or not stripe_is_live(account) or account.get("subscription_source") == "apple_iap":
        return "not_live"
    with timing("stripe.api.latency", {"op": "subscription.retrieve"}):
        subscription = stripe.Subscription.retrieve(sub_id)
    status = subscription.get("status")
    if status not in ("active", "trialing"):
        return "not_live"
    if subscription_item_on_price(subscription["items"]["data"], price):
        return "already"
    key = f"front-desk-item:v1:{sub_id}"
    if status == "trialing":
        # The add-on carries no trial: the paid plan starts now, invoiced now.
        with timing("stripe.api.latency", {"op": "subscription.modify"}):
            stripe.Subscription.modify(
                sub_id,
                items=[{"price": price, "quantity": 1}],
                trial_end="now",
                proration_behavior="always_invoice",
                idempotency_key=key,
            )
        return "added"
    with timing("stripe.api.latency", {"op": "subscription_item.create"}):
        stripe.SubscriptionItem.create(
            subscription=sub_id,
            price=price,
            quantity=1,
            proration_behavior="always_invoice",
            idempotency_key=key,
        )
    return "added"


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
    expires_at: int | None = None,
    allow_promotion_codes: bool = True,
    origin_context: str | None = None,
    trial_end: int | None = None,
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
    if trial_end is not None:
        subscription_data["trial_end"] = trial_end

    kwargs: dict = dict(
        customer=account["stripe_customer_id"],
        mode="subscription",
        line_items=line_items,
        success_url=success_url,
        cancel_url=cancel_url,
        allow_promotion_codes=allow_promotion_codes,
        payment_method_collection="always",
        # The SAME dict on BOTH: subscription_data.metadata lands on the created
        # Subscription, which customer.subscription.* carries
        # (handle_subscription_state); metadata stays on the Session itself.
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
    return await _set_pause_collection(user_id, pause=True)


async def resume_subscription_for_owner(user_id: str) -> dict:
    """Admin: undo ``pause_subscription_for_owner`` (clear pause_collection)."""
    return await _set_pause_collection(user_id, pause=False)


async def _set_pause_collection(user_id: str, *, pause: bool) -> dict:
    """Reads Stripe first so a pause of a paused subscription (or a resume of a
    running one) reports the no-op instead of "paused". No Stripe idempotency key:
    setting pause_collection is already idempotent, and a fixed key would replay a
    pause → resume → pause cycle's first response inside Stripe's 24 h window."""
    billing = await billing_repo.get_by_owner_id(user_id)
    if not billing or not billing.get("stripe_subscription_id"):
        return {"status": "no_subscription"}
    sub_id = billing["stripe_subscription_id"]
    verb = "pause" if pause else "resume"
    try:
        paused = bool(stripe.Subscription.retrieve(sub_id).get("pause_collection"))
        if paused == pause:
            return {"status": "already_paused" if pause else "not_paused", "subscription_id": sub_id}
        stripe.Subscription.modify(sub_id, pause_collection={"behavior": "mark_uncollectible"} if pause else "")
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_{verb}_failed: {e}")
    return {"status": "paused" if pause else "resumed", "subscription_id": sub_id}


async def issue_credit_for_owner(user_id: str, *, amount_cents: int, reason: str, request_id: str) -> dict:
    """Admin: credit the user's Stripe customer balance by amount_cents.

    Stripe applies negative balance amounts as a credit toward future invoices.
    """
    billing = await billing_repo.get_by_owner_id(user_id)
    if not billing or not billing.get("stripe_customer_id"):
        return {"status": "no_customer"}
    customer_id = billing["stripe_customer_id"]
    # ``request_id`` is one admin SUBMISSION: a double click or a retry of it
    # reuses the id and collapses to one Stripe write, while a second deliberate
    # credit of the same amount and reason carries a new id and is a new credit.
    # Without it the key was (amount, reason) alone, and Stripe merged two
    # identical credits issued within its 24 h idempotency window.
    reason_hash = hashlib.sha256(f"{amount_cents}:{reason}".encode("utf-8")).hexdigest()[:16]
    key = f"balance_tx:{customer_id}:{reason_hash}:{request_id}"
    try:
        txn = stripe.Customer.create_balance_transaction(
            customer_id,
            amount=-abs(amount_cents),
            currency="usd",
            description=reason,
            idempotency_key=key,
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_credit_failed: {e}")
    # Stripe marks a response it served from an earlier request with the same key.
    headers = getattr(getattr(txn, "last_response", None), "headers", None) or {}
    replayed = any(k.lower() == "idempotent-replayed" and str(v).lower() == "true" for k, v in headers.items())
    return {
        "status": "duplicate" if replayed else "credited",
        "transaction_id": txn["id"],
        "amount_cents": amount_cents,
    }


async def mark_invoice_resolved(user_id: str, invoice_id: str) -> dict:
    """Admin: mark a Stripe invoice as paid out-of-band (e.g. wire transfer).

    The invoice must belong to this owner's Stripe customer: an invoice id is
    typed by hand, and paying another customer's invoice would also write an
    audit row naming the wrong workspace. Every refusal returns before any write.
    """
    billing = await billing_repo.get_by_owner_id(user_id)
    customer_id = (billing or {}).get("stripe_customer_id")
    if not customer_id:
        return {"status": "no_customer"}
    try:
        current = stripe.Invoice.retrieve(invoice_id)
    except Exception as e:  # noqa: BLE001
        if _is_stripe_missing(e):
            return {"status": "invoice_not_found"}
        raise BillingServiceError(f"stripe_invoice_retrieve_failed: {e}")
    if current.get("customer") != customer_id:
        return {"status": "invoice_customer_mismatch"}
    if current.get("status") == "paid":
        return {"status": "already_paid"}
    try:
        invoice = stripe.Invoice.pay(
            invoice_id,
            paid_out_of_band=True,
            idempotency_key=f"invoice_pay:{invoice_id}",
        )
    except Exception as e:  # noqa: BLE001
        raise BillingServiceError(f"stripe_invoice_pay_failed: {e}")
    return {"status": "resolved", "invoice_id": invoice_id, "stripe_status": invoice.get("status")}
