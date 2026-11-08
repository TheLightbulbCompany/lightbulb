"""Clerk webhook router.

Receives lifecycle events from Clerk (user, organization,
organizationMembership) and keeps internal state — including the
Paperclip mirror — in sync.

Verification uses the svix HMAC-SHA256 signature scheme:
  https://docs.svix.com/receiving/verifying-payloads/how

The signature is the ONLY authentication on this endpoint, so verification
fails CLOSED in every deployed env: a missing or malformed
CLERK_WEBHOOK_SECRET returns 503 rather than waving traffic through. The
check is skipped only when ENVIRONMENT is unset (bare local dev), where no
Clerk instance signs anything. Any deployed stage sets ENVIRONMENT, so the
fail-closed path is the only one that runs off a laptop.

**Workspace dispatch**:

  * ``organization.created``           -> ``provision_new_workspace`` (trial row + gate + provision dispatch)
  * ``organizationMembership.created`` -> idempotent workspace JOIN via ``_ensure_workspace_member``
    (join-only; a still-provisioning org self-heals on the member's next /teams request)
  * ``organizationMembership.deleted`` -> ``remove_member`` from THAT org's workspace only
  * ``organization.deleted``           -> ``disable_workspace`` (purged after the grace window)
  * ``user.deleted``                   -> delete that user's stored identity row
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
from core.repositories import billing_repo, channel_link_repo
from core.repositories.paperclip_repo import PaperclipRepo
from core.services import network_member
from core.services.webhook_dedup import MembershipLifecycleSuperseded

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
    a forged ``user.deleted`` sweeps channel_link rows and disables
    Paperclip for that user id. Unverifiable mail is now rejected instead.

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


def _clerk_event_sequence(payload: dict, headers: dict, event_id: str, event_type: str) -> str:
    """Return a stable signed ordering token for a membership lifecycle."""
    timestamp = payload.get("timestamp")
    if not isinstance(timestamp, int) or isinstance(timestamp, bool) or timestamp < 0:
        try:
            timestamp = int(headers.get("svix-timestamp", "")) * 1000
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail="Clerk lifecycle event timestamp unavailable") from exc
    # At equal timestamps revocation wins; a later create can still re-admit.
    deny_rank = 1 if event_type.endswith(".deleted") else 0
    return f"{timestamp:020d}:{deny_rank}:{event_id}"


def _membership_alias(user_id: str) -> str:
    """The stable per-member lifecycle key.

    Shape is frozen: ``webhook_dedup``'s membership-lifecycle rows are keyed
    off it, so changing the digest orphans every in-flight claim. It predates
    this module (it was minted by the removed native-rooms path) and is kept
    verbatim for that reason, not because anything still reads it from there.
    """
    value = json.dumps([settings.CLERK_ISSUER, user_id], ensure_ascii=False, separators=(",", ":"))
    return f"clerk-{hashlib.sha256(value.encode()).hexdigest()}@identity.lightbulb.invalid"


async def _renew_member_lifecycle(owner_id: str, user_id: str, claim_id: str | None) -> None:
    if claim_id is None:
        return
    from core.services.webhook_dedup import renew_membership_lifecycle

    await renew_membership_lifecycle(owner_id, _membership_alias(user_id), claim_id)


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


async def _handle_organization_created(data: dict) -> None:
    """Start the new workspace's first provision without waiting for a browser poll.

    ``provision_new_workspace`` creates the trial row, checks the provision gate and
    dispatches the provision funnel (the workspace is seeded INSIDE the provision execution,
    so no half-state). Nothing else starts a first provision, so a failure RAISES: the
    dedup row is rolled back and the 5xx makes Svix redeliver. A gate that is up is a
    normal return.
    """
    org_id = data.get("id", "")
    owner_user_id = data.get("created_by", "")
    if not org_id or not owner_user_id:
        logger.warning("Clerk organization.created missing id/created_by: %s", data)
        return
    logger.info(
        "organization.created: org=%s owner=%s recorded",
        org_id,
        owner_user_id,
    )
    from core.services import join_analytics, workspace_source

    join_analytics.workspace_created(
        org_id, owner_user_id, await workspace_source.adopt_for_workspace(org_id, owner_user_id)
    )
    # Hand the agent-email slug the owner held at "Create your workspace" to the workspace
    # BEFORE provisioning (routers/company_slug.py). Idempotent, so a Svix redelivery is safe.
    from core.repositories import company_slug_repo
    from core.services.user_provision import provision_new_workspace

    # The page normally claims it first (a keyed, consistent write); this GSI-backed backstop is
    # a no-op then, and a miss just means the owner chose no slug -- worth a log, not a retry.
    if await company_slug_repo.claim_hold(user_id=owner_user_id, owner_id=org_id) is None:
        logger.info("organization.created: no live slug hold for %s -> %s", owner_user_id, org_id)
    await provision_new_workspace(org_id, created_by=owner_user_id)


async def _sync_seats(org_id: str) -> None:
    """Seat billing follows membership (T04). A failure RAISES: the lifecycle claims stay open
    and Clerk redelivers the event. Every step before this is idempotent, and the sync itself
    sets an absolute quantity, so the replay is safe."""
    from core.services.billing_service import sync_seat_quantity

    try:
        await sync_seat_quantity(org_id)
    except Exception:
        logger.exception("membership: seat sync failed org=%s; Clerk will redeliver", org_id)
        put_metric("billing.seat_sync", dimensions={"outcome": "error"})
        raise


async def _handle_organization_membership_created(
    data: dict,
    *,
    lifecycle_claim_id: str | None = None,
) -> None:
    """Record a membership lifecycle event, then join the org's workspace.

    Room admission remains explicit; the member receives an opaque user stream
    when they first request messaging live-config or are added to a room. The
    one exception is the vendor shared channel, which ``on_org_member_joined``
    keeps in step with org membership.

    The join itself is delegated to
    ``core.services.user_provision._ensure_workspace_member`` — the same
    join-only, idempotent helper ``ensure_user_ready`` calls from the member's
    own ``/teams`` requests. It returns gracefully when the org's workspace isn't
    active yet (the member's next ``/teams`` request self-heals). Any exception is
    logged and metered here but never re-raised: this must not turn into a
    non-2xx that makes Clerk retry the whole event — the lifecycle bookkeeping
    above already happened and must still be acknowledged. Without this call,
    a member who never polls (or polls in the wrong context) stays invisible
    to every Teams surface even though chat already works for them.
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
    await _renew_member_lifecycle(org_id, user_id, lifecycle_claim_id)
    logger.info(
        "membership.created: org=%s member=%s owner=%s recorded",
        org_id,
        user_id,
        owner_user_id,
    )

    from core.services.user_provision import _ensure_workspace_member

    try:
        await _ensure_workspace_member(org_id=org_id, member_user_id=user_id)
    except Exception:
        logger.exception(
            "membership.created: workspace membership-add failed org=%s user=%s "
            "— the member's next /teams request retries",
            org_id,
            user_id,
        )
        put_metric("workspace.member_add", dimensions={"outcome": "webhook_error"})

    # A real (re)join is the ONE door that turns a removed member's private stream back on
    # (fresh key); every other ensure_user_stream caller leaves an inactive stream off.
    from core.repositories import messaging_repo

    try:
        await messaging_repo.ensure_user_stream(org_id, user_id, rejoin=True)
    except Exception:
        logger.exception("membership.created: user stream rejoin failed org=%s user=%s", org_id, user_id)
        put_metric("messaging.user_stream.rejoin_error")

    from core.services.messaging import on_org_member_joined

    try:
        await on_org_member_joined(org_id, user_id)
    except Exception:
        logger.exception("membership.created: vendor room add failed org=%s user=%s", org_id, user_id)
        put_metric("messaging.vendor_room.error")

    # The #general seat for a new teammate (idempotent: one-time AUTOJOIN marker). A
    # member who joins before the room exists is seated by the provision executor.
    from core.services.messaging import join_default_room, join_feed_room

    try:
        await join_default_room(org_id, user_id)
    except Exception:
        logger.exception("membership.created: default room join failed org=%s user=%s", org_id, user_id)
        put_metric("messaging.default_room.error")
    # The feed seat, the same way (a workspace without a feed yet seats them when it is made).
    try:
        await join_feed_room(org_id, user_id)
    except Exception:
        logger.exception("membership.created: feed join failed org=%s user=%s", org_id, user_id)
        put_metric("messaging.feed_room.error")

    from core.services import join_analytics

    await join_analytics.member_joined(
        org_id, user_id, email=email, creator_id=owner_user_id, membership_id=data.get("id") or ""
    )
    # The teammate welcome, from the CEO's address (D9, D10). The creator's own membership is
    # skipped: the owner version goes out when provisioning finishes (user_provision).
    if user_id != owner_user_id:
        from core.services.welcome_email import send_teammate_welcome

        try:
            await send_teammate_welcome(org_id, user_id=user_id, email=email, first_name=pud.get("first_name"))
        except Exception:
            logger.exception("membership.created: teammate welcome failed org=%s user=%s", org_id, user_id)


async def _handle_organization_membership_deleted(
    data: dict,
    *,
    lifecycle_claim_id: str | None = None,
    lifecycle_event_id: str | None = None,
) -> None:
    """Revoke canonical room/user streams, then drop the workspace membership."""
    pud = data.get("public_user_data") or {}
    user_id = pud.get("user_id", "") or data.get("user_id", "")
    org_id = (data.get("organization") or {}).get("id", "")
    if not user_id or not org_id:
        raise HTTPException(status_code=400, detail="Clerk membership deletion target unavailable")

    from core.services import agent_keys
    from core.services.mac_browser import revoke_member_soon
    from core.services.messaging import revoke_human_membership
    from core.services.text_lines import unlink_member

    revocation_error: BaseException | None = None
    still_current = None
    try:
        await _renew_member_lifecycle(org_id, user_id, lifecycle_claim_id)
        # Security boundary: every room key rotates before the private stream is
        # disabled. Repeated delivery is idempotent.
        await revoke_human_membership(org_id, user_id)
        await agent_keys.delete_for_member(org_id, user_id)  # a rejoin must not revive old keys
        await unlink_member(org_id, user_id)  # their texts to the business's number are a customer's now
        await _renew_member_lifecycle(org_id, user_id, lifecycle_claim_id)
        # Only once the revocation landed and this delete is still the current lifecycle event:
        # their paired Macs are refused at once, cleared of the workspace's sign-ins if
        # reachable within a deadline, then unpaired and disconnected. In the background, so it
        # never delays or fails this webhook; it re-checks the lifecycle epoch (the lease is
        # gone once this webhook completes) once, before touching any device; after that the conditional
        # device writes spare a re-pair.
        if lifecycle_event_id:
            from core.services.webhook_dedup import membership_revocation_current

            alias = _membership_alias(user_id)

            async def still_current() -> bool:
                return await membership_revocation_current(org_id, alias, lifecycle_event_id)

        revoke_member_soon(org_id, user_id, still_current)
    except Exception as exc:
        # The membership drop still gets its turn, but the event is not marked
        # complete until canonical revocation succeeds on a retry.
        revocation_error = exc

    # Dropping the workspace membership is the Teams authorization fence. A failure raises below,
    # so the redelivery retries it.
    try:
        await _renew_member_lifecycle(org_id, user_id, lifecycle_claim_id)
        await PaperclipRepo(table_name="paperclip-companies").remove_member(org_id, user_id)
        await _renew_member_lifecycle(org_id, user_id, lifecycle_claim_id)
        put_metric("workspace.webhook.remove_member", dimensions={"result": "ok"})
    except MembershipLifecycleSuperseded:
        raise
    except Exception as exc:
        put_metric("workspace.webhook.remove_member", dimensions={"result": "error"})
        logger.exception("workspace membership fence failed for user=%s org=%s", user_id, org_id)
        revocation_error = revocation_error or exc

    # Their private agent memory (1:1 DM facts) goes at once, no grace period. Its own try, so a
    # failure neither skips the Mac revocation nor completes the event: records never expire, so
    # it raises below and the redelivery retries it.
    try:
        from core.services.workspace_memory import delete_member_memory

        await _renew_member_lifecycle(org_id, user_id, lifecycle_claim_id)
        # Its late tail re-checks the same epoch: a re-add before it runs keeps the new memories.
        await delete_member_memory(org_id, user_id, still_gone=still_current)
    except MembershipLifecycleSuperseded:
        raise
    except Exception as exc:
        logger.exception("membership.deleted: private memory wipe failed org=%s user=%s", org_id, user_id)
        revocation_error = revocation_error or exc

    if revocation_error is not None:
        raise revocation_error

    await _teardown_if_no_members_left(org_id)


async def _teardown_if_no_members_left(org_id: str) -> None:
    """The LAST member leaving (or deleting their Clerk account) leaves an org nobody can sign in
    to, but Clerk keeps the empty org, so ``organization.deleted`` never fires and the workspace
    runs on (prod org_3JKLW…, 2026-09-23). Run the same teardown the org deletion does.

    STRICT roster read: a Clerk blip must fail closed (raise, and the webhook retries) rather
    than read as "no members" and tear a live workspace down."""
    from core.services.clerk_admin import list_organization_members

    if not settings.CLERK_SECRET_KEY:
        return  # no roster to read (tests, local dev): unknown is not "empty"
    if await list_organization_members(org_id, strict=True):
        return
    logger.info("organizationMembership.deleted: org %s has no members left -- tearing it down", org_id)
    await _handle_organization_deleted({"id": org_id})


async def _handle_organization_deleted(data: dict) -> None:
    """Cancel the org's Stripe subscription, then
    disable every Paperclip member row attached to the deleted org.

    The org's workspace subscription is billed on the org's own
    ``billing_accounts`` row (owner_id == org_id), which nothing else
    tears down when the Clerk org itself is deleted -- left alone it
    would keep charging the workspace forever. We cancel it via the same
    Stripe path the admin "cancel subscription" action uses
    (``billing_service.cancel_subscription_for_owner``): cancelling at
    Stripe (not just clearing the DDB row) fires the inbound
    ``customer.subscription.deleted`` webhook, which handles container
    teardown. Best-effort -- a Stripe error must not break this webhook,
    which still has to return 200 to Clerk.

    This runs BEFORE the workspace disable below, and in its own
    independent try/except, so a DynamoDB failure there can never prevent
    the billing-critical cancel from running.

    The workspace's ONE row goes ``disabled`` with a purge timer; the
    cleanup cron deletes it after the grace window. Idempotent.
    """
    org_id = data.get("id", "")
    if not org_id:
        logger.warning("Clerk organization.deleted missing id: %s", data)
        return

    # Drop the workspace's join link first. It is a live credential, and a
    # deleted org is exactly when nobody is watching it any more; the delete is
    # idempotent and its failure must not stop the teardown below.
    try:
        from core.services.workspace_join import revoke_join_link

        await revoke_join_link(org_id)
    except Exception:
        logger.exception("join-link revoke on organization.deleted failed for org %s", org_id)

    # A deleted org must stop funded inference even if Stripe cancellation
    # fails. It may retain an old managed key after switching to BYOK/OAuth,
    # so retire by key state rather than the current provider choice.
    try:
        from core.repositories.openrouter_key_repo import mark_retiring

        await mark_retiring(org_id)
    except Exception:
        logger.exception("OpenRouter key retirement on organization.deleted failed for org %s", org_id)

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

    # The runtime itself. A paid workspace also reaches this through the Stripe
    # subscription.deleted teardown, but a free trial has no subscription to cancel, so
    # nothing else would ever stop it. Idempotent; profiles are kept (delete_profiles=False).
    try:
        from core.services.hermes_provisioning import delete_workspace_runtime

        await delete_workspace_runtime(org_id)
    except Exception:
        logger.exception("runtime teardown on organization.deleted failed for org %s", org_id)

    try:
        from core.services.workspace_provisioning import disable_workspace

        await disable_workspace(org_id)
        put_metric("workspace.webhook.disable", dimensions={"trigger": "organization_deleted"})
    except Exception:
        logger.exception("disable_workspace on organization.deleted failed for org=%s", org_id)


async def _handle_user_deleted_paperclip(user_id: str) -> None:
    """Forget a deleted Clerk user's Paperclip identity row. Their company
    memberships go with each org's ``organizationMembership.deleted``."""
    from core.repositories.paperclip_repo import PaperclipRepo

    try:
        await PaperclipRepo(table_name="paperclip-companies").delete(user_id)
        put_metric("paperclip.webhook.disable", dimensions={"trigger": "user_deleted"})
    except Exception:
        logger.exception("identity delete on user_deleted failed for user=%s", user_id)


# ----------------------------------------------------------------------
# Routes
# ----------------------------------------------------------------------


@router.post(
    "/clerk",
    summary="Handle Clerk webhooks",
    description=(
        "Processes Clerk lifecycle events: user.created, user.updated, user.deleted, "
        "organization.created/deleted, organizationMembership.created/deleted."
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
    data: dict[str, Any] = payload.get("data", {})
    svix_id = request.headers.get("svix-id")
    lifecycle_event = event_type in {
        "user.deleted",
        "organizationMembership.created",
        "organizationMembership.deleted",
    }
    lifecycle_id = svix_id or f"local-{hashlib.sha256(body).hexdigest()}"
    lifecycle_sequence = (
        _clerk_event_sequence(payload, dict(request.headers), lifecycle_id, event_type) if lifecycle_event else ""
    )
    processing_claim = None
    membership_claim = None
    recorded_event_id = None
    membership_owner_id = None
    membership_alias = None

    if event_type in {"organizationMembership.created", "organizationMembership.deleted"}:
        pud = data.get("public_user_data") or {}
        membership_user_id = pud.get("user_id", "") or data.get("user_id", "")
        membership_owner_id = (data.get("organization") or {}).get("id", "")
        if not membership_user_id or not membership_owner_id:
            raise HTTPException(status_code=400, detail="Clerk membership lifecycle target unavailable")

        from core.services.webhook_dedup import (
            MembershipLifecycleInProgress,
            begin_membership_lifecycle,
        )

        membership_alias = _membership_alias(membership_user_id)
        try:
            membership_claim = await begin_membership_lifecycle(
                membership_owner_id,
                membership_alias,
                event_id=lifecycle_id,
                sequence=lifecycle_sequence,
                access_state="allowed" if event_type.endswith(".created") else "revoked",
            )
        except MembershipLifecycleInProgress as exc:
            raise HTTPException(status_code=503, detail="Older Clerk membership event is still processing") from exc
        if membership_claim is None:
            put_metric("clerk.webhook.membership_stale", dimensions={"event_type": event_type})
            return {"status": "stale"}

    # Idempotency dedupe — svix will retry on any non-2xx and on its own
    # at-least-once delivery insurance. Without this, two retries from
    # svix on a flaky webhook would each call ``provision_member``, the
    # second of which fails with a duplicate-membership conflict
    # (non-retryable: the member's Better Auth account already exists
    # from the first run, so sign_up_user 409s) and marks the row failed
    # forever. Keyed on svix-id which is unique per genuine Clerk event,
    # identical across retries.
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
                if membership_claim is not None:
                    from core.services.webhook_dedup import complete_membership_lifecycle

                    await complete_membership_lifecycle(
                        membership_owner_id,
                        membership_alias,
                        membership_claim,
                    )
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
                recorded_event_id = svix_id
            except Exception:
                # Non-security events retain the historical best-effort
                # behavior; lifecycle claims above deliberately fail closed.
                logger.exception("clerk webhook dedupe check failed; processing anyway")

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
        # Their own memory data (sessions, drafts, episodes, computer keys). Memory records never
        # expire, so a failure fails the event and the redelivery retries it.
        from core.services.workspace_memory import delete_person_memory

        await delete_person_memory(user_id)

    elif event_type == "organization.created":
        try:
            await _handle_organization_created(data)
        except Exception:
            # The dedup row was recorded before dispatch: roll it back so Svix's redelivery
            # of this 5xx re-attempts instead of being acked as a duplicate (billing.py's
            # Stripe pattern). The handler is idempotent, so a partial attempt replays safely.
            if recorded_event_id:
                from core.services.webhook_dedup import delete_event

                await delete_event(recorded_event_id)
            raise

    elif event_type == "organizationMembership.created":
        pud = data.get("public_user_data") or {}
        user_id = pud.get("user_id", "") or data.get("user_id", "")
        org_id = (data.get("organization") or {}).get("id", "")
        if not user_id or not org_id:
            raise HTTPException(status_code=400, detail="Clerk membership creation target unavailable")
        await _handle_organization_membership_created(data, lifecycle_claim_id=membership_claim)
        await _sync_seats(org_id)
        await network_member.publish_sessions(org_id)  # the network's member list follows (never raises)

    elif event_type == "organizationMembership.deleted":
        await _handle_organization_membership_deleted(
            data,
            lifecycle_claim_id=membership_claim,
            lifecycle_event_id=lifecycle_id if membership_claim is not None else None,
        )
        await _sync_seats(membership_owner_id)
        await network_member.publish_sessions(membership_owner_id)

    elif event_type == "organization.deleted":
        await _handle_organization_deleted(data)

    else:
        logger.debug("Clerk webhook: unhandled event type %s", event_type)

    if membership_claim is not None:
        from core.services.webhook_dedup import complete_membership_lifecycle

        await complete_membership_lifecycle(
            membership_owner_id,
            membership_alias,
            membership_claim,
        )
    if processing_claim is not None:
        from core.services.webhook_dedup import complete_event_processing

        await complete_event_processing(lifecycle_id, processing_claim)
    return {"status": "ok"}
