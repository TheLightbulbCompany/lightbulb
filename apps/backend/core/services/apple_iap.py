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

Two entry points. :func:`verify_transaction` is the purchase half — the app
sends the StoreKit transaction right after a purchase or restore, and
``link_purchase`` (behind ``POST /billing/apple/link``) calls it. :func:`apply_notification`
is the lifecycle half — App Store Server Notifications V2, the ONLY way we hear
about renewals, expiries, refunds and billing retries after that. The client is
not involved in the lifecycle half and may never open the app again.

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
from dataclasses import dataclass, replace
from functools import lru_cache
from pathlib import Path

from core.config import settings
from core.observability.metrics import put_metric
from core.repositories import billing_repo
from core.services import webhook_dedup
from core.services.webhook_dedup import WebhookDedupResult

logger = logging.getLogger(__name__)


class AppleIapError(Exception):
    """Verification failed, or the payload is not one we can act on."""


class AppleIapNotConfigured(AppleIapError):
    """IAP settings are absent — the feature is off in this environment."""


class StripeManaged(AppleIapError):
    """The workspace already pays through Stripe; a second subscription is refused."""


class CheckoutInProgress(AppleIapError):
    """A Stripe Checkout for this workspace is mid-flight; retry once it settles."""


def stripe_is_live(row: dict | None) -> bool:
    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    return bool(row) and row.get("stripe_subscription_status") in LIVE_SUBSCRIPTION_STATUSES


def apple_is_live(row: dict | None) -> bool:
    from core.services.provision_gate import LIVE_SUBSCRIPTION_STATUSES

    return (
        bool(row)
        and row.get("apple_subscription_status") in LIVE_SUBSCRIPTION_STATUSES
        and not billing_repo.apple_lapsed(row)
    )


#: Every product we sell, and the billing plan it buys. A code constant, not
#: config: the ids are fixed in App Store Connect forever, and a blank or
#: partial config was the one way the old single-id setting could grant a plan
#: for a product we never sold. Ranked in one subscription group in ASC
#: (Ultra > Workspace > Base) so Apple handles upgrades and downgrades.
PRODUCT_PLANS: dict[str, str] = {
    "company.thelightbulb.mobile.base.monthly": "base",
    "company.thelightbulb.mobile.plan.monthly": "standard",
    "company.thelightbulb.mobile.ultra.monthly": "ultra_pro",
}


#: What the paywall offers (T06: one plan). PRODUCT_PLANS still maps the
#: retired Base/Ultra products so existing purchases keep verifying.
SELLING_PRODUCTS: tuple[str, ...] = ("company.thelightbulb.mobile.plan.monthly",)


def owner_account_token(owner_id: str) -> str:
    """The ``appAccountToken`` a purchase for ``owner_id`` must carry.

    SHA-256 of the owner id shaped into a v5-style UUID (StoreKit accepts only a
    UUID there, and ``org_…`` ids are not one). Derived, never stored: the
    server recomputes and compares. The client never computes it — it gets it
    from POST /billing/apple/purchase-intent. Never change this computation: it
    would unbind every existing purchase.
    """
    h = hashlib.sha256(owner_id.encode()).hexdigest()[:32]
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
    #: Billing plan this product buys (PRODUCT_PLANS). Always from the VERIFIED
    #: productId, and required — a default would grant a plan nobody chose.
    plan: str
    #: Which Apple environment signed this. A "sandbox" grant is a real plan
    #: given away free, so it is logged at link time — see _verify_signed for
    #: why that is accepted rather than gated.
    environment: str = "sandbox"
    #: When Apple SIGNED this payload — not when the purchase happened. Re-fetching
    #: a transaction re-signs it, so a replayed old blob carries an old value and
    #: loses the watermark comparison to any notification that came after it.
    signed_date_ms: int | None = None
    #: ``appAccountToken`` stamped at purchase. Apple echoes it inside the signed
    #: payload, so it cannot be forged or stripped by a replaying account.
    owner_hint: str | None = None


