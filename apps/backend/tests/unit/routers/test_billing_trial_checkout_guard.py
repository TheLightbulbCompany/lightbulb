"""Tests for the checkout 409 guard on ``POST /billing/trial-checkout``.

The guard blocks a second checkout for any subscription state that has
ever billed or might still bill (``TRIAL_BLOCKED_STATUSES``) — Stripe
Checkout in subscription mode creates a NEW subscription on every
successful return, and checkout now bills immediately, so an unguarded
retry double-charges. The expanded blocklist (beyond
``{"active", "trialing", "past_due"}``) dates from audit C3, when
checkout still carried a 14-day Stripe trial and cancel-before-conversion
could mint unlimited free trials (= unlimited always-on ECS Fargate cost).

These tests pin down the expanded blocklist.
"""

from unittest.mock import AsyncMock, patch

import pytest

from core.tenancy_codes import PENDING_ORG_INVITATION


_BLOCKED_LOCAL_STATUSES = [
    "active",
    "trialing",
    "past_due",
    "canceled",
    "incomplete",
    "incomplete_expired",
    "unpaid",
    "paused",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_status", _BLOCKED_LOCAL_STATUSES)
@patch("routers.billing.billing_repo")
async def test_trial_checkout_409s_on_blocked_local_status(mock_repo, blocked_status, async_client):
    """Local subscription_status in any blocked state -> 409, no
    new Checkout session created."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_test_123",
            "stripe_customer_id": "cus_X",
            "stripe_subscription_id": "sub_X",
            "subscription_status": blocked_status,
        }
    )

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 409, f"status={blocked_status} should be blocked but got {resp.status_code}"
    assert resp.json()["detail"].startswith("already_subscribed:")


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.Subscription.retrieve")
@patch("routers.billing.billing_repo")
async def test_trial_checkout_409s_when_live_stripe_status_is_canceled(
    mock_repo, mock_retrieve, mock_create_checkout, async_client
):
    """Local row missing subscription_status but legacy stripe_subscription_id
    is set; live Stripe call returns ``canceled`` -> 409.

    Pre-fix this fell through and minted a NEW subscription, which is
    the trial-gaming exploit.
    """
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_test_123",
            "stripe_customer_id": "cus_X",
            "stripe_subscription_id": "sub_X",
            "subscription_status": None,  # local row not backfilled
        }
    )
    mock_retrieve.return_value = {"status": "canceled"}

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 409
    mock_create_checkout.assert_not_called()


@pytest.mark.asyncio
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_trial_checkout_with_pending_org_invitation_returns_409(mock_repo, mock_clerk, async_client):
    """Caller in personal context with a pending invite must be redirected
    to /onboarding/invitations, not allowed to subscribe personally."""
    # No prior billing row — gate B fires before billing-row checks.
    mock_repo.get_by_owner_id = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(
        return_value=[
            {
                "id": "orginv_pending",
                "public_organization_data": {"name": "Acme Org"},
            }
        ]
    )

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == PENDING_ORG_INVITATION
    assert "Acme Org" in body["detail"]["message"]
    assert body["detail"]["redirect_to"] == "/onboarding/invitations"


@pytest.mark.asyncio
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_trial_checkout_with_pending_invite_no_org_name_falls_back(mock_repo, mock_clerk, async_client):
    """When Clerk's pending invite is missing public_organization_data,
    the message uses the generic 'an organization' fallback."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[{"id": "orginv_no_data"}])

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == PENDING_ORG_INVITATION
    assert "an organization" in body["detail"]["message"]


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.Subscription.list")
@patch("core.services.billing_service.BillingService.create_customer_for_owner")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_trial_checkout_with_no_pending_invitations_passes_gate_b(
    mock_repo, mock_clerk, mock_create_customer, mock_sub_list, mock_create_checkout, async_client
):
    """Empty pending-invitations list lets the request through Gate B. The
    downstream BillingService is mocked so the test verifies ONLY that Gate
    B invoked the Clerk lookup and did not raise its own 409."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=None)
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    # The live-subscription guard lists subs on the (possibly email-reused)
    # Stripe customer — empty here, this test is about Gate B only.
    mock_sub_list.return_value = _sub_list([])
    # Mock the downstream Stripe customer + checkout creation so the test
    # doesn't reach real DDB / Stripe.
    mock_create_customer.return_value = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "stripe_customer_id": "cus_test",
    }
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/test"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    # Gate B was invoked (proves it WAS evaluated, not skipped).
    mock_clerk.list_pending_invitations_for_user.assert_awaited_once()
    # Gate B did NOT block — if a 409 fires, it must be from a different guard.
    if resp.status_code == 409:
        body = resp.json()
        detail = body.get("detail")
        if isinstance(detail, dict):
            assert detail.get("code") != PENDING_ORG_INVITATION
    mock_clerk.list_pending_invitations_for_user.assert_awaited_once()


@pytest.mark.asyncio
async def test_trial_checkout_in_org_context_skips_pending_invite_check(app, mock_org_admin_user):
    """Org-context callers (org admins running org trial-checkout) must
    skip the pending-invite check — they're creating an org subscription,
    not a personal one."""
    from core.auth import get_current_user
    from httpx import AsyncClient, ASGITransport

    app.dependency_overrides[get_current_user] = mock_org_admin_user
    try:
        with patch("routers.billing.clerk_admin") as mock_clerk, patch("routers.billing.billing_repo") as mock_repo:
            mock_clerk.list_pending_invitations_for_user = AsyncMock()
            mock_repo.get_by_owner_id = AsyncMock(
                return_value={
                    "owner_id": "org_test_456",
                    "stripe_subscription_id": "sub_existing",
                    "subscription_status": "active",  # forces a 409, but NOT from gate B
                }
            )
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
                await ac.post(
                    "/api/v1/billing/trial-checkout",
                    json={"provider_choice": "bedrock_claude"},
                )
            # Critical assertion: gate B was NOT invoked for org context.
            mock_clerk.list_pending_invitations_for_user.assert_not_awaited()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("core.services.billing_service.BillingService.create_customer_for_owner")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_trial_local_owner_gets_a_stripe_customer_backfilled(
    mock_repo, mock_clerk, mock_create_customer, mock_create_checkout, async_client
):
    """A trial_local row EXISTS but carries no stripe_customer_id, so guarding
    customer creation on ``if not account`` skipped it for exactly the owners
    who needed it — and create_flat_fee_checkout then raised "No Stripe
    customer", 400ing every free user who clicked Upgrade. The guard must key on
    the MISSING FIELD, not the missing row."""
    mock_repo.get_by_owner_id = AsyncMock(
        return_value={
            "owner_id": "user_test_123",
            "owner_type": "personal",
            "subscription_status": "trial_local",
            # no stripe_customer_id — the trial row writes no stripe_* fields
        }
    )
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_create_customer.return_value = {
        "owner_id": "user_test_123",
        "owner_type": "personal",
        "subscription_status": "trial_local",
        "stripe_customer_id": "cus_backfilled",
    }
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/x"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 200, resp.text
    # The backfill ran for the trial row (this is the fix)...
    mock_create_customer.assert_awaited_once()
    # ...and checkout reached Stripe instead of raising "No Stripe customer".
    mock_create_checkout.assert_called_once()


# ---------------------------------------------------------------------------
# Live-subscription check: the local row alone can't be trusted for owners
# with no stored subscription id. A checkout that completed seconds ago (its
# webhook still in flight) or a transient `incomplete` (deliberately not
# persisted over trial_local by handle_subscription_state) both leave the row
# checkout-eligible while a billable subscription already exists on the
# Stripe customer — a second Checkout session would double-subscribe.
# ---------------------------------------------------------------------------

_TRIAL_LOCAL_ROW = {
    "owner_id": "user_test_123",
    "owner_type": "personal",
    "subscription_status": "trial_local",
    "stripe_customer_id": "cus_X",
    # no stripe_subscription_id — the legacy-sub retrieve path must not run
}


def _live_sub(status, owner_id="user_test_123"):
    return {"id": "sub_live", "status": status, "metadata": {"owner_id": owner_id}}


def _sub_list(subs):
    """Mimic stripe's ListObject: the router walks auto_paging_iter() so a
    matching sub can't hide behind pagination."""
    from unittest.mock import MagicMock

    return MagicMock(auto_paging_iter=lambda: iter(subs))


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", ["incomplete", "active", "trialing", "past_due"])
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_live_billable_sub_on_customer_409s(
    mock_repo, mock_clerk, mock_sub_list, mock_create_checkout, live_status, async_client
):
    """A billable subscription already on the Stripe customer (matching this
    owner's metadata) blocks a new checkout even though the local row still
    reads trial_local with no stored subscription id."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_TRIAL_LOCAL_ROW))
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([_live_sub(live_status)])

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == f"already_subscribed:{live_status}"
    mock_create_checkout.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sub",
    [
        _live_sub("active", owner_id="org_someone_else"),  # shared email-keyed customer
        _live_sub("incomplete_expired"),  # terminal, can never bill
        _live_sub("canceled"),  # terminal, local-row policy governs instead
    ],
)
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_non_blocking_live_subs_allow_checkout(
    mock_repo, mock_clerk, mock_sub_list, mock_create_checkout, sub, async_client
):
    """Another owner's subscription on the shared email-keyed customer, and
    terminal states for this owner, must NOT block the checkout."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_TRIAL_LOCAL_ROW))
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([sub])
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/ok"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 200, resp.json()
    mock_create_checkout.assert_called_once()


# ---------------------------------------------------------------------------
# Single-open-session guard: completing two open Checkout sessions creates two
# charged subscriptions, and no subscription exists at session-open time for
# the row/live-list guards to see. The pending marker + live session status
# keep it to at most one completable session per owner.
# ---------------------------------------------------------------------------

_PENDING_ROW = {
    **_TRIAL_LOCAL_ROW,
    "pending_checkout_session_id": "cs_pending",
    "pending_checkout_provider": "bedrock_claude",
}


def _open_session(url="https://checkout.stripe.com/cs_pending", credits_cents=None):
    from unittest.mock import MagicMock

    m = MagicMock(url=url)
    meta = {"credits_cents": str(credits_cents)} if credits_cents is not None else {}
    m.get.side_effect = lambda k, d=None: {"status": "open", "metadata": meta}.get(k, d)
    return m


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_same_provider_reuses_open_pending_session(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_create_checkout, async_client
):
    """Re-clicking Upgrade with the same provider returns the SAME hosted
    session URL — no second completable session is ever minted."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    mock_retrieve.return_value = _open_session()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/cs_pending"
    mock_create_checkout.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.expire")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_changed_credit_amount_expires_the_open_session(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_expire, mock_create_checkout, async_client
):
    """Same provider, DIFFERENT starter amount is a different purchase.

    This guard used to key on provider alone, which was sufficient while
    provider fully determined the session body. Bundled credits made the
    amount a second body-varying input, and this guard returns BEFORE
    create_flat_fee_checkout — so its amount-aware idempotency key never got a
    say. An owner who opened a $1,050 session, went back, and chose the $5
    preset was handed the old session: charged $1,050 against a page that had
    just quoted $55.
    """
    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_repo.release_checkout_slot = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    mock_retrieve.return_value = _open_session(credits_cents=100_000)
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/fresh"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude", "credits_cents": 500},
    )

    assert resp.status_code == 200
    # The stale $1,050 session must be killed, not handed back.
    mock_expire.assert_called_once()
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/fresh"


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_same_provider_and_same_amount_still_reuses(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_create_checkout, async_client
):
    """The reuse behaviour itself must survive: an identical re-click (same
    provider AND same amount) still returns the one open session rather than
    minting a second completable one."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    mock_retrieve.return_value = _open_session(credits_cents=2000)

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude", "credits_cents": 2000},
    )

    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/cs_pending"
    mock_create_checkout.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.expire")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_provider_switch_expires_old_session_and_mints_fresh(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_expire, mock_create_checkout, async_client
):
    """Picking a DIFFERENT provider must kill the old open session (its
    subscription metadata carries the old provider, and leaving it open would
    allow completing both) and mint a fresh one."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_repo.release_checkout_slot = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    mock_retrieve.return_value = _open_session()
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/fresh"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "chatgpt_oauth"},
    )

    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/fresh"
    mock_expire.assert_called_once_with("cs_pending")
    # The switch must NOT force-clear the slot (that unconditional clear was the
    # double-charge ABA). The fresh mint's own try_claim_checkout_slot handles a
    # stale claim; a live one correctly 409s. See routers/billing.py.
    mock_repo.release_checkout_slot.assert_not_awaited()
    mock_create_checkout.assert_called_once()


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_completed_pending_session_409s_until_webhook_lands(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_create_checkout, async_client
):
    """A completed session whose webhook hasn't flipped the row yet must 409
    rather than mint a parallel session for a customer who already paid."""
    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    completed = _open_session()
    completed.get.side_effect = lambda k, d=None: {"status": "complete"}.get(k, d)
    mock_retrieve.return_value = completed

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "already_subscribed:checkout_completed"
    mock_create_checkout.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_expired_or_missing_pending_session_mints_fresh(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_create_checkout, async_client
):
    """A marker pointing at an expired/unretrievable session self-heals — a
    fresh session is minted."""
    import stripe as stripe_mod

    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_repo.release_checkout_slot = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    mock_retrieve.side_effect = stripe_mod.error.InvalidRequestError("gone", param=None)
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/fresh"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/fresh"
    mock_create_checkout.assert_called_once()


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.expire")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_unexpirable_pending_session_409s_unless_truly_expired(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_expire, mock_create_checkout, async_client
):
    """Thin race: the session completes in another tab between retrieve
    (open) and expire. Minting fresh would leave BOTH chargeable — the
    handler must re-check and stop."""
    import stripe as stripe_mod

    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    completed = _open_session()
    completed.get.side_effect = lambda k, d=None: {"status": "complete"}.get(k, d)
    mock_retrieve.side_effect = [_open_session(), completed]
    mock_expire.side_effect = stripe_mod.error.InvalidRequestError("already complete", param=None)

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "chatgpt_oauth"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "already_subscribed:checkout_completed"
    mock_create_checkout.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.expire")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_unexpirable_but_expired_session_mints_fresh(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_expire, mock_create_checkout, async_client
):
    """The benign variant of the expire race: the session hit its expires_at
    between retrieve and expire. Nothing is chargeable — mint fresh."""
    import stripe as stripe_mod

    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_repo.release_checkout_slot = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    expired = _open_session()
    expired.get.side_effect = lambda k, d=None: {"status": "expired"}.get(k, d)
    mock_retrieve.side_effect = [_open_session(), expired]
    mock_expire.side_effect = stripe_mod.error.InvalidRequestError("already expired", param=None)
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/fresh"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "chatgpt_oauth"},
    )

    assert resp.status_code == 200
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/fresh"


@pytest.mark.asyncio
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_lost_checkout_slot_claim_maps_to_409(
    mock_repo, mock_clerk, mock_sub_list, mock_create_checkout, async_client
):
    """Losing the per-owner checkout-slot claim to a concurrent request is a
    409 (retry reuses the winner's session), not a generic 400."""
    from core.services.billing_service import BillingServiceError

    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_TRIAL_LOCAL_ROW))
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    mock_create_checkout.side_effect = BillingServiceError("checkout_in_progress")

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "checkout_in_progress"


