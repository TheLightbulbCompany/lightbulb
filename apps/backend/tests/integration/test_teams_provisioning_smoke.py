"""End-to-end smoke for the Teams BFF: provision (DDB-seeded) -> create
agent through the BFF -> assert the upstream POST that hits Paperclip
carries the canonical ``openclaw_gateway`` adapter shape with the
underscore form (the bug Tasks 1-26 fix) and the user's own service
token + sessionKey.

This is the only pre-Playwright check that the FULL ``_ctx`` chain
(auth -> paperclip_repo.get -> user-session sign-in -> admin.create_agent)
runs end-to-end. Previous unit tests in
``tests/unit/routers/teams/test_agents.py`` mock ``_ctx`` out wholesale
and only cover the synthesis call; this file exercises the real
dependency chain with only Clerk, DynamoDB, and the upstream HTTP
transport mocked. Why it matters:

  - the previous production bug was ``adapterType="openclaw-gateway"``
    (hyphen) reaching Paperclip's ``assertKnownAdapterType``, which
    rejects everything that isn't the underscore form. Mocked unit
    tests passed because they verified the kwarg, not the wire body.
    This smoke verifies the wire body.
  - the adapter-config dict shape is sealed: ``{url, authToken,
    sessionKeyStrategy, sessionKey}`` and nothing else. Any future
    code path that smuggles in a ``headers`` / ``deviceToken`` /
    ``password`` field via the synthesizer would fail this test.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import boto3
import httpx
import pytest
from fastapi.testclient import TestClient
from moto import mock_aws

# service_token.mint() is called on the /teams agents.create path now;
# pin a deterministic key so the smoke test can decode and assert on the
# minted JWT.
os.environ.setdefault(
    "PAPERCLIP_SERVICE_TOKEN_KEY",
    "test-key-teams-provisioning-smoke",
)

from core.auth import AuthContext, get_current_user  # noqa: E402
from core.encryption import encrypt  # noqa: E402
from core.repositories.paperclip_repo import PaperclipCompany, PaperclipRepo  # noqa: E402
from core.services.paperclip_admin_client import PaperclipAdminClient  # noqa: E402

PAPERCLIP_COMPANIES_TABLE = "paperclip-companies"

# Allowlist regex from ``paperclip_adapter_config.py`` — must match the
# url synthesized by the BFF or the test (correctly) fails.
_GATEWAY_URL_RE = re.compile(r"\A(?:wss://ws(?:-[a-z]+)?\.isol8\.co|ws://localhost:[0-9]+)\Z")


@pytest.fixture
def smoke_user_id() -> str:
    return "user_smoke_27"


@pytest.fixture
def smoke_service_token_plaintext() -> str:
    return "openclaw-service-token-smoke-27"


@pytest.fixture
def smoke_company_id() -> str:
    return "co_smoke_27"


@pytest.fixture
def smoke_paperclip_user_id() -> str:
    return "pcu_smoke_27"


@pytest.fixture
def captured_upstream_calls():
    """Mutable dict the MockTransport handler writes into so tests can
    inspect every upstream call by path."""
    return {}


@pytest.fixture
def mocked_paperclip_transport(captured_upstream_calls):
    """httpx.MockTransport that fakes Paperclip's REST API for the smoke.

    Routes:
      - POST /api/auth/sign-in/email -> 200 {user, token} + Set-Cookie
      - POST /api/companies/{co}/agents -> 200 {id, name, role, ...}
        (captures the body for assertions)
    Everything else 404s — keeps the test honest about which upstream
    routes the BFF actually hits.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        body_bytes = request.read()
        captured_upstream_calls.setdefault(path, []).append(
            {
                "method": request.method,
                "body": body_bytes,
                "headers": dict(request.headers),
            }
        )

        if request.method == "POST" and path == "/api/auth/sign-in/email":
            # Better Auth's sign-in response shape; the admin client
            # extracts the .session_token cookie via _extract_session_cookie.
            return httpx.Response(
                200,
                json={"user": {"id": "pcu_smoke_27"}, "token": "tok_smoke"},
                headers={
                    "Set-Cookie": ("paperclip-test.session_token=signed-cookie-value; Path=/; HttpOnly; SameSite=Lax"),
                },
            )

        if request.method == "POST" and re.match(r"^/api/companies/[^/]+/agents$", path):
            # Echo the request back; the BFF strips adapter fields on
            # the read path, so this won't leak into the response.
            payload = json.loads(body_bytes.decode()) if body_bytes else {}
            return httpx.Response(
                200,
                json={
                    "id": "agent_smoke_new",
                    "name": payload.get("name"),
                    "role": payload.get("role"),
                },
            )

        return httpx.Response(404, json={"error": f"unmocked route {request.method} {path}"})

    return httpx.MockTransport(handler)


