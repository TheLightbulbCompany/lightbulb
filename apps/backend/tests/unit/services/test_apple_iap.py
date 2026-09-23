"""Apple IAP entitlement mapping and notification handling.

The purchase-link half is gone — ``POST /billing/apple/link`` is a 410
tombstone, so ``apply_notification`` is the only way a transaction reaches this
module. The JWS verifier itself is Apple's library and is not re-tested here —
what IS tested is everything we built around it: that an unverifiable payload
never reaches a write, that event types map to the right
``subscription_status``, and that Apple's retry / out-of-order delivery cannot
corrupt entitlement.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from core.services import apple_iap
from core.services.apple_iap import AppleIapError, AppleIapNotConfigured
from core.services.webhook_dedup import WebhookDedupResult


def _transaction(**overrides):
    base = dict(
        originalTransactionId="2000000000000001",
        productId="company.thelightbulb.mobile.plan.monthly",
        expiresDate=1_800_000_000_000,
        revocationDate=None,
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
        subtype=None,
        signedDate=1_700_000_000_000,
        data=SimpleNamespace(signedTransactionInfo="signed-tx"),
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.fixture(autouse=True)
def _product_id(monkeypatch):
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_PRODUCT_ID", "company.thelightbulb.mobile.plan.monthly")


@pytest.fixture(autouse=True)
def _no_trial_conversion(monkeypatch):
    """apply_notification reads the row before writing (to see whether this
    notification converts an owner off the free tier) and may dispatch a
    container reprovision. Both are stubbed by default so the mapping tests stay
    hermetic; the conversion tests say the interesting thing explicitly."""
    # No row -> _reconcile_active_subscription returns
    # immediately. The function itself is deliberately NOT replaced: stubbing it
    # would mean the conversion tests below could only ever assert against a
    # mock of the thing they exist to exercise.
    monkeypatch.setattr(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", AsyncMock())


@pytest.fixture(autouse=True)
def _no_teardown(monkeypatch):
    """Revocations now run entitlement teardown, which reads DynamoDB and talks
    to ECS + Paperclip. Stubbed by default so the mapping tests stay hermetic;
    the teardown tests assert against it explicitly."""
    import core.services.entitlement_teardown as teardown

    monkeypatch.setattr(teardown, "teardown_if_unentitled", AsyncMock(return_value=False))


@pytest.fixture(autouse=True)
def _no_revocation_tombstone(monkeypatch):
    """Default: this transaction was not revoked before linking.

    The revoked/cleared pair is written from the notification path, which reads
    the dedup table no unit test has. Stubbed to the ordinary answer here so the
    tombstone tests can say the interesting one explicitly rather than every
    other test tripping over it.
    """
    monkeypatch.setattr(apple_iap.webhook_dedup, "has_event", AsyncMock(return_value=False))
    monkeypatch.setattr(apple_iap.webhook_dedup, "record_event_or_skip", AsyncMock())
    monkeypatch.setattr(apple_iap.webhook_dedup, "delete_event", AsyncMock())


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


# ---------------------------------------------------------------------------
# Environment fallback. App Review buys with SANDBOX transactions against the
# PRODUCTION backend, so pinning one environment fails the very submission
# this feature exists to pass.
# ---------------------------------------------------------------------------


def test_a_sandbox_transaction_verifies_against_a_production_backend(monkeypatch):
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

    with patch.object(apple_iap, "_verifier", _fake_verifier):
        payload, environment = apple_iap._verify_signed("verify_and_decode_signed_transaction", "jws")
    result = apple_iap._subscription_from_transaction(payload, None, environment)

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
        apple_iap._verify_signed("verify_and_decode_signed_transaction", "jws")

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
        payload, environment = apple_iap._verify_signed("verify_and_decode_signed_transaction", "jws")
    assert apple_iap._subscription_from_transaction(payload, None, environment).status == "active"


def test_an_unconfigured_primary_still_raises_not_configured():
    """Feature-off must stay a 503, not become a generic rejection."""

    def _fake_verifier(environment_name: str = ""):
        raise AppleIapNotConfigured("Apple IAP is off in this environment")

    with patch.object(apple_iap, "_verifier", _fake_verifier):
        with pytest.raises(AppleIapNotConfigured):
            apple_iap._verify_signed("verify_and_decode_signed_transaction", "jws")


def test_a_sandbox_transaction_grants_a_real_plan_deliberately(monkeypatch):
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

    with patch.object(apple_iap, "_verifier", _fake_verifier):
        payload, environment = apple_iap._verify_signed("verify_and_decode_signed_transaction", "jws")
    result = apple_iap._subscription_from_transaction(payload, None, environment)

    assert result.status == "active"
    # Carried so the log line can name it — the only way a leaked TestFlight
    # link would be visible.
    assert result.environment == "sandbox"


# ---------------------------------------------------------------------------
# A revocation that lands BEFORE the purchase is ever linked. There is no row,
# so there is no watermark for a later link to lose against.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_refund_for_an_unlinked_purchase_leaves_a_tombstone(monkeypatch):
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="REFUND"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    marked = AsyncMock()
    monkeypatch.setattr(apple_iap.webhook_dedup, "record_event_or_skip", marked)

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
    ):
        # Raises rather than acking: "no row" can just mean the link has not
        # propagated through the GSI yet, and Apple never redelivers an accepted
        # notification — so acking would drop the refund permanently.
        with pytest.raises(AppleIapError, match="retry expected"):
            await apple_iap.apply_notification("signed")

    keys = [c.args[0] if c.args else c.kwargs.get("event_id") for c in marked.await_args_list]
    assert "apple_revoked:2000000000000001" in keys


@pytest.mark.asyncio
async def test_a_genuine_resubscribe_clears_the_tombstone(monkeypatch):
    """A resubscribe reuses the SAME originalTransactionId, so a tombstone that
    never cleared would refuse a paying customer forever."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    cleared = AsyncMock()
    monkeypatch.setattr(apple_iap.webhook_dedup, "delete_event", cleared)

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        await apple_iap.apply_notification("signed")

    keys = [c.args[0] if c.args else c.kwargs.get("event_id") for c in cleared.await_args_list]
    assert "apple_revoked:2000000000000001" in keys


