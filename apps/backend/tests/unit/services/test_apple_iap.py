"""Apple IAP entitlement mapping, purchase linking and notification handling.

A transaction reaches this module two ways: ``link_purchase`` (the app's
``POST /billing/apple/link``) and ``apply_notification`` (Apple's webhook). The JWS verifier itself is Apple's library and is not re-tested here —
what IS tested is everything we built around it: that an unverifiable payload
never reaches a write, that event types map to the right
``subscription_status``, and that Apple's retry / out-of-order delivery cannot
corrupt entitlement.
"""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

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
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", AsyncMock())


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


def test_an_upgrade_notification_is_granting_not_informational():
    # DID_CHANGE_RENEWAL_PREF/UPGRADE carries the NEW product's transaction, effective now.
    assert apple_iap._effective_type("DID_CHANGE_RENEWAL_PREF", "UPGRADE") == "DID_RENEW"
    assert apple_iap._effective_type("DID_CHANGE_RENEWAL_PREF", "DOWNGRADE") == "DID_CHANGE_RENEWAL_PREF"


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
    """The other half of the grace fix. Only this write moves the STORED status
    and runs teardown; unmapped, the row stays "active" until the read-time
    expiry check catches it and the container is never torn down."""
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
# _reconcile_active_subscription: the plan is written, never a provider.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_active_apple_sub_writes_the_plan_and_never_a_provider(monkeypatch):
    row = {"owner_id": "org_x", "subscription_status": "active", "apple_subscription_status": "active"}
    monkeypatch.setattr(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    set_plan = AsyncMock()
    set_provider = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", set_plan)
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", set_provider)
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())

    sub = apple_iap._subscription_from_transaction(
        _transaction(productId="company.thelightbulb.mobile.ultra.monthly"), None
    )
    await apple_iap._reconcile_active_subscription("org_x", subscription=sub)

    set_plan.assert_awaited_once()
    assert set_plan.await_args.kwargs["plan"] == "ultra_pro"
    set_provider.assert_not_awaited()
    dispatch.assert_awaited_once_with("org_x")


# ---------------------------------------------------------------------------
# Trial → paid. The ORDINARY purchase: a trial_local owner hits a cap, buys on
# the paywall, and must end up with a working paid container — not a "pick a
# provider on the web" card seconds after being charged.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_buying_off_the_free_tier_sets_the_plan_and_reprovisions(monkeypatch):
    """The original transaction row (from the GSI) carries no subscription_status
    yet, so this is a first entry into paid: the plan lands, on top of the running reprovision."""
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "trial_local"}),
    )
    set_plan = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", set_plan)
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())

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

    # The plan the product bought — no provider picker, the App Store sheet has none.
    assert set_plan.await_args.kwargs["plan"] == "standard"
    # And the trial container is rolled, or the customer keeps the capped box.
    assert started == ["user_1"]


@pytest.mark.asyncio
async def test_a_renewal_reconciles_without_touching_provider(monkeypatch):
    """A provider is never written by the Apple path at all (see
    _reconcile_active_subscription). The reconcile still runs on every active
    notification (idempotent -- a runtime already on the paid shape reconciles
    to "current"), but a renewal (the original transaction row is already
    "active") must not refire the first-entry side effects."""
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={"owner_id": "user_1", "subscription_status": "active", "provider_choice": "chatgpt_oauth"}
        ),
    )
    provider = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", provider)
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", AsyncMock())
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    dispatch = AsyncMock(return_value=True)
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="DID_RENEW"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
    with (
        patch.object(apple_iap, "_verifier", return_value=verifier),
        patch.object(
            apple_iap.billing_repo,
            "get_by_apple_original_transaction_id",
            AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "active"}),
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
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", AsyncMock())
    # The helper decides against its own consistent re-read, not the GSI row.
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "user_1", "subscription_status": "trial_local"}),
    )
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())

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

    assert started == ["user_1"]