# Notification types that GRANT or KEEP access. Apple sends SUBSCRIBED on the
# first purchase and on a resubscribe; DID_RENEW on every successful renewal.
# DID_CHANGE_RENEWAL_STATUS fires when auto-renew is toggled — access continues
# to the paid-through date either way, so it is deliberately NOT a revoke.
# RENEWAL_EXTENDED / RENEWAL_EXTENSION are Apple pushing the paid-through date
# out (a customer-service extension). The transaction carries the new date, so
# they grant for the same reason a renewal does.
_GRANTING_TYPES = frozenset({"SUBSCRIBED", "DID_RENEW", "OFFER_REDEEMED", "RENEWAL_EXTENDED", "RENEWAL_EXTENSION"})

# Types that END access immediately. REFUND and REVOKE are money-back events;
# EXPIRED is the paid-through date passing with no renewal.
#
# GRACE_PERIOD_EXPIRED is the close of the window the grace handling above
# opens, and it MUST be here. The read-time expiry check
# (``billing_repo.apple_lapsed``) would end access at the grace deadline anyway,
# but only this write moves the stored status, runs teardown and stops a row
# left "active" by a grace notification from lingering until some LATER
# notification is mapped to a revoke. Leaving this type unmapped meant it raised
# as unhandled, was acked as unactionable, and the subscriber kept paid access
# indefinitely after grace ran out.
_REVOKING_TYPES = frozenset({"EXPIRED", "REFUND", "REVOKE", "GRACE_PERIOD_EXPIRED"})

# Types that RESTORE access previously revoked. REFUND_REVERSED is Apple undoing
# a refund it had granted (a dispute resolved in the seller's favour) and the
# guidance is to restore the content. It needs its own set rather than joining
# _GRANTING_TYPES because the transaction can still carry a revocationDate from
# the original refund, and that check fires before any type mapping — so
# "granting" alone would still resolve to canceled.
_REINSTATING_TYPES = frozenset({"REFUND_REVERSED"})

# Billing retry: Apple could not charge and may still recover. Mapped to
# "past_due", matching the Stripe path — and "past_due" is NOT in
# provision_gate._ACTIVE_STATUSES, so it ends access.
#
# That is right for a plain retry and WRONG for the GRACE_PERIOD subtype, where
# Apple explicitly keeps the subscriber entitled while it retries. Those are
# separated at the call site by `in_grace_period`, because the difference is not
# in the type — it is in the subtype plus a deadline that lives in the renewal
# info. Cutting a grace-period subscriber off makes a paying customer lose
# access, which is the same class of bug as an Apple expiry cancelling a live
# Stripe subscription.
_GRACE_TYPES = frozenset({"DID_FAIL_TO_RENEW"})

# Types that change NOTHING about entitlement. Enumerated rather than left to
# fall through, because an unmapped type raises and is acked as unactionable —
# which looks identical in the logs to a type we have never seen. Two rounds of
# review found real bugs hiding in that noise (GRACE_PERIOD_EXPIRED and
# REFUND_REVERSED both sat there), so "considered and deliberately ignored" and
# "unknown" must be distinguishable.
#
# DID_CHANGE_RENEWAL_PREF/STATUS: a plan change or an auto-renew toggle. Access
#   runs to the paid-through date regardless; EXPIRED arrives later if it lapses.
#   Exception: DID_CHANGE_RENEWAL_PREF's UPGRADE subtype takes effect immediately
#   and is redirected to DID_RENEW by _effective_type before this set is even
#   checked -- an UPGRADE never actually lands here.
# PRICE_INCREASE / PRICE_CHANGE / REFUND_DECLINED: informational.
# CONSUMPTION_REQUEST / TEST: carry no transaction at all.
# ONE_TIME_CHARGE / EXTERNAL_PURCHASE_TOKEN / RESCIND_CONSENT / METADATA_UPDATE /
#   MIGRATION: other commerce shapes we do not sell.
_INFORMATIONAL_TYPES = frozenset(
    {
        "DID_CHANGE_RENEWAL_PREF",
        "DID_CHANGE_RENEWAL_STATUS",
        "PRICE_INCREASE",
        "PRICE_CHANGE",
        "REFUND_DECLINED",
        "CONSUMPTION_REQUEST",
        "TEST",
        "ONE_TIME_CHARGE",
        "EXTERNAL_PURCHASE_TOKEN",
        "RESCIND_CONSENT",
        "METADATA_UPDATE",
        "MIGRATION",
    }
)


