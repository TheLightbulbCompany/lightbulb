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
        # Every purchase our client makes carries this — link_purchase requires
        # it, because it is the only cross-account fence that is not a race.
        # Derived, not pasted, so the fixture cannot drift from the algorithm.
        appAccountToken=apple_iap.owner_account_token("user_1"),
        # When Apple SIGNED this blob. StoreKit re-signs on every fetch, so a
        # genuine restore is recent and only a replayed old copy is stale.
        signedDate=1_700_000_500_000,
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
def _no_pending_session(monkeypatch):
    """No open Checkout Session on the row — the ordinary starting state.

    create_mobile_web_checkout reads this BEFORE claiming a slot: recording a
    pending session prevents a second one only if something checks for it.
    """
    from core.services import billing_service

    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": "user_1"}))


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

    _no_pending_session(monkeypatch)
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
    """A refused write on a row bound to ANOTHER transaction is a real failure.
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
        patch.object(
            apple_iap.billing_repo,
            "get_by_owner_id",
            AsyncMock(return_value={"owner_id": "user_1", "apple_original_transaction_id": "SOMEONE_ELSE"}),
        ),
    ):
        with pytest.raises(AppleIapError, match="could not link"):
            await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")


@pytest.mark.asyncio
async def test_a_refunded_owner_cannot_replay_their_old_transaction():
    """The replay: after a REFUND cancels the entitlement, the owner still holds
    the original JWS. Its signature and account token are still valid and its
    `revocationDate` is absent — the blob predates the refund — so re-POSTing it
    used to flip the row back to active and hand back a subscription that had
    been paid back.

    The write now carries the JWS signing time, so the old blob loses the
    watermark to the refund notification, and the reply reports Apple's status
    rather than the stale payload's."""
    verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
        # Refused: a REFUND notification already landed with a later signedDate.
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value=None)) as set_sub,
        patch.object(
            apple_iap.billing_repo,
            "get_by_owner_id",
            AsyncMock(
                return_value={
                    "owner_id": "user_1",
                    "apple_original_transaction_id": "2000000000000001",
                    "apple_subscription_status": "canceled",
                }
            ),
        ),
    ):
        result = await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    # Apple's status wins over the replayed payload's "active".
    assert result.status == "canceled"
    # And the watermark was actually supplied, which is what refused the write.
    assert set_sub.await_args.kwargs["signed_date"] == 1_700_000_500_000


@pytest.mark.asyncio
async def test_the_link_path_supplies_the_jws_signing_time_as_a_watermark():
    """Without it the link path bypasses the out-of-order fence entirely — the
    condition is skipped when signed_date is None."""
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
        await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    assert set_sub.await_args.kwargs["signed_date"] == 1_700_000_500_000


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
    _no_pending_session(monkeypatch)
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

    _no_pending_session(monkeypatch)
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


# ---------------------------------------------------------------------------
# appAccountToken — the only cross-account fence that is not a race.
# ---------------------------------------------------------------------------


def test_owner_account_token_is_a_uuid_and_stable():
    token = apple_iap.owner_account_token("user_2abcDEF")
    assert token == apple_iap.owner_account_token("user_2abcDEF")
    assert len(token) == 36
    assert [len(p) for p in token.split("-")] == [8, 4, 4, 4, 12]
    # StoreKit requires a UUID; version + variant nibbles must be well-formed.
    assert token[14] == "5"
    assert token[19] == "8"
    assert token != apple_iap.owner_account_token("user_other")


def test_owner_account_token_matches_the_client_derivation():
    """Lockstep check against apps/mobile/src/lib/appleIap.ts.

    The two implementations are the same algorithm written twice. If they
    diverge, EVERY real purchase is rejected — so the expected values here are
    literals produced by the JS side, not by calling the Python one.
    """
    assert apple_iap.owner_account_token("user_2abcDEF") == "6d2d23ed-6454-54b1-8b21-49eca4734604"
    assert apple_iap.owner_account_token("user_x") == "0f0a552a-1e17-5118-8bfc-6a9430304d4b"