@pytest.mark.asyncio
async def test_a_failed_plan_write_is_retried_by_the_next_notification(monkeypatch):
    """A transient set_plan failure must raise so Apple redelivers -- not get
    silently swallowed and leave the workspace on its pre-purchase plan."""
    attempts: list[str] = []

    async def _flaky(owner_id, **kwargs):
        attempts.append(owner_id)
        if len(attempts) == 1:
            raise RuntimeError("dynamodb throttled")

    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", _flaky)
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())

    started: list[str] = []

    async def _start(owner_id):
        started.append(owner_id)
        return True

    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", _start)

    verifier = SimpleNamespace(
        verify_and_decode_notification=lambda _: _notification(notificationType="SUBSCRIBED"),
        verify_and_decode_signed_transaction=lambda _: _transaction(),
    )
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
        # First notification: the plan write fails, which raises so Apple redelivers.
        with pytest.raises(RuntimeError, match="throttled"):
            await apple_iap.apply_notification("signed")
        assert started == [], "reprovision dispatched without the plan it should reconcile to"

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
    through the config-rewriting branch again. Their pre-picked provider must
    stay untouched -- the Apple path never writes provider_choice at all."""
    provider = AsyncMock()
    monkeypatch.setattr(apple_iap.billing_repo, "set_provider_choice", provider)
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", AsyncMock())
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
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

    # Their chosen provider is NOT touched...
    provider.assert_not_awaited()
    # ...but the trial container is still rolled to the paid shape.
    assert started == ["user_1"]


@pytest.mark.asyncio
async def test_a_redelivery_after_a_failed_dispatch_dispatches_again(monkeypatch):
    """The REAL redelivery path. Attempt 1 flips the row to active, persists the plan, then
    its dispatch cannot start and it raises. The redelivery sees a row that is already active
    WITH the plan set -- no first-entry edge left -- and must still dispatch."""
    row = {"owner_id": "org_1", "subscription_status": "trial_local"}

    async def _set_apple(owner_id, **kwargs):
        row["subscription_status"] = "active"
        return dict(row)

    async def _set_plan(owner_id, **kwargs):
        row["plan"] = kwargs["plan"]

    monkeypatch.setattr(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(side_effect=lambda *_a, **_k: dict(row)))
    monkeypatch.setattr(apple_iap.billing_repo, "set_plan", _set_plan)
    monkeypatch.setattr(
        apple_iap.billing_repo, "get_by_apple_original_transaction_id", AsyncMock(side_effect=lambda _t: dict(row))
    )
    monkeypatch.setattr(apple_iap.billing_repo, "set_apple_subscription", _set_apple)
    monkeypatch.setattr("core.services.owner_facts.write_owner_facts", AsyncMock())
    dispatch = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr("core.services.provision_dispatch.dispatch_reconcile", dispatch)
    verifier = SimpleNamespace(verify_and_decode_signed_transaction=lambda _: _transaction())
    notification = _notification(notificationType="SUBSCRIBED")

    with patch.object(apple_iap, "_verifier", return_value=verifier):
        with pytest.raises(RuntimeError, match="reconcile dispatch failed"):
            await apple_iap._apply_verified_notification(
                notification=notification, notification_type="SUBSCRIBED", signed_date=1_700_000_000_000
            )
        assert row == {"owner_id": "org_1", "subscription_status": "active", "plan": "standard"}

        result = await apple_iap._apply_verified_notification(
            notification=notification, notification_type="SUBSCRIBED", signed_date=1_700_000_000_000
        )

    assert result == ("org_1", "active")
    assert dispatch.await_count == 2


# ---------------------------------------------------------------------------
# Product -> plan map, owner account token, and family-sharing refusal.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "product_id,plan",
    [
        ("company.thelightbulb.mobile.base.monthly", "base"),
        ("company.thelightbulb.mobile.plan.monthly", "standard"),
        ("company.thelightbulb.mobile.ultra.monthly", "ultra_pro"),
    ],
)
def test_each_product_maps_to_its_plan(product_id, plan):
    sub = apple_iap._subscription_from_transaction(_transaction(productId=product_id), None)
    assert sub.plan == plan


def test_an_unknown_product_never_grants():
    with pytest.raises(AppleIapError):
        apple_iap._subscription_from_transaction(_transaction(productId="com.example.other"), None)


def test_family_shared_transactions_are_refused():
    with pytest.raises(AppleIapError, match="family"):
        apple_iap._subscription_from_transaction(_transaction(inAppOwnershipType="FAMILY_SHARED"), None)


def test_owner_hint_is_read_from_app_account_token():
    sub = apple_iap._subscription_from_transaction(_transaction(appAccountToken="ABC-123"), None)
    assert sub.owner_hint == "ABC-123"


def test_owner_account_token_is_a_stable_v5_shaped_uuid():
    import uuid

    token = apple_iap.owner_account_token("org_3JlId4cAIysT9sovRPThedSNY5S")
    assert token == apple_iap.owner_account_token("org_3JlId4cAIysT9sovRPThedSNY5S")
    parsed = uuid.UUID(token)
    assert str(parsed) == token and parsed.version == 5
    assert token != apple_iap.owner_account_token("org_other")


def _xcode_settings(monkeypatch, *, accept: bool, environment: str):
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ACCEPT_XCODE", accept)
    monkeypatch.setattr(apple_iap.settings, "ENVIRONMENT", environment)
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_BUNDLE_ID", "co.isol8.mobile")
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ROOT_CERTS_DIR", "certs/apple")
    apple_iap._verifier.cache_clear()


def _xcode_jws() -> str:
    """An unsigned-by-Apple JWS shaped like an Xcode StoreKit Testing transaction."""
    import jwt

    payload = {
        "originalTransactionId": "0",
        "transactionId": "0",
        "productId": "company.thelightbulb.mobile.base.monthly",
        "bundleId": "co.isol8.mobile",
        "environment": "Xcode",
        "expiresDate": 4_000_000_000_000,
        "signedDate": int(time.time() * 1000),
        "type": "Auto-Renewable Subscription",
        "inAppOwnershipType": "PURCHASED",
        "appAccountToken": apple_iap.owner_account_token("org_x"),
    }
    return jwt.encode(payload, "local-xcode-key", algorithm="HS256")


def test_an_xcode_transaction_verifies_in_dev_when_enabled(monkeypatch):
    _xcode_settings(monkeypatch, accept=True, environment="dev")
    sub = apple_iap.verify_transaction(_xcode_jws())
    assert sub.environment == "xcode" and sub.plan == "base"


def test_an_xcode_transaction_is_refused_when_disabled(monkeypatch):
    _xcode_settings(monkeypatch, accept=False, environment="dev")
    with pytest.raises(AppleIapError):
        apple_iap.verify_transaction(_xcode_jws())


def test_prod_refuses_xcode_even_if_misconfigured(monkeypatch):
    _xcode_settings(monkeypatch, accept=True, environment="prod")
    with pytest.raises(AppleIapError):
        apple_iap.verify_transaction(_xcode_jws())


@pytest.mark.parametrize("accept_xcode", [True, False])
def test_xcode_as_the_configured_environment_cannot_bypass_the_accept_flag_in_prod(monkeypatch, accept_xcode):
    """APPLE_IAP_ENVIRONMENT="xcode" makes Xcode the CONFIGURED (primary)
    environment, so `_verify_signed`'s primary attempt builds an XCODE verifier
    directly via `_verifier(primary)` -- never reaching `_verify_signed`'s own
    Xcode-fallback gate at all. An XCODE verifier skips chain verification
    entirely (an unsigned/HS256-"signed" blob decodes as-is), so without a
    check inside `_verifier` itself this granted a plan for a forged
    transaction in prod regardless of APPLE_IAP_ACCEPT_XCODE."""
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ACCEPT_XCODE", accept_xcode)
    monkeypatch.setattr(apple_iap.settings, "ENVIRONMENT", "prod")
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ENVIRONMENT", "xcode")
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_BUNDLE_ID", "co.isol8.mobile")
    monkeypatch.setattr(apple_iap.settings, "APPLE_IAP_ROOT_CERTS_DIR", "certs/apple")
    apple_iap._verifier.cache_clear()

    # AppleIapNotConfigured is a subclass of AppleIapError -- either is an
    # acceptable refusal here, the point is that it MUST raise.
    with pytest.raises(AppleIapError):
        apple_iap.verify_transaction(_xcode_jws())


def _linkable(monkeypatch, *, row=None, existing=None, token_owner="org_x", **tx):
    tx.setdefault("appAccountToken", apple_iap.owner_account_token(token_owner))
    monkeypatch.setattr(apple_iap, "_verify_signed", lambda method, blob: (_transaction(**tx), "sandbox"))
    monkeypatch.setattr(
        apple_iap.billing_repo, "get_by_apple_original_transaction_id", AsyncMock(return_value=existing)
    )
    monkeypatch.setattr(
        apple_iap.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value=row if row is not None else {"owner_id": "org_x", "subscription_status": "trial_local"}),
    )
    write = AsyncMock(return_value={"owner_id": "org_x"})
    monkeypatch.setattr(apple_iap.billing_repo, "set_apple_subscription", write)
    monkeypatch.setattr(apple_iap, "_reconcile_active_subscription", AsyncMock())
    return write


@pytest.mark.asyncio
async def test_link_binds_a_purchase_to_its_workspace(monkeypatch):
    write = _linkable(monkeypatch)
    sub = await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")
    assert sub.status == "active" and sub.plan == "standard"
    write.assert_awaited_once()
    # The link shares the notification watermark: the JWS's own signedDate.
    assert write.await_args.kwargs["signed_date"] == _transaction().signedDate
    apple_iap._reconcile_active_subscription.assert_awaited_once()


def _pending_stripe_checkout(monkeypatch, session: dict, expire_error: Exception | None = None):
    """The linking row carries a pending Checkout marker; Stripe is faked."""
    import stripe as real_stripe
    from core.services import billing_service

    row = {"owner_id": "org_x", "subscription_status": "trial_local", "pending_checkout_session_id": "cs_1"}
    monkeypatch.setattr(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    monkeypatch.setattr(apple_iap.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value="tok"))
    monkeypatch.setattr(apple_iap.billing_repo, "release_checkout_slot", AsyncMock())
    fake = MagicMock()
    fake.error = real_stripe.error
    fake.checkout.Session.retrieve.return_value = session
    if expire_error is not None:
        fake.checkout.Session.expire.side_effect = expire_error
    monkeypatch.setattr(billing_service, "stripe", fake)
    return fake


@pytest.mark.asyncio
async def test_link_expires_an_open_stripe_checkout_and_proceeds(monkeypatch):
    """Apple has already charged: an open Checkout is retired, never a reason to refuse."""
    write = _linkable(monkeypatch)
    fake = _pending_stripe_checkout(monkeypatch, {"id": "cs_1", "status": "open"})
    sub = await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")
    fake.checkout.Session.expire.assert_called_once_with("cs_1")
    assert sub.status == "active"
    write.assert_awaited_once()


@pytest.mark.asyncio
async def test_link_refuses_when_the_pending_checkout_completed_into_a_live_subscription(monkeypatch):
    write = _linkable(monkeypatch)
    _pending_stripe_checkout(monkeypatch, {"id": "cs_1", "status": "complete", "subscription": {"status": "active"}})
    with pytest.raises(apple_iap.StripeManaged):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_link_refuses_a_purchase_made_for_another_workspace(monkeypatch):
    _linkable(monkeypatch, token_owner="org_other")
    with pytest.raises(AppleIapError, match="different"):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_link_refuses_a_transaction_without_an_account_token(monkeypatch):
    _linkable(monkeypatch, appAccountToken=None)
    with pytest.raises(AppleIapError, match="binding"):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_link_refuses_a_subscription_already_linked_elsewhere(monkeypatch):
    _linkable(monkeypatch, existing={"owner_id": "org_other"})
    with pytest.raises(AppleIapError, match="another"):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_link_refuses_when_stripe_already_bills_the_workspace(monkeypatch):
    _linkable(monkeypatch, row={"owner_id": "org_x", "stripe_subscription_status": "active"})
    metric = MagicMock()
    monkeypatch.setattr(apple_iap, "put_metric", metric)
    with pytest.raises(apple_iap.StripeManaged):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")
    # Apple charged and we did not apply it: support needs a signal to refund.
    metric.assert_called_once_with("apple_iap.link_refused_stripe_managed")


@pytest.mark.asyncio
async def test_link_refuses_a_revoked_transaction(monkeypatch):
    _linkable(monkeypatch)
    monkeypatch.setattr(apple_iap.webhook_dedup, "has_event", AsyncMock(return_value=True))
    with pytest.raises(AppleIapError, match="refunded"):
        await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")


@pytest.mark.asyncio
async def test_a_superseded_link_reports_apples_status_not_the_blobs(monkeypatch):
    write = _linkable(monkeypatch)
    write.return_value = None
    apple_iap.billing_repo.get_by_owner_id.side_effect = [
        {"owner_id": "org_x", "subscription_status": "active"},
        {
            "owner_id": "org_x",
            "apple_original_transaction_id": "2000000000000001",
            "apple_subscription_status": "canceled",
        },
    ]
    sub = await apple_iap.link_purchase(owner_id="org_x", signed_transaction="jws")
    assert sub.status == "canceled"
