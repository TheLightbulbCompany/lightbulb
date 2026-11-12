"""
Contract test fixtures.

Uses httpx AsyncClient with ASGITransport and mocked auth dependencies.
No real database, gateway, or Clerk needed.
"""

import boto3
import httpx
import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from moto import mock_aws

from core.auth import AuthContext

# Every table the app's request paths can reach, mirroring apps/infra/lib/stacks/
# database-stack.ts (the source of truth — keep in sync when a table is added).
#
# WHY THE WHOLE SET and not just the tables today's routes touch: this suite boots
# the real ASGI app and fuzzes EVERY route in the OpenAPI schema, so any route may
# reach any table, and a table that is merely ABSENT surfaces as
# ResourceNotFoundException -> 500 -> `not_a_server_error` fails. Provisioning them
# one-at-a-time as failures appear is whack-a-mole that re-breaks every time a route
# starts reading a new table. Over-provisioning in moto is free.
#
# Format: short name -> (primary key, {gsi name: gsi key}). Keys are
# [(attribute, HASH|RANGE, S|N)]; AttributeDefinitions are derived from their union.
_CONTRACT_TABLES: dict[str, tuple[list[tuple[str, str, str]], dict[str, list[tuple[str, str, str]]]]] = {
    "users": ([("user_id", "HASH", "S")], {}),
    "containers": (
        [("owner_id", "HASH", "S")],
        {
            "status-index": [("status", "HASH", "S")],
        },
    ),
    "billing-accounts": (
        [("owner_id", "HASH", "S")],
        {
            "stripe-customer-index": [("stripe_customer_id", "HASH", "S")],
            "apple-original-transaction-index": [("apple_original_transaction_id", "HASH", "S")],
        },
    ),
    "usage-counters": ([("owner_id", "HASH", "S"), ("period", "RANGE", "S")], {}),
    "pending-updates": (
        [("owner_id", "HASH", "S"), ("update_id", "RANGE", "S")],
        {"status-index": [("status", "HASH", "S"), ("scheduled_at", "RANGE", "S")]},
    ),
    "channel-links": (
        [("owner_id", "HASH", "S"), ("sk", "RANGE", "S")],
        {"by-member": [("member_id", "HASH", "S"), ("owner_provider_agent", "RANGE", "S")]},
    ),
    "company-slugs": ([("slug", "HASH", "S")], {"by-owner": [("owner_id", "HASH", "S")]}),
    "agent-email-addresses": ([("address", "HASH", "S")], {"by-owner": [("owner_id", "HASH", "S")]}),
    "slack-installs": ([("team_id", "HASH", "S")], {"by-owner": [("owner_id", "HASH", "S")]}),
    "agent-mail-inbox": (
        [("address", "HASH", "S"), ("received_key", "RANGE", "S")],
        {"by-message-id": [("message_id", "HASH", "S")]},
    ),
    "webhook-event-dedup": ([("event_id", "HASH", "S")], {}),
    "admin-actions": (
        [("admin_user_id", "HASH", "S"), ("timestamp_action_id", "RANGE", "S")],
        {
            "target-timestamp-index": [
                ("target_user_id", "HASH", "S"),
                ("timestamp_action_id", "RANGE", "S"),
            ]
        },
    ),
    "metronome-fast-gate": ([("owner_id", "HASH", "S")], {}),
    "oauth-tokens": ([("user_id", "HASH", "S")], {}),
    "oauth-state": ([("state", "HASH", "S")], {}),
    "integrations": ([("owner_id", "HASH", "S"), ("provider", "RANGE", "S")], {}),
    "provisioning-workflows": (
        [("workflow_id", "HASH", "S")],
        {"owner-state-index": [("owner_id", "HASH", "S"), ("state", "RANGE", "S")]},
    ),
    "workspace-join-links": ([("org_id", "HASH", "S")], {}),
    "setup-requests": ([("id", "HASH", "S")], {}),
    "paperclip-companies": (
        [("user_id", "HASH", "S")],
        {
            "by-status-purge-at": [("status", "HASH", "S"), ("scheduled_purge_at", "RANGE", "S")],
            "by-org-id": [("org_id", "HASH", "S")],
        },
    ),
}


