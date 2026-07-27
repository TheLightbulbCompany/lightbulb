"""Clerk webhook router.

Receives lifecycle events from Clerk (user, organization,
organizationMembership) and keeps internal state — including the
Paperclip mirror — in sync.

Verification uses the svix HMAC-SHA256 signature scheme, via the shared
fail-closed verifier in ``core.crypto.svix``:
  https://docs.svix.com/receiving/verifying-payloads/how

If CLERK_WEBHOOK_SECRET is not configured the signature check is skipped
(safe for local dev; every deployed env gets it from Secrets Manager).

**Provisioning triggers.** Two events here are provisioning triggers, not
record-keeping. Both are safe to fire concurrently with each other and with
the ``/container/status`` poll — ``container_repo.try_claim_provision`` is a
DynamoDB conditional write that elects one winner per owner, so extra
triggers no-op rather than double-provision.

  * ``user.created``                   -> trial_local row + container provision
                                          (personal owner; gated on a VERIFIED
                                          primary email)
  * ``organizationMembership.created`` -> paperclip membership add + seat sync
                                          + dual-tenancy probe. NEVER a
                                          container: orgs have no free tier and
                                          become eligible via Stripe, so the org
                                          container trigger lives in
                                          ``stripe_webhook_handlers``.
  * ``organizationMembership.deleted`` -> ``disable`` for that user
  * ``organization.deleted``           -> ``disable`` for every member of the org
  * ``user.deleted``                   -> ``disable`` for that user

``organization.created`` is deliberately NOT handled and should not be
subscribed: Clerk org creation carries no eligibility information at all, and
the org creator is resolved from the Clerk Backend API by
``provision_runner._seed_owner_paperclip``, not from that payload.

The poll remains a non-negotiable fallback for all of it — Svix silently
auto-disables an endpoint after 5 days of sustained failure.

Retry semantics: if provisioning raises an exception with
``retryable=True`` (``OrgNotProvisionedError`` always; ``PaperclipApiError``
on 5xx + 429), we enqueue a row in the ``pending-updates`` table for the
cleanup cron to re-drive. Either way we return 200 to Clerk so the svix
delivery is acked — we own retries from this point onward.

Non-retryable errors (4xx state errors, programmer errors) are logged
and swallowed; returning 5xx would just have Clerk retry the same
permanently-broken event.
"""

import json
import logging
import time
from typing import Any, Optional

import stripe
from fastapi import APIRouter, HTTPException, Request

from core.config import settings
from core.crypto.svix import verify_svix_signature
from core.observability.metrics import put_metric
from core.repositories import billing_repo, channel_link_repo, update_repo
from core.services.paperclip_provisioning import (
    build_paperclip_provisioning,
    close_paperclip_provisioning,
)
from core.services.update_service import PAPERCLIP_RETRY_KIND

logger = logging.getLogger(__name__)

router = APIRouter()


def _verify_svix_signature(body: bytes, headers: dict) -> None:
    """Raise HTTPException(400) if the svix signature is invalid.

    Delegates the HMAC to the shared ``core.crypto.svix`` verifier rather than
    carrying a second copy of the scheme. That module is deliberately
    FAIL-CLOSED: a malformed secret returns False instead of skipping the
    check. The inline verifier this replaced logged and skipped on a bad
    secret, so a typo'd ``whsec_`` silently reopened the endpoint to anyone.

    The one policy this router owns is the unset-secret case: skip, for local
    dev where no secret is provisioned. Every deployed environment gets
    ``CLERK_WEBHOOK_SECRET`` from Secrets Manager (service-stack), so the skip
    is unreachable there — and it logs at WARNING so an env that lost the
    secret is visible rather than quietly unauthenticated.
    """
    secret = settings.CLERK_WEBHOOK_SECRET
    if not secret:
        logger.warning(
            "CLERK_WEBHOOK_SECRET is unset — Clerk webhook signature verification "
            "SKIPPED. Expected only in local dev; any deployed environment is "
            "accepting unsigned webhook bodies."
        )
        return

    if not verify_svix_signature(secret=secret, body=body, headers=headers):
        put_metric("webhook.clerk.sig_fail")
        raise HTTPException(status_code=400, detail="Invalid svix signature")