@pytest.mark.asyncio
async def test_an_expiry_does_not_clear_the_tombstone(monkeypatch):
    """Only a LIVE state may clear it. Clearing on any applied notification
    would let the EXPIRED that follows a refund reopen the replay."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="EXPIRED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    cleared = AsyncMock()
    monkeypatch.setattr(apple_iap.webhook_dedup, "delete_event", cleared)

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        await apple_iap.apply_notification("signed")

    keys = [c.args[0] if c.args else c.kwargs.get("event_id") for c in cleared.await_args_list]
    assert "apple_revoked:2000000000000001" not in keys


# ---------------------------------------------------------------------------
# Billing grace period. Apple keeps the subscriber entitled while it retries the
# card; "past_due" is not in provision_gate._ACTIVE_STATUSES, so mapping grace
# to it cuts off a customer who is still a customer.
# ---------------------------------------------------------------------------


def test_past_due_really_does_end_access():
    """The premise of the grace fix. If this ever changes, the special-casing
    below is unnecessary and should go."""
    from core.services.provision_gate import _ACTIVE_STATUSES

    assert "past_due" not in _ACTIVE_STATUSES


@pytest.mark.asyncio
async def test_a_grace_period_subscriber_keeps_access(monkeypatch):
    grace_until = int(time.time() * 1000) + 5 * 24 * 60 * 60 * 1000
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(
            notificationType="DID_FAIL_TO_RENEW",
            subtype="GRACE_PERIOD",
            data=SimpleNamespace(signedTransactionInfo="tx", signedRenewalInfo="renewal"),
        ),
        # The paid-through date has ALREADY passed — that is why a renewal was
        # attempted — so the elapsed-expiry check would cancel without the
        # grace deadline standing in for it.
        verify_and_decode_signed_transaction=lambda _: _transaction(expiresDate=int(time.time() * 1000) - 60_000),
        verify_and_decode_renewal_info=lambda _: SimpleNamespace(gracePeriodExpiresDate=grace_until),
    )

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(
            apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})
        ) as set_sub,
    ):
        result = await apple_iap.apply_notification("signed")

    assert result == ("user_1", "active")
    # Entitlement runs to the GRACE deadline, not the elapsed paid-through date.
    assert set_sub.await_args.kwargs["expires_at"] == grace_until


@pytest.mark.asyncio
async def test_a_plain_billing_retry_is_still_past_due(monkeypatch):
    """Only the GRACE_PERIOD subtype keeps access. A bare DID_FAIL_TO_RENEW
    means Apple is not extending anything."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="DID_FAIL_TO_RENEW", subtype=None),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        assert await apple_iap.apply_notification("signed") == ("user_1", "past_due")