def _create_contract_tables(client, table_name) -> None:
    """Create every _CONTRACT_TABLES entry in moto under its prefixed name.

    ``table_name`` is ``core.dynamodb.table_name``, which prepends
    DYNAMODB_TABLE_PREFIX — repos resolve through it, so moto must create the
    fully-prefixed name or the lookup still misses.
    """
    for short_name, (pk, gsis) in _CONTRACT_TABLES.items():
        attrs = {(a, t) for a, _, t in pk}
        for gsi_key in gsis.values():
            attrs.update((a, t) for a, _, t in gsi_key)
        kwargs = {
            "TableName": table_name(short_name),
            "KeySchema": [{"AttributeName": a, "KeyType": k} for a, k, _ in pk],
            "AttributeDefinitions": [{"AttributeName": a, "AttributeType": t} for a, t in sorted(attrs)],
            "BillingMode": "PAY_PER_REQUEST",
        }
        # Omit the key entirely when there are no GSIs — DynamoDB rejects an
        # empty list with "List of GlobalSecondaryIndexes is empty".
        if gsis:
            kwargs["GlobalSecondaryIndexes"] = [
                {
                    "IndexName": name,
                    "KeySchema": [{"AttributeName": a, "KeyType": k} for a, k, _ in key],
                    "Projection": {"ProjectionType": "ALL"},
                }
                for name, key in gsis.items()
            ]
        client.create_table(**kwargs)


