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

from unittest.mock import AsyncMock, patch

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
async def test_link_resolves_owner_from_auth_context_without_500():
    """The owner must come from resolve_owner_id. A 400 here is fine — it means
    the handler ran and rejected the payload; a 500 means it never got that far."""
    app = _app_with_auth(PERSONAL)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/billing/apple/link", json={})
    assert r.status_code == 400, r.text


@pytest.mark.asyncio
async def test_link_passes_the_resolved_owner_to_the_service():
    """Personal context → user_id. Pins that the value threaded into billing is
    the resolved owner, not something else that merely exists on AuthContext."""
    app = _app_with_auth(PERSONAL)
    with patch.object(apple_iap, "link_purchase", AsyncMock()) as link:
        link.return_value = type("S", (), {"status": "active", "product_id": "p", "expires_at_ms": 1})()
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/billing/apple/link", json={"signed_transaction": "jws"})

    assert r.status_code == 200, r.text
    assert link.await_args.kwargs["owner_id"] == "user_1"


@pytest.mark.asyncio
async def test_org_context_resolves_to_the_org_and_is_refused_by_the_checkout_fence():
    """Org context → org_id, and the personal-plan endpoints refuse it. Also
    proves the fence reads the RESOLVED owner rather than the user id, which
    would have let every org member straight through."""
    app = _app_with_auth(ORG)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/api/v1/billing/apple/web-checkout", json={})
    assert r.status_code == 403, r.text
    assert "per seat" in r.text


@pytest.mark.asyncio
async def test_portal_resolves_the_owner_too():
    """The portal handler resolves its own owner; a missing binding here 500s
    exactly like the link route did."""
    app = _app_with_auth(PERSONAL)
    with patch.object(apple_iap.billing_repo, "get_by_owner_id", AsyncMock(return_value=None)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/v1/billing/apple/portal", json={})
    assert r.status_code == 404, r.text
