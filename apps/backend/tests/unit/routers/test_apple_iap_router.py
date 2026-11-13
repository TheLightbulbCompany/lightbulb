"""Apple IAP router — request-shape handling on the unauthenticated webhook.

The webhook is a PUBLIC endpoint: Apple cannot present a Clerk token, so its
only authentication is the JWS signature inside the payload. That means anything
on the internet can POST to it with any body, and every one of those must land
on a 4xx rather than a 5xx. A 500 here is not merely untidy — providers and
scanners alike read it as "temporarily broken, retry", so a permanently
malformed delivery would be retried for days against an endpoint that can never
accept it.

The chaos suite (tests/chaos/api/test_contract_negative.py) fuzzes every route
in the OpenAPI schema for exactly this and caught an empty-body 500 here.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
import routers.apple_iap as apple_router
from core.services import apple_iap


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(apple_router.router, prefix="/api/v1")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content,headers",
    [
        (b"", {}),
        (b"", {"content-type": "application/json"}),
        (b"not json at all", {"content-type": "application/json"}),
        (b"{", {"content-type": "application/json"}),
    ],
    ids=["empty", "empty-json-ct", "plain-text", "truncated-json"],
)
async def test_webhook_rejects_unparseable_bodies_without_5xx(client, content, headers):
    async with client as c:
        r = await c.post("/api/v1/webhooks/apple-iap", content=content, headers=headers)
    assert r.status_code == 400, r.text
    assert r.status_code < 500


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [{}, {"signedPayload": ""}, {"signedPayload": None}, {"signedPayload": 42}, []],
    ids=["no-key", "empty-string", "null", "wrong-type", "json-array"],
)
async def test_webhook_rejects_valid_json_without_a_usable_payload(client, payload):
    """Parseable JSON that carries no signed payload is still a bad request —
    including a top-level array, which is JSON but has no `.get`."""
    async with client as c:
        r = await c.post("/api/v1/webhooks/apple-iap", json=payload)
    assert r.status_code == 400, r.text


# ---------------------------------------------------------------------------
# Authenticated routes, driven through a REAL AuthContext.
#
# This is the gap that let a 500 into every Apple billing endpoint: the handlers
# reached for `auth.owner_id`, which does not exist on AuthContext (the owner is
# DERIVED via resolve_owner_id — org_id in org context, else user_id). Service
# tests mocked below the router and never saw it, and the chaos suite fuzzes
# authenticated routes UNauthenticated, so it gets a 401 before the handler runs.
#
# These construct the real dataclass and override the auth dependency, so any
# future attribute that AuthContext does not actually have fails here.
# ---------------------------------------------------------------------------


def _app_with_auth(auth: AuthContext) -> FastAPI:
    app = FastAPI()
    app.include_router(apple_router.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: auth
    return app


PERSONAL = AuthContext(user_id="user_1", email="a@b.co")
ORG = "org_1"
ORG_ADMIN = AuthContext(user_id="user_1", org_id=ORG, org_role="org:admin", email="a@b.co")


@pytest.mark.asyncio
async def test_link_requires_authentication(client):
    async with client as c:
        r = await c.post("/api/v1/billing/apple/link", json={"signed_transaction": "jws"})
    assert r.status_code in {401, 403}, r.text


@pytest.mark.asyncio
async def test_retired_mobile_web_checkout_route_is_not_mounted():
    """There is one subscription purchase route: /billing/checkout."""
    app = _app_with_auth(ORG_ADMIN)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/billing/apple/web-checkout", json={})
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_portal_resolves_the_workspace_owner_too():
    """The portal handler resolves its own owner; a missing binding here 500s
    exactly like the link route did."""
    app = _app_with_auth(ORG_ADMIN)
    with patch.object(apple_router.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/billing/apple/portal", json={})
    assert r.status_code == 404, r.text


ORG_MEMBER = AuthContext(user_id="user_2", org_id="org_1", org_role="org:member", email="m@b.co")


@pytest.mark.asyncio
async def test_the_mobile_portal_still_admits_an_org_admin():
    """The fence is on ROLE, not on org context — an admin managing the team's
    own subscription is the legitimate caller this route exists for."""
    app = _app_with_auth(ORG_ADMIN)
    with patch.object(apple_router.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/billing/apple/portal", json={})
    # 404 "no billing account" — past the fence, which is what this pins.
    assert r.status_code == 404, r.text


@pytest.fixture
def admin_client():
    return AsyncClient(transport=ASGITransport(app=_app_with_auth(ORG_ADMIN)), base_url="http://t")


@pytest.fixture
def member_client():
    return AsyncClient(transport=ASGITransport(app=_app_with_auth(ORG_MEMBER)), base_url="http://t")


@pytest.mark.asyncio
async def test_intent_is_hidden_while_selling_is_off(admin_client, monkeypatch):
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", False)
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_intent_returns_the_workspace_token_and_products(admin_client, monkeypatch):
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", True)
    monkeypatch.setattr(apple_router.billing_repo, "get_by_owner_id", AsyncMock(return_value={"owner_id": ORG}))
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 200
    body = r.json()
    assert body["app_account_token"] == apple_iap.owner_account_token(ORG)
    assert sorted(body["product_ids"]) == sorted(apple_iap.PRODUCT_PLANS)


@pytest.mark.asyncio
async def test_intent_refuses_a_stripe_billed_workspace(admin_client, monkeypatch):
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", True)
    monkeypatch.setattr(
        apple_router.billing_repo,
        "get_by_owner_id",
        AsyncMock(return_value={"owner_id": ORG, "stripe_subscription_status": "active"}),
    )
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 409 and r.json()["detail"] == "stripe_managed"


@pytest.mark.asyncio
async def test_intent_and_link_refuse_a_member(member_client, monkeypatch):
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", True)
    assert (await member_client.post("/api/v1/billing/apple/purchase-intent")).status_code == 403
    assert (await member_client.post("/api/v1/billing/apple/link", json={"signed_transaction": "x"})).status_code == 403


@pytest.mark.asyncio
async def test_link_returns_the_plan(admin_client, monkeypatch):
    sub = apple_iap.AppleSubscription(
        original_transaction_id="1",
        product_id="company.thelightbulb.mobile.base.monthly",
        expires_at_ms=None,
        status="active",
        plan="base",
    )
    monkeypatch.setattr(apple_router, "link_purchase", AsyncMock(return_value=sub))
    r = await admin_client.post("/api/v1/billing/apple/link", json={"signed_transaction": "jws"})
    assert r.status_code == 200 and r.json()["plan"] == "base"


@pytest.mark.asyncio
async def test_link_maps_stripe_managed_to_409(admin_client, monkeypatch):
    monkeypatch.setattr(apple_router, "link_purchase", AsyncMock(side_effect=apple_iap.StripeManaged("stripe_managed")))
    r = await admin_client.post("/api/v1/billing/apple/link", json={"signed_transaction": "jws"})
    assert r.status_code == 409


# --- An open Stripe Checkout must not complete alongside an Apple purchase ---


def _pending_checkout(monkeypatch, *, session: dict, expire_error: Exception | None = None):
    """A workspace row carrying a pending Checkout marker, plus a fake Stripe."""
    import stripe as real_stripe
    from core.services import billing_service

    row = {"owner_id": ORG, "stripe_customer_id": "cus_1", "pending_checkout_session_id": "cs_1"}
    monkeypatch.setattr(apple_router.settings, "APPLE_IAP_SELLING", True)
    monkeypatch.setattr(apple_router.billing_repo, "get_by_owner_id", AsyncMock(return_value=row))
    claim = AsyncMock(return_value="tok")
    release = AsyncMock()
    monkeypatch.setattr(apple_router.billing_repo, "try_claim_checkout_slot", claim)
    monkeypatch.setattr(apple_router.billing_repo, "release_checkout_slot", release)
    fake = MagicMock()
    fake.error = real_stripe.error
    fake.checkout.Session.retrieve.return_value = session
    if expire_error is not None:
        fake.checkout.Session.expire.side_effect = expire_error
    monkeypatch.setattr(billing_service, "stripe", fake)
    return fake, claim, release


@pytest.mark.asyncio
async def test_intent_expires_an_open_checkout_then_succeeds(admin_client, monkeypatch):
    fake, claim, release = _pending_checkout(monkeypatch, session={"id": "cs_1", "status": "open"})
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 200, r.text
    fake.checkout.Session.expire.assert_called_once_with("cs_1")
    claim.assert_awaited_once()
    release.assert_awaited_once_with(ORG, token="tok")


@pytest.mark.asyncio
async def test_intent_409s_when_the_open_checkout_cannot_be_expired(admin_client, monkeypatch):
    import stripe as real_stripe

    _pending_checkout(
        monkeypatch,
        session={"id": "cs_1", "status": "open"},
        expire_error=real_stripe.error.InvalidRequestError("not open", None),
    )
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 409 and r.json()["detail"] == "checkout_in_progress"


@pytest.mark.asyncio
async def test_intent_409s_while_another_checkout_holds_the_slot(admin_client, monkeypatch):
    _, claim, _ = _pending_checkout(monkeypatch, session={"id": "cs_1", "status": "open"})
    claim.return_value = None
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 409 and r.json()["detail"] == "checkout_in_progress"


@pytest.mark.asyncio
async def test_intent_refuses_when_the_checkout_completed_into_a_live_subscription(admin_client, monkeypatch):
    fake, _, _ = _pending_checkout(
        monkeypatch, session={"id": "cs_1", "status": "complete", "subscription": {"status": "active"}}
    )
    r = await admin_client.post("/api/v1/billing/apple/purchase-intent")
    assert r.status_code == 409 and r.json()["detail"] == "stripe_managed"
    fake.checkout.Session.expire.assert_not_called()


@pytest.mark.asyncio
async def test_link_maps_checkout_in_progress_to_409(admin_client, monkeypatch):
    monkeypatch.setattr(
        apple_router, "link_purchase", AsyncMock(side_effect=apple_iap.CheckoutInProgress("checkout_in_progress"))
    )
    r = await admin_client.post("/api/v1/billing/apple/link", json={"signed_transaction": "jws"})
    assert r.status_code == 409 and r.json()["detail"] == "checkout_in_progress"