@pytest.mark.asyncio
@patch("core.services.launch_close.winner_checkout_discount", new_callable=AsyncMock)
@patch("core.services.billing_service.create_flat_fee_checkout")
@patch("routers.billing.stripe.checkout.Session.expire")
@patch("routers.billing.stripe.checkout.Session.retrieve")
@patch("routers.billing.stripe.Subscription.list")
@patch("routers.billing.clerk_admin")
@patch("routers.billing.billing_repo")
async def test_launch_winner_with_a_stale_undiscounted_session_gets_a_fresh_one(
    mock_repo, mock_clerk, mock_sub_list, mock_retrieve, mock_expire, mock_create_checkout, mock_discount, async_client
):
    """The owner opened a session, THEN the close flagged them a winner: the
    cached session carries no free-month discount (marker absent), so reusing
    it would charge them full price. Expire it and mint fresh — the fresh
    session gets the discount pre-applied by the builder."""
    mock_discount.return_value = [{"coupon": "coup_1"}]
    mock_repo.get_by_owner_id = AsyncMock(return_value=dict(_PENDING_ROW))
    mock_repo.set_provider_choice = AsyncMock(return_value=None)
    mock_repo.release_checkout_slot = AsyncMock(return_value=None)
    mock_clerk.list_pending_invitations_for_user = AsyncMock(return_value=[])
    mock_sub_list.return_value = _sub_list([])
    mock_retrieve.return_value = _open_session()  # same provider, no marker
    mock_create_checkout.return_value = type("Session", (), {"url": "https://checkout.stripe.com/fresh"})()

    resp = await async_client.post(
        "/api/v1/billing/trial-checkout",
        json={"provider_choice": "bedrock_claude"},
    )

    assert resp.status_code == 200
    mock_expire.assert_called_once()
    assert resp.json()["checkout_url"] == "https://checkout.stripe.com/fresh"
