"""End-to-end smoke for the Teams BFF: provision (DDB-seeded) -> create
agent through the BFF -> assert the upstream POST that hits Paperclip
carries the canonical ``openclaw_gateway`` adapter shape with the
underscore form (the bug Tasks 1-26 fix) and the workspace's hook token.

This is the only pre-Playwright check that the FULL ``_ctx`` chain
(auth -> workspace + identity rows -> user-session sign-in -> admin.create_agent)
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
from core.repositories.paperclip_repo import PaperclipIdentity, PaperclipRepo, PaperclipWorkspace  # noqa: E402
from core.services.paperclip_admin_client import PaperclipAdminClient  # noqa: E402

PAPERCLIP_COMPANIES_TABLE = "paperclip-companies"


@pytest.fixture
def smoke_user_id() -> str:
    return "user_smoke_27"


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
      to ``wss://bv-dev.<configured apex>/api/v1/gateway/ws`` (in the allowlist).
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

        # The agent-create door builds the adapter from the owner's containers row -- and the
        # shared builder REFUSES an adapter with no hook token (every run would 401 at the
        # facade, reported per RUN rather than as a broken agent; M4-R33). The row is real
        # here rather than mocked, because "the wire shape Paperclip is sent" is exactly what
        # this smoke exists to pin end to end.
        containers = resource.create_table(
            TableName="containers",
            KeySchema=[{"AttributeName": "owner_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "owner_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        containers.put_item(
            Item={
                "owner_id": "org_smoke_27",
                "status": "running",
                "hook_token": "hook-smoke-27",
                "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/smoke",
            }
        )

        # Build a real admin client wired to the mocked Paperclip transport.
        http = httpx.AsyncClient(transport=mocked_paperclip_transport, base_url="http://paperclip.test")
        admin = PaperclipAdminClient(http_client=http)

        # Point core.dynamodb.get_table at the moto resource and disable
        # the env prefix (matches the pattern used by tests/unit/repositories/test_paperclip_repo.py).
        with (
            patch("core.dynamodb._table_prefix", ""),
            patch("core.dynamodb._dynamodb_resource", resource),
            patch("core.config.settings.ENVIRONMENT", "dev"),
            # Keep PAPERCLIP_PUBLIC_URL unset so the admin client's
            # _headers() helper doesn't add an Origin header. The test
            # doesn't assert on Origin (covered by dedicated unit tests).
            patch("core.config.settings.PAPERCLIP_PUBLIC_URL", ""),
            # The Teams BFF cold cookie-mint sets an HttpOnly binding cookie
            # (isol8_pc_bind) signed with PAPERCLIP_SERVICE_TOKEN_KEY read from
            # the settings singleton (the same source the proxy's
            # isol8_paperclip cookie uses). The module-level
            # os.environ.setdefault above covers service_token (which reads
            # os.environ); mirror it onto settings so the binding mint resolves.
            patch(
                "core.config.settings.PAPERCLIP_SERVICE_TOKEN_KEY",
                os.environ["PAPERCLIP_SERVICE_TOKEN_KEY"],
            ),
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
):
    """Insert the workspace's 'active' row (the smoke user joined) and the
    smoke user's settled identity row.

    Uses the real PaperclipRepo (resolved against the moto resource via
    smoke_env's monkeypatch) so this exercises the same DDB read path
    the BFF takes at request time.
    """
    repo = PaperclipRepo(table_name=PAPERCLIP_COMPANIES_TABLE)
    now = datetime.now(timezone.utc)
    ws = PaperclipWorkspace(
        org_id="org_smoke_27",
        company_id=smoke_company_id,
        status="active",
        created_at=now,
        updated_at=now,
        provisioner_user_id=smoke_user_id,
        member_user_ids={smoke_user_id},
    )
    identity = PaperclipIdentity(
        user_id=smoke_user_id,
        paperclip_user_id=smoke_paperclip_user_id,
        paperclip_password_encrypted=encrypt("smoke-password"),
    )

    # Run the async puts synchronously.
    import asyncio

    loop = asyncio.get_event_loop()
    loop.run_until_complete(repo.put_workspace(ws))
    loop.run_until_complete(repo.put_identity(identity))
    return ws


async def test_create_agent_through_bff_sends_canonical_adapter(
    smoke_env,
    seeded_company,
    smoke_user_id,
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

    # Override Clerk JWT auth — the BFF's _ctx chain reads auth.org_id for
    # the workspace row and auth.user_id for the identity row.
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
        with (
            patch(
                "core.services.clerk_admin.get_user",
                new=AsyncMock(return_value=fake_clerk_user),
            ),
            # A genuine end-to-end profile write needs a billing-accounts table + an S3 bucket
            # this smoke does not set up, and is not this test's point anyway (the wire shape
            # sent to Paperclip's create endpoint, asserted below, is) -- so it is mocked to a
            # no-op success here.
            patch(
                "core.services.hermes_provisioning.write_profile_for_agent",
                new=AsyncMock(return_value=None),
            ),
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
    assert upstream_body["adapterType"] == "hermes_gateway", (
        f"adapterType must be the underscore form 'hermes_gateway' "
        f"(canonical per paperclip/packages/shared/src/constants.ts); "
        f"got {upstream_body.get('adapterType')!r}"
    )

    # 2. The adapterConfig dict carries exactly the canonical fields for Paperclip's NATIVE
    #    adapter: an owner-scoped callback URL into our backend facade and that workspace's hook
    #    token, plus the run-timeout / reconnect / poll bounds. No device identity, no relay URL,
    #    no service JWT -- the device-paired gateway adapter went with the ECS runtime, and with
    #    it the per-agent ed25519 key it made the operator relay sign the handshake with.
    #
    #    There is deliberately NO pre-run cap gate here. The free tier is enforced at the AWS
    #    layer (the bedrock-gate refuses over-cap requests before a token is generated), so an
    #    adapter-side allowance check would be unreachable code.
    adapter_config = upstream_body["adapterConfig"]
    assert set(adapter_config.keys()) == {
        "apiBaseUrl",
        "apiKey",
        "sessionKeyStrategy",
        "timeoutSec",
        "eventReconnectMs",
        "pollIntervalMs",
    }, f"adapterConfig must have exactly the canonical keys; got {set(adapter_config.keys())}"

    # 3. The callback URL is scoped to the OWNER (org_id for org members, user_id otherwise).
    #    The smoke fixture plants an org-context auth, so a member-keyed URL here would point
    #    Paperclip's runs at a workspace that is not the one the agent belongs to.
    assert adapter_config["apiBaseUrl"].endswith("/api/v1/internal/paperclip/hermes/org_smoke_27"), (
        f"apiBaseUrl must be scoped to the owner; got {adapter_config['apiBaseUrl']!r}"
    )

    # 4. apiKey is the WORKSPACE's hook token. It is a SNAPSHOT (Paperclip persists it as
    #    a managed company secret), so rotating the owner's hook token means re-syncing every
    #    agent's adapter config -- see sync_agent_adapter_config.
    assert adapter_config["apiKey"], "apiKey must not be empty"
    assert adapter_config["apiKey"] == "hook-smoke-27", "apiKey must be the WORKSPACE's hook token"

    # 5. sessionKeyStrategy is "issue" so each Paperclip issue gets its own session
    #    (transcripts don't bleed across unrelated issues). There is no ``sessionKey`` fallback
    #    any more: the native adapter derives the real agent id from Paperclip's signed run
    #    context and sends it in ``X-Hermes-Session-Key``.
    assert adapter_config["sessionKeyStrategy"] == "issue"

    # 6. Defense-in-depth: forbidden fields that earlier audit flagged as smuggling carriers
    #    MUST NOT appear in adapterConfig -- plus the device identity this adapter replaced.
    forbidden = {"headers", "password", "deviceToken", "token", "authToken", "devicePrivateKeyPem", "url"}
    leaked = forbidden & set(adapter_config.keys())
    assert not leaked, f"forbidden fields leaked into adapterConfig: {leaked}"

    # 7. The whole upstream POST body should also not carry any
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
        "permissions",
        # Backend-built, never client-supplied: the engineer run cap (paperclip_adapter_config).
        "runtimeConfig",
    }
    extra_top_level = set(upstream_body.keys()) - allowed_top_level
    assert not extra_top_level, f"unexpected top-level fields in upstream create_agent body: {extra_top_level}"

    # 8. Governed agent creation: Paperclip 916 made new agents hire-capable by default,
    #    so every BFF-created agent must carry canCreateAgents=false explicitly.
    assert upstream_body["permissions"] == {"canCreateAgents": False}
