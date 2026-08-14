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

The status this produces is written into the SAME ``subscription_status`` field
Stripe writes (see ``billing_repo.set_apple_subscription``), so no entitlement
predicate anywhere needs to learn that Apple exists.
"""

from __future__ import annotations

import logging
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


@dataclass(frozen=True)
class AppleSubscription:
    """The subset of a verified transaction this system acts on."""

    original_transaction_id: str
    product_id: str
    # None for a lifetime/non-renewing shape; every auto-renewable one has it.
    expires_at_ms: int | None
    #: Isol8 ``subscription_status`` this transaction implies.
    status: str
    #: Owner id the client stamped into ``appAccountToken`` at purchase, when
    #: present. Absent on notifications, which is why the GSI lookup exists.
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


@lru_cache(maxsize=1)
def _verifier():
    """Build the SignedDataVerifier once.

    Cached because constructing it parses Apple's root certificates, and because
    a per-request rebuild would make every verification pay that cost on the
    checkout path. Raises AppleIapNotConfigured when the feature is unwired, so
    a backend booted without Apple settings (local dev, unit tests, any env
    before the Paid Apps Agreement is active) still starts — the router turns
    that into a 503 rather than a 500.
    """
    # APPLE_IAP_PRODUCT_ID is required here, NOT merely checked per-transaction.
    # The product check in _subscription_from_transaction is written as
    # `if expected and ...`, so an empty setting would silently skip it — and
    # the rollout wires the bundle id and certs in a separate step from the
    # product id, so "verifier live, product id blank" is a reachable state. In
    # that window ANY validly-signed transaction for our bundle would be
    # accepted and granted a plan. Failing configuration instead keeps the
    # whole feature off until all three are present.
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

    environment = Environment.PRODUCTION if settings.APPLE_IAP_ENVIRONMENT == "production" else Environment.SANDBOX
    return SignedDataVerifier(
        root_certificates=root_certs,
        enable_online_checks=True,
        environment=environment,
        bundle_id=settings.APPLE_IAP_BUNDLE_ID,
        app_apple_id=settings.APPLE_IAP_APP_APPLE_ID or None,
    )


def _status_for(notification_type: str | None, *, revoked: bool = False) -> str:
    """Map an Apple event to the Isol8 ``subscription_status`` vocabulary.

    Kept as one function so the link path and the notification path can never
    disagree about what a given event means.
    """
    if revoked:
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
    if expected and product_id != expected:
        # A transaction for a product we do not sell must never grant a plan.
        raise AppleIapError(f"unexpected productId {product_id!r} (expected {expected!r})")

    # revocationDate is set on a refund/revoke even when the notification type
    # itself is something else, so it is checked independently.
    revoked = getattr(transaction, "revocationDate", None) is not None

    return AppleSubscription(
        original_transaction_id=str(original_transaction_id),
        product_id=product_id,
        expires_at_ms=getattr(transaction, "expiresDate", None),
        status=_status_for(notification_type, revoked=revoked),
        owner_hint=getattr(transaction, "appAccountToken", None) or None,
    )


def verify_transaction(signed_transaction: str) -> AppleSubscription:
    """Verify a client-supplied JWS transaction and project it.

    Raises AppleIapError when the signature, chain, bundle id, environment, or
    product id does not check out.
    """
    try:
        payload = _verifier().verify_and_decode_signed_transaction(signed_transaction)
    except AppleIapNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001 — any verifier failure is a rejection
        raise AppleIapError(f"signed transaction failed verification: {e}") from e
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

    # Refuse to move a live subscription between accounts. Without this, one
    # Apple ID could be walked across accounts to keep several of them paid.
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
        raise AppleIapError(f"no billing row for owner {owner_id}")
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
        notification = _verifier().verify_and_decode_notification(signed_payload)
    except AppleIapNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001
        raise AppleIapError(f"notification failed verification: {e}") from e

    notification_type = getattr(notification, "notificationType", None)
    notification_type = getattr(notification_type, "value", notification_type)
    notification_uuid = getattr(notification, "notificationUUID", None)
    signed_date = getattr(notification, "signedDate", None)

    if notification_uuid:
        dedup = await webhook_dedup.record_event_or_skip(event_id=f"apple:{notification_uuid}", source="apple-iap")
        if dedup is WebhookDedupResult.ALREADY_SEEN:
            logger.info("apple_iap.notification_replay uuid=%s", notification_uuid)
            return None

    data = getattr(notification, "data", None)
    signed_transaction = getattr(data, "signedTransactionInfo", None) if data else None
    if not signed_transaction:
        # CONSUMPTION_REQUEST, TEST, and friends carry no transaction. Nothing
        # to apply, and not an error.
        logger.info("apple_iap.notification_ignored type=%s", notification_type)
        return None

    try:
        transaction = _verifier().verify_and_decode_signed_transaction(signed_transaction)
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
    # entitlement backwards (or, worse, forwards after a cancellation).
    applied_at = row.get("apple_last_signed_date")
    if signed_date is not None and applied_at is not None and int(signed_date) < int(applied_at):
        logger.info(
            "apple_iap.notification_stale owner_id=%s type=%s signed_date=%s applied=%s",
            owner_id,
            notification_type,
            signed_date,
            applied_at,
        )
        return None

    await billing_repo.set_apple_subscription(
        owner_id,
        original_transaction_id=subscription.original_transaction_id,
        status=subscription.status,
        expires_at=subscription.expires_at_ms,
        signed_date=int(signed_date) if signed_date is not None else None,
    )
    logger.info(
        "apple_iap.notification_applied owner_id=%s type=%s status=%s",
        owner_id,
        notification_type,
        subscription.status,
    )
    return owner_id, subscription.status