# The canonical home for ``PAPERCLIP_RETRY_KIND`` is
# ``core.services.update_service.PAPERCLIP_RETRY_KIND``. Both this
# webhook router (which enqueues retries) and the scheduled worker
# (which consumes them) read the same constant — keeping them in
# lockstep is the only way the retry cron actually picks up the
# rows we wrote. The import lives at the top of this file alongside
# the other ``core.*`` imports.
#
# The Paperclip provisioning factory (build_paperclip_provisioning +
# close_paperclip_provisioning) lives in core/services/paperclip_provisioning.py
# — imported at the top of this file. Service-layer ownership keeps the
# routers→services dependency direction one-way.


async def _enqueue_paperclip_retry(*, op: str, payload: dict, owner_id: str) -> None:
    """Enqueue a Paperclip provisioning retry row in pending-updates.

    ``owner_id`` is the partition key of the pending-updates table —
    we use the affected user_id (or org owner) so T13 can group
    retries per-tenant. ``op`` distinguishes which provisioning entry
    point T13 should call back into.

    Failure to enqueue is logged but not re-raised: enqueueing is
    best-effort, and the alternative (5xx-ing the webhook) would just
    have Clerk retry the original event, which has the same retryable
    classification.

    SCOPE — reviewed when event-triggered provisioning landed, KEPT deliberately.
    This queue was originally built to re-drive Paperclip PROVISIONING from
    webhooks and has never executed a row (no Clerk endpoint existed). Its one
    live op is now ``archive_member`` from ``organizationMembership.deleted``,
    and the worker (``update_service.paperclip_provision_retry_pass``) already
    errors on any other op. Folding the retry inline into that handler was
    considered and rejected: Svix requires a 2xx within 15 SECONDS and disables
    an endpoint after 5 days of failures, so an in-handler retry loop is exactly
    the wrong shape, and the durable pending-updates row is what survives a
    backend restart mid-retry. A one-op queue is the cheaper correct answer.
    """
    try:
        await update_repo.create(
            owner_id=owner_id,
            update_type=PAPERCLIP_RETRY_KIND,
            description=f"Retry {op} (Paperclip)",
            changes={"op": op, **payload},
        )
        put_metric("paperclip.webhook.retry_enqueued", dimensions={"op": op})
    except Exception:
        logger.exception(
            "paperclip retry enqueue failed (op=%s, owner_id=%s)",
            op,
            owner_id,
        )


def _is_retryable(exc: BaseException) -> bool:
    """Read the ``retryable`` attribute from a Paperclip exception.

    ``OrgNotProvisionedError`` carries a class-level ``retryable=True``;
    ``PaperclipApiError`` sets ``retryable`` in __init__ from the
    status code (5xx + 429 => True). Anything else is treated as
    non-retryable.
    """
    return bool(getattr(exc, "retryable", False))


# ----------------------------------------------------------------------
# Clerk org event payload helpers
# ----------------------------------------------------------------------
#
# Clerk webhook payload shapes (verified against
# https://clerk.com/docs/webhooks/event-shapes):
#
# organization.created:
#   data.id                         -- org_id (e.g. "org_2abc...")
#   data.created_by                 -- user_id of the org owner
#   data.name, data.slug, data.created_at, ...
#
# organizationMembership.created / deleted:
#   data.id                         -- membership id
#   data.organization.id            -- org_id
#   data.organization.created_by    -- org owner's user_id
#   data.public_user_data.user_id   -- the affected member's user_id
#   data.public_user_data.identifier -- the member's primary email
#   data.role                       -- "org:admin" | "org:member" | ...
#
# organization.deleted:
#   data.id                         -- the deleted org_id
#   (deleted=true, slug present, members NOT enumerated — Clerk fires
#    organizationMembership.deleted separately for each member)
#
# user.deleted:
#   data.id                         -- the deleted user_id
#   data.deleted = True
def _extract_org_member_email(data: dict) -> Optional[str]:
    """Pull the new member's primary email out of an organizationMembership
    payload. Clerk packages the email under ``public_user_data.identifier``
    for both ``.created`` and ``.deleted`` events; older payload shapes used
    a sibling ``email_addresses`` array. We try both for forward/backward
    compatibility.
    """
    pud = data.get("public_user_data") or {}
    identifier = pud.get("identifier")
    if identifier:
        return identifier
    # Fallback for older / non-standard payload shapes.
    addrs = data.get("email_addresses") or pud.get("email_addresses") or []
    for a in addrs:
        if a.get("email_address"):
            return a["email_address"]
    return None


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


