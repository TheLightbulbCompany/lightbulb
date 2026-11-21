"""Spark's $20/month Stripe subscription, keyed on the person (their users row), never a workspace.

The Mac app opens two links in the default browser, which has no Clerk session for our API, so each is a
short-lived signed link to the backend (`/os/checkout?t=`, `/os/portal?t=`) that makes the Stripe call when
opened. A token is an HS256 JWT (`sub`, `kind`, `exp`) on the service-token key, the key the emailed
unsubscribe links use; `kind` keeps it from opening anything else. The users row carries
`os_stripe_customer_id`, `os_subscription_id` and `os_subscription_status` (Stripe's status string), written by
the Spark branch of the Stripe webhook, and `os_checkout_session_id` (the last Checkout Session issued)."""

import logging
import time

import jwt as pyjwt
import stripe
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.observability.metrics import put_metric
from core.repositories import user_repo
from core.services.billing_service import (
    BillingServiceError,
    _checkout_idempotency_key,
    _checkout_trial_end,
    _expire_open_checkout_session,
    _retrieve_checkout_session,
)
from core.services.service_token import _signing_key

logger = logging.getLogger(__name__)

CHECKOUT, PORTAL = "spark_checkout", "spark_portal"
TOKEN_TTL_S = 15 * 60
LIVE = ("active", "trialing")
# Stripe still holds these open: a new checkout would be a second subscription, so they use the portal.
# `incomplete` is not here: its first payment never went through and Stripe expires it within a day.
BILLED = (*LIVE, "past_due", "unpaid", "paused")
CHECKOUT_TTL_S = 1800  # Stripe's minimum Checkout Session lifetime


def _key() -> str:
    try:
        return _signing_key()
    except RuntimeError:  # unset: no links
        return ""


def _now() -> int:
    return int(time.time())


def _users():
    return get_table("users")


def status(row: dict, now: int) -> str:
    """`active` with a live subscription, else `trial` until `os_trial_ends_at`, else `ended`."""
    if row.get("os_subscription_status") in LIVE:
        return "active"
    return "trial" if now < int(row["os_trial_ends_at"]) else "ended"


def _link(kind: str, user_id: str, now: int) -> str | None:
    if not (key := _key()):
        return None
    token = pyjwt.encode({"sub": user_id, "kind": kind, "exp": now + TOKEN_TTL_S}, key, algorithm="HS256")
    return f"{settings.PUBLIC_API_BASE.rstrip('/')}/api/v1/os/{kind.removeprefix('spark_')}?t={token}"


def links(row: dict, user_id: str, now: int) -> tuple[str | None, str | None]:
    """(checkoutUrl, portalUrl) for the account read. Makes no Stripe call."""
    can_check_out = settings.STRIPE_SPARK_PRICE_ID and row.get("os_subscription_status") not in BILLED
    has_portal = row.get("os_stripe_customer_id") and row.get("os_subscription_id")
    return (
        _link(CHECKOUT, user_id, now) if can_check_out else None,
        _link(PORTAL, user_id, now) if has_portal else None,
    )


def verify(token: str, kind: str) -> str | None:
    """The user id a valid, unexpired token of this kind names. PyJWT compares the HMAC with compare_digest."""
    if not ((key := _key()) and token):
        return None
    try:
        claims = pyjwt.decode(
            token,
            key,
            algorithms=["HS256"],
            options={"verify_exp": False, "require": ["sub", "kind", "exp"]},  # expiry is checked on _now below
        )
    except pyjwt.InvalidTokenError:
        return None
    sub, exp = claims["sub"], claims["exp"]
    ok = claims["kind"] == kind and isinstance(sub, str) and sub.startswith("user_") and isinstance(exp, int)
    return sub if ok and _now() < exp else None


