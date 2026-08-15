"""Apple In-App Purchase — signed-transaction verification and entitlement mapping.

Exists because App Review rejected 0.1.0 (17) under guideline 3.1.1: a
subscription sold on the web must also be purchasable in the app via IAP
(3.1.3(b)). This is the server half — the client half is the paywall in
apps/mobile.

**Nothing here trusts the client.** A StoreKit transaction arrives as a JWS
whose header carries an x5c certificate chain; a valid one chains to Apple's
own root CA, is issued for OUR bundle id, and comes from the environment we
expect. Verifying that is the entire security boundary of this feature — a
forged payload that got past it would hand out a paid plan for free — so it is
delegated to Apple's `app-store-server-library` rather than hand-rolled from
PyJWT. That library does chain building, root pinning, OCSP-style checks, and
bundle/environment matching. Do not "simplify" it into a `jwt.decode(...,
options={"verify_signature": False})`; that is the whole bug.

Two entry points, deliberately sharing one mapping function:

- :func:`link_purchase` — the client posts the JWS it just received. Used once,
  at purchase, to bind ``originalTransactionId`` to an owner.
- :func:`apply_notification` — App Store Server Notifications V2, which is the
  ONLY way we hear about renewals, expiries, refunds and billing retries. The
  client is not involved and may never open the app again.

The status this produces is stored as Apple's OWN status, and the shared
``subscription_status`` every entitlement predicate reads is derived from both
providers (see ``billing_repo._effective_status``). So no predicate needs to
learn that Apple exists — but neither provider can cancel the other's live
subscription, which is what happened while both wrote the shared field
directly.

Two properties are enforced by the DATABASE rather than by checks in this
module, because every read available here comes from an eventually-consistent
GSI and therefore cannot decide a race: the ``signedDate`` watermark (Apple does
not promise delivery order) and the binding of a row to one
``originalTransactionId`` (two accounts can submit the same JWS at once). Both
are conditions on the write; a rejected write returns None and MUST NOT be read
as success.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from core.auth import is_org_owner_id
from core.config import settings
from core.repositories import billing_repo
from core.services import webhook_dedup
from core.services.webhook_dedup import WebhookDedupResult

logger = logging.getLogger(__name__)


class AppleIapError(Exception):
    """Verification failed, or the payload is not one we can act on."""


class AppleIapNotConfigured(AppleIapError):
    """IAP settings are absent — the feature is off in this environment."""


def owner_account_token(owner_id: str) -> str:
    """The ``appAccountToken`` a purchase by ``owner_id`` must carry.

    Derived rather than stored: SHA-256 of the owner id, first 16 bytes shaped
    into a v5-style UUID, because StoreKit accepts only a UUID in that field and
    owner ids (``user_2ab…``) are not one. Deriving means there is no per-owner
    token to mint, store, look up, or leak — the server recomputes it and
    compares.

    ``ownerAccountToken`` in ``apps/mobile/src/lib/appleIap.ts`` is the same
    computation and the two MUST stay in lockstep: a divergence rejects every
    real purchase, since the comparison is what authorizes the link.

    Lowercase hex, matching what Apple echoes back and what the JS side emits.
    """
    h = hashlib.sha256(owner_id.encode()).hexdigest()[:32]
    # Version 5 in the high nibble of byte 6; RFC-4122 variant in byte 8.
    h = f"{h[:12]}5{h[13:16]}8{h[17:]}"
    return f"{h[:8]}-{h[8:12]}-{h[12:16]}-{h[16:20]}-{h[20:32]}"


@dataclass(frozen=True)
class AppleSubscription:
    """The subset of a verified transaction this system acts on."""

    original_transaction_id: str
    product_id: str
    # None for a lifetime/non-renewing shape; every auto-renewable one has it.
    expires_at_ms: int | None
    #: Isol8 ``subscription_status`` this transaction implies.
    status: str
    #: ``appAccountToken`` the client stamped at purchase, when present — the
    #: UUID form of an owner id (see :func:`owner_account_token`), not the owner
    #: id itself, because StoreKit only accepts a UUID here.
    owner_hint: str | None


# Notification types that GRANT or KEEP access. Apple sends SUBSCRIBED on the
# first purchase and on a resubscribe; DID_RENEW on every successful renewal.
# DID_CHANGE_RENEWAL_STATUS fires when auto-renew is toggled — access continues
# to the paid-through date either way, so it is deliberately NOT a revoke.
_GRANTING_TYPES = frozenset({"SUBSCRIBED", "DID_RENEW", "OFFER_REDEEMED"})

# Types that END access immediately. REFUND and REVOKE are money-back events;
# EXPIRED is the paid-through date passing with no renewal.
_REVOKING_TYPES = frozenset({"EXPIRED", "REFUND", "REVOKE"})

# Billing retry: Apple could not charge, but the subscription is in a grace
# window and may still recover. Mapped to "past_due" — the same status the
# Stripe path uses for exactly this situation, so the existing gate copy and
# predicates already handle it.
_GRACE_TYPES = frozenset({"DID_FAIL_TO_RENEW"})


@lru_cache(maxsize=4)
def _verifier(environment_name: str = ""):
    """Build a SignedDataVerifier for one Apple environment.

    Cached because constructing it parses Apple's root certificates, and because
    a per-request rebuild would make every verification pay that cost on the
    checkout path. Raises AppleIapNotConfigured when the feature is unwired, so
    a backend booted without Apple settings (local dev, unit tests, any env
    before the Paid Apps Agreement is active) still starts — the router turns
    that into a 503 rather than a 500.

    ``environment_name`` defaults to the configured one; :func:`_verify_signed`
    passes the other explicitly. A verifier is bound to ONE environment and
    rejects the other's transactions outright, which matters more than it
    sounds — see that function.
    """
    # APPLE_IAP_PRODUCT_ID is required here, NOT merely checked per-transaction.
    # The rollout wires the bundle id and certs in a separate step from the
    # product id, so "verifier live, product id blank" is a reachable state, and
    # in that window any validly-signed transaction for our bundle could be
    # granted a plan. Failing configuration keeps the whole feature off until
    # all three are present. `_subscription_from_transaction` ALSO refuses a
    # blank expected id rather than skipping its own check — neither guard is
    # allowed to be conditional on its own configuration existing.
    if not settings.APPLE_IAP_BUNDLE_ID or not settings.APPLE_IAP_ROOT_CERTS_DIR or not settings.APPLE_IAP_PRODUCT_ID:
        raise AppleIapNotConfigured(
            "APPLE_IAP_BUNDLE_ID / APPLE_IAP_ROOT_CERTS_DIR / APPLE_IAP_PRODUCT_ID unset "
            "— Apple IAP is off in this environment"
        )

    # Imported lazily so the dependency is not required to import this module
    # (tests exercising the mapping logic do not need it).
    from appstoreserverlibrary.models.Environment import Environment
    from appstoreserverlibrary.signed_data_verifier import SignedDataVerifier

    certs_dir = Path(settings.APPLE_IAP_ROOT_CERTS_DIR)
    root_certs = [p.read_bytes() for p in sorted(certs_dir.glob("*.cer"))]
    if not root_certs:
        raise AppleIapNotConfigured(f"no Apple root certificates found in {certs_dir}")

    resolved = environment_name or settings.APPLE_IAP_ENVIRONMENT
    environment = Environment.PRODUCTION if resolved == "production" else Environment.SANDBOX

    # SignedDataVerifier compares this against Apple's NUMERIC appAppleId, so a
    # string never matches and every production notification fails verification
    # — silently, as an ordinary rejection. The setting is a str because env
    # vars are strings; convert here rather than changing its type, so a
    # non-numeric value is caught as misconfiguration instead of being passed
    # through to fail later at a much less obvious place.
    app_apple_id: int | None = None
    raw_app_id = (settings.APPLE_IAP_APP_APPLE_ID or "").strip()
    if raw_app_id:
        try:
            app_apple_id = int(raw_app_id)
        except ValueError as e:
            raise AppleIapNotConfigured(f"APPLE_IAP_APP_APPLE_ID must be numeric, got {raw_app_id!r}") from e
    # Apple requires it in production; sandbox tolerates its absence.
    if environment == Environment.PRODUCTION and app_apple_id is None:
        raise AppleIapNotConfigured("APPLE_IAP_APP_APPLE_ID is required for the production environment")

    return SignedDataVerifier(
        root_certificates=root_certs,
        enable_online_checks=True,
        environment=environment,
        bundle_id=settings.APPLE_IAP_BUNDLE_ID,
        app_apple_id=app_apple_id,
    )


def _verify_signed(method: str, blob: str):
    """Verify a JWS against the configured environment, then the other one.

    A SignedDataVerifier is bound to ONE environment and rejects the other's
    payloads as unverifiable, which would be fine if traffic were segregated by
    deployment — and it is not. **App Review tests every submission with SANDBOX
    transactions, sent to the PRODUCTION backend.** Pinning production to
    `production` alone therefore rejects the reviewer's own purchase and fails
    the submission this whole feature exists to pass. TestFlight has the same
    shape: sandbox transactions against whichever backend the build points at.

    So the configured environment is tried first (the common case, one call) and
    the other only as a fallback. Trying both is not a weakening: each verifier
    still enforces the full chain, bundle id, and app id — the environment is
    the only thing that differs, and Apple stamps it inside the signed payload.

    A fallback verifier that cannot be BUILT (production needs
    APPLE_IAP_APP_APPLE_ID, sandbox does not) is skipped rather than raised, or
    a dev backend with no app id configured could never verify anything.
    """
    primary = settings.APPLE_IAP_ENVIRONMENT or "sandbox"
    fallback = "sandbox" if primary == "production" else "production"

    # Not configured at all is a 503, and only the PRIMARY gets to say so.
    try:
        return getattr(_verifier(primary), method)(blob)
    except AppleIapNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001 — any verifier failure is a rejection
        first_error = e

    # Reached only when the configured environment rejected the payload, so the
    # steady-state path builds and calls exactly ONE verifier.
    other = _fallback_verifier(fallback)
    if other is not None:
        try:
            return getattr(other, method)(blob)
        except Exception:  # noqa: BLE001 — report the primary's error, not this one
            pass

    raise AppleIapError(f"signed payload failed verification: {first_error}")


def _fallback_verifier(environment_name: str):
    """The other environment's verifier, or None when it cannot be built."""
    try:
        return _verifier(environment_name)
    except AppleIapNotConfigured:
        return None