def _primary_email_verified(data: dict) -> bool:
    """True iff the PRIMARY email on a ``user.*`` payload is verified.

    Deliberately STRICT — no "first email in the array" fallback, unlike
    ``_extract_primary_email``. A missing ``primary_email_address_id`` means we
    cannot tell which address is primary, and the caller must then treat the
    user as unverified.

    Why this gates the provisioning trigger: ``ensure_personal_trial_default``
    and the provision gate both refuse a personal trial when the user has a
    pending org invitation, and **Clerk matches invitations to VERIFIED emails
    only**. ``user.created`` fires at the earliest possible instant — plausibly
    before verification — so triggering on an unverified primary email would
    hand an about-to-be-invited user a personal container, a personal billing
    row, and a throwaway Paperclip company, all of it instant junk. Skipping is
    free: the ``/container/status`` poll picks the owner up moments later,
    exactly as it does today.
    """
    primary_id = data.get("primary_email_address_id")
    if not primary_id:
        return False
    for entry in data.get("email_addresses") or []:
        if isinstance(entry, dict) and entry.get("id") == primary_id:
            verification = entry.get("verification") or {}
            return verification.get("status") == "verified"
    return False


async def _lookup_owner_email(*, org_id: str, fallback_user_id: Optional[str]) -> Optional[str]:
    """Pull the org owner's email so ``provision_member`` can sign them in.

    Thin delegator to the shared resolver in
    ``core.services.paperclip_owner_email``. The retry worker
    (``update_service``) needs the same behaviour and routers can't
    safely import from services without re-introducing a cycle, so
    the helper lives in a shared module — this in-module name is
    kept so existing call sites and any test ``patch`` strings
    remain stable. See the shared module docstring for the full
    resolution chain.
    """
    from core.services.paperclip_owner_email import lookup_owner_email

    return await lookup_owner_email(org_id=org_id, fallback_user_id=fallback_user_id)


# ----------------------------------------------------------------------
# Per-event handlers
# ----------------------------------------------------------------------


