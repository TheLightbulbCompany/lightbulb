"""Clerk webhook router.

Receives lifecycle events from Clerk (user.*) and keeps internal state in sync.

Verification uses the svix HMAC-SHA256 signature scheme:
  https://docs.svix.com/receiving/verifying-payloads/how

The signature is the ONLY authentication on this endpoint, so verification
fails CLOSED in every deployed env: a missing or malformed
CLERK_WEBHOOK_SECRET returns 503 rather than waving traffic through. The
check is skipped only when ENVIRONMENT is unset (bare local dev), where no
Clerk instance signs anything. Any deployed stage sets ENVIRONMENT, so the
fail-closed path is the only one that runs off a laptop.

**Dispatch**:

  * ``user.created``  -> the users row only (a person is provisioned when they open the product)
  * ``user.updated``  -> the Stripe customer's email follows
  * ``user.deleted``  -> ``account_wipe.wipe_account`` of the person's own tenant
  * ``organization*`` -> acknowledged and ignored: the tenant is a person, never a Clerk org
"""

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any, Optional

import stripe
from fastapi import APIRouter, HTTPException, Request

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo

logger = logging.getLogger(__name__)

# How far a svix-timestamp may sit from now before the payload is treated as
# a replay. svix's own recommendation; applied in both directions so ordinary
# clock skew between Clerk and the task doesn't reject live traffic.
_SVIX_TOLERANCE_S = 300

router = APIRouter()


def _verify_svix_signature(body: bytes, headers: dict) -> None:
    """Raise HTTPException if the svix signature is missing or invalid.

    Svix signs the payload with HMAC-SHA256 using a timestamp + body
    combination.  The ``svix-signature`` header contains one or more
    ``v1,<base64>`` tokens; we accept the payload if ANY token matches.

    **Fails CLOSED in any deployed env.** This endpoint is unauthenticated by
    design (Clerk has no bearer token to present), so the signature IS the
    authentication. A misconfigured secret previously fell through to
    "accept everything", which let anyone who knew the URL forge events —
    a forged ``user.deleted`` wipes that person's tenant and memory data. Unverifiable mail is now rejected instead.

    The skip survives only for local dev (ENVIRONMENT unset), where there is
    no Clerk instance to sign anything.

    Misconfiguration returns **503, not 400**: svix retries 5xx with backoff,
    so events queued during a bad-secret window are redelivered once the
    secret is fixed rather than being silently dropped.
    """
    secret = settings.CLERK_WEBHOOK_SECRET
    deployed = bool(settings.ENVIRONMENT.strip())

    if not secret:
        if deployed:
            put_metric("webhook.clerk.sig_unconfigured")
            logger.error("CLERK_WEBHOOK_SECRET is not set in a deployed env; rejecting Clerk webhook")
            raise HTTPException(status_code=503, detail="Webhook signature verification not configured")
        return

    # Strip the ``whsec_`` prefix that Clerk/svix adds to the secret.
    raw_secret = secret.removeprefix("whsec_")
    try:
        key = base64.b64decode(raw_secret)
    except Exception:
        if deployed:
            put_metric("webhook.clerk.sig_unconfigured")
            logger.error("CLERK_WEBHOOK_SECRET is not valid base64; rejecting Clerk webhook")
            raise HTTPException(status_code=503, detail="Webhook signature secret is malformed")
        logger.warning("CLERK_WEBHOOK_SECRET is not valid base64; skipping signature check")
        return

    msg_id = headers.get("svix-id", "")
    msg_timestamp = headers.get("svix-timestamp", "")
    msg_signature = headers.get("svix-signature", "")

    if not msg_id or not msg_timestamp or not msg_signature:
        put_metric("webhook.clerk.sig_fail")
        raise HTTPException(status_code=400, detail="Missing svix signature headers")

    # Reject payloads outside the replay window. The timestamp is part of the
    # signed content, so it can't be edited without breaking the HMAC — but
    # without a freshness check a captured valid (payload, signature) pair
    # stays replayable forever, and replaying a `user.deleted` is exactly the
    # destructive action the signature is here to prevent. svix recommends a
    # ~5 minute tolerance; allow it in both directions to absorb clock skew.
    try:
        age = abs(time.time() - int(msg_timestamp))
    except ValueError:
        put_metric("webhook.clerk.sig_fail")
        raise HTTPException(status_code=400, detail="Malformed svix timestamp")
    if age > _SVIX_TOLERANCE_S:
        put_metric("webhook.clerk.sig_stale")
        raise HTTPException(status_code=400, detail="svix timestamp outside tolerance")

    signed_content = f"{msg_id}.{msg_timestamp}.".encode() + body
    expected = hmac.new(key, signed_content, hashlib.sha256).digest()
    expected_b64 = base64.b64encode(expected).decode()

    # svix-signature may contain multiple space-separated tokens.
    tokens = msg_signature.split(" ")
    for token in tokens:
        if token.startswith("v1,"):
            candidate = token[3:]
            if hmac.compare_digest(expected_b64.encode("utf-8"), candidate.encode("utf-8")):
                return

    put_metric("webhook.clerk.sig_fail")
    raise HTTPException(status_code=400, detail="Invalid svix signature")


