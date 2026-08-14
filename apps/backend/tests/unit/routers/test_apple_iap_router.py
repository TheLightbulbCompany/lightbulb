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

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

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