@pytest.fixture
def smoke_env(mocked_paperclip_transport):
    """Sets up:
    - moto-backed paperclip-companies DDB table with the 'active'
      row for ``smoke_user_id``.
    - a real PaperclipAdminClient pointed at the mocked transport.
    - patches ``routers.teams.agents._admin`` to return that client.
    - patches ``core.services.paperclip_user_session.get_user_session_cookie``'s
      Clerk email resolver dependency by patching ``core.services.clerk_admin.get_user``.
    - sets ENVIRONMENT='dev' so ``_ws_gateway_url`` resolves
      to ``wss://ws-dev.isol8.co`` (in the allowlist).
    """
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        resource.create_table(
            TableName=PAPERCLIP_COMPANIES_TABLE,
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "user_id", "AttributeType": "S"},
                {"AttributeName": "status", "AttributeType": "S"},
                {"AttributeName": "scheduled_purge_at", "AttributeType": "S"},
                {"AttributeName": "org_id", "AttributeType": "S"},
            ],
            BillingMode="PAY_PER_REQUEST",
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-status-purge-at",
                    "KeySchema": [
                        {"AttributeName": "status", "KeyType": "HASH"},
                        {"AttributeName": "scheduled_purge_at", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                },
                {
                    "IndexName": "by-org-id",
                    "KeySchema": [{"AttributeName": "org_id", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                },
            ],
        )

        # Build a real admin client wired to the mocked Paperclip transport.
        http = httpx.AsyncClient(transport=mocked_paperclip_transport, base_url="http://paperclip.test")
        admin = PaperclipAdminClient(http_client=http)

        # Point core.dynamodb.get_table at the moto resource and disable
        # the env prefix (matches the pattern used by tests/test_paperclip_repo.py).
        with (
            patch("core.dynamodb._table_prefix", ""),
            patch("core.dynamodb._dynamodb_resource", resource),
            patch("core.config.settings.ENVIRONMENT", "dev"),
            # Keep PAPERCLIP_PUBLIC_URL unset so the admin client's
            # _headers() helper doesn't add an Origin header. The test
            # doesn't assert on Origin (covered by dedicated unit tests).
            patch("core.config.settings.PAPERCLIP_PUBLIC_URL", ""),
        ):
            yield {
                "admin": admin,
                "resource": resource,
                "transport": mocked_paperclip_transport,
            }


@pytest.fixture
def seeded_company(
    smoke_env,
    smoke_user_id,
    smoke_company_id,
    smoke_paperclip_user_id,
    smoke_service_token_plaintext,
):
    """Insert an 'active' paperclip-companies row for the smoke user.

    Uses the real PaperclipRepo (resolved against the moto resource via
    smoke_env's monkeypatch) so this exercises the same DDB read path
    the BFF takes at request time.
    """
    repo = PaperclipRepo(table_name=PAPERCLIP_COMPANIES_TABLE)
    now = datetime.now(timezone.utc)
    company = PaperclipCompany(
        user_id=smoke_user_id,
        org_id="org_smoke_27",
        company_id=smoke_company_id,
        paperclip_user_id=smoke_paperclip_user_id,
        paperclip_password_encrypted=encrypt("smoke-password"),
        service_token_encrypted=encrypt(smoke_service_token_plaintext),
        status="active",
        created_at=now,
        updated_at=now,
    )

    # Run the async put synchronously.
    import asyncio

    asyncio.get_event_loop().run_until_complete(repo.put(company))
    return company


async def test_create_agent_through_bff_sends_canonical_adapter(
    smoke_env,
    seeded_company,
    smoke_user_id,
    smoke_service_token_plaintext,
    smoke_company_id,
    captured_upstream_calls,
):
    """Provision (DDB-seeded) -> POST /api/v1/teams/agents -> upstream
    POST /api/companies/{co}/agents carries the canonical wire shape.

    Mocks pinned at:
      - DynamoDB (moto) — real repo reads.
      - Paperclip upstream (httpx.MockTransport) — real admin client.
      - Clerk get_user — for the email resolver inside
        ``get_user_session_cookie``.
      - Clerk JWT — replaced by FastAPI dependency override of
        ``get_current_user``.

    Everything else (BFF Pydantic schemas, ``_ctx`` chain,
    ``synthesize_openclaw_adapter``, ``_admin().create_agent`` body
    assembly) runs for real.
    """
    from main import app
    from routers.teams import agents as agents_mod

    # Override Clerk JWT auth — the BFF's _ctx chain reads auth.user_id
    # to look up the company row.
    async def _fake_current_user() -> AuthContext:
        return AuthContext(user_id=smoke_user_id, org_id="org_smoke_27")

    app.dependency_overrides[get_current_user] = _fake_current_user

    # Pin the admin client to the mocked-transport one. The real _admin()
    # singleton would build a fresh httpx client against
    # settings.PAPERCLIP_INTERNAL_URL, which would 404 in tests.
    original_admin = agents_mod._admin
    agents_mod._admin = lambda: smoke_env["admin"]

    # Mock the Clerk email lookup that get_user_session_cookie's
    # clerk_email_resolver eventually hits.
    fake_clerk_user = {
        "id": smoke_user_id,
        "primary_email_address_id": "idn_primary",
        "email_addresses": [{"id": "idn_primary", "email_address": "smoke@example.com"}],
    }

    try:
        with patch(
            "core.services.clerk_admin.get_user",
            new=AsyncMock(return_value=fake_clerk_user),
        ):
            with TestClient(app) as client:
                resp = client.post(
                    "/api/v1/teams/agents",
                    json={"name": "Smoke Agent", "role": "engineer"},
                    headers={"Authorization": "Bearer fake-clerk-jwt"},
                )
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        agents_mod._admin = original_admin

    assert resp.status_code == 200, f"BFF returned {resp.status_code}: {resp.text}"

    # The BFF response itself MUST NOT carry adapter fields (the
    # _redact_agent defense-in-depth strip).
    bff_body = resp.json()
    assert "adapterConfig" not in bff_body
    assert "adapterType" not in bff_body
    assert bff_body.get("name") == "Smoke Agent"

    # The BFF should have called BOTH:
    #   1. Better Auth sign-in (per-request, _ctx -> get_user_session_cookie)
    #   2. Paperclip companies/{co}/agents (the create_agent admin call)
    assert "/api/auth/sign-in/email" in captured_upstream_calls, (
        f"BFF should have signed in upstream as the user; captured: {list(captured_upstream_calls)}"
    )
    create_agent_path = f"/api/companies/{smoke_company_id}/agents"
    assert create_agent_path in captured_upstream_calls, (
        f"BFF should have hit create_agent at {create_agent_path}; captured: {list(captured_upstream_calls)}"
    )
    create_calls = captured_upstream_calls[create_agent_path]
    assert len(create_calls) == 1, f"expected exactly one create_agent call, got {len(create_calls)}"

    # ------ The core wire-shape assertions ------
    upstream_body = json.loads(create_calls[0]["body"].decode())

    # 1. adapterType is the canonical underscore form (the production bug).
    assert upstream_body["adapterType"] == "openclaw_gateway", (
        f"adapterType must be the underscore form 'openclaw_gateway' "
        f"(canonical per paperclip/packages/shared/src/constants.ts); "
        f"got {upstream_body.get('adapterType')!r}"
    )

    # 2. The adapterConfig dict carries exactly the canonical four fields.
    adapter_config = upstream_body["adapterConfig"]
    assert set(adapter_config.keys()) == {"url", "authToken", "sessionKeyStrategy", "sessionKey"}, (
        f"adapterConfig must have exactly the canonical 4 keys; got {set(adapter_config.keys())}"
    )

    # 3. URL matches the gateway allowlist (env=dev -> wss://ws-dev.isol8.co).
    assert _GATEWAY_URL_RE.match(adapter_config["url"]), (
        f"adapterConfig.url must match the gateway allowlist; got {adapter_config['url']!r}"
    )

    # 4. authToken is a freshly-minted service-token JWT whose ``sub``
    # claim equals the container's owner_id (org_id for org members,
    # user_id for personal owners). Codex P1 finding 3 (PR #563): the
    # BFF used to pass through the persisted ``service_token_encrypted``
    # (decrypted), which for legacy member rows could carry the wrong
    # ``sub``; the route now mints fresh per call. The smoke fixture
    # plants an org-context auth (org_id="org_smoke_27"), so the new
    # token must be keyed at the org_id.
    import jwt as _jwt_lib

    auth_token = adapter_config["authToken"]
    assert auth_token, "authToken must not be empty"
    assert auth_token != smoke_service_token_plaintext, (
        "authToken must be a freshly-minted JWT, not the persisted "
        "service_token_encrypted (the legacy pre-fix behavior)"
    )
    minted_claims = _jwt_lib.decode(
        auth_token,
        os.environ["PAPERCLIP_SERVICE_TOKEN_KEY"],
        algorithms=["HS256"],
    )
    assert minted_claims["sub"] == "org_smoke_27", (
        f"authToken sub must equal owner_id (org_id for org members); got {minted_claims['sub']!r}"
    )
    assert minted_claims["kind"] == "paperclip_service"

    # 5. sessionKeyStrategy is "issue" so each Paperclip issue gets its
    # own OpenClaw session (transcripts don't bleed across unrelated
    # issues). The sessionKey field is the fallback for manual wakes
    # without an issueId.
    assert adapter_config["sessionKeyStrategy"] == "issue"

    # 6. sessionKey is the user_id (so the gateway routes to the right
    # OpenClaw container regardless of which agent is calling).
    assert adapter_config["sessionKey"] == smoke_user_id

    # 7. Defense-in-depth: forbidden fields that earlier audit flagged
    # as smuggling carriers MUST NOT appear in adapterConfig.
    forbidden = {"headers", "password", "deviceToken", "token"}
    leaked = forbidden & set(adapter_config.keys())
    assert not leaked, f"forbidden fields leaked into adapterConfig: {leaked}"

    # 8. The whole upstream POST body should also not carry any
    # client-smuggleable adapter alias keys at the top level beyond
    # adapterType + adapterConfig + the request fields.
    # CreateAgentBody defines: name, role, title, capabilities,
    # reports_to (-> reportsTo), budget_monthly_cents (-> budgetMonthlyCents).
    allowed_top_level = {
        "name",
        "role",
        "adapterType",
        "adapterConfig",
        "title",
        "capabilities",
        "reportsTo",
        "budgetMonthlyCents",
    }
    extra_top_level = set(upstream_body.keys()) - allowed_top_level
    assert not extra_top_level, f"unexpected top-level fields in upstream create_agent body: {extra_top_level}"