def _status_for(
    notification_type: str | None,
    *,
    revoked: bool = False,
    expires_at_ms: int | None = None,
    now_ms: int | None = None,
) -> str:
    """Map an Apple event to the Isol8 ``subscription_status`` vocabulary.

    Kept as one function so the link path and the notification path can never
    disagree about what a given event means.

    ``expires_at_ms`` is checked BEFORE any granting branch. A signed StoreKit
    transaction stays cryptographically valid forever — expiry is a date inside
    the payload, not a signature property — so verification alone says nothing
    about whether the subscription is live. The link path passes
    ``notification_type=None`` (there is no event, just a transaction the client
    handed us), which would otherwise fall straight through to "active": a user
    could replay or restore a long-expired transaction and hold paid access
    indefinitely, since no newer notification would ever arrive to correct it.
    """
    if revoked:
        return "canceled"
    # An elapsed paid-through date beats every other signal, including a
    # granting notification type — Apple can only be telling us about a period
    # that has since ended.
    if expires_at_ms is not None:
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if int(expires_at_ms) <= current_ms:
            return "canceled"
    if notification_type is None or notification_type in _GRANTING_TYPES:
        return "active"
    if notification_type in _REVOKING_TYPES:
        return "canceled"
    if notification_type in _GRACE_TYPES:
        return "past_due"
    # An unmapped type must not silently change entitlement. Callers treat
    # this as "no write" rather than guessing.
    raise AppleIapError(f"unhandled notification type: {notification_type}")


