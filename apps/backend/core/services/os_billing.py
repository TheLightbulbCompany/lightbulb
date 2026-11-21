"""Spark's $20/month Stripe subscription, keyed on the person (their users row), never a workspace.

The Mac app opens two links in the default browser, which has no Clerk session for our API, so each is a
short-lived signed link to the backend (`/os/checkout?t=`, `/os/portal?t=`) that makes the Stripe call when
opened. A token is an HS256 JWT (`sub`, `kind`, `exp`) on PAPERCLIP_SERVICE_TOKEN_KEY, the key the emailed
unsubscribe links use; `kind` keeps it from opening anything else. The users row carries
`os_stripe_customer_id`, `os_subscription_id` and `os_subscription_status` (Stripe's status string), written by
the Spark branch of the Stripe webhook."""

import logging
import time

import jwt as pyjwt
import stripe
from botocore.exceptions import ClientError

from core.config import settings
from core.dynamodb import get_table, run_in_thread
from core.services.billing_service import _checkout_idempotency_key, _checkout_trial_end

logger = logging.getLogger(__name__)

CHECKOUT, PORTAL = "spark_checkout", "spark_portal"
TOKEN_TTL_S = 15 * 60
LIVE = ("active", "trialing")
# Stripe still bills these: a new checkout would be a second subscription, so the card is fixed in the portal.
BILLED = (*LIVE, "past_due", "unpaid")


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
    if not settings.PAPERCLIP_SERVICE_TOKEN_KEY:
        return None
    claims = {"sub": user_id, "kind": kind, "exp": now + TOKEN_TTL_S}
    token = pyjwt.encode(claims, settings.PAPERCLIP_SERVICE_TOKEN_KEY, algorithm="HS256")
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
    if not (settings.PAPERCLIP_SERVICE_TOKEN_KEY and token):
        return None
    try:
        claims = pyjwt.decode(
            token,
            settings.PAPERCLIP_SERVICE_TOKEN_KEY,
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


async def checkout(user_id: str) -> str:
    """Where the checkout link sends the browser: a Stripe Checkout Session, or a billing page state."""
    row = await _row(user_id)
    if not (settings.STRIPE_SPARK_PRICE_ID and row.get("os_trial_ends_at")):
        return page("expired")
    if row.get("os_subscription_status") in BILLED:
        return page("already")
    customer = await _customer(user_id, row)
    now = _now()
    # The workspace rule: the Stripe trial ends where the free trial does, stretched to Stripe's 48-hour floor
    # near the end (never charged early); an ended trial is charged now. The bucket start keeps retries identical.
    trial_end = _checkout_trial_end(
        {"subscription_status": "trial_local", "trial_end": int(row["os_trial_ends_at"])}, now - now % 300
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
        idempotency_key=_checkout_idempotency_key("spark", user_id, str(trial_end)),
        api_key=settings.STRIPE_SECRET_KEY,
    )
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
    Stripe's current state (re-read), never the event's snapshot, and an old subscription never replaces a
    live one. A person with no Spark account gets no row."""
    sub = event["data"]["object"]
    user_id = sub["metadata"].get("user_id")
    if not isinstance(user_id, str) or not user_id.startswith("user_"):
        return None
    current = await run_in_thread(stripe.Subscription.retrieve, sub["id"], api_key=settings.STRIPE_SECRET_KEY)
    customer = current.get("customer")
    try:
        await run_in_thread(
            _users().update_item,
            Key={"user_id": user_id},
            UpdateExpression="SET os_subscription_id = :id, os_subscription_status = :st, os_stripe_customer_id = :c",
            ConditionExpression="attribute_exists(os_trial_ends_at) AND (attribute_not_exists(os_subscription_id)"
            " OR os_subscription_id = :id OR NOT os_subscription_status IN (:active, :trialing))",
            ExpressionAttributeValues={
                ":id": current["id"],
                ":st": current["status"],
                ":c": customer if isinstance(customer, str) else customer["id"],
                ":active": LIVE[0],
                ":trialing": LIVE[1],
            },
        )
    except ClientError as e:
        if e.response["Error"]["Code"] != "ConditionalCheckFailedException":
            raise
        logger.info("spark subscription %s not written for %s", current["id"], user_id)
    return None


async def cancel_for_deleted_user(user_id: str) -> None:
    """Cancel a deleted person's Spark subscription now, so it stops charging. Raises on failure, so the
    Clerk event is redelivered."""
    row = await _row(user_id)
    if row.get("os_subscription_status") not in BILLED or not row.get("os_subscription_id"):
        return
    try:
        await run_in_thread(stripe.Subscription.cancel, row["os_subscription_id"], api_key=settings.STRIPE_SECRET_KEY)
    except stripe.InvalidRequestError:  # already canceled (a redelivery after the cancel went through)
        logger.info("spark subscription %s already gone for %s", row["os_subscription_id"], user_id)