def _extract_primary_email(data: dict) -> Optional[str]:
    """Pull a Clerk user's primary email from a ``user.*`` payload.

    Clerk's ``user.created`` / ``user.updated`` payload shape:

        {
          "data": {
            "id": "user_xxx",
            "email_addresses": [
              {"id": "idn_xxx", "email_address": "u@example.com", ...},
              ...
            ],
            "primary_email_address_id": "idn_xxx",
            ...
          }
        }

    We resolve the entry whose ``id`` matches ``primary_email_address_id``;
    if that lookup fails (older payload shape, or Clerk omitted the
    pointer) we fall back to the first email in the array. Returns
    ``None`` if no usable email is present.
    """
    primary_id = data.get("primary_email_address_id")
    addresses = data.get("email_addresses") or []
    if primary_id:
        for entry in addresses:
            if isinstance(entry, dict) and entry.get("id") == primary_id:
                addr = entry.get("email_address")
                if addr:
                    return addr
    # Fallback: first email.
    for entry in addresses:
        if isinstance(entry, dict):
            addr = entry.get("email_address")
            if addr:
                return addr
    return None


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@router.post(
    "/clerk",
    summary="Handle Clerk webhooks",
    description=(
        "Processes Clerk lifecycle events: user.created, user.updated, user.deleted. "
        "organization* events are acknowledged and ignored."
    ),
    operation_id="handle_clerk_webhook",
    include_in_schema=False,
)
async def handle_clerk_webhook(request: Request):
    """Handle Clerk webhook events. No Clerk JWT auth — uses svix signature."""
    body = await request.body()
    _verify_svix_signature(body, dict(request.headers))

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("type", "")
    if event_type.startswith("organization"):
        return {"status": "ignored"}
    data: dict[str, Any] = payload.get("data", {})
    svix_id = request.headers.get("svix-id")
    lifecycle_event = event_type == "user.deleted"
    lifecycle_id = svix_id or f"local-{hashlib.sha256(body).hexdigest()}"
    processing_claim = None

    # Idempotency dedupe — svix retries on any non-2xx and redelivers on its own
    # at-least-once insurance. Keyed on svix-id, which is unique per genuine Clerk
    # event and identical across retries.
    if svix_id:
        if lifecycle_event:
            from core.services.webhook_dedup import (
                WebhookProcessingInProgress,
                begin_event_processing,
            )

            try:
                processing_claim = await begin_event_processing(svix_id, source="clerk")
            except WebhookProcessingInProgress as exc:
                raise HTTPException(status_code=503, detail="Clerk lifecycle event is already processing") from exc
            if processing_claim is None:
                put_metric("clerk.webhook.dedup_skipped")
                logger.info("completed clerk lifecycle webhook duplicate suppressed: %s", svix_id)
                return {"status": "duplicate"}
        else:
            try:
                from core.services.webhook_dedup import (
                    WebhookDedupResult,
                    record_event_or_skip,
                )

                dedup = await record_event_or_skip(svix_id, source="clerk")
                if dedup is WebhookDedupResult.ALREADY_SEEN:
                    put_metric("clerk.webhook.dedup_skipped")
                    logger.info("clerk webhook duplicate suppressed: %s", svix_id)
                    return {"status": "duplicate"}
            except Exception:
                # Non-security events retain the historical best-effort
                # behavior; lifecycle claims above deliberately fail closed.
                logger.exception("clerk webhook dedupe check failed; processing anyway")

    put_metric("webhook.clerk.received", dimensions={"event_type": event_type})

    if event_type == "user.created":
        user_id = data.get("id", "")
        logger.info("Clerk user.created webhook received for %s", user_id)

        # Persist user_id + primary email into the users table. The /users/sync
        # path also calls user_repo.put but only knows the user_id; the webhook
        # is the authoritative source of email.
        if user_id:
            email = _extract_primary_email(data)
            try:
                from core.repositories import user_repo

                await user_repo.put(user_id, email=email)
            except Exception:
                # Persistence is non-fatal here — the /users/sync REST
                # path will write the row again the first time the user
                # opens the app. Logging is enough.
                logger.exception("user_repo.put failed for user.created %s", user_id)

            # No welcome here: it comes from the workspace's CEO once a workspace exists
            # (core/services/welcome_email.py). Sign-up joins nothing either -- competition
            # sign-up-sheet membership is explicit (POST /launch/join, inside the product).

    elif event_type == "user.updated":
        user_id = data.get("id", "")
        logger.info("Clerk user.updated webhook received for %s", user_id)

        # Sync the primary email to the user's Stripe Customer if one exists.
        # Catches receipt / invoice / trial-end emails going to a stale address.
        new_email = _extract_primary_email(data)
        if new_email and user_id:
            account = await billing_repo.get_by_owner_id(user_id)
            if account and account.get("stripe_customer_id"):
                # Use the Clerk webhook's unique svix-id as the Stripe idempotency
                # key. Each genuine Clerk event gets a unique id; a retry of the
                # SAME event reuses it. Embedding user_id+email instead would let
                # an A→B→A→B email flip within Stripe's 24h idempotency window
                # collide with the first A→B and silently skip the modify.
                svix_id = request.headers.get("svix-id")
                if svix_id:
                    idempotency_key = f"customer_email_sync:{svix_id}"
                else:
                    # Defensive fallback (shouldn't happen with real Clerk
                    # traffic — _verify_svix_signature already requires it
                    # when CLERK_WEBHOOK_SECRET is set). 1-min bucket bounds
                    # the worst-case skipped writes.
                    idempotency_key = f"customer_email_sync:{user_id}:{new_email}:{int(time.time() // 60)}"
                try:
                    stripe.Customer.modify(
                        account["stripe_customer_id"],
                        email=new_email,
                        idempotency_key=idempotency_key,
                    )
                    put_metric("stripe.customer.email_sync", dimensions={"result": "ok"})
                except stripe.StripeError as e:
                    put_metric("stripe.customer.email_sync", dimensions={"result": "error"})
                    logger.warning(
                        "Stripe email sync failed for %s: %s",
                        user_id,
                        e,
                    )
                    # Non-fatal — Clerk update succeeded.

    elif event_type == "user.deleted":
        user_id = data.get("id", "")
        if not user_id:
            raise HTTPException(status_code=400, detail="Clerk user deletion target unavailable")

        # The person's own tenant goes for good: runtime, profiles, memory, credentials, rooms, billing,
        # their memory data and their Spark subscription (account_wipe). Every step is idempotent and a no-op for a
        # person who never had a tenant; a failed step fails the event, so the redelivery retries the wipe.
        from core.services.account_wipe import MalformedUserIdError, wipe_account

        try:
            report = await wipe_account(user_id, actor_admin_id="clerk:user.deleted", clerk_user_deleted=True)
        except MalformedUserIdError as exc:  # not a Clerk user id: a redelivery cannot fix it
            put_metric("clerk.webhook.user_deleted_wipe_failed")
            raise HTTPException(status_code=400, detail="Clerk user deletion target is not a user id") from exc
        except Exception:  # before any step ran (a read failed): the redelivery retries it
            put_metric("clerk.webhook.user_deleted_wipe_failed")
            raise
        failed = [s.system for s in report.steps if s.status == "failed"]
        if failed:
            put_metric("clerk.webhook.user_deleted_wipe_failed")
            raise HTTPException(status_code=503, detail=f"user teardown incomplete: {', '.join(failed)}")

    else:
        logger.debug("Clerk webhook: unhandled event type %s", event_type)

    if processing_claim is not None:
        from core.services.webhook_dedup import complete_event_processing

        await complete_event_processing(lifecycle_id, processing_claim)
    return {"status": "ok"}