def page(state: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/os/billing?state={state}"


async def _row(user_id: str) -> dict:
    return (await run_in_thread(_users().get_item, Key={"user_id": user_id}, ConsistentRead=True)).get("Item") or {}


async def _customer(user_id: str, row: dict) -> str:
    """The person's Stripe customer, created and stored once. Two concurrent first opens may each create one;
    the conditional write keeps the first and the loser's customer is left unused in Stripe."""
    if row.get("os_stripe_customer_id"):
        return row["os_stripe_customer_id"]
    kwargs = {"email": row["email"]} if row.get("email") else {}
    customer = await run_in_thread(
        stripe.Customer.create, metadata={"user_id": user_id}, api_key=settings.STRIPE_SECRET_KEY, **kwargs
    )
    try:
        await run_in_thread(
            _users().update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET os_stripe_customer_id = :c",
            ConditionExpression="attribute_not_exists(os_stripe_customer_id)",
            ExpressionAttributeValues={":c": customer["id"]},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        return (await _row(user_id))["os_stripe_customer_id"]
    return customer["id"]


async def _retire_pending(session_id: str | None) -> str | None:
    """Make sure the person's earlier Checkout Session can no longer be paid: expire it when still open. The
    billing page to answer instead of a new session, or None to go on."""
    if not session_id:
        return None
    pending = await run_in_thread(_retrieve_checkout_session, session_id)
    if pending is not None and pending.get("status") == "open":
        try:
            await run_in_thread(_expire_open_checkout_session, session_id)
            return None
        except BillingServiceError:  # no longer open: it just expired, or was paid in another tab
            pending = await run_in_thread(_retrieve_checkout_session, session_id)
    if pending is None or pending.get("status") == "expired":
        return None
    if pending.get("status") == "complete":
        sub = pending.get("subscription")
        return page("already") if not isinstance(sub, dict) or sub.get("status") in BILLED else None
    return page("expired")  # still open after a failed expire: try again


async def checkout(user_id: str) -> str:
    """Where the checkout link sends the browser: a Stripe Checkout Session, or a billing page state."""
    row = await _row(user_id)
    if not (settings.STRIPE_SPARK_PRICE_ID and row.get("os_trial_ends_at")):
        return page("expired")
    if row.get("os_subscription_status") in BILLED:
        return page("already")
    customer = await _customer(user_id, row)
    # The row can lag the webhook: Stripe is the truth for whether one is already open.
    subs = await run_in_thread(
        stripe.Subscription.list, customer=customer, status="all", limit=100, api_key=settings.STRIPE_SECRET_KEY
    )
    if any(s["status"] in BILLED for s in subs["data"]):
        return page("already")
    pending_id = row.get("os_checkout_session_id")
    if answer := await _retire_pending(pending_id):
        return answer
    now = _now()
    bucket = now - now % 300
    # The workspace rule: the Stripe trial ends where the free trial does, stretched to Stripe's 48-hour floor
    # near the end (never charged early); an ended trial is charged now. The bucket start keeps retries identical.
    trial_end = _checkout_trial_end(
        {"subscription_status": "trial_local", "trial_end": int(row["os_trial_ends_at"])}, bucket
    )
    metadata = {"product": "spark", "user_id": user_id}
    session = await run_in_thread(
        stripe.checkout.Session.create,
        mode="subscription",
        customer=customer,
        line_items=[{"price": settings.STRIPE_SPARK_PRICE_ID, "quantity": 1}],
        allow_promotion_codes=False,
        client_reference_id=user_id,
        subscription_data={"metadata": metadata} | ({"trial_end": trial_end} if trial_end else {}),
        success_url=page("success"),
        cancel_url=page("cancelled"),
        expires_at=bucket + 300 + CHECKOUT_TTL_S,  # 30 to 35 minutes out, identical within the bucket
        # The earlier session is in the key: after expiring it, a retry in this bucket must get a new one.
        idempotency_key=_checkout_idempotency_key("spark", user_id, f"{trial_end}:{pending_id}"),
        api_key=settings.STRIPE_SECRET_KEY,
    )
    try:
        await run_in_thread(
            _users().update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET os_checkout_session_id = :new",
            ConditionExpression=(
                "os_checkout_session_id = :new OR "
                + ("os_checkout_session_id = :prev" if pending_id else "attribute_not_exists(os_checkout_session_id)")
            ),
            ExpressionAttributeValues={":new": session["id"]} | ({":prev": pending_id} if pending_id else {}),
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        # Another open issued a different session meanwhile: this one is never tracked, so it must not stay payable.
        await _retire_pending(session["id"])
        return page("expired")
    return session["url"]


async def portal(user_id: str) -> str:
    customer = (await _row(user_id)).get("os_stripe_customer_id")
    if not customer:
        return page("expired")
    session = await run_in_thread(
        stripe.billing_portal.Session.create,
        customer=customer,
        return_url=page("portal"),
        api_key=settings.STRIPE_SECRET_KEY,
    )
    return session["url"]


def stripe_handler(event: dict):
    """The Spark handler for a Spark subscription event, else None (the event goes to the existing handlers)."""
    obj = event["data"]["object"]
    spark = event["type"].startswith("customer.subscription.") and (obj.get("metadata") or {}).get("product") == "spark"
    return sync_subscription if spark else None


async def sync_subscription(event: dict, _billing_service=None) -> None:
    """Mirror a Spark subscription onto the person's users row. Events arrive out of order, so it writes
    Stripe's current state (re-read), never the event's snapshot, and another subscription never replaces
    one Stripe still holds open (BILLED). A second open one is a duplicate checkout: it is cancelled. A person
    with no Spark account (deleted) gets no row, and an open subscription of theirs is cancelled. A trialing
    subscription whose Stripe trial ends before `os_trial_ends_at` (a referral landed during checkout) is
    moved to it."""
    sub = event["data"]["object"]
    user_id = sub["metadata"].get("user_id")
    if not isinstance(user_id, str) or not user_id.startswith("user_"):
        return None
    try:
        current = await run_in_thread(stripe.Subscription.retrieve, sub["id"], api_key=settings.STRIPE_SECRET_KEY)
    except stripe.InvalidRequestError:  # gone at Stripe: retrying would only fail again on the shared endpoint
        logger.warning("spark subscription %s not found for %s", sub["id"], user_id)
        return None
    customer = current.get("customer")
    try:
        await run_in_thread(
            _users().update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET os_subscription_id = :id, os_subscription_status = :st, os_stripe_customer_id = :c",
            ConditionExpression="attribute_exists(os_trial_ends_at) AND (attribute_not_exists(os_subscription_id)"
            " OR os_subscription_id = :id OR NOT os_subscription_status IN (:b0, :b1, :b2, :b3, :b4))",
            ExpressionAttributeValues={
                ":id": current["id"],
                ":st": current["status"],
                ":c": customer if isinstance(customer, str) else customer["id"],
            }
            | {f":b{i}": s for i, s in enumerate(BILLED)},
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        row = await _row(user_id)
        stored = row.get("os_subscription_id")
        if current["status"] in BILLED and not row.get("os_trial_ends_at"):
            try:
                await run_in_thread(stripe.Subscription.cancel, current["id"], api_key=settings.STRIPE_SECRET_KEY)
            except stripe.InvalidRequestError:  # already canceled
                pass
            put_metric("os_billing.orphan_subscription_cancelled")
            logger.warning("spark subscription %s cancelled: no Spark account for %s", current["id"], user_id)
        elif current["status"] in BILLED and stored and stored != current["id"]:
            await run_in_thread(stripe.Subscription.cancel, current["id"], api_key=settings.STRIPE_SECRET_KEY)
            put_metric("os_billing.duplicate_subscription_cancelled")
            logger.warning("spark duplicate subscription %s cancelled for %s (kept %s)", current["id"], user_id, stored)
        else:
            logger.info("spark subscription %s not written for %s", current["id"], user_id)
    else:
        if current["status"] == "trialing":
            await extend_trial(user_id)  # never raises
    return None


async def extend_trial(user_id: str) -> None:
    """After a referral moves a person's `os_trial_ends_at`, move a trialing subscription's Stripe trial with
    it, so they are not charged before their longer trial ends. A paying (`active`) subscriber keeps the day on
    `os_trial_ends_at` only, where it has no effect. Never raises: the referral stands either way."""
    try:
        row = await _row(user_id)
        if row.get("os_subscription_status") != "trialing" or not row.get("os_subscription_id"):
            return
        sub_id, new_end = row["os_subscription_id"], int(row["os_trial_ends_at"])
        current = await run_in_thread(stripe.Subscription.retrieve, sub_id, api_key=settings.STRIPE_SECRET_KEY)
        # Only ever later (the checkout trial may already sit past the free trial, at Stripe's 48-hour floor).
        if current["status"] == "trialing" and new_end > max(int(current.get("trial_end") or 0), _now()):
            await run_in_thread(
                stripe.Subscription.modify,
                sub_id,
                trial_end=new_end,
                proration_behavior="none",
                api_key=settings.STRIPE_SECRET_KEY,
            )
    except Exception:  # noqa: BLE001  the metric is the repair signal
        put_metric("os_billing.trial_extend_failed")
        logger.warning("spark trial extend failed for %s", user_id, exc_info=True)


async def cancel_for_deleted_user(user_id: str) -> None:
    """Cancel a person's Spark subscription now, so it stops charging, before their users row (which holds
    the subscription id) is deleted, and expire an open Checkout Session of theirs. Raises on failure, so the
    caller keeps the row and can retry."""
    row = await user_repo.get(user_id) or {}
    # An issued Checkout Session could still become a subscription for the deleted account: expire it first.
    if await _retire_pending(row.get("os_checkout_session_id")) == page("expired"):
        raise RuntimeError(f"spark checkout session {row['os_checkout_session_id']} still open")
    if row.get("os_subscription_status") not in BILLED or not row.get("os_subscription_id"):
        return
    try:
        await run_in_thread(stripe.Subscription.cancel, row["os_subscription_id"], api_key=settings.STRIPE_SECRET_KEY)
    except stripe.InvalidRequestError:  # already canceled (a redelivery after the cancel went through)
        logger.info("spark subscription %s already gone for %s", row["os_subscription_id"], user_id)