def _subscription_from_transaction(transaction, notification_type: str | None) -> AppleSubscription:
    """Project a verified transaction payload onto :class:`AppleSubscription`."""
    original_transaction_id = getattr(transaction, "originalTransactionId", None)
    if not original_transaction_id:
        raise AppleIapError("verified transaction carries no originalTransactionId")

    product_id = getattr(transaction, "productId", None) or ""
    expected = settings.APPLE_IAP_PRODUCT_ID
    # Fail CLOSED on an unset expected id. `_verifier` already refuses to build
    # without APPLE_IAP_PRODUCT_ID, so this is belt-and-braces — but the old
    # `if expected and ...` form meant a future caller reaching this with the
    # setting blank would accept a transaction for ANY product of ours and
    # grant the full plan. The guard must never be conditional on its own
    # configuration being present.
    if not expected:
        raise AppleIapError("APPLE_IAP_PRODUCT_ID is not configured — refusing to grant")
    if product_id != expected:
        # A transaction for a product we do not sell must never grant a plan.
        raise AppleIapError(f"unexpected productId {product_id!r} (expected {expected!r})")

    # revocationDate is set on a refund/revoke even when the notification type
    # itself is something else, so it is checked independently.
    revoked = getattr(transaction, "revocationDate", None) is not None
    expires_at_ms = getattr(transaction, "expiresDate", None)

    return AppleSubscription(
        original_transaction_id=str(original_transaction_id),
        product_id=product_id,
        expires_at_ms=expires_at_ms,
        status=_status_for(notification_type, revoked=revoked, expires_at_ms=expires_at_ms),
        owner_hint=getattr(transaction, "appAccountToken", None) or None,
    )