@pytest.mark.asyncio
async def test_link_purchase_refuses_a_transaction_bought_by_another_account():
    """The replay: B submits A's signed transaction. The token is inside the
    payload Apple signed, so B cannot strip or forge it."""
    verifier = SimpleNamespace(
        verify_and_decode_signed_transaction=lambda _: _transaction(
            appAccountToken=apple_iap.owner_account_token("user_A")
        )
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        with pytest.raises(AppleIapError, match="different account"):
            await apple_iap.link_purchase(owner_id="user_B", signed_transaction="jws")

    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_purchase_refuses_a_transaction_with_no_account_binding():
    """Required, not advisory. Checking it "when present" fenced nothing while
    the client sent nothing — the branch simply never ran."""
    verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction(appAccountToken=None))
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        with pytest.raises(AppleIapError, match="no account binding"):
            await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_purchase_accepts_apples_uppercase_token():
    """Apple echoes appAccountToken as an uppercase UUID in some payloads. A
    case-sensitive compare would reject every genuine purchase."""
    verifier = SimpleNamespace(
        verify_and_decode_signed_transaction=lambda _: _transaction(
            appAccountToken=apple_iap.owner_account_token("user_1").upper()
        )
    )
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


# ---------------------------------------------------------------------------
# The pending-session marker only prevents a second session if it is READ.
# An earlier version wrote it and never looked at it, which fixed nothing.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mobile_web_checkout_returns_the_already_open_session(monkeypatch):
    """The slot claim self-expires after 90s while a session stays completable
    for ~31 minutes. Past that window the only thing standing between an owner
    and two charged subscriptions is this read."""
    from core.services import billing_service

    monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "pending_checkout_session_id": "cs_open"}),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session,
        "retrieve",
        lambda _id: {"status": "open", "id": "cs_open", "url": "https://checkout.stripe.com/c/pay/cs_open"},
    )
    built = AsyncMock()
    monkeypatch.setattr(billing_service, "_build_subscription_checkout", built)
    claim = AsyncMock(return_value="tok")
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", claim)

    session = await billing_service.create_mobile_web_checkout(owner_id="user_1")

    assert session["url"].endswith("cs_open")
    built.assert_not_awaited()
    # Reused, so no slot is taken and no second session is minted.
    claim.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_web_checkout_refuses_when_the_pending_session_is_already_paid(monkeypatch):
    """Paid but the webhook has not landed, so every local guard still reads
    "not subscribed". Minting another session here is the double-charge."""
    from core.services import billing_service

    monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "pending_checkout_session_id": "cs_done"}),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session,
        "retrieve",
        lambda _id: {"status": "complete", "id": "cs_done"},
    )
    built = AsyncMock()
    monkeypatch.setattr(billing_service, "_build_subscription_checkout", built)

    with pytest.raises(billing_service.BillingServiceError, match="already_subscribed"):
        await billing_service.create_mobile_web_checkout(owner_id="user_1")

    built.assert_not_awaited()


@pytest.mark.asyncio
async def test_mobile_web_checkout_mints_fresh_past_a_dead_marker(monkeypatch):
    """Nothing clears the marker when a session expires, so a stale one must
    self-heal rather than wedge the owner out of ever checking out again."""
    from core.services import billing_service

    monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "pending_checkout_session_id": "cs_expired"}),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session,
        "retrieve",
        lambda _id: {"status": "expired", "id": "cs_expired"},
    )

    async def _fake_build(**_):
        return SimpleNamespace(id="cs_new", url="https://checkout.stripe.com/c/pay/cs_new")

    monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_build)
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value="tok"))
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())

    session = await billing_service.create_mobile_web_checkout(owner_id="user_1")
    assert session.id == "cs_new"


@pytest.mark.asyncio
async def test_mobile_web_checkout_mints_fresh_when_the_marker_cannot_be_retrieved(monkeypatch):
    """Same self-heal, via the exception path rather than an "expired" status.

    Covers the except clause specifically: naming the wrong exception class
    there turns an ordinary stale marker into a 500 on the one button meant to
    convert a free user.
    """
    from core.services import billing_service

    monkeypatch.setattr(billing_service.settings, "STRIPE_FLAT_PRICE_ID", "price_flat")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "pending_checkout_session_id": "cs_gone"}),
    )

    def _gone(_id):
        raise billing_service.stripe.error.InvalidRequestError("No such session", param="id")

    monkeypatch.setattr(billing_service.stripe.checkout.Session, "retrieve", _gone)

    async def _fake_build(**_):
        return SimpleNamespace(id="cs_new", url="https://checkout.stripe.com/c/pay/cs_new")

    monkeypatch.setattr(billing_service, "_build_subscription_checkout", _fake_build)
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value="tok"))
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())

    session = await billing_service.create_mobile_web_checkout(owner_id="user_1")
    assert session.id == "cs_new"