@pytest.fixture(autouse=True)
def _mock_oauth_external_deps(monkeypatch):
    """Auto-applied to every contract test.

    The /oauth/chatgpt/{start,poll,disconnect} endpoints have two external
    dependencies that would 500 in the contract suite without mocking:

    1. A DynamoDB table named by the OAUTH_TOKENS_TABLE env var, plus an
       ENCRYPTION_KEY for Fernet — both raise RuntimeError("... is empty")
       if missing.
    2. httpx POSTs to auth.openai.com/{codex/device,oauth/token}, which
       fail with no network / no real OAuth flow.

    The contract suite only asserts non-5xx, so we mock both with moto +
    a deterministic httpx.AsyncClient.post that returns canned device-code
    + authorization_pending shapes. This keeps the mocks scoped to contract
    tests (autouse here, not in the global conftest).
    """
    # 1. Provision the OAuth tokens table in moto.
    mock = mock_aws()
    mock.start()
    try:
        client = boto3.client("dynamodb", region_name="us-east-1")
        client.create_table(
            TableName="contract-test-oauth-tokens",
            KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"}],
            AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"}],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("OAUTH_TOKENS_TABLE", "contract-test-oauth-tokens")
        monkeypatch.setenv("ENCRYPTION_KEY", Fernet.generate_key().decode())
        # The device-revoke path force-closes the live WS session, which builds a
        # ManagementApiClient — and its constructor RAISES when
        # WS_MANAGEMENT_API_URL is unset rather than degrading. Neither the CI
        # workflow nor tests/conftest.py sets it, so give it a syntactically
        # valid endpoint; moto intercepts the apigatewaymanagementapi calls, so
        # nothing leaves the process.
        monkeypatch.setenv(
            "WS_MANAGEMENT_API_URL",
            "https://contract-test.execute-api.us-east-1.amazonaws.com/test",
        )

        # Repos resolve tables through core.dynamodb.get_table, which prepends
        # DYNAMODB_TABLE_PREFIX (default "isol8-dev-"), so moto must create them
        # under the fully-prefixed name — not the short one — or every lookup
        # misses and the endpoint 5xxes. (The oauth table above is the exception:
        # it is read from the OAUTH_TOKENS_TABLE env var verbatim.)
        from core.dynamodb import table_name as _table_name

        _create_contract_tables(client, _table_name)

        # Two tables are named by env vars, not the prefix. Signed in, the computer/apps
        # routes look up the caller's live connections (WS_CONNECTIONS_TABLE, GSI
        # by-owner-id) and the company source route dedups its webhook (WEBHOOK_DEDUP_TABLE).
        client.create_table(
            TableName="contract-test-ws-connections",
            KeySchema=[{"AttributeName": "connectionId", "KeyType": "HASH"}],
            AttributeDefinitions=[
                {"AttributeName": "connectionId", "AttributeType": "S"},
                {"AttributeName": "ownerId", "AttributeType": "S"},
            ],
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-owner-id",
                    "KeySchema": [{"AttributeName": "ownerId", "KeyType": "HASH"}],
                    "Projection": {"ProjectionType": "ALL"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setenv("WS_CONNECTIONS_TABLE", "contract-test-ws-connections")
        monkeypatch.setenv("WEBHOOK_DEDUP_TABLE", _table_name("webhook-event-dedup"))

        # The messaging table is named by settings.MESSAGING_TABLE, not the prefix.
        # The public session-link routes (/public/session-links/*,
        # /public/session-copies/redeem) take no sign-in and read it first, so
        # without it the repo raises "MESSAGING_TABLE is not configured" -> 500.
        # Empty, every fuzzed token or code misses and the route answers its 404.
        from core.config import settings as _settings

        client.create_table(
            TableName="contract-test-messaging",
            KeySchema=[{"AttributeName": "pk", "KeyType": "HASH"}, {"AttributeName": "sk", "KeyType": "RANGE"}],
            AttributeDefinitions=[
                {"AttributeName": "pk", "AttributeType": "S"},
                {"AttributeName": "sk", "AttributeType": "S"},
                {"AttributeName": "sweep", "AttributeType": "S"},
                {"AttributeName": "deadlineAt", "AttributeType": "N"},
            ],
            # The run-ledger sweeper runs in this harness's lifespan and queries this index.
            GlobalSecondaryIndexes=[
                {
                    "IndexName": "by-run-deadline",
                    "KeySchema": [
                        {"AttributeName": "sweep", "KeyType": "HASH"},
                        {"AttributeName": "deadlineAt", "KeyType": "RANGE"},
                    ],
                    "Projection": {"ProjectionType": "KEYS_ONLY"},
                }
            ],
            BillingMode="PAY_PER_REQUEST",
        )
        monkeypatch.setattr(_settings, "MESSAGING_TABLE", "contract-test-messaging")

        # Pre-seed a pending device-code session for the contract test user
        # so /poll has a row to look up. /start would normally create this
        # row, but schemathesis hits each endpoint independently. Field
        # names match the new OpenAI Codex CLI flow (device_auth_id, not
        # the old OAuth-spec device_code).
        # The fake httpx client returns 403 (still pending) for
        # /deviceauth/token, so /poll resolves to 200 {"status":"pending"}.
        client.put_item(
            TableName="contract-test-oauth-tokens",
            Item={
                "user_id": {"S": "contract_test_user"},
                "state": {"S": "pending"},
                "device_auth_id": {"S": "ct_device_auth_id"},
                "user_code": {"S": "CONT-RACT"},
                "interval": {"N": "5"},
            },
        )

        # 2. Replace httpx.AsyncClient *only inside core.services.oauth_service*
        # so the OpenAI device-code/token URLs return canned responses. We
        # avoid monkey-patching httpx.AsyncClient.post globally because the
        # contract suite's test client (httpx.AsyncClient + ASGITransport) is
        # also an httpx.AsyncClient — a global patch would intercept the
        # test client's own requests and break unrelated contract tests.
        class _FakeOAuthClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return None

            async def post(self, url, **kwargs):
                url_str = str(url)
                # OpenAI's actual Codex CLI flow:
                #   /api/accounts/deviceauth/usercode → returns
                #     {device_auth_id, user_code, interval}
                #   /api/accounts/deviceauth/token   → returns 403/404
                #     while pending; success returns
                #     {authorization_code, code_verifier}
                #   /oauth/token                    → exchanges PKCE
                #     pair for access+refresh
                if "/deviceauth/usercode" in url_str:
                    return httpx.Response(
                        200,
                        json={
                            "device_auth_id": "ct_device_auth_id",
                            "user_code": "CONT-RACT",
                            "interval": 5,
                        },
                        request=httpx.Request("POST", url),
                    )
                if "/deviceauth/token" in url_str:
                    # Pretend the user is still pending so /poll returns
                    # 200 with status=pending instead of erroring.
                    return httpx.Response(
                        403,
                        request=httpx.Request("POST", url),
                    )
                if "/oauth/token" in url_str:
                    # Shouldn't be reached in pending state, but stub
                    # for completeness.
                    return httpx.Response(
                        400,
                        json={"error": "authorization_pending"},
                        request=httpx.Request("POST", url),
                    )
                return httpx.Response(200, json={}, request=httpx.Request("POST", url))

        import core.services.oauth_service as oauth_module

        monkeypatch.setattr(oauth_module.httpx, "AsyncClient", _FakeOAuthClient)
        yield
    finally:
        mock.stop()


@pytest.fixture(autouse=True)
def _signed_in_as_contract_user():
    """Every contract case runs signed in as contract_test_user, set and restored per test.

    The override used to be installed at import of test_api_contracts.py, so it
    leaked into every test in the process and other tests' teardowns removed it
    -- whether this fuzz ran signed in depended on test order.
    """
    from core.auth import get_current_user
    from main import app

    async def _user():
        return AuthContext(user_id="contract_test_user")

    saved = app.dependency_overrides.get(get_current_user)
    app.dependency_overrides[get_current_user] = _user
    yield
    if saved is None:
        app.dependency_overrides.pop(get_current_user, None)
    else:
        app.dependency_overrides[get_current_user] = saved


@pytest.fixture
def mock_auth():
    """Mock auth context for contract tests."""
    return AuthContext(user_id="contract_test_user")


@pytest.fixture
async def contract_client(mock_auth):
    """
    Async test client for contract tests.

    Overrides auth dependency so tests don't need real infrastructure.
    """
    from main import app
    from core.auth import get_current_user

    async def mock_get_current_user():
        return mock_auth

    app.dependency_overrides[get_current_user] = mock_get_current_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


@pytest.fixture
async def openapi_spec(contract_client):
    """Fetch and return the OpenAPI spec as a dict."""
    response = await contract_client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    return response.json()