@pytest.mark.asyncio
async def test_an_elapsed_grace_period_ends_access(monkeypatch):
    """Grace is not an indefinite extension — once the deadline passes the
    ordinary expiry check has to end it."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(
            notificationType="DID_FAIL_TO_RENEW",
            subtype="GRACE_PERIOD",
            data=SimpleNamespace(signedTransactionInfo="tx", signedRenewalInfo="renewal"),
        ),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
        verify_and_decode_renewal_info=lambda _: SimpleNamespace(
            gracePeriodExpiresDate=int(time.time() * 1000) - 60_000
        ),
    )

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        assert await apple_iap.apply_notification("signed") == ("user_1", "past_due")


@pytest.mark.asyncio
async def test_undecodable_renewal_info_falls_back_rather_than_granting(monkeypatch):
    """Fail closed. An unreadable grace deadline must not become an open-ended
    free extension."""

    def _boom(_):
        raise ValueError("bad renewal info")

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(
            notificationType="DID_FAIL_TO_RENEW",
            subtype="GRACE_PERIOD",
            data=SimpleNamespace(signedTransactionInfo="tx", signedRenewalInfo="renewal"),
        ),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
        verify_and_decode_renewal_info=_boom,
    )

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        assert await apple_iap.apply_notification("signed") == ("user_1", "past_due")


# ---------------------------------------------------------------------------
# Closing the grace window, and reversing a refund. Both are types the mapping
# did not know, and an unknown type is acked as unactionable — which means the
# LAST known status stands forever.
# ---------------------------------------------------------------------------


def test_grace_period_expired_ends_access():
    """The other half of the grace fix. Nothing re-evaluates entitlement on a
    timer — the gates read the stored status, never apple_expires_at — so if
    this type is unmapped the subscriber keeps paid access indefinitely."""
    assert apple_iap._status_for("GRACE_PERIOD_EXPIRED") == "canceled"


def test_a_reversed_refund_restores_access():
    """Apple undid the refund; the customer is owed their content back."""
    assert apple_iap._status_for("REFUND_REVERSED") == "active"


def test_a_reversed_refund_beats_the_stale_revocation_date():
    """The reinstated transaction can still carry the revocationDate from the
    refund Apple just undid, and that check runs before any type mapping — so
    without an explicit yield the reinstatement could never take effect."""
    assert apple_iap._status_for("REFUND_REVERSED", revoked=True) == "active"


def test_a_reversed_refund_after_the_paid_period_does_not_grant():
    """Reinstatement is not immunity from the calendar."""
    assert apple_iap._status_for("REFUND_REVERSED", revoked=True, expires_at_ms=1_000, now_ms=2_000) == "canceled"


@pytest.mark.asyncio
async def test_an_unlinked_resubscribe_clears_its_own_tombstone(monkeypatch):
    """The gap the tombstone opened. If the original purchase was never linked,
    the resubscribe's SUBSCRIBED lands in the SAME no-row branch — so the
    tombstone outlived the new purchase and refused a customer
    who had just paid again, for up to the 30-day TTL."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    cleared = AsyncMock()
    monkeypatch.setattr(apple_iap.webhook_dedup, "delete_event", cleared)

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),  # still unlinked
        ),
    ):
        assert await apple_iap.apply_notification("signed") is None

    keys = [c.args[0] if c.args else c.kwargs.get("event_id") for c in cleared.await_args_list]
    assert "apple_revoked:2000000000000001" in keys