def verify_transaction(signed_transaction: str) -> AppleSubscription:
    """Verify a client-supplied JWS transaction and project it.

    Raises AppleIapError when the signature, chain, bundle id, environment, or
    product id does not check out.
    """
    payload = _verify_signed("verify_and_decode_signed_transaction", signed_transaction)
    return _subscription_from_transaction(payload, None)


async def link_purchase(*, owner_id: str, signed_transaction: str) -> AppleSubscription:
    """Bind a freshly-verified purchase to ``owner_id``.

    Called from the app right after StoreKit reports success. Idempotent: the
    same transaction replayed writes the same row, so a client retrying after a
    dropped response cannot double-grant or corrupt anything.
    """
    # The Apple product is the PERSONAL flat plan. An org pays a different
    # shape entirely — base + per-seat, quantity driven by Clerk membership via
    # sync_team_seat_quantity — so there is nothing here an org can validly buy.
    #
    # This is a money fence, not a UX one, and it belongs on the server. An org
    # that reaches this by any route (client bug, stale build, direct call) has
    # ALREADY been charged by Apple by the time we see the transaction, and the
    # write below would fail anyway: a team_billing_required org has no billing
    # row by construction, so set_apple_subscription returns None and the caller
    # gets a permanent dead end with real money taken. Refuse before StoreKit is
    # ever reached instead.
    if is_org_owner_id(owner_id):
        raise AppleIapError("Team workspaces are billed per seat and cannot subscribe through the App Store")

    subscription = verify_transaction(signed_transaction)

    # `appAccountToken` is the ONLY binding here that is not a race: the client
    # stamps it at purchase and Apple echoes it inside the SIGNED payload, so a
    # replay by another account cannot strip or forge it, and the comparison
    # needs no read at all.
    #
    # REQUIRED, not advisory. The earlier version checked it "when present" —
    # which sounds careful and fenced nothing, because the client was not
    # stamping it, so `owner_hint` was always None and the branch never ran.
    # Requiring it is safe precisely because IAP has never shipped: there is no
    # install base of un-stamped purchases to grandfather, and every transaction
    # that can exist comes from a build that stamps.
    #
    # Deliberately narrows one case: an owner who deletes their Isol8 account
    # and signs up again cannot restore onto the new one, because the token is
    # bound to the id that bought it. That is the correct trade — the looser
    # rule ("allow it when no row claims the transaction") makes deleting an
    # account the bypass for sharing a subscription, since the wipe removes the
    # very row the check reads. Recovery is to cancel in iOS Settings and
    # subscribe again on the new account.
    if not subscription.owner_hint:
        raise AppleIapError("this Apple transaction carries no account binding and cannot be linked")
    if subscription.owner_hint.lower() != owner_account_token(owner_id):
        raise AppleIapError("this Apple transaction was purchased by a different account")

    # Refuse to move a live subscription between accounts. Without this, one
    # Apple ID could be walked across accounts to keep several of them paid.
    #
    # Kept as a second line even though the token check above already decides
    # the cross-account case, because it catches an id owned by a DIFFERENT row
    # — something no single-row write condition can see. It is NOT sufficient
    # alone: it reads a non-unique, eventually-consistent GSI, so two accounts
    # submitting the same unlinked JWS could both be told "unowned", and each
    # one's write condition only fences its own row.
    existing = await billing_repo.get_by_apple_original_transaction_id(subscription.original_transaction_id)
    if existing is not None and existing.get("owner_id") != owner_id:
        raise AppleIapError("this Apple subscription is already linked to another account")

    written = await billing_repo.set_apple_subscription(
        owner_id,
        original_transaction_id=subscription.original_transaction_id,
        status=subscription.status,
        expires_at=subscription.expires_at_ms,
    )
    if written is None:
        # Either no billing row, or the conditional write refused because this
        # row is already bound to a different Apple transaction. Both mean the
        # link did not happen, and neither should read as success.
        raise AppleIapError(f"could not link this transaction to owner {owner_id}")
    logger.info(
        "apple_iap.linked owner_id=%s original_transaction_id=%s status=%s",
        owner_id,
        subscription.original_transaction_id,
        subscription.status,
    )
    return subscription


