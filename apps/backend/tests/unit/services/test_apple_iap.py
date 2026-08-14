"""Apple IAP entitlement mapping, linking, and notification handling.

The JWS verifier itself is Apple's library and is not re-tested here — what IS
tested is everything we built around it: that an unverifiable payload never
reaches a write, that event types map to the right ``subscription_status``, that
a subscription cannot be walked between accounts, and that Apple's retry /
out-of-order delivery cannot corrupt entitlement.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.services import apple_iap
from core.services.apple_iap import AppleIapError, AppleIapNotConfigured
from core.services.webhook_dedup import WebhookDedupResult


def _transaction(**overrides):
    base = dict(
        originalTransactionId="2000000000000001",
        productId="co.isol8.mobile.plan.monthly",
        expiresDate=1_800_000_000_000,
        revocationDate=None,
        appAccountToken=None,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _notification(**overrides):
    base = dict(
        notificationType="DID_RENEW",
        notificationUUID="uuid-1",
        signedDate=1_700_000_000_000,
        data=SimpleNamespace(signedTransactionInfo="signed-tx"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _product_id(monkeypatch):
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_PRODUCT_ID", "co.isol8.mobile.plan.monthly")


# ---------------------------------------------------------------------------
# Status mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "notification_type,expected",
    [
        ("SUBSCRIBED", "active"),
        ("DID_RENEW", "active"),
        ("OFFER_REDEEMED", "active"),
        ("EXPIRED", "canceled"),
        ("REFUND", "canceled"),
        ("REVOKE", "canceled"),
        # Billing retry: recoverable, so it maps to the same status the Stripe
        # path uses for a failed charge rather than cutting access immediately.
        ("DID_FAIL_TO_RENEW", "past_due"),
    ],
)
def test_status_mapping(notification_type, expected):
    assert apple_iap._status_for(notification_type) == expected


def test_unmapped_notification_type_raises_rather_than_guessing():
    with pytest.raises(AppleIapError):
        apple_iap._status_for("SOME_FUTURE_APPLE_EVENT")


def test_revocation_date_revokes_regardless_of_type():
    """A refunded transaction must lose access even when the notification type
    itself is a granting one — the money has been returned."""
    subscription = apple_iap._subscription_from_transaction(_transaction(revocationDate=1_700_000_000_000), "DID_RENEW")
    assert subscription.status == "canceled"


def test_foreign_product_id_is_rejected():
    """A valid transaction for a product we do not sell must never grant a plan."""
    with pytest.raises(AppleIapError, match="unexpected productId"):
        apple_iap._subscription_from_transaction(_transaction(productId="com.someone.else.pro"), "SUBSCRIBED")


def test_transaction_without_original_id_is_rejected():
    with pytest.raises(AppleIapError):
        apple_iap._subscription_from_transaction(_transaction(originalTransactionId=None), "SUBSCRIBED")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def test_unconfigured_environment_raises_not_configured(monkeypatch):
    """The feature is off until the Paid Apps Agreement is active. That must be
    a distinguishable error so the router answers 503, not 500."""
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_BUNDLE_ID", "")
    apple_iap._verifier.cache_clear()
    with pytest.raises(AppleIapNotConfigured):
        apple_iap._verifier()
    apple_iap._verifier.cache_clear()


# ---------------------------------------------------------------------------
# link_purchase
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_purchase_writes_entitlement():
    verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
        patch.object(
            apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})
        ) as set_sub,
    ):
        result = await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    assert result.status == "active"
    set_sub.assert_awaited_once()
    assert set_sub.await_args.kwargs["original_transaction_id"] == "2000000000000001"


@pytest.mark.asyncio
async def test_link_purchase_rejects_an_unverifiable_transaction():
    """The security boundary: a payload the verifier rejects must never reach a
    write. A forged one that did would hand out a paid plan for free."""

    def _boom(_):
        raise ValueError("bad signature")

    verifier = SimpleNamespace(verify_and_decode_signed_transaction=_boom)
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        with pytest.raises(AppleIapError):
            await apple_iap.link_purchase(owner_id="user_1", signed_transaction="forged")

    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_purchase_refuses_to_move_a_subscription_between_accounts():
    """Without this, one Apple ID could be walked across accounts to keep
    several of them paid off a single subscription."""
    verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_OTHER"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        with pytest.raises(AppleIapError, match="already linked"):
            await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_purchase_is_idempotent_for_the_same_owner():
    """A client retrying after a dropped response must not be an error."""
    verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        result = await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    assert result.status == "active"


# ---------------------------------------------------------------------------
# apply_notification
# ---------------------------------------------------------------------------


def _notification_verifier(notification, transaction=None):
    return SimpleNamespace(
        verify_and_decode_notification=lambda _: notification,
        verify_and_decode_signed_transaction=lambda _: transaction or _transaction(),
    )


@pytest.mark.asyncio
async def test_notification_applies_status_to_the_linked_owner():
    with (
        patch.object(apple_iap, "_verifier", return_value=_notification_verifier(_notification())),
        patch.object(
            apple_iap.webhook_dedup,
            "record_event_or_skip",
            AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        result = await apple_iap.apply_notification("signed")

    assert result == ("user_1", "active")
    set_sub.assert_awaited_once()


@pytest.mark.asyncio
async def test_notification_replay_is_skipped():
    """Apple retries for up to three days; the same uuid must not be reapplied."""
    with (
        patch.object(apple_iap, "_verifier", return_value=_notification_verifier(_notification())),
        patch.object(
            apple_iap.webhook_dedup,
            "record_event_or_skip",
            AsyncMock(return_value=WebhookDedupResult.ALREADY_SEEN),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        result = await apple_iap.apply_notification("signed")

    assert result is None
    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_stale_notification_does_not_move_entitlement_backwards():
    """Apple does not promise ordering. A retried DID_RENEW landing after an
    EXPIRED must not restore access that legitimately ended.

    Staleness is now enforced as a CONDITION on the write, not a check the
    caller makes first: the caller's row comes from an eventually-consistent
    GSI, so two overlapping notifications could both read an old watermark and
    both believe they were newest. A rejected write returns None, and the
    caller must treat that as "not applied" rather than reporting success.
    """
    stale = _notification(notificationType="DID_RENEW", signedDate=1_000)
    with (
        patch.object(apple_iap, "_verifier", return_value=_notification_verifier(stale)),
        patch.object(
            apple_iap.webhook_dedup,
            "record_event_or_skip",
            AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1", "apple_last_signed_date": 9_000}),
        ),
        # The conditional write rejects it and reports nothing written.
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value=None)) as set_sub,
    ):
        result = await apple_iap.apply_notification("signed")

    assert result is None
    # The write IS attempted — that is where the comparison now happens — and
    # the signed date is threaded through so the condition has something to
    # compare against.
    set_sub.assert_awaited_once()
    assert set_sub.await_args.kwargs["signed_date"] == 1_000


@pytest.mark.asyncio
async def test_notification_for_an_unlinked_subscription_is_acked_not_applied():
    """The app died between StoreKit returning and the link call landing. There
    is nothing to update; the client's restore path recovers it."""
    with (
        patch.object(apple_iap, "_verifier", return_value=_notification_verifier(_notification())),
        patch.object(
            apple_iap.webhook_dedup,
            "record_event_or_skip",
            AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        result = await apple_iap.apply_notification("signed")

    assert result is None
    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_notification_without_a_transaction_is_ignored():
    """CONSUMPTION_REQUEST / TEST carry no transaction — not an error."""
    empty = _notification(notificationType="TEST", data=None)
    with (
        patch.object(apple_iap, "_verifier", return_value=_notification_verifier(empty)),
        patch.object(
            apple_iap.webhook_dedup,
            "record_event_or_skip",
            AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        result = await apple_iap.apply_notification("signed")

    assert result is None
    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_unverifiable_notification_raises_so_apple_retries():
    """Swallowing a bad-signature notification would also swallow a real
    renewal during a misconfiguration, cutting off a paying customer."""

    def _boom(_):
        raise ValueError("bad chain")

    verifier = SimpleNamespace(verify_and_decode_notification=_boom)
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        with pytest.raises(AppleIapError):
            await apple_iap.apply_notification("forged")

    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_unactionable_notification_is_acked_not_retried():
    """An unmapped type is a permanent condition — answering 400 would make
    Apple retry it for three days."""
    unknown = _notification(notificationType="SOME_FUTURE_APPLE_EVENT")
    with (
        patch.object(apple_iap, "_verifier", return_value=_notification_verifier(unknown)),
        patch.object(
            apple_iap.webhook_dedup,
            "record_event_or_skip",
            AsyncMock(return_value=WebhookDedupResult.RECORDED),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        result = await apple_iap.apply_notification("signed")

    assert result is None
    set_sub.assert_not_awaited()


# ---------------------------------------------------------------------------
# App-to-web checkout (the paywall's link-out half)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mobile_web_checkout_uses_the_app_optimized_origin_context(monkeypatch):
    """`origin_context="mobile_app"` is what makes Checkout render the
    app-to-web page rather than the desktop one, and is why Apple Pay shows up
    on a phone. Losing it silently degrades the whole point of the link-out."""
    from core.services import billing_service

    monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat")
    captured = {}

    async def _fake_build(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(id="cs_1", url="https://checkout.stripe.com/c/pay/cs_1")

    monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_build)
    # The slot claim + pending marker are real repo writes; this test is about
    # the session shape, so stub them. Their own behaviour is covered below.
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value="tok"))
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())

    session = await billing_service.create_mobile_web_checkout(owner_id="user_1")

    assert session.url.startswith("https://checkout.stripe.com/")
    assert captured["origin_context"] == "mobile_app"
    assert captured["line_items"] == [{"price": "price_flat", "quantity": 1}]
    # Promo field is deliberately off on a phone keyboard.
    assert captured["allow_promotion_codes"] is False
    # Universal link back into the app, not the settings panel.
    assert "/checkout_redirect/success" in captured["success_url"]


@pytest.mark.asyncio
async def test_mobile_web_checkout_requires_a_configured_price(monkeypatch):
    from core.services import billing_service
    from core.services.billing_service import BillingServiceError

    monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "")
    with pytest.raises(BillingServiceError):
        await billing_service.create_mobile_web_checkout(owner_id="user_1")


def test_web_checkout_omits_origin_context_for_web_callers():
    """Web checkouts must keep their existing session shape. `origin_context`
    participates in Stripe's idempotency body-compare, so adding it
    unconditionally would change every web session's fingerprint."""
    import inspect

    from core.services import billing_service

    src = inspect.getsource(billing_service._build_subscription_checkout)
    # Set only under the guard, never in the base kwargs dict.
    assert "if origin_context:" in src
    assert 'kwargs["origin_context"] = origin_context' in src


# ---------------------------------------------------------------------------
# Org fence (PR review finding: an org routed here gets CHARGED and then fails)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_link_purchase_refuses_org_owners():
    """The Apple product is the PERSONAL flat plan; orgs pay base + per-seat.

    An org reaching link_purchase has already been charged by Apple, and the
    write would fail anyway — a team_billing_required org has no billing row by
    construction, so set_apple_subscription returns None. Refuse before the
    verifier, so no charge can be recorded against the wrong plan shape.
    """
    with (
        patch.object(apple_iap, "_verifier") as verifier,
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        with pytest.raises(AppleIapError, match="per seat"):
            await apple_iap.link_purchase(owner_id="org_abc", signed_transaction="jws")

    verifier.assert_not_called()
    set_sub.assert_not_awaited()


def test_product_id_must_be_configured_before_the_verifier_is_live(monkeypatch):
    """The per-transaction product check is `if expected and ...`, so an empty
    APPLE_IAP_PRODUCT_ID would skip it silently. Rollout sets the bundle id and
    certs in a separate step from the product id, so "verifier live, product id
    blank" is reachable — and in that window any validly-signed transaction for
    our bundle would be granted a plan. Configuration must fail instead.
    """
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_BUNDLE_ID", "co.isol8.mobile")
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ROOT_CERTS_DIR", "/tmp/certs")
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_PRODUCT_ID", "")
    apple_iap._verifier.cache_clear()
    try:
        with pytest.raises(AppleIapNotConfigured, match="APPLE_IAP_PRODUCT_ID"):
            apple_iap._verifier()
    finally:
        apple_iap._verifier.cache_clear()


@pytest.mark.asyncio
async def test_link_rejects_a_transaction_purchased_by_another_account():
    """appAccountToken is stamped by the client at purchase and echoed inside
    the SIGNED payload, so a mismatch is proof the buyer was someone else — the
    strongest binding available, and unlike the GSI lookup it cannot race."""
    verifier = SimpleNamespace(
        verify_and_decode_signed_transaction=lambda _: _transaction(appAccountToken="user_OTHER")
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        with pytest.raises(AppleIapError, match="different account"):
            await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_fails_loudly_when_the_conditional_write_refuses():
    """A refused write means the row is already bound to another transaction.
    Returning success there would tell a customer they are subscribed when
    nothing was recorded."""
    verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value=None)),
    ):
        with pytest.raises(AppleIapError, match="could not link"):
            await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")


@pytest.mark.asyncio
async def test_mobile_web_checkout_refuses_a_second_concurrent_session(monkeypatch):
    """Two completable sessions means two Stripe subscriptions.

    The idempotency key buckets to 5 minutes while a session stays completable
    for ~31, and is_trial_blocked reads a local row that says nothing until a
    webhook lands — so "the app is the only caller" does not prevent an
    authenticated caller from minting a second one across a bucket boundary.
    """
    from core.services import billing_service

    monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat")
    # Slot already held by an in-flight checkout.
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value=None))
    built = AsyncMock()
    monkeypatch.setattr(billing_service, "_build_subscription_checkout", built)

    with pytest.raises(billing_service.BillingServiceError, match="checkout_in_progress"):
        await billing_service.create_mobile_web_checkout(owner_id="user_1")

    built.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_web_checkout_frees_the_slot_when_session_creation_fails(monkeypatch):
    """A failed create must not wedge the owner out of checkout for the claim TTL."""
    from core.services import billing_service

    monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat")
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value="tok"))
    release = AsyncMock()
    monkeypatch.setattr(billing_service.billing_repo, "release_checkout_slot", release)

    async def _boom(**_):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(billing_service, "_build_subscription_checkout", _boom)

    with pytest.raises(RuntimeError):
        await billing_service.create_mobile_web_checkout(owner_id="user_1")

    release.assert_awaited_once()
    assert release.await_args.kwargs["token"] == "tok"