async def _handle_organization_membership_created(data: dict) -> None:
    """Add the new member to the org's Paperclip company, + dual-tenancy probe.

    A member joining an already-subscribed org needs NO container — they ride
    the shared org box. What they need is a Paperclip membership row, or every
    Teams surface 401s on them (the silent orphan-member incident). So this
    handler dispatches ``_ensure_paperclip_for_member`` and **never** a
    container provision: an org's eligibility comes from its Stripe
    subscription, not from anyone joining it.

    ``_ensure_paperclip_for_member`` is join-only by construction — it never
    creates a company — and returns gracefully when the org's company does not
    exist yet, emitting ``paperclip.member_add{outcome=no_company}``. That is
    the CORRECT out-of-order behaviour: Clerk/Svix guarantee no ordering, so a
    membership event can legitimately beat the org's own provisioning. The
    member's own ``/container/status`` poll retries. Do not add a retry queue.
    """
    org = data.get("organization") or {}
    org_id = org.get("id", "")
    owner_user_id = org.get("created_by", "")
    pud = data.get("public_user_data") or {}
    user_id = pud.get("user_id", "") or data.get("user_id", "")
    email = _extract_org_member_email(data)

    if not org_id or not user_id or not email:
        logger.warning(
            "Clerk organizationMembership.created missing fields: org=%s user=%s email=%s",
            org_id,
            user_id,
            bool(email),
        )
        return

    # Defense-in-depth: Gates A+B should have prevented dual-tenancy.
    # If a new org member somehow has an active personal billing row,
    # log loudly and emit a metric — but don't block, because Clerk
    # has already accepted the membership and refusing here would just
    # orphan the org provisioning.
    try:
        personal_account = await billing_repo.get_by_owner_id(user_id)
        if personal_account and personal_account.get("subscription_status") in (
            "active",
            "trialing",
        ):
            logger.error(
                "tenancy_invariant.violated user=%s org=%s personal_status=%s",
                user_id,
                org_id,
                personal_account.get("subscription_status"),
            )
            put_metric(
                "tenancy_invariant.violation",
                dimensions={"path": "membership_created"},
            )
    except Exception:  # noqa: BLE001
        # Never let an observability check break a real provisioning path.
        logger.exception("tenancy invariant probe failed for user=%s org=%s", user_id, org_id)

    logger.info(
        "membership.created: org=%s member=%s owner=%s — dispatching paperclip membership-add",
        org_id,
        user_id,
        owner_user_id,
    )

    # The member trigger. Best-effort: a Paperclip outage must not 5xx the
    # webhook (Svix would retry, and 5 days of failures silently disable the
    # endpoint). The member's /container/status poll re-drives this via
    # ensure_user_ready, which is why no retry queue is needed here.
    try:
        from core.services.user_provision import _ensure_paperclip_for_member

        await _ensure_paperclip_for_member(
            org_id=org_id,
            member_user_id=user_id,
            member_email=email,
        )
    except Exception:
        logger.exception(
            "paperclip membership-add on membership.created failed for org=%s user=%s",
            org_id,
            user_id,
        )

    # Keep the org's per-seat subscription quantity in step with membership.
    # Best-effort: the reconciler self-guards (no-op unless the org has an
    # active team sub with an item id), and any error is swallowed so the
    # webhook still 200s — the /container/status reconcile net heals drift.
    try:
        from core.services.billing_service import sync_team_seat_quantity

        await sync_team_seat_quantity(org_id)
    except Exception:
        logger.exception("seat sync on membership.created failed for org=%s", org_id)