def _effective_type(notification_type: str | None, subtype: str | None) -> str | None:
    """An UPGRADE takes effect immediately and carries the new product's transaction,
    so it grants like a renewal. Downgrades and crossgrades wait for the next DID_RENEW."""
    if notification_type == "DID_CHANGE_RENEWAL_PREF" and subtype == "UPGRADE":
        return "DID_RENEW"
    return notification_type


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
    if not settings.APPLE_IAP_BUNDLE_ID or not settings.APPLE_IAP_ROOT_CERTS_DIR:
        raise AppleIapNotConfigured(
            "APPLE_IAP_BUNDLE_ID / APPLE_IAP_ROOT_CERTS_DIR unset — Apple IAP is off in this environment"
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
    environment = {
        "production": Environment.PRODUCTION,
        "xcode": Environment.XCODE,
    }.get(resolved, Environment.SANDBOX)

    # Enforced HERE, not only in _verify_signed's Xcode fallback branch. Xcode
    # can also be reached as the CONFIGURED (primary) environment — e.g.
    # APPLE_IAP_ENVIRONMENT=xcode — in which case _verify_signed's primary
    # attempt builds this verifier directly and never reaches its own Xcode
    # gate. An XCODE verifier skips chain verification entirely (it decodes an
    # unsigned/HS256-signed blob as-is), so unguarded here it grants a plan for
    # a forged transaction regardless of APPLE_IAP_ACCEPT_XCODE.
    if environment == Environment.XCODE and not (settings.APPLE_IAP_ACCEPT_XCODE and settings.ENVIRONMENT != "prod"):
        raise AppleIapNotConfigured("Xcode StoreKit Testing is not enabled in this environment")

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


def _grace_period_expiry(data) -> int | None:
    """The grace deadline from a notification's renewal info, if it is still ahead.

    Separate from the transaction because Apple puts it there: the transaction
    only knows the paid-through date, which has already elapsed during grace.
    Best effort — if the renewal info is absent or will not decode, the caller
    falls back to the ordinary mapping (``past_due``), which is the conservative
    answer rather than an indefinite free extension.
    """
    signed_renewal_info = getattr(data, "signedRenewalInfo", None) if data else None
    if not signed_renewal_info:
        return None
    try:
        renewal, _env = _verify_signed("verify_and_decode_renewal_info", signed_renewal_info)
    except Exception:  # noqa: BLE001 — an undecodable renewal info is not a grant
        logger.warning("apple_iap.renewal_info_undecodable — grace period not honoured")
        return None
    expires = getattr(renewal, "gracePeriodExpiresDate", None)
    if expires is None:
        return None
    if int(expires) <= int(time.time() * 1000):
        # Grace already over; let the normal mapping cancel.
        return None
    return int(expires)


async def _reconcile_active_subscription(owner_id: str, *, subscription: AppleSubscription) -> None:
    """An active Apple subscription: make the workspace match it, on EVERY such event.

    Mirrors what a Stripe checkout does (stripe_webhook_handlers.handle_subscription_state):
    the plan, the OpenRouter limit, owner facts, then a reconcile.

    ``provider_choice`` is deliberately NOT written. An unset provider on a paid plan is
    the capped managed default (trial_cap.is_managed_capped) — what a Stripe buyer gets.
    Writing ``openrouter_managed`` sent an Apple-only owner (no Stripe customer, so no
    Metronome contract) to ``credits_required`` the moment they paid.

    Raises when the dispatch fails, so the caller (link → 5xx, notification → Apple
    redelivers) runs it again; every step is idempotent.
    """
    if subscription.status != "active":
        return
    row = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if not row:
        return

    from core.dynamodb import utc_now_iso
    from core.services.owner_facts import write_owner_facts
    from core.services.provision_dispatch import dispatch_reconcile

    await billing_repo.set_plan(owner_id, plan=subscription.plan, now_iso=utc_now_iso())
    if settings.OPENROUTER_WORKSPACE_ID:
        try:
            from core.services.openrouter_keys import relimit_owner_key

            await relimit_owner_key(owner_id)
        except Exception:
            logger.exception("apple_iap.relimit_failed owner_id=%s", owner_id)
    await write_owner_facts(owner_id, account_overrides={"subscription_status": "active", "plan": subscription.plan})
    if not await dispatch_reconcile(owner_id):
        raise RuntimeError(f"reconcile dispatch failed for owner {owner_id}")


def _revoked_key(original_transaction_id: str) -> str:
    return f"apple_revoked:{original_transaction_id}"


#: Margin on top of the transaction's own paid-through date. Clock skew between
#: Apple and us is small, but a tombstone that expires even slightly early
#: reopens the replay it exists to close.
_TOMBSTONE_MARGIN_SECONDS = 7 * 24 * 60 * 60


async def _mark_revoked(original_transaction_id: str, expires_at_ms: int | None = None) -> None:
    """Remember that this transaction was revoked while nothing was linked to it.

    Stored in the webhook-dedup table because the shape is identical: a keyed
    marker with a TTL.

    The TTL must outlive the transaction's PAID-THROUGH date, not the table's
    default. The tombstone stops a replay of the pre-revocation JWS, and that
    JWS stays grantable until its own expiresDate passes (after which
    ``_status_for`` refuses it regardless). A monthly StoreKit period can run 31
    days, so the generic 30-day retention could drop the marker while the
    original blob was still live — leaving exactly the gap this closes.
    """
    ttl = None
    if expires_at_ms is not None:
        remaining = int(expires_at_ms / 1000) - int(time.time()) + _TOMBSTONE_MARGIN_SECONDS
        ttl = max(remaining, 0) or None
    await webhook_dedup.record_event_or_skip(
        _revoked_key(original_transaction_id), source="apple-iap-revoked", ttl_seconds=ttl
    )


async def _clear_revoked(original_transaction_id: str, *, best_effort: bool = True) -> None:
    """Drop the tombstone when Apple says this subscription is live again.

    A resubscribe reuses the SAME originalTransactionId, so without this a
    refunded owner who later genuinely pays again would be refused forever.

    ``best_effort`` only holds where the row itself was updated: entitlement is
    already correct there and a lingering tombstone costs at most one refused
    re-link. On the UNLINKED path it must be False — the tombstone IS the
    entitlement decision, so swallowing a failed delete acks the notification,
    dedups it permanently, and leaves a paying customer locked out until the TTL
    expires. Letting it raise rolls back the dedup claim so Apple retries.
    """
    try:
        await webhook_dedup.delete_event(_revoked_key(original_transaction_id))
    except Exception:
        logger.exception("apple_iap.revoked_clear_failed original_transaction_id=%s", original_transaction_id)
        if not best_effort:
            raise


def _verify_signed(method: str, blob: str) -> tuple[object, str]:
    """Verify a JWS against the configured environment, then the other one.

    Returns ``(payload, environment_name)`` — the environment MATTERS to the
    caller and must not be dropped, because a sandbox purchase costs nothing.

    A SignedDataVerifier is bound to ONE environment and rejects the other's
    payloads as unverifiable, which would be fine if traffic were segregated by
    deployment — and it is not. **App Review tests every submission with SANDBOX
    transactions, sent to the PRODUCTION backend.** Pinning production to
    `production` alone therefore rejects the reviewer's own purchase and fails
    the submission this whole feature exists to pass.

    Verifying both is not itself a weakening — each verifier still enforces the
    full chain, bundle id, and app id.

    **A sandbox transaction does grant a real plan here, and that is a
    deliberate decision, not an oversight.** A sandbox purchase costs the buyer
    nothing, and TestFlight builds run sandbox StoreKit against the PRODUCTION
    api — and the TestFlight link is PUBLIC, so anyone can grant themselves a
    paid plan this way. What bounds it is expiry, not distribution: a sandbox
    subscription renews on an accelerated clock and stops after a few
    renewals, its lifecycle notifications go to the SANDBOX notification URL
    rather than ours, and ``billing_repo.apple_lapsed`` makes an Apple "active"
    whose ``apple_expires_at`` has passed read as not live. So a free grant
    lasts one sandbox period (minutes to an hour), not forever. Gating the
    grant instead (an allowlist, or a flag opened only for the review window)
    was rejected because forgetting it fails App Review's own purchase, which
    is a rejection. ``environment`` is carried on the returned subscription and
    logged at link time so sandbox grants stay visible.

    A fallback verifier that cannot be BUILT (production needs
    APPLE_IAP_APP_APPLE_ID, sandbox does not) is skipped rather than raised, or
    a dev backend with no app id configured could never verify anything.
    """
    primary = settings.APPLE_IAP_ENVIRONMENT or "sandbox"
    fallback = "sandbox" if primary == "production" else "production"

    # Not configured at all is a 503, and only the PRIMARY gets to say so.
    try:
        return getattr(_verifier(primary), method)(blob), primary
    except AppleIapNotConfigured:
        raise
    except Exception as e:  # noqa: BLE001 — any verifier failure is a rejection
        first_error = e

    # Reached only when the configured environment rejected the payload, so the
    # steady-state path builds and calls exactly ONE verifier.
    other = _fallback_verifier(fallback)
    if other is not None:
        try:
            return getattr(other, method)(blob), fallback
        except Exception:  # noqa: BLE001 — report the primary's error, not this one
            pass

    # Xcode StoreKit Testing: dev acceptance without Apple's sandbox. The library
    # skips the chain check for Environment.XCODE (these are signed by a local
    # Xcode key), so this path is a free plan for anyone who can reach it — hence
    # BOTH conditions, and prod refuses even a misconfigured flag.
    if settings.APPLE_IAP_ACCEPT_XCODE and settings.ENVIRONMENT != "prod":
        xcode = _fallback_verifier("xcode")
        if xcode is not None:
            try:
                return getattr(xcode, method)(blob), "xcode"
            except Exception:  # noqa: BLE001 — report the primary's error
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
    in_grace_period: bool = False,
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
    # A REFUND_REVERSED transaction can still carry the revocationDate from the
    # refund Apple has just undone, so this check has to yield to it or the
    # reinstatement can never take effect.
    if revoked and notification_type not in _REINSTATING_TYPES:
        return "canceled"
    # An elapsed paid-through date beats every other signal, including a
    # granting notification type — Apple can only be telling us about a period
    # that has since ended.
    if expires_at_ms is not None:
        current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        if int(expires_at_ms) <= current_ms:
            return "canceled"
    # A billing grace period is a GRANTING state, even though its notification
    # type is DID_FAIL_TO_RENEW: Apple is still retrying the card and expects
    # the subscriber to keep their access until the grace deadline. Mapping it
    # to "past_due" cut off a customer Apple still considered entitled —
    # "past_due" is not in provision_gate._ACTIVE_STATUSES.
    if (
        in_grace_period
        or notification_type is None
        or notification_type in _GRANTING_TYPES
        or notification_type in _REINSTATING_TYPES
    ):
        return "active"
    if notification_type in _REVOKING_TYPES:
        return "canceled"
    if notification_type in _GRACE_TYPES:
        return "past_due"
    # An unmapped type must not silently change entitlement. Callers treat
    # this as "no write" rather than guessing.
    raise AppleIapError(f"unhandled notification type: {notification_type}")


def _subscription_from_transaction(
    transaction,
    notification_type: str | None,
    environment: str = "sandbox",
    *,
    grace_period_expires_ms: int | None = None,
) -> AppleSubscription:
    """Project a verified transaction payload onto :class:`AppleSubscription`."""
    original_transaction_id = getattr(transaction, "originalTransactionId", None)
    if not original_transaction_id:
        raise AppleIapError("verified transaction carries no originalTransactionId")

    product_id = getattr(transaction, "productId", None) or ""
    plan = PRODUCT_PLANS.get(product_id)
    if plan is None:
        # A transaction for a product we do not sell must never grant a plan.
        raise AppleIapError(f"unexpected productId {product_id!r}")
    ownership = getattr(transaction, "inAppOwnershipType", None)
    if getattr(ownership, "value", ownership) == "FAMILY_SHARED":
        # One workspace per purchase; Family Sharing is off in ASC and refused here too.
        raise AppleIapError("family-shared purchases cannot be linked to a workspace")

    # revocationDate is set on a refund/revoke even when the notification type
    # itself is something else, so it is checked independently.
    revoked = getattr(transaction, "revocationDate", None) is not None
    expires_at_ms = getattr(transaction, "expiresDate", None)
    # In a billing grace period the paid-through date has already passed, so the
    # grace deadline IS the effective expiry — entitlement runs to it, and the
    # ordinary elapsed-expiry check then ends access when it lapses without any
    # special casing.
    if grace_period_expires_ms is not None:
        expires_at_ms = grace_period_expires_ms

    return AppleSubscription(
        original_transaction_id=str(original_transaction_id),
        product_id=product_id,
        expires_at_ms=expires_at_ms,
        status=_status_for(
            notification_type,
            revoked=revoked,
            expires_at_ms=expires_at_ms,
            in_grace_period=grace_period_expires_ms is not None,
        ),
        environment=environment,
        signed_date_ms=getattr(transaction, "signedDate", None),
        plan=plan,
        owner_hint=getattr(transaction, "appAccountToken", None) or None,
    )


def verify_transaction(signed_transaction: str) -> AppleSubscription:
    """Verify a client-supplied StoreKit 2 transaction JWS and project it."""
    payload, environment = _verify_signed("verify_and_decode_signed_transaction", signed_transaction)
    return _subscription_from_transaction(payload, None, environment)


def _stripe_managed(owner_id: str, subscription: AppleSubscription) -> StripeManaged:
    """Apple has charged, but Stripe already bills the workspace: signal support to refund."""
    put_metric("apple_iap.link_refused_stripe_managed")
    logger.warning(
        "apple_iap.link_refused_stripe_managed owner_id=%s original_transaction_id=%s",
        owner_id,
        subscription.original_transaction_id,
    )
    return StripeManaged("stripe_managed")


async def link_purchase(*, owner_id: str, signed_transaction: str) -> AppleSubscription:
    """Bind a freshly-verified purchase to ``owner_id``.

    Called from the app right after StoreKit reports success. Idempotent: the
    same transaction replayed writes the same row, so a client retrying after a
    dropped response cannot double-grant or corrupt anything.
    """
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
        raise AppleIapError("this Apple purchase belongs to a different workspace")

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

    # A revocation that arrived while nothing was linked leaves no row and so no
    # watermark for the write below to lose against. Without this check, an
    # owner refunded before linking could link afterwards and be handed the
    # subscription their money was returned for.
    if await webhook_dedup.has_event(_revoked_key(subscription.original_transaction_id)):
        logger.warning(
            "apple_iap.link_refused_revoked owner_id=%s original_transaction_id=%s",
            owner_id,
            subscription.original_transaction_id,
        )
        raise AppleIapError("this Apple transaction was refunded or revoked and cannot be linked")

    before = await billing_repo.get_by_owner_id(owner_id, consistent_read=True)
    if stripe_is_live(before):
        raise _stripe_managed(owner_id, subscription)
    # Apple has ALREADY charged by the time this runs, so an open web Checkout is
    # expired rather than refused; only one that completed into a live Stripe
    # subscription makes this purchase the duplicate (support refunds it).
    from core.services.billing_service import BillingServiceError, retire_pending_checkout_for_apple

    try:
        await retire_pending_checkout_for_apple(owner_id, before)
    except BillingServiceError as e:
        if str(e) == "stripe_managed":
            raise _stripe_managed(owner_id, subscription) from e
        raise CheckoutInProgress(str(e)) from e

    written = await billing_repo.set_apple_subscription(
        owner_id,
        original_transaction_id=subscription.original_transaction_id,
        status=subscription.status,
        expires_at=subscription.expires_at_ms,
        # The JWS signing time, so the LINK path shares the notification
        # watermark instead of bypassing it. Without this, an owner who was
        # refunded could re-POST the transaction they still hold — signature and
        # account token both still valid, `revocationDate` absent because the
        # blob predates the refund — and flip their own row back to active.
        # StoreKit re-signs on every fetch, so a genuine restore carries a fresh
        # value and only a replayed old blob loses the comparison.
        signed_date=subscription.signed_date_ms,
    )
    if written is None:
        # The write was refused. That is not automatically a failure: it also
        # happens when a server notification for this same subscription already
        # landed with a later signedDate, which is the ordinary race between the
        # client's link call and Apple's SUBSCRIBED webhook.
        current = await billing_repo.get_by_owner_id(owner_id)
        if current and current.get("apple_original_transaction_id") == subscription.original_transaction_id:
            # Apple's own notification is authoritative over the client's
            # snapshot, so report ITS status rather than the JWS's. A refunded
            # owner replaying an old blob lands here and is told "canceled".
            authoritative = current.get("apple_subscription_status") or subscription.status
            logger.info(
                "apple_iap.link_superseded owner_id=%s original_transaction_id=%s status=%s",
                owner_id,
                subscription.original_transaction_id,
                authoritative,
            )
            return replace(subscription, status=authoritative)
        # No billing row, or the row is bound to a different Apple transaction.
        raise AppleIapError(f"could not link this transaction to owner {owner_id}")
    await _reconcile_active_subscription(owner_id, subscription=subscription)
    logger.info(
        # environment is logged because a "sandbox" grant is a REAL plan given
        # away for free (bounded by its expiry — see _verify_signed).
        "apple_iap.linked owner_id=%s original_transaction_id=%s status=%s environment=%s",
        owner_id,
        subscription.original_transaction_id,
        subscription.status,
        subscription.environment,
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
        notification, _environment = _verify_signed("verify_and_decode_notification", signed_payload)
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
    subtype = getattr(notification, "subtype", None)
    notification_type = _effective_type(notification_type, getattr(subtype, "value", subtype))

    if notification_type in _INFORMATIONAL_TYPES:
        # Deliberately no entitlement change — see _INFORMATIONAL_TYPES. Handled
        # here rather than by falling through to the mapper so it is logged as a
        # decision instead of as an unhandled type.
        logger.info("apple_iap.notification_informational type=%s", notification_type)
        return None

    data = getattr(notification, "data", None)
    signed_transaction = getattr(data, "signedTransactionInfo", None) if data else None
    if not signed_transaction:
        # CONSUMPTION_REQUEST, TEST, and friends carry no transaction. Nothing
        # to apply, and not an error.
        logger.info("apple_iap.notification_ignored type=%s", notification_type)
        return None

    try:
        transaction, _tx_environment = _verify_signed("verify_and_decode_signed_transaction", signed_transaction)
    except Exception as e:  # noqa: BLE001
        raise AppleIapError(f"notification transaction failed verification: {e}") from e

    # Billing GRACE PERIOD. Apple keeps the subscriber entitled while it retries
    # the card, and says to keep serving them — but the transaction's own
    # expiresDate has already passed by then (that is why a renewal was
    # attempted), so both the type mapping and the elapsed-expiry check would
    # otherwise cancel a customer who is still a customer. The grace deadline
    # lives in the renewal info, not the transaction, so it has to be decoded
    # separately and substituted as the effective expiry.
    grace_until = None
    if notification_type == "DID_FAIL_TO_RENEW" and getattr(notification, "subtype", None) == "GRACE_PERIOD":
        grace_until = _grace_period_expiry(data)

    try:
        subscription = _subscription_from_transaction(
            transaction, notification_type, grace_period_expires_ms=grace_until
        )
    except AppleIapError as e:
        # An unmapped type or a foreign product is not a verification failure:
        # answering 400 would make Apple retry it forever. Log and ack.
        logger.info("apple_iap.notification_unactionable type=%s: %s", notification_type, e)
        return None

    row = await billing_repo.get_by_apple_original_transaction_id(subscription.original_transaction_id)
    if row is None:
        # The purchase was never linked — the app died between StoreKit
        # returning and POST /billing/apple/link landing. The client's restore
        # path recovers this; there is no row to update now.
        #
        # A REVOKING one cannot simply be dropped, though. The owner still holds
        # the pre-revocation JWS, and with no row there is no watermark for a
        # later link write to lose against — so they could link afterwards and
        # be granted the subscription they were just refunded for. Leave a
        # tombstone, which ``link_purchase`` checks before it writes. See
        # `_mark_revoked`.
        if subscription.status == "canceled":
            await _mark_revoked(subscription.original_transaction_id, subscription.expires_at_ms)
            # The GSI is eventually consistent, so "no row" can simply mean the
            # link that just happened has not propagated. Acking here would drop
            # the revocation permanently — Apple does not redeliver an accepted
            # notification — and leave a refunded owner active, because nothing
            # reconciles the tombstone against a row that appears afterwards.
            # Raising rolls back the dedup claim so the retry (seconds later)
            # finds the row and applies it properly. A purchase that genuinely
            # was never linked keeps the tombstone and simply exhausts Apple's
            # retries, which is the outcome it already had.
            raise AppleIapError(
                f"no billing row yet for {subscription.original_transaction_id} — "
                "revocation not applied, retry expected"
            )
        else:
            # A resubscribe reuses the SAME originalTransactionId, and if the
            # original purchase was never linked this branch is where its
            # SUBSCRIBED lands too. Without clearing here, the tombstone from
            # the earlier refund outlives the new purchase for up to the
            # 30-day TTL. `_clear_revoked` further down only runs once a row exists,
            # which by definition it does not here.
            await _clear_revoked(subscription.original_transaction_id, best_effort=False)
        logger.warning(
            "apple_iap.notification_unlinked original_transaction_id=%s type=%s status=%s",
            subscription.original_transaction_id,
            notification_type,
            subscription.status,
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
    if subscription.status == "active":
        # Every active notification reconciles, redeliveries included (see the helper).
        await _reconcile_active_subscription(owner_id, subscription=subscription)

    if subscription.status != "canceled":
        # Apple says this subscription is live again — a resubscribe reuses the
        # SAME originalTransactionId, so a tombstone left by an earlier refund
        # would otherwise refuse the owner's genuine new purchase forever.
        await _clear_revoked(subscription.original_transaction_id)
    else:
        # Apple ended it. Whether that ends the ENTITLEMENT depends on the other
        # provider, so the decision is deferred to the shared derived status
        # rather than made from this notification. Until this existed the Apple
        # path wrote "canceled" and stopped: an Apple-only owner's container
        # kept running after their last entitlement ended, and its agents kept
        # spending on inference indefinitely.
        from core.services.entitlement_teardown import teardown_if_unentitled

        await teardown_if_unentitled(owner_id, trigger=f"apple_{(notification_type or 'unknown').lower()}")
    logger.info(
        "apple_iap.notification_applied owner_id=%s type=%s status=%s",
        owner_id,
        notification_type,
        subscription.status,
    )
    return owner_id, subscription.status