@pytest.mark.asyncio
async def test_an_unlinked_refund_still_leaves_its_tombstone(monkeypatch):
    """The clear must not be unconditional — that would undo the protection in
    the very branch that creates it."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="REFUND"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    marked = AsyncMock()
    cleared = AsyncMock()
    monkeypatch.setattr(apple_iap.webhook_dedup, "record_event_or_skip", marked)
    monkeypatch.setattr(apple_iap.webhook_dedup, "delete_event", cleared)

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(AppleIapError):
            await apple_iap.apply_notification("signed")

    assert any("apple_revoked:2000000000000001" in str(c) for c in marked.await_args_list)
    assert not any("apple_revoked:2000000000000001" in str(c) for c in cleared.await_args_list)


def test_every_apple_notification_type_is_classified():
    """The guard against the pattern that produced two rounds of findings.

    GRACE_PERIOD_EXPIRED and REFUND_REVERSED were both bugs of the same shape:
    a type Apple sends that no map mentioned, so it raised, got acked as
    unactionable, and silently froze entitlement at whatever came before. Fixing
    them one at a time as a reviewer notices is not a strategy — this fails the
    moment Apple's own enum contains something nobody here has decided about.
    """
    from appstoreserverlibrary.models.NotificationTypeV2 import NotificationTypeV2

    classified = (
        apple_iap._GRANTING_TYPES
        | apple_iap._REVOKING_TYPES
        | apple_iap._REINSTATING_TYPES
        | apple_iap._GRACE_TYPES
        | apple_iap._INFORMATIONAL_TYPES
    )
    unclassified = {t.value for t in NotificationTypeV2} - classified
    assert not unclassified, (
        f"Apple sends these and nothing decides what they mean: {sorted(unclassified)}. "
        "Add each to the granting, revoking, reinstating, grace, or informational set."
    )


def test_the_classifications_do_not_overlap():
    """A type in two sets is a coin toss over someone's access."""
    sets = {
        "granting": apple_iap._GRANTING_TYPES,
        "revoking": apple_iap._REVOKING_TYPES,
        "reinstating": apple_iap._REINSTATING_TYPES,
        "informational": apple_iap._INFORMATIONAL_TYPES,
    }
    seen: dict[str, str] = {}
    for name, members in sets.items():
        for t in members:
            assert t not in seen, f"{t} is in both {seen[t]} and {name}"
            seen[t] = name


def test_a_service_extension_keeps_the_subscriber_active():
    """Apple pushed the paid-through date out; the transaction carries the new
    one. Left unmapped this raised, so a customer Apple had just extended saw no
    change at all."""
    future = int(time.time() * 1000) + 86_400_000
    assert apple_iap._status_for("RENEWAL_EXTENDED", expires_at_ms=future) == "active"
    assert apple_iap._status_for("RENEWAL_EXTENSION", expires_at_ms=future) == "active"


@pytest.mark.asyncio
async def test_an_informational_notification_touches_nothing(monkeypatch):
    """An auto-renew toggle must not move entitlement — access runs to the
    paid-through date either way."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="DID_CHANGE_RENEWAL_STATUS"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock()) as set_sub,
    ):
        assert await apple_iap.apply_notification("signed") is None

    set_sub.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_tombstone_delete_is_not_swallowed_when_unlinked(monkeypatch):
    """On the unlinked path the tombstone IS the entitlement decision. Acking a
    notification whose delete failed dedups it forever and locks out a customer
    who has just paid again, until the 30-day TTL."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )

    async def _delete_fails(event_id):
        raise RuntimeError("dynamodb throttled")

    monkeypatch.setattr(apple_iap.webhook_dedup, "delete_event", _delete_fails)

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(RuntimeError):
            await apple_iap.apply_notification("signed")


@pytest.mark.asyncio
async def test_a_failed_tombstone_delete_is_tolerated_once_the_row_is_updated(monkeypatch):
    """Where the row itself was written, entitlement is already right and a
    lingering tombstone costs at most one refused re-link — not worth failing a
    notification that otherwise applied."""
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )

    async def _delete_fails(event_id):
        raise RuntimeError("dynamodb throttled")

    monkeypatch.setattr(apple_iap.webhook_dedup, "delete_event", _delete_fails)

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        assert await apple_iap.apply_notification("signed") == ("user_1", "active")


# ---------------------------------------------------------------------------
# Apple revocation has to reach the runtime, not just the row. Before this the
# notification path wrote "canceled" and stopped: an Apple-only owner's
# container kept running, and its agents kept spending on inference.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_apple_revocation_runs_entitlement_teardown(monkeypatch):
    import core.services.entitlement_teardown as teardown

    ran = AsyncMock(return_value=True)
    monkeypatch.setattr(teardown, "teardown_if_unentitled", ran)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="REFUND"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        assert await apple_iap.apply_notification("signed") == ("user_1", "canceled")

    ran.assert_awaited_once()
    assert ran.await_args.args[0] == "user_1"