# ---------------------------------------------------------------------------
# Environment fallback. App Review buys with SANDBOX transactions against the
# PRODUCTION backend, so pinning one environment fails the very submission
# this feature exists to pass.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_sandbox_transaction_verifies_against_a_production_backend(monkeypatch):
    """The App Review case. A production-pinned verifier rejects the reviewer's
    own purchase, and the submission fails for a reason no log makes obvious."""
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ENVIRONMENT", "production")

    prod = SimpleNamespace(
        verify_and_decode_signed_transaction=lambda _: (_ for _ in ()).throw(ValueError("wrong environment"))
    )
    sandbox = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())
    built: list[str] = []

    def _fake_verifier(environment_name: str = ""):
        built.append(environment_name)
        return prod if environment_name == "production" else sandbox

    with (
        patch.object(apple_iap, "_verifier", _fake_verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        result = await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    assert result.status == "active"
    # Configured environment first — the common case must not pay for the rare one.
    assert built == ["production", "sandbox"]


def test_the_configured_environment_is_tried_first_and_alone_when_it_works():
    """The fallback must not double every verification in steady state."""
    built: list[str] = []

    def _fake_verifier(environment_name: str = ""):
        built.append(environment_name)
        return SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())

    with patch.object(apple_iap, "_verifier", _fake_verifier):
        apple_iap.verify_transaction("jws")

    assert built == ["sandbox"]


def test_an_unbuildable_fallback_does_not_break_the_working_environment(monkeypatch):
    """Sandbox needs no APPLE_IAP_APP_APPLE_ID and production does, so on a dev
    backend the production fallback cannot be constructed. That must be skipped,
    not raised — otherwise dev can verify nothing at all."""
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ENVIRONMENT", "sandbox")

    def _fake_verifier(environment_name: str = ""):
        if environment_name == "production":
            raise AppleIapNotConfigured("APPLE_IAP_APP_APPLE_ID is required for the production environment")
        return SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())

    with patch.object(apple_iap, "_verifier", _fake_verifier):
        assert apple_iap.verify_transaction("jws").status == "active"


def test_an_unconfigured_primary_still_raises_not_configured():
    """Feature-off must stay a 503, not become a generic rejection."""

    def _fake_verifier(environment_name: str = ""):
        raise AppleIapNotConfigured("Apple IAP is off in this environment")

    with patch.object(apple_iap, "_verifier", _fake_verifier):
        with pytest.raises(AppleIapNotConfigured):
            apple_iap.verify_transaction("jws")


@pytest.mark.asyncio
async def test_a_sandbox_transaction_grants_a_real_plan_deliberately(monkeypatch):
    """Pins a DECISION, not just behaviour.

    A sandbox purchase is free and TestFlight runs sandbox StoreKit against the
    production api, so this hands the paid plan to anyone who can install the
    TestFlight build. That is accepted because the TestFlight link is kept to
    trusted testers — the control is distribution, not code. A per-account
    allowlist and a review-window flag were both considered and rejected: each
    fails App Review's own sandbox purchase if someone forgets to set it.

    If this test is ever failing because a gate was added, the TestFlight link
    had better not be public any more.
    """
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ENVIRONMENT", "production")

    prod = SimpleNamespace(
        verify_and_decode_signed_transaction=lambda _: (_ for _ in ()).throw(ValueError("wrong environment"))
    )
    sandbox = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())

    def _fake_verifier(environment_name: str = ""):
        return prod if environment_name == "production" else sandbox

    with (
        patch.object(apple_iap, "_verifier", _fake_verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        result = await apple_iap.link_purchase(owner_id="user_1", signed_transaction="jws")

    assert result.status == "active"
    # Carried so the log line can name it — the only way a leaked TestFlight
    # link would be visible.
    assert result.environment == "sandbox"