async def _handle_organization_membership_deleted(data: dict) -> None:
    """Archive the member's Paperclip membership AND disable their DDB row.

    Spec §3 case C. Calls ``archive_member`` rather than ``disable``
    because we need the Paperclip-side membership row archived too —
    otherwise the Teams UI keeps showing the removed user. If
    Paperclip's archive call 5xx's, enqueue a retry like other
    Paperclip-touching handlers; non-retryable errors fall through
    to a defensive ``disable`` call so the DDB row is marked
    ``status="disabled"`` even when Paperclip archive 4xx'd —
    otherwise the removed Clerk-org member could keep hitting
    ``/api/v1/teams/*`` because ``resolve_teams_context`` only
    inspects DDB state.
    """
    pud = data.get("public_user_data") or {}
    user_id = pud.get("user_id", "") or data.get("user_id", "")
    org_id = (data.get("organization") or {}).get("id", "")
    if not user_id:
        logger.warning("Clerk organizationMembership.deleted missing user_id: %s", data)
        return

    provisioning = await build_paperclip_provisioning()
    try:
        try:
            await provisioning.archive_member(user_id=user_id)
            put_metric(
                "paperclip.webhook.archive_member",
                dimensions={"trigger": "membership_deleted", "result": "ok"},
            )
        except Exception as e:
            retryable = _is_retryable(e)
            put_metric(
                "paperclip.webhook.archive_member",
                dimensions={
                    "trigger": "membership_deleted",
                    "result": "retryable" if retryable else "error",
                },
            )
            logger.warning(
                "archive_member on membership_deleted failed for user=%s retryable=%s err=%s",
                user_id,
                retryable,
                e,
            )
            if retryable:
                # Retryable archive failure (5xx/429): we'll retry the
                # Paperclip-side archive in the update worker, but the
                # user is already gone from Clerk. Leaving the DDB row
                # ``status="active"`` for the entire retry window is a
                # backend auth bypass because ``resolve_teams_context``
                # only checks DDB status, not Clerk membership — the
                # removed member could keep hitting ``/api/v1/teams/*``
                # for minutes/hours until the retry succeeds. Disable
                # now (idempotent; no-ops if no row exists) and let the
                # retry re-attempt only the Paperclip-side archive.
                try:
                    await provisioning.disable(user_id=user_id)
                    put_metric(
                        "paperclip.webhook.disable",
                        dimensions={
                            "trigger": "membership_deleted",
                            "reason": "archive_member_retryable",
                        },
                    )
                except Exception:
                    logger.exception(
                        "disable fallback before archive_member retry enqueue failed for user=%s",
                        user_id,
                    )
                await _enqueue_paperclip_retry(
                    op="archive_member",
                    payload={"user_id": user_id},
                    owner_id=user_id,
                )
            else:
                # Non-retryable archive failure (4xx/non-429): the
                # Paperclip-side archive call won't succeed on retry,
                # but the user is gone from Clerk — leaving the DDB row
                # ``status="active"`` is a backend auth bypass because
                # ``resolve_teams_context`` only checks DDB status, not
                # Clerk membership. Mark the row disabled (idempotent;
                # no-ops if no row exists) so ``/api/v1/teams/*`` access
                # is revoked even though Paperclip-side archive failed.
                # Operators can clean up the orphan Paperclip member row
                # out-of-band if needed.
                try:
                    await provisioning.disable(user_id=user_id)
                    put_metric(
                        "paperclip.webhook.disable",
                        dimensions={
                            "trigger": "membership_deleted",
                            "reason": "archive_member_non_retryable",
                        },
                    )
                except Exception:
                    logger.exception(
                        "disable fallback after non-retryable archive_member failure failed for user=%s",
                        user_id,
                    )
    finally:
        await close_paperclip_provisioning(provisioning)

    if org_id:
        try:
            from core.services.billing_service import sync_team_seat_quantity

            await sync_team_seat_quantity(org_id)
        except Exception:
            logger.exception("seat sync on membership.deleted failed for org=%s", org_id)


async def _handle_organization_deleted(data: dict) -> None:
    """Cancel the org's Stripe subscription so seat billing stops, then
    disable every Paperclip member row attached to the deleted org.

    The org's per-seat subscription is billed on the org's own
    ``billing_accounts`` row (owner_id == org_id), which nothing else
    tears down when the Clerk org itself is deleted -- left alone it
    would keep charging N seats forever. We cancel it via the same
    Stripe path the admin "cancel subscription" action uses
    (``billing_service.cancel_subscription_for_owner``): cancelling at
    Stripe (not just clearing the DDB row) fires the inbound
    ``customer.subscription.deleted`` webhook, which handles container
    teardown. Best-effort -- a Stripe error must not break this webhook,
    which still has to return 200 to Clerk.

    This runs BEFORE the member-disable sweep below, and in its own
    independent try/except, so that a failure in the sweep (e.g. the
    GSI ``table.query`` pagination call throwing on a DynamoDB
    throttle/error) can never prevent the billing-critical cancel from
    running. The sweep is Paperclip cleanup, not billing-critical --
    it must not be able to block the Stripe cancel.

    Clerk also fires ``organizationMembership.deleted`` for each
    member, but we belt-and-braces against missed events by sweeping
    the org-id GSI. ``disable`` is idempotent so a double-fire is safe.
    """
    org_id = data.get("id", "")
    if not org_id:
        logger.warning("Clerk organization.deleted missing id: %s", data)
        return

    try:
        from core.services.billing_service import cancel_subscription_for_owner

        result = await cancel_subscription_for_owner(org_id)
        logger.info(
            "Clerk organization.deleted: Stripe subscription cancel for org %s -> %s",
            org_id,
            result.get("status"),
        )
    except Exception:
        logger.exception(
            "Stripe subscription cancel on organization.deleted failed for org %s",
            org_id,
        )

    from boto3.dynamodb.conditions import Key

    from core.dynamodb import run_in_thread

    provisioning = await build_paperclip_provisioning()
    try:
        # Reach through the repo to the underlying GSI to enumerate members.
        # We could promote this into the repo, but for v1 the only caller
        # is this webhook handler and adding a public method now would
        # outgrow its single use site.
        table = provisioning._repo._table()  # noqa: SLF001 — same package, single call site
        last_key = None
        disabled = 0
        while True:
            kwargs: dict = {
                "IndexName": "by-org-id",
                "KeyConditionExpression": Key("org_id").eq(org_id),
            }
            if last_key is not None:
                kwargs["ExclusiveStartKey"] = last_key
            resp = await run_in_thread(table.query, **kwargs)
            for item in resp.get("Items", []):
                uid = item.get("user_id")
                if not uid:
                    continue
                try:
                    await provisioning.disable(user_id=uid)
                    disabled += 1
                except Exception:
                    logger.exception(
                        "disable on organization.deleted failed for user=%s org=%s",
                        uid,
                        org_id,
                    )
            last_key = resp.get("LastEvaluatedKey")
            if not last_key:
                break
        put_metric(
            "paperclip.webhook.disable",
            value=disabled,
            dimensions={"trigger": "organization_deleted"},
        )
        logger.info(
            "Clerk organization.deleted: disabled %d member rows for org %s",
            disabled,
            org_id,
        )
    finally:
        await close_paperclip_provisioning(provisioning)