@pytest.mark.asyncio
async def test_a_renewal_does_not_run_teardown(monkeypatch):
    """Only a revoking outcome asks the question — a renewal must never even
    approach the teardown path."""
    import core.services.entitlement_teardown as teardown

    ran = AsyncMock(return_value=False)
    monkeypatch.setattr(teardown, "teardown_if_unentitled", ran)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="DID_RENEW"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        await apple_iap.apply_notification("signed")

    ran.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_tombstone_outlives_the_transactions_paid_period(monkeypatch):
    """The tombstone stops a replay of the pre-revocation JWS, and that JWS
    stays grantable until its own expiresDate. A monthly StoreKit period can run
    31 days, so the dedup table's generic 30-day retention could drop the marker
    while the original blob was still live."""
    marked = AsyncMock()
    monkeypatch.setattr(apple_iap.webhook_dedup, "record_event_or_skip", marked)

    thirty_one_days_out = int(time.time() * 1000) + 31 * 24 * 60 * 60 * 1000
    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="REFUND"),
        verify_and_decode_signed_transaction=lambda _: _transaction(expiresDate=thirty_one_days_out),
    )

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(AppleIapError):
            await apple_iap.apply_notification("signed")

    ttl = marked.await_args.kwargs["ttl_seconds"]
    assert ttl > 31 * 24 * 60 * 60, "tombstone would expire while the JWS is still grantable"


@pytest.mark.asyncio
async def test_a_tombstone_for_an_already_expired_transaction_uses_the_default(monkeypatch):
    """Nothing to outlive — the transaction's own expiry already refuses it, so
    a negative remainder must not become a zero or negative TTL."""
    marked = AsyncMock()
    monkeypatch.setattr(apple_iap.webhook_dedup, "record_event_or_skip", marked)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="REFUND"),
        verify_and_decode_signed_transaction=lambda _: _transaction(expiresDate=1_000),
    )

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value=None),
        ),
    ):
        with pytest.raises(AppleIapError):
            await apple_iap.apply_notification("signed")

    ttl = marked.await_args.kwargs["ttl_seconds"]
    assert ttl is None or ttl > 0


# ---------------------------------------------------------------------------
# Trial → paid. The ORDINARY purchase: a trial_local owner hits a cap, buys on
# the paywall, and must end up with a working paid container — not a "pick a
# provider on the web" card seconds after being charged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buying_off_the_free_tier_persists_a_provider_and_reprovisions(monkeypatch):
    """A trial_local row deliberately has no provider_choice. The moment the
    derived status leaves trial_local, provision_gate's trial short-circuit
    stops applying and the next check is `provider_choice is None` →
    provider_required, which mobile renders as an outbound web handoff. And the
    running container is still the locked-down trial box."""
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "trial_local"}),
    )
    provider = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", provider)

    started: list[str] = []

    async def _start(owner_id):
        started.append(owner_id)
        return True

    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", _start)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        await apple_iap.apply_notification("signed")

    # The managed gate: what the trial already ran on, and it asks the buyer for nothing
    # — the App Store sheet has no provider picker.
    assert provider.await_args.kwargs["provider_choice"] == "openrouter_managed"
    # And the trial container is rolled, or the customer keeps the capped box.
    assert started == ["user_1"]


@pytest.mark.asyncio
async def test_an_owner_who_already_had_a_plan_keeps_it_and_still_reconciles(monkeypatch):
    """A provider the owner chose is never overwritten; the reconcile still runs on every active
    notification (idempotent -- a runtime already on the paid shape reconciles to "current")."""
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={"owner_id": "user_1", "subscription_status": "active", "provider_choice": "chatgpt_oauth"}
        ),
    )
    provider = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", provider)
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        await apple_iap.apply_notification("signed")

    provider.assert_not_awaited()
    dispatch.assert_awaited_once_with("user_1")


@pytest.mark.asyncio
async def test_the_subscribed_notification_can_convert_when_it_beats_the_link(monkeypatch):
    """The webhook can arrive before the client's link call, or the app can die
    before making it. The reconcile is idempotent, so whichever path lands
    first converts and the other converges on it."""
    provider = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", provider)
    # The helper decides against its own consistent re-read, not the GSI row.
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "trial_local"}),
    )

    started: list[str] = []

    async def _start(owner_id):
        started.append(owner_id)
        return True

    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", _start)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "trial_local"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        await apple_iap.apply_notification("signed")

    assert provider.await_args.kwargs["provider_choice"] == "openrouter_managed"
    assert started == ["user_1"]