async def apply_notification(signed_payload: str) -> tuple[str, str] | None:
    """Verify and apply an App Store Server Notification V2.

    Returns ``(owner_id, status)`` when a row was updated, or None when the
    notification is one we deliberately ignore. Raises AppleIapError when
    verification fails, so the router can answer 400 and Apple will retry —
    important, because a silently-swallowed renewal means an owner loses access
    at the paid-through date despite having paid.

    Two guards against Apple's delivery model, which retries for up to three
    days and does NOT promise ordering:

    - Dedup on ``notificationUUID``, taken from the VERIFIED payload. Reading it
      from the unverified envelope would let anyone freeze an owner's
      subscription by pre-claiming the id of a notification not yet delivered.
    - Monotonicity on ``signedDate``. Dedup alone does not help when two
      DIFFERENT notifications arrive out of order: a retried DID_RENEW landing
      after an EXPIRED would otherwise restore access that had legitimately
      ended. An older signedDate than the one already applied is dropped.
    """
    try:
        notification = _verify_signed("verify_and_decode_notification", signed_payload)
    except AppleIapNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001
        raise AppleIapError(f"notification failed verification: {e}") from e

    notification_type = getattr(notification, "notificationType", None)
    notification_type = getattr(notification_type, "value", notification_type)
    notification_uuid = getattr(notification, "notificationUUID", None)
    signed_date = getattr(notification, "signedDate", None)

    dedup_event_id = f"apple:{notification_uuid}" if notification_uuid else None
    if dedup_event_id:
        dedup = await webhook_dedup.record_event_or_skip(event_id=dedup_event_id, source="apple-iap")
        if dedup is WebhookDedupResult.ALREADY_SEEN:
            logger.info("apple_iap.notification_replay uuid=%s", notification_uuid)
            return None

    # Everything past the dedup claim runs under a rollback. The claim is
    # recorded BEFORE the work (that is what makes it a claim), so any failure
    # afterwards would otherwise leave the uuid permanently marked seen: this
    # delivery returns an error, Apple retries, the retry reads ALREADY_SEEN and
    # is acknowledged without ever applying the renewal, expiry or refund. The
    # owner silently keeps or loses access against what Apple actually said.
    # Same rollback pattern the Stripe marketplace webhook uses.
    try:
        return await _apply_verified_notification(
            notification=notification,
            notification_type=notification_type,
            signed_date=signed_date,
        )
    except Exception:
        if dedup_event_id:
            try:
                await webhook_dedup.delete_event(event_id=dedup_event_id)
            except Exception:  # noqa: BLE001 — never mask the original failure
                logger.exception("apple_iap.dedup_rollback_failed uuid=%s", notification_uuid)
        raise