async def _handle_user_deleted_paperclip(user_id: str) -> None:
    """Disable Paperclip for a deleted Clerk user."""
    provisioning = await build_paperclip_provisioning()
    try:
        await provisioning.disable(user_id=user_id)
        put_metric("paperclip.webhook.disable", dimensions={"trigger": "user_deleted"})
    except Exception:
        logger.exception("disable on user_deleted failed for user=%s", user_id)
    finally:
        await close_paperclip_provisioning(provisioning)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@router.post(
    "/clerk",
    summary="Handle Clerk webhooks",
    description=(
        "Processes Clerk lifecycle events: user.created, user.updated, user.deleted, "
        "organization.deleted, organizationMembership.created/deleted."
    ),
    operation_id="handle_clerk_webhook",
    include_in_schema=False,
)
async def handle_clerk_webhook(request: Request):
    """Handle Clerk webhook events. No Clerk JWT auth — uses svix signature."""
    body = await request.body()
    _verify_svix_signature(body, dict(request.headers))

    # Idempotency dedupe — svix will retry on any non-2xx and on its own
    # at-least-once delivery insurance. Without this, two retries from
    # svix on a flaky webhook would each call ``provision_member``, the
    # second of which fails with a duplicate-membership conflict
    # (non-retryable: the member's Better Auth account already exists
    # from the first run, so sign_up_user 409s) and marks the row failed
    # forever. Keyed on svix-id which is unique per genuine Clerk event,
    # identical across retries.
    svix_id = request.headers.get("svix-id")
    if svix_id:
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
            # Dedupe is best-effort — a misconfigured WEBHOOK_DEDUP_TABLE
            # shouldn't black-hole all Clerk webhooks. Log and continue
            # so genuine traffic still flows; the worst case (DDB
            # outage) reverts to the pre-dedupe behavior of double-handling
            # a retry, which the per-event idempotency in provision_*
            # already mostly tolerates.
            logger.exception("clerk webhook dedupe check failed; processing anyway")

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON payload")

    event_type = payload.get("type", "")
    data: dict[str, Any] = payload.get("data", {})
    put_metric("webhook.clerk.received", dimensions={"event_type": event_type})

    if event_type == "user.created":
        user_id = data.get("id", "")
        logger.info("Clerk user.created webhook received for %s", user_id)

        # Persist user_id + primary email into the users table. The email
        # column is what ``_lookup_owner_email`` (and the Paperclip
        # provisioning chain) reads to sign the org owner in to Better
        # Auth — without this, organization.created webhooks never find
        # an email and would loop on retries forever. The /users/sync
        # path also calls user_repo.put but only knows the user_id;
        # the webhook is the authoritative source of email.
        if user_id:
            email = _extract_primary_email(data)
            try:
                from core.repositories import user_repo

                await user_repo.put(user_id, email=email)
            except Exception:
                # Persistence is non-fatal here — the /users/sync REST
                # path will write the row again the first time the user
                # opens the app, and downstream paperclip handlers fall
                # back to Clerk admin API. Logging is enough.
                logger.exception("user_repo.put failed for user.created %s", user_id)

            # Best-effort welcome email. send_welcome_email already swallows
            # its own errors, but we also guard here so a welcome blowup can
            # never 500 the webhook (which would trigger a Clerk retry storm).
            if email:
                try:
                    from core.services.welcome_email import send_welcome_email

                    await send_welcome_email(
                        to_email=email,
                        user_id=user_id,
                        first_name=data.get("first_name"),
                    )
                except Exception:
                    logger.exception("welcome email send raised for user.created %s", user_id)

            # PERSONAL provisioning trigger. A personal owner becomes
            # provision-eligible the instant the card-less trial_local row
            # exists (the gate has an explicit trial_local short-circuit that
            # skips the provider layer), so this event genuinely IS the
            # eligibility transition — no other signal is needed.
            #
            # Gated on primary-email verification; see _primary_email_verified
            # for why (pending-invitation tenancy invariant).
            #
            # Note on dedup: the svix-id row is written BEFORE this handler
            # runs and the Clerk handler does not use the delete_event
            # rollback, so a crash here suppresses Svix's retry. That is
            # deliberately fine for THIS branch and no rollback is warranted:
            # /container/status re-runs the identical trigger within ~3s, and
            # the trigger is idempotent (try_claim_provision collapses
            # concurrent dispatches to one execution). A blanket rollback
            # would reopen double-handling for every other branch.
            if not _primary_email_verified(data):
                put_metric(
                    "provision.trigger",
                    dimensions={"source": "clerk.user_created", "result": "unverified_email"},
                )
                logger.info(
                    "user.created: primary email not verified for %s — deferring "
                    "provisioning to the /container/status poll",
                    user_id,
                )
            else:
                try:
                    from core.services.user_provision import (
                        ensure_owner_provisioned,
                        ensure_personal_trial_default_for_user,
                    )

                    # Creates the trial_local row (which is what clears the
                    # gate), then triggers. Both are idempotent; both refuse a
                    # user holding a pending org invitation.
                    await ensure_personal_trial_default_for_user(user_id)
                    await ensure_owner_provisioned(user_id, trigger="clerk.user_created")
                except Exception:
                    # Never 5xx the webhook: Svix treats it as a delivery
                    # failure and 5 days of those silently disable the
                    # endpoint. The poll fallback covers this owner.
                    logger.exception("provisioning trigger failed for user.created %s", user_id)

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
            logger.warning("Clerk user.deleted webhook missing data.id")
            return {"status": "ok"}

        count = await channel_link_repo.sweep_by_member(user_id)
        logger.info(
            "Clerk user.deleted webhook: swept %d channel_link rows for %s",
            count,
            user_id,
        )
        # Disable Paperclip for this user (account-level deletion). Org
        # membership cleanup is handled via the organizationMembership
        # event stream — we don't enumerate orgs here.
        await _handle_user_deleted_paperclip(user_id)

    elif event_type == "organizationMembership.created":
        await _handle_organization_membership_created(data)

    elif event_type == "organizationMembership.deleted":
        await _handle_organization_membership_deleted(data)

    elif event_type == "organization.deleted":
        await _handle_organization_deleted(data)

    else:
        logger.debug("Clerk webhook: unhandled event type %s", event_type)

    return {"status": "ok"}