@pytest.mark.asyncio
async def test_a_failed_provider_write_is_retried_by_the_next_notification(monkeypatch):
    """Keyed on the missing STATE, not the trial→paid EDGE: the row has already left
    trial_local, so only a state-keyed check retries a failed provider write."""
    attempts: list[str] = []

    async def _flaky(owner_id, **kwargs):
        attempts.append(owner_id)
        if len(attempts) == 1:
            raise RuntimeError("dynamodb throttled")

    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", _flaky)

    started: list[str] = []

    async def _start(owner_id):
        started.append(owner_id)
        return True

    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", _start)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    # The row keeps NO provider_choice throughout — which is exactly the
    # condition the retry keys on. Its subscription_status is irrelevant.
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "active"}),
    )

    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        # First notification: the provider write fails, which raises so Apple redelivers.
        with pytest.raises(RuntimeError, match="throttled"):
            await apple_iap.apply_notification("signed")
        assert started == [], "reprovision dispatched without a provider to resolve"

        # Second: it succeeds, and the conversion completes.
        await apple_iap.apply_notification("signed")

    assert len(attempts) == 2
    assert started == ["user_1"]


@pytest.mark.asyncio
async def test_a_trial_owner_with_a_pre_picked_provider_still_gets_reprovisioned(monkeypatch):
    """The abandoned-web-checkout case. /trial-checkout persists provider_choice
    BEFORE the Stripe session exists, so a trial_local row WITH a provider is
    the state of everyone who started a web checkout and bailed. Skipping them
    left a paying customer active on the locked-down trial container — and the
    idle reaper only scales down trial_local rows, so nothing ever wakes it
    through the config-rewriting branch again."""
    provider = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", provider)
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "user_1",
                "subscription_status": "trial_local",
                "provider_choice": "chatgpt_oauth",
            }
        ),
    )

    started: list[str] = []

    async def _start(owner_id):
        started.append(owner_id)
        return True

    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", _start)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "trial_local"}),
        ),
        patch.object(apple_iap.billing_repo, "set_apple_subscription", AsyncMock(return_value={"owner_id": "user_1"})),
    ):
        await apple_iap.apply_notification("signed")

    # Their chosen provider is NOT overwritten with the managed default...
    provider.assert_not_awaited()
    # ...but the trial container is still rolled to the paid shape.
    assert started == ["user_1"]


@pytest.mark.asyncio
async def test_a_redelivery_after_a_failed_dispatch_dispatches_again(monkeypatch):
    """The REAL redelivery path. Attempt 1 flips the row to active, persists the provider, then
    its dispatch cannot start and it raises. The redelivery sees a row that is already active
    WITH a provider -- no trial edge left -- and must still dispatch."""
    row = {"owner_id": "org_1", "subscription_status": "trial_local"}

    async def _set_apple(owner_id, **kwargs):
        row["subscription_status"] = "active"
        return dict(row)

    async def _set_provider(owner_id, **kwargs):
        row["provider_choice"] = kwargs["provider_choice"]

    monkeypatch.setattr(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(side_effect=lambda *_a, **_k: dict(row)))
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", _set_provider)
    monkeypatch.setattr(
        apple_iap.billing_repo, "get_by_apple_original_transaction_id", AsyncMock(side_effect=lambda _t: dict(row))
    )
    monkeypatch.setattr(apple_iap.billing_repo, "set_apple_subscription", _set_apple)
    dispatch = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)
    verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())
    notification = _notification(notificationType="SUBSCRIBED")

    with patch.object(apple_iap, "_verifier", return_value=verifier):
        with pytest.raises(RuntimeError, match="reconcile dispatch failed"):
            await apple_iap._apply_verified_notification(
                notification=notification, notification_type="SUBSCRIBED", signed_date=1_700_000_000_000
            )
        assert row == {"owner_id": "org_1", "subscription_status": "active", "provider_choice": "openrouter_managed"}

        result = await apple_iap._apply_verified_notification(
            notification=notification, notification_type="SUBSCRIBED", signed_date=1_700_000_000_000
        )

    assert result == ("org_1", "active")
    assert dispatch.await_count == 2
