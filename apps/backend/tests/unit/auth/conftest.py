"""Two tenants, A and B, signed in with real Clerk session tokens (RS256, checked by get_current_user against
a test JWKS), over moto DynamoDB with every table the request paths reach. Each test seeds A's rows with the
product's own writers, carrying MARKER, then calls a door as B.

`tenants` is parametrized: "user" (no `o` claim: the tenant is the person) and "org" (each person is admin of
their own org). Every AWS call and every outbound HTTP request B's call makes is recorded (`calls`), and the
network is closed: nothing reaches Stripe, Clerk, Twilio or AWS."""

import json
import re
import socket
import time
from dataclasses import dataclass

import boto3
import botocore.client
import httpx
import jwt
import pytest
import stripe
from cryptography.hazmat.primitives.asymmetric import rsa
from moto import mock_aws

from core import auth, dynamodb
from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from tests.contract.conftest import _create_contract_tables

MARKER = "zebra-a-7f3q"  # in every row seeded for A; B's responses and calls must never carry it

_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
_JWK = {**jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True), "kid": "test", "use": "sig"}


@dataclass(frozen=True)
class Tenant:
    user_id: str
    org_id: str | None
    token: str = "session"  # or "oauth" (an `at+jwt` access token), "template" (a JWT-template token: no `sid`)

    @property
    def tenant(self) -> str:
        return resolve_owner_id(AuthContext(user_id=self.user_id, org_id=self.org_id, human_session=True))

    @property
    def headers(self) -> dict:
        now = int(time.time())
        claims = {"sub": self.user_id, "iss": settings.CLERK_ISSUER, "iat": now, "exp": now + 3600}
        claims |= {"email": f"{self.user_id}@example.com"}
        if self.token != "template":
            claims["sid"] = f"sess_{self.user_id}"
        if self.token == "oauth":
            claims["client_id"] = "oauth_app"
        if self.org_id:
            claims["o"] = {"id": self.org_id, "rol": "admin", "slg": self.org_id}
        typ = {"typ": "at+jwt"} if self.token == "oauth" else {}
        token = jwt.encode(claims, _KEY, algorithm="RS256", headers={"kid": "test", **typ})
        return {"Authorization": f"Bearer {token}"}


def _table(client, name: str, key: list, gsis: dict | None = None) -> None:
    """Keys as tests/contract/conftest.py writes them: [(attribute, HASH|RANGE, S|N)]."""
    schema = lambda k: [{"AttributeName": a, "KeyType": t} for a, t, _ in k]  # noqa: E731
    attrs = {(a, t) for k in [key, *(gsis or {}).values()] for a, _, t in k}
    indexes = [
        {"IndexName": n, "KeySchema": schema(k), "Projection": {"ProjectionType": "ALL"}}
        for n, k in (gsis or {}).items()
    ]
    client.create_table(
        TableName=name,
        KeySchema=schema(key),
        AttributeDefinitions=[{"AttributeName": a, "AttributeType": t} for a, t in sorted(attrs)],
        BillingMode="PAY_PER_REQUEST",
        **({"GlobalSecondaryIndexes": indexes} if indexes else {}),
    )


def _create_tables(resource) -> None:
    """The contract suite's tables, plus the ones named by settings rather than the prefix."""
    client = resource.meta.client
    _create_contract_tables(client, dynamodb.table_name)
    pk, sk, h = ("pk", "HASH", "S"), ("sk", "RANGE", "S"), "HASH"
    by_member = [("memberKey", h, "S"), ("memberSort", "RANGE", "S")]
    _table(
        client, settings.MESSAGING_TABLE, [pk, sk],
        {"by-member": by_member, "by-run-deadline": [("sweep", h, "S"), ("deadlineAt", "RANGE", "N")]},
    )  # fmt: skip
    _table(client, settings.WS_CONNECTIONS_TABLE, [("connectionId", h, "S")], {"by-owner-id": [("ownerId", h, "S")]})
    _table(client, settings.OAUTH_TOKENS_TABLE, [("user_id", h, "S")])
    for name in (settings.METRONOME_FAST_GATE_TABLE, dynamodb.table_name("openrouter-keys")):
        _table(client, name, [("owner_id", h, "S")])