async def _apply_verified_notification(
    *, notification, notification_type: str | None, signed_date: int | None
) -> tuple[str, str] | None:
    """The body of apply_notification, after the dedup claim is held.

    Split out purely so the caller can wrap it in one rollback rather than
    threading try/except through every early return.
    """
    data = getattr(notification, "data", None)
    signed_transaction = getattr(data, "signedTransactionInfo", None) if data else None
    if not signed_transaction:
        # CONSUMPTION_REQUEST, TEST, and friends carry no transaction. Nothing
        # to apply, and not an error.
        logger.info("apple_iap.notification_ignored type=%s", notification_type)
        return None

    try:
        transaction = _verify_signed("verify_and_decode_signed_transaction", signed_transaction)
    except Exception as e:  # noqa: BLE001
        raise AppleIapError(f"notification transaction failed verification: {e}") from e

    try:
        subscription = _subscription_from_transaction(transaction, notification_type)
    except AppleIapError as e:
        # An unmapped type or a foreign product is not a verification failure:
        # answering 400 would make Apple retry it forever. Log and ack.
        logger.info("apple_iap.notification_unactionable type=%s: %s", notification_type, e)
        return None

    row = await billing_repo.get_by_apple_original_transaction_id(subscription.original_transaction_id)
    if row is None:
        # The purchase was never linked — the app died between StoreKit
        # returning and POST /billing/apple/link landing. The client's restore
        # path recovers this; there is nothing to update now.
        logger.warning(
            "apple_iap.notification_unlinked original_transaction_id=%s type=%s",
            subscription.original_transaction_id,
            notification_type,
        )
        return None

    owner_id = row["owner_id"]

    # Out-of-order guard. Apple does not promise delivery order, so a retried
    # older notification can land after a newer one; applying it would move
    # entitlement backwards — or worse, forwards after a cancellation.
    #
    # The comparison is made INSIDE the write as a DynamoDB condition, not here.
    # `row` came from an eventually-consistent GSI read, so a check out here can
    # be made against a stale watermark, and two overlapping notifications can
    # both pass it before either writes. The condition re-evaluates against the
    # item at write time, so the older one is rejected no matter how the reads
    # interleaved.
    written = await billing_repo.set_apple_subscription(
        owner_id,
        original_transaction_id=subscription.original_transaction_id,
        status=subscription.status,
        expires_at=subscription.expires_at_ms,
        signed_date=int(signed_date) if signed_date is not None else None,
    )
    if written is None:
        logger.info(
            "apple_iap.notification_stale owner_id=%s type=%s signed_date=%s",
            owner_id,
            notification_type,
            signed_date,
        )
        return None
    logger.info(
        "apple_iap.notification_applied owner_id=%s type=%s status=%s",
        owner_id,
        notification_type,
        subscription.status,
    )
    return owner_id, subscription.status
