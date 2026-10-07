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

from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext, get_current_user
from routers import apple_iap


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(apple_iap.router, prefix="/api/v1")
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
    app.include_router(apple_iap.router, prefix="/api/v1")
    app.dependency_overrides[get_current_user] = lambda: auth
    return app


PERSONAL = AuthContext(user_id="user_1", email="a@b.co")
ORG = AuthContext(user_id="user_1", org_id="org_1", org_role="org:admin", email="a@b.co")


@pytest.mark.asyncio
async def test_retired_link_route_still_requires_authentication(client):
    async with client as c:
        r = await c.post("/api/v1/billing/apple/link", json={"signed_transaction": "jws"})
    assert r.status_code in {401, 403}, r.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"json": {}},
        {"json": {"signed_transaction": "jws"}},
        {"content": b"not-json", "headers": {"content-type": "application/json"}},
    ],
    ids=["missing-transaction", "signed-transaction", "malformed-body"],
)
async def test_retired_link_route_is_always_gone_before_verification(request_kwargs):
    app = _app_with_auth(ORG)
    # `link_purchase` is gone; `_verify_signed` is the shared door into Apple's
    # verifier that BOTH the retired link path and the live webhook used, so
    # patching it still proves the 410 fires before any verification.
    with patch.object(
        apple_iap.apple_iap_service,
        "_verify_signed",
        Mock(side_effect=AssertionError("retired route reached Apple verification")),
    ) as verify:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/billing/apple/link", **request_kwargs)

    assert r.status_code == 410, r.text
    verify.assert_not_called()


@pytest.mark.asyncio
async def test_retired_mobile_web_checkout_route_is_not_mounted():
    """There is one subscription purchase route: /billing/checkout."""
    app = _app_with_auth(ORG)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/billing/apple/web-checkout", json={})
    assert r.status_code == 404, r.text


@pytest.mark.asyncio
async def test_portal_resolves_the_workspace_owner_too():
    """The portal handler resolves its own owner; a missing binding here 500s
    exactly like the link route did."""
    app = _app_with_auth(ORG)
    with patch.object(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/billing/apple/portal", json={})
    assert r.status_code == 404, r.text


ORG_MEMBER = AuthContext(user_id="user_2", org_id="org_1", org_role="org:member", email="m@b.co")


@pytest.mark.asyncio
async def test_the_mobile_portal_refuses_a_non_admin_org_member():
    """resolve_owner_id returns the ORG's billing row in org context, so an
    unfenced portal session lets any member cancel the whole team's
    subscription. /billing/portal has always required admin; this route was
    written from its behaviour and inherited the reasoning without the guard."""
    app = _app_with_auth(ORG_MEMBER)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/billing/apple/portal", json={})
    assert r.status_code == 403, r.text


@pytest.mark.asyncio
async def test_the_mobile_portal_still_admits_an_org_admin():
    """The fence is on ROLE, not on org context — an admin managing the team's
    own subscription is the legitimate caller this route exists for."""
    app = _app_with_auth(ORG)
    with patch.object(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/billing/apple/portal", json={})
    # 404 "no billing account" — past the fence, which is what this pins.
    assert r.status_code == 404, r.text
