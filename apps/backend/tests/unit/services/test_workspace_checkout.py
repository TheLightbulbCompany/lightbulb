"""Workspace-only Stripe subscription checkout contract."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from botocore.exceptions import ClientError

from core.services import billing_service
from core.services.billing_service import BillingService


@pytest.fixture(autouse=True)
def checkout_slot(monkeypatch):
    monkeypatch.setattr(
        billing_service.billing_repo,
        "try_claim_checkout_slot",
        AsyncMock(return_value="claim"),
    )
    monkeypatch.setattr(billing_service.billing_repo, "release_checkout_slot", AsyncMock())
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", AsyncMock())
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([]))),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("plan", "price_id"),
    [("workspace", "price_workspace")],
)
async def test_workspace_checkout_has_one_recurring_item_and_no_promotion_codes(monkeypatch, plan, price_id):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_BASE_PRICE_ID", "price_base")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    # Stripe objects are mapping-shaped; don't let a MagicMock hide accidental
    # attribute-only response handling at this public boundary.
    session = {"id": "cs_1", "url": "https://checkout.test/cs_1"}
    create = MagicMock(return_value=session)
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan=plan)

    assert result == {"checkout_url": "https://checkout.test/cs_1"}
    kwargs = create.call_args.kwargs
    assert kwargs["line_items"] == [{"price": price_id, "quantity": 1}]
    assert kwargs["allow_promotion_codes"] is False
    assert kwargs["payment_method_collection"] == "always"
    assert kwargs["metadata"] == {
        "workspace_id": "org_1",
        "owner_id": "org_1",
        "clerk_user_id": "user_1",
        "plan": plan,
        "purpose": "workspace_subscription_v2",
        "return_to": "web",
        "front_desk": "0",
    }
    assert "automatic_tax" not in kwargs
    assert "customer_update" not in kwargs
    assert "discounts" not in kwargs


@pytest.mark.asyncio
async def test_workspace_checkout_return_to_app_uses_universal_link_urls(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    session = {"id": "cs_1", "url": "https://checkout.test/cs_1"}
    create = MagicMock(return_value=session)
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    result = await billing_service.create_workspace_checkout(
        owner_id="org_1", clerk_user_id="user_1", plan="workspace", return_to="app"
    )

    assert result == {"checkout_url": "https://checkout.test/cs_1"}
    kwargs = create.call_args.kwargs
    assert kwargs["success_url"] == f"{billing_service.settings.FRONTEND_URL}/checkout_redirect/success"
    assert kwargs["cancel_url"] == f"{billing_service.settings.FRONTEND_URL}/checkout_redirect/cancel"
    assert kwargs["origin_context"] == "mobile_app"
    assert kwargs["metadata"]["return_to"] == "app"
    # A web and an app retry in one bucket must never share a Stripe idempotency key.
    assert kwargs["idempotency_key"].endswith(":app:fd0")


@pytest.mark.asyncio
async def test_workspace_checkout_return_to_web_default_is_unchanged(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    session = {"id": "cs_1", "url": "https://checkout.test/cs_1"}
    create = MagicMock(return_value=session)
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert result == {"checkout_url": "https://checkout.test/cs_1"}
    kwargs = create.call_args.kwargs
    assert kwargs["success_url"] == f"{billing_service.settings.FRONTEND_URL}/settings?panel=billing&checkout=success"
    assert kwargs["cancel_url"] == f"{billing_service.settings.FRONTEND_URL}/settings?panel=billing&checkout=cancel"
    assert "origin_context" not in kwargs
    assert kwargs["metadata"]["return_to"] == "web"
    assert kwargs["idempotency_key"].endswith(":web:fd0")


@pytest.mark.asyncio
async def test_workspace_checkout_reuses_pending_web_session_only_for_web_return_to(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    pending = {
        "status": "open",
        "url": "https://checkout.test/pending",
        "metadata": {
            "purpose": "workspace_subscription_v2",
            "plan": "workspace",
            "workspace_id": "org_1",
            "return_to": "web",
        },
        "customer": "cus_1",
        "mode": "subscription",
    }
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "pending_checkout_session_id": "cs_pending",
            }
        ),
    )
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "retrieve", MagicMock(return_value=pending))
    create = MagicMock(return_value={"id": "cs_new", "url": "https://checkout.test/cs_new"})
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)
    expire = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "expire", expire)

    # A "web" pending session is reused for another "web" request.
    result = await billing_service.create_workspace_checkout(
        owner_id="org_1", clerk_user_id="user_1", plan="workspace", return_to="web"
    )
    assert result == {"checkout_url": "https://checkout.test/pending"}
    create.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_does_not_reuse_a_web_pending_session_for_app(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    pending = {
        "status": "open",
        "url": "https://checkout.test/pending",
        "metadata": {
            "purpose": "workspace_subscription_v2",
            "plan": "workspace",
            "workspace_id": "org_1",
            "return_to": "web",
        },
        "customer": "cus_1",
        "mode": "subscription",
    }
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "pending_checkout_session_id": "cs_pending",
            }
        ),
    )
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "retrieve", MagicMock(return_value=pending))
    create = MagicMock(return_value={"id": "cs_new", "url": "https://checkout.test/cs_new"})
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)
    expire = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "expire", expire)

    # A "web" pending session is NOT reused for an "app" request: it is expired
    # and a new session is minted instead.
    result = await billing_service.create_workspace_checkout(
        owner_id="org_1", clerk_user_id="user_1", plan="workspace", return_to="app"
    )
    assert result == {"checkout_url": "https://checkout.test/cs_new"}
    expire.assert_called_once_with("cs_pending")
    create.assert_called_once()


@pytest.mark.asyncio
async def test_workspace_checkout_swaps_an_active_subscription_item_in_place(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "stripe_subscription_id": "sub_1",
                "subscription_status": "active",
            }
        ),
    )
    stored = {
        "id": "sub_1",
        "customer": "cus_1",
        "metadata": {"workspace_id": "org_1", "owner_id": "org_1", "workspace_plan_revision": "0"},
        "status": "active",
        "items": {"data": [{"id": "si_1", "price": {"id": "price_ultra"}}]},
    }
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "retrieve",
        MagicMock(return_value=stored),
    )
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([stored]))),
    )
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    # Unconfirmed: nothing is changed; the caller must confirm the prorated change.
    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert result == {"updated": False, "confirm_required": True}
    modify.assert_not_called()

    result = await billing_service.create_workspace_checkout(
        owner_id="org_1", clerk_user_id="user_1", plan="workspace", confirm=True
    )

    assert result == {"updated": True}
    assert modify.call_args.args == ("sub_1",)
    assert modify.call_args.kwargs["items"] == [{"id": "si_1", "price": "price_workspace"}]
    assert modify.call_args.kwargs["proration_behavior"] == "create_prorations"
    assert modify.call_args.kwargs["metadata"] == {
        "workspace_id": "org_1",
        "owner_id": "org_1",
        "plan": "workspace",
        "purpose": "workspace_subscription_v2",
        "workspace_plan_revision": "1",
        "provider_choice": "",
        "byo_provider": "",
        "credits_cents": "",
    }
    assert "clerk_user_id" not in modify.call_args.kwargs["metadata"]
    assert modify.call_args.kwargs["idempotency_key"].startswith("workspace-plan-change:v2:")


@pytest.mark.asyncio
async def test_workspace_checkout_rejects_a_legacy_multi_item_subscription(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "stripe_subscription_id": "sub_legacy_team",
            }
        ),
    )
    stored = {
        "id": "sub_legacy_team",
        "customer": "cus_1",
        "metadata": {"workspace_id": "org_1", "owner_id": "org_1"},
        "status": "active",
        "items": {
            "data": [
                {"id": "si_base", "price": {"id": "price_ultra"}},
                {"id": "si_seat", "price": {"id": "price_seat"}},
            ]
        },
    }
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "retrieve",
        MagicMock(return_value=stored),
    )
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([stored]))),
    )
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    with pytest.raises(billing_service.BillingServiceError, match="can't be changed automatically"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    modify.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_rejects_multiple_live_subscriptions_even_with_a_stored_id(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    stored = {
        "id": "sub_1",
        "customer": "cus_1",
        "status": "active",
        "metadata": {"workspace_id": "org_1", "owner_id": "org_1"},
        "items": {"data": [{"id": "si_1", "price": {"id": "price_ultra"}}]},
    }
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1", "stripe_subscription_id": "sub_1"}),
    )
    monkeypatch.setattr(billing_service.stripe.Subscription, "retrieve", MagicMock(return_value=stored))
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([stored, {**stored, "id": "sub_2"}]))),
    )
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    with pytest.raises(billing_service.BillingServiceError, match="Multiple live subscriptions"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    modify.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_does_not_create_a_second_subscription_while_webhook_is_in_flight(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    subscription = {
        "id": "sub_webhook",
        "customer": "cus_1",
        "status": "active",
        "metadata": {"workspace_id": "org_1", "owner_id": "org_1", "workspace_plan_revision": "0"},
        "items": {"data": [{"id": "si_1", "price": {"id": "price_ultra"}}]},
    }
    monkeypatch.setattr(
        billing_service.stripe.Subscription,
        "list",
        MagicMock(return_value=MagicMock(auto_paging_iter=lambda: iter([subscription]))),
    )
    create = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)
    modify = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Subscription, "modify", modify)

    result = await billing_service.create_workspace_checkout(
        owner_id="org_1", clerk_user_id="user_1", plan="workspace", confirm=True
    )

    assert result == {"updated": True}
    assert modify.call_args.args[0] == "sub_webhook"
    create.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_reuses_its_open_pending_session(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    pending = {
        "status": "open",
        "url": "https://checkout.test/pending",
        "metadata": {"purpose": "workspace_subscription_v2", "plan": "workspace", "workspace_id": "org_1"},
        "customer": "cus_1",
        "mode": "subscription",
    }
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "pending_checkout_session_id": "cs_pending",
            }
        ),
    )
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "retrieve", MagicMock(return_value=pending))
    create = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)
    expire = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "expire", expire)

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert result == {"checkout_url": "https://checkout.test/pending"}
    create.assert_not_called()
    expire.assert_not_called()
    billing_service.billing_repo.release_checkout_slot.assert_awaited_once_with("org_1", token="claim")


@pytest.mark.asyncio
async def test_workspace_checkout_rejects_pending_marker_with_conflicting_legacy_owner(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={
                "owner_id": "org_1",
                "stripe_customer_id": "cus_1",
                "pending_checkout_session_id": "cs_pending",
            }
        ),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session,
        "retrieve",
        MagicMock(
            return_value={
                "status": "open",
                "customer": "cus_1",
                "mode": "subscription",
                "metadata": {
                    "purpose": "workspace_subscription_v2",
                    "plan": "workspace",
                    "workspace_id": "org_1",
                    "owner_id": "org_other",
                },
            }
        ),
    )

    with pytest.raises(billing_service.BillingServiceError, match="pending_checkout_invalid"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")


def _with_pending_marker(monkeypatch, session: dict) -> dict:
    """An owner with no live subscription and ``session`` as the pending marker."""
    monkeypatch.setattr(billing_service.settings, "STRIPE_BASE_PRICE_ID", "price_base")
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(
            return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1", "pending_checkout_session_id": "cs_old"}
        ),
    )
    stripe_calls = {
        "retrieve": MagicMock(return_value={"id": "cs_old", **session}),
        "create": MagicMock(return_value={"id": "cs_new", "url": "https://checkout.test/cs_new"}),
        "expire": MagicMock(),
    }
    for name, mock in stripe_calls.items():
        monkeypatch.setattr(billing_service.stripe.checkout.Session, name, mock)
    return stripe_calls


_OPEN_BASE = {
    "status": "open",
    "payment_status": "unpaid",
    "url": "https://checkout.test/cs_old",
    "customer": "cus_1",
    "mode": "subscription",
    "metadata": {"purpose": "workspace_subscription_v2", "plan": "base", "workspace_id": "org_1", "owner_id": "org_1"},
}


@pytest.mark.asyncio
async def test_cancelled_checkout_then_another_plan_expires_the_old_session_and_mints_a_new_one(monkeypatch):
    """The reported bug: cancel Base Checkout, pick Workspace → 400 for ~31 min."""
    order = []
    monkeypatch.setattr(
        billing_service.billing_repo,
        "try_claim_checkout_slot",
        AsyncMock(side_effect=lambda *_a, **_k: order.append("claim") or "claim"),
    )
    stripe_calls = _with_pending_marker(monkeypatch, _OPEN_BASE)
    stripe_calls["expire"].side_effect = lambda *_a, **_k: order.append("expire")
    stripe_calls["create"].side_effect = lambda **_k: (
        order.append("create") or {"id": "cs_new", "url": "https://checkout.test/cs_new"}
    )

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert result == {"checkout_url": "https://checkout.test/cs_new"}
    # Serialized: the old session is expired under the claim, before the new one exists.
    assert order == ["claim", "expire", "create"]
    stripe_calls["expire"].assert_called_once_with("cs_old")
    assert stripe_calls["create"].call_args.kwargs["metadata"]["plan"] == "workspace"
    # The marker is re-read strongly consistent under the claim.
    billing_service.billing_repo.get_by_owner_id.assert_any_await("org_1", consistent_read=True)
    billing_service.billing_repo.set_pending_checkout.assert_awaited_once_with("org_1", session_id="cs_new")
    billing_service.billing_repo.release_checkout_slot.assert_awaited_once_with("org_1", token="claim")


@pytest.mark.asyncio
async def test_switching_back_to_a_plan_never_replays_its_expired_session(monkeypatch):
    """Web -> cancel -> app -> cancel -> web inside one 5-minute bucket. Without
    the superseded id in the key, the third create reuses the first key and
    Stripe replays the first (expired) session."""
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    monkeypatch.setattr(billing_service.time, "time", lambda: 1_800_000_001)
    row = {"owner_id": "org_1", "stripe_customer_id": "cus_1"}
    sessions = {}

    async def record(_owner_id, *, session_id):
        row["pending_checkout_session_id"] = session_id

    def create(**kwargs):
        sid = f"cs_{len(sessions) + 1}"
        sessions[sid] = kwargs["metadata"]["return_to"]
        return {"id": sid, "url": f"https://checkout.test/{sid}"}

    def retrieve(sid, **_kwargs):
        return {
            **_OPEN_BASE,
            "id": sid,
            "metadata": {**_OPEN_BASE["metadata"], "plan": "workspace", "return_to": sessions[sid]},
        }

    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(side_effect=lambda *_a, **_k: row))
    monkeypatch.setattr(billing_service.billing_repo, "set_pending_checkout", record)
    create_mock = MagicMock(side_effect=create)
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create_mock)
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "retrieve", MagicMock(side_effect=retrieve))
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "expire", MagicMock())

    for return_to in ("web", "app", "web"):
        await billing_service.create_workspace_checkout(
            owner_id="org_1", clerk_user_id="user_1", plan="workspace", return_to=return_to
        )

    keys = [call.kwargs["idempotency_key"] for call in create_mock.call_args_list]
    assert len(keys) == 3 and len(set(keys)) == 3


@pytest.mark.asyncio
async def test_an_old_session_that_can_no_longer_be_expired_never_gets_a_sibling(monkeypatch):
    """Expire only succeeds on an open session. If another tab submitted it in
    between, minting a second session could double-subscribe: 409 and let the
    retry see the resulting subscription instead."""
    stripe_calls = _with_pending_marker(monkeypatch, _OPEN_BASE)
    stripe_calls["expire"].side_effect = billing_service.stripe.error.InvalidRequestError(
        "session is not open", param=None
    )

    with pytest.raises(billing_service.BillingServiceError, match="checkout_in_progress"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    stripe_calls["create"].assert_not_called()
    billing_service.billing_repo.set_pending_checkout.assert_not_awaited()
    billing_service.billing_repo.release_checkout_slot.assert_awaited_once_with("org_1", token="claim")


@pytest.mark.asyncio
async def test_a_foreign_open_marker_for_another_plan_is_never_expired(monkeypatch):
    stripe_calls = _with_pending_marker(monkeypatch, {**_OPEN_BASE, "customer": "cus_other"})

    with pytest.raises(billing_service.BillingServiceError, match="pending_checkout_invalid"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    stripe_calls["expire"].assert_not_called()
    stripe_calls["create"].assert_not_called()


@pytest.mark.asyncio
async def test_a_completed_ach_session_still_settling_is_never_superseded(monkeypatch):
    """ACH: `complete` means "payment processing may still be in progress" and
    `payment_status` stays `unpaid` for days, while its subscription is live."""
    stripe_calls = _with_pending_marker(
        monkeypatch,
        {
            **_OPEN_BASE,
            "status": "complete",
            "payment_status": "unpaid",
            "subscription": {"id": "sub_ach", "status": "incomplete"},
        },
    )

    with pytest.raises(billing_service.BillingServiceError, match="already_subscribed:checkout_completed"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert stripe_calls["retrieve"].call_args.kwargs == {"expand": ["subscription"]}
    stripe_calls["expire"].assert_not_called()
    stripe_calls["create"].assert_not_called()
    billing_service.billing_repo.set_pending_checkout.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("subscription", [{"id": "sub_gone", "status": "canceled"}, None])
async def test_a_completed_marker_whose_subscription_is_gone_no_longer_strands_the_owner(monkeypatch, subscription):
    """#2748: cancel (or incomplete_expired), then re-subscribe. The complete
    marker used to raise forever because nothing clears the field."""
    stripe_calls = _with_pending_marker(
        monkeypatch, {**_OPEN_BASE, "status": "complete", "payment_status": "paid", "subscription": subscription}
    )

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert result == {"checkout_url": "https://checkout.test/cs_new"}
    stripe_calls["expire"].assert_not_called()
    billing_service.billing_repo.set_pending_checkout.assert_awaited_once_with("org_1", session_id="cs_new")


@pytest.mark.asyncio
async def test_an_expired_marker_mints_fresh(monkeypatch):
    stripe_calls = _with_pending_marker(monkeypatch, {**_OPEN_BASE, "status": "expired"})

    result = await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert result == {"checkout_url": "https://checkout.test/cs_new"}
    stripe_calls["expire"].assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_retries_within_a_bucket_send_identical_stripe_kwargs(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    clock = {"now": 1_800_000_001}
    monkeypatch.setattr(billing_service.time, "time", lambda: clock["now"])

    async def get_account(_owner_id, **_kwargs):
        # The DDB await crosses a five-minute boundary after the key/expiry
        # were captured. The builder must not sample that later clock value.
        clock["now"] = 1_800_000_301
        return {"owner_id": "org_1", "stripe_customer_id": "cus_1"}

    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        get_account,
    )
    create = MagicMock(side_effect=[{"id": "cs_1", "url": "url_1"}, {"id": "cs_2", "url": "url_2"}])
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")
    clock["now"] = 1_800_000_001
    await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    assert create.call_args_list[0].kwargs == create.call_args_list[1].kwargs


@pytest.mark.asyncio
async def test_workspace_checkout_claim_denied_never_builds_a_session(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(billing_service.billing_repo, "try_claim_checkout_slot", AsyncMock(return_value=None))
    create = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    with pytest.raises(billing_service.BillingServiceError, match="checkout_in_progress"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    create.assert_not_called()


@pytest.mark.asyncio
async def test_workspace_checkout_marker_failure_expires_unreturned_session(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session, "create", MagicMock(return_value={"id": "cs_1", "url": "url"})
    )
    expire = MagicMock()
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "expire", expire)
    monkeypatch.setattr(
        billing_service.billing_repo, "set_pending_checkout", AsyncMock(side_effect=RuntimeError("ddb"))
    )

    with pytest.raises(billing_service.BillingServiceError, match="checkout_unavailable"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    expire.assert_called_once_with("cs_1")
    billing_service.billing_repo.release_checkout_slot.assert_awaited_once_with("org_1", token="claim")


@pytest.mark.asyncio
async def test_workspace_checkout_build_failure_releases_claim(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    monkeypatch.setattr(
        billing_service.stripe.checkout.Session, "create", MagicMock(side_effect=RuntimeError("stripe down"))
    )

    with pytest.raises(RuntimeError, match="stripe down"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan="workspace")

    billing_service.billing_repo.release_checkout_slot.assert_awaited_once_with("org_1", token="claim")


@pytest.mark.asyncio
async def test_workspace_customer_does_not_reuse_another_workspaces_email_customer(monkeypatch):
    service = BillingService()
    monkeypatch.setattr(billing_service.billing_repo, "get_by_owner_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        billing_service.billing_repo,
        "create_if_not_exists",
        AsyncMock(return_value={"owner_id": "org_2", "stripe_customer_id": "cus_org_2"}),
    )
    create = MagicMock(return_value=MagicMock(id="cus_org_2"))
    monkeypatch.setattr(billing_service.stripe.Customer, "create", create)
    list_customers = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Customer, "list", list_customers)

    account = await service.create_customer_for_owner(owner_id="org_2", owner_type="org", email="owner@example.com")

    list_customers.assert_not_called()
    create.assert_called_once_with(
        metadata={"workspace_id": "org_2", "owner_id": "org_2", "owner_type": "org"},
        idempotency_key="workspace-customer:v2:org_2",
    )
    assert account["stripe_customer_id"] == "cus_org_2"


@pytest.mark.asyncio
async def test_workspace_customer_backfill_race_keeps_the_existing_row_winner(monkeypatch):
    service = BillingService()
    existing = {"owner_id": "org_2", "subscription_status": "trial_local"}
    winner = {"owner_id": "org_2", "stripe_customer_id": "cus_winner"}
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(side_effect=[existing, winner]),
    )
    monkeypatch.setattr(
        billing_service.billing_repo,
        "set_stripe_customer_id",
        AsyncMock(
            side_effect=ClientError(
                {"Error": {"Code": "ConditionalCheckFailedException", "Message": "already claimed"}},
                "UpdateItem",
            )
        ),
    )
    monkeypatch.setattr(
        billing_service.stripe.Customer,
        "create",
        MagicMock(return_value=MagicMock(id="cus_loser")),
    )
    delete = MagicMock()
    monkeypatch.setattr(billing_service.stripe.Customer, "delete", delete)

    account = await service.create_customer_for_owner(owner_id="org_2", owner_type="org", email="owner@example.com")

    assert account == winner
    delete.assert_called_once_with("cus_loser", idempotency_key="delete_customer:cus_loser")


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", ["base", "ultra"])
async def test_retired_plans_are_never_sold(monkeypatch, plan):
    monkeypatch.setattr(billing_service.settings, "STRIPE_BASE_PRICE_ID", "price_base")
    monkeypatch.setattr(billing_service.settings, "STRIPE_ULTRA_PRICE_ID", "price_ultra")
    with pytest.raises(billing_service.BillingServiceError, match="plan_retired"):
        await billing_service.create_workspace_checkout(owner_id="org_1", clerk_user_id="user_1", plan=plan)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("session_fd", "click_fd", "reused"), [("0", True, False), ("1", False, False), ("1", True, True)]
)
async def test_a_pending_session_is_reused_only_for_the_same_front_desk_choice(
    monkeypatch, session_fd, click_fd, reused
):
    monkeypatch.setattr(billing_service.settings, "STRIPE_FRONT_DESK_PRICE_ID", "price_fd")
    pending = {
        **_OPEN_BASE,
        "metadata": {**_OPEN_BASE["metadata"], "plan": "workspace", "return_to": "web", "front_desk": session_fd},
    }
    stripe_calls = _with_pending_marker(monkeypatch, pending)

    await billing_service.create_workspace_checkout(
        owner_id="org_1", clerk_user_id="user_1", plan="workspace", front_desk=click_fd
    )

    assert stripe_calls["expire"].called is not reused
    assert stripe_calls["create"].called is not reused


@pytest.mark.asyncio
async def test_workspace_checkout_for_chatgpt_returns_into_chatgpt_sign_in(monkeypatch):
    """A free workspace that picked ChatGPT: plan -> Checkout -> straight back into ChatGPT sign-in."""
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    create = MagicMock(return_value={"id": "cs_2", "url": "https://checkout.test/cs_2"})
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    await billing_service.create_workspace_checkout(
        owner_id="org_1", clerk_user_id="user_1", plan="workspace", then="chatgpt"
    )

    kwargs = create.call_args.kwargs
    assert kwargs["success_url"] == (
        f"{billing_service.settings.FRONTEND_URL}/settings?panel=providers&action=connect-chatgpt&checkout=success"
    )
    assert kwargs["idempotency_key"].endswith(":web:fd0:chatgpt")


@pytest.mark.asyncio
async def test_workspace_checkout_from_onboarding_returns_to_the_onboarding_chatgpt_step(monkeypatch):
    monkeypatch.setattr(billing_service.settings, "STRIPE_WORKSPACE_PRICE_ID", "price_workspace")
    monkeypatch.setattr(
        billing_service.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": "org_1", "stripe_customer_id": "cus_1"}),
    )
    create = MagicMock(return_value={"id": "cs_3", "url": "https://checkout.test/cs_3"})
    monkeypatch.setattr(billing_service.stripe.checkout.Session, "create", create)

    await billing_service.create_workspace_checkout(
        owner_id="org_1", clerk_user_id="user_1", plan="workspace", then="chatgpt_onboarding"
    )

    kwargs = create.call_args.kwargs
    assert (
        kwargs["success_url"] == f"{billing_service.settings.FRONTEND_URL}/chat?provider=chatgpt_oauth&checkout=success"
    )
    assert kwargs["idempotency_key"].endswith(":web:fd0:chatgpt_onboarding")