@pytest.fixture
def ddb(monkeypatch):
    monkeypatch.setattr(settings, "MESSAGING_TABLE", "test-messaging")
    monkeypatch.setattr(settings, "OAUTH_TOKENS_TABLE", "test-oauth-tokens")
    monkeypatch.setattr(settings, "METRONOME_FAST_GATE_TABLE", "test-metronome-fast-gate")
    monkeypatch.setattr(settings, "WEBHOOK_DEDUP_TABLE", dynamodb.table_name("webhook-event-dedup"))
    monkeypatch.setattr(settings, "OWNER_STATE_BUCKET", "test-owner-state")
    monkeypatch.setattr(settings, "AGENT_EMAIL_DOMAIN", "mail.test")
    monkeypatch.setenv("OAUTH_TOKENS_TABLE", "test-oauth-tokens")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        monkeypatch.setattr(dynamodb, "_dynamodb_resource", resource)
        _create_tables(resource)
        boto3.client("s3", region_name="us-east-1").create_bucket(Bucket=settings.OWNER_STATE_BUCKET)
        yield resource


@pytest.fixture
def jwks(monkeypatch) -> None:
    """get_current_user checks the tokens Tenant mints against this key."""

    async def keys(url):
        return {"keys": [_JWK]}

    monkeypatch.setattr(auth, "_get_cached_jwks", keys)


@pytest.fixture(params=["user", "org"])
def tenants(request, ddb, jwks) -> tuple[Tenant, Tenant]:
    if request.param == "user":
        return Tenant("user_a", None), Tenant("user_b", None)
    return Tenant("user_a", "org_a"), Tenant("user_b", "org_b")


@pytest.fixture
def calls(monkeypatch) -> list:
    """(kind, what, params) of every AWS call, outbound HTTP request and Stripe call; the network is closed."""
    seen: list = []
    aws = botocore.client.BaseClient._make_api_call

    def record_aws(self, operation, params):
        seen.append(("aws", f"{self.meta.service_model.service_name}.{operation}", params))
        return aws(self, operation, params)

    def record_http(self, request):
        seen.append(("http", str(request.url), request.content))
        return httpx.Response(503, request=request, json={"error": "no network in tests"})

    async def record_http_async(self, request):
        return record_http(self, request)

    def no_network(*args, **kwargs):
        raise OSError("no network in tests")

    class _Stripe(stripe.HTTPClient):  # every Stripe call answers one generic object
        name = "test"

        def request(self, method, url, headers, post_data=None, *, _usage=None):
            seen.append(("stripe", f"{method} {url}", post_data))
            path = url.split("?")[0]
            listing = {"object": "list", "data": [], "has_more": False, "url": path}
            if method == "get" and "_" not in path.rsplit("/", 1)[-1]:
                return json.dumps(listing), 200, {}
            obj = {"id": "fake_1", "object": "fake", "status": "active", "url": "https://stripe.test/s"}
            return json.dumps({**obj, "items": {**listing, "url": "/v1/subscription_items"}}), 200, {}

        def close(self):
            pass

    monkeypatch.setattr(stripe, "api_key", "sk_test_cross_account")
    monkeypatch.setattr(stripe, "default_http_client", _Stripe())
    monkeypatch.setattr(botocore.client.BaseClient, "_make_api_call", record_aws)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", record_http)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", record_http_async)
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    return seen


def mentions(text: str, needles) -> list[str]:
    """The needles *text* names, each as a whole token: "user_a" is not in "user_agent", "org_a" not in "org_admin"."""
    return [n for n in needles if re.search(rf"(?<![A-Za-z0-9]){re.escape(n)}(?![A-Za-z0-9])", text)]


def snapshot(resource, *needles: str) -> list[str]:
    """Every DynamoDB item (any table) and S3 object (key and body) carrying one of *needles*, sorted."""
    out = []
    for name in resource.meta.client.list_tables()["TableNames"]:
        for item in resource.Table(name).scan()["Items"]:
            text = json.dumps(item, default=str, sort_keys=True)
            if mentions(text, needles):
                out.append(f"{name} {text}")
    s3 = boto3.client("s3", region_name="us-east-1")
    for obj in s3.list_objects_v2(Bucket=settings.OWNER_STATE_BUCKET).get("Contents", []):
        body = s3.get_object(Bucket=settings.OWNER_STATE_BUCKET, Key=obj["Key"])["Body"].read().decode("latin-1")
        if mentions(f"{obj['Key']} {body}", needles):
            out.append(f"s3 {obj['Key']} {body}")
    return sorted(out)
