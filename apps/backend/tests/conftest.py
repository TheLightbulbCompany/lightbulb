"""
Shared test fixtures for Isol8 backend tests.

Tests use mocked DynamoDB (via moto or plain mocks) — no real database needed.
"""

import os

# Set required env vars for the test environment before any module imports.
# core.auth -> core.config constructs Settings() at module load and raises if
# CLERK_ISSUER is missing, so collection fails without these defaults.
os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("ENCRYPTION_KEY", "wHc3hAOcLlFzWyu3Ph7xIyClIdVQTrIzFOZDtu_pIEY=")
os.environ.setdefault("NETWORK_PROOF_KEY", "test-network-proof-key")
os.environ.setdefault("GATE_BEARER_HMAC_KEY", "test-gate-bearer-hmac-key")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
# The current product requires one canonical pool. Tests of missing configuration
# explicitly clear this setting; retained-behavior tests use a configured tier.
os.environ.setdefault("HERMES_SHARED_RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-east-1:877352799272:runtime/test-pool")
# The admin ids the router tests sign in as (require_platform_admin).
os.environ.setdefault("PLATFORM_ADMIN_USER_IDS", "user_platform_admin,admin_1,user_admin_123,user_admin")

import json
import zlib
from typing import AsyncGenerator, Generator
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext
from core.middleware import shutdown_aware


def pytest_collection_modifyitems(config, items):
    """BACKEND_TEST_SHARD=k/n keeps every n-th test (by a stable hash of its id) for shard k.

    Backend CI splits the suite across n hosted runners this way. crc32, not hash():
    every process (each xdist worker too) must pick the same tests."""
    shard = os.environ.get("BACKEND_TEST_SHARD")
    if not shard:
        return
    k, n = (int(x) for x in shard.split("/"))
    keep, drop = [], []
    for item in items:
        (keep if zlib.crc32(item.nodeid.encode()) % n == k - 1 else drop).append(item)
    config.hook.pytest_deselected(items=drop)
    items[:] = keep


def _serialize_moto_dynamodb() -> None:
    """Make moto's DynamoDB apply one request at a time, as DynamoDB applies writes to one item.

    moto's update_item/put_item read the item, evaluate the ConditionExpression and write
    with no lock, and repo calls run on worker threads (core.dynamodb.run_in_thread). Two
    racing conditional writes could then both pass the condition, so the "only one wins"
    tests (billing trial arm, apps invite claim, tasks transition) failed whenever the OS
    interleaved the threads -- often on the 2-core hosted runners, rarely on a laptop."""
    import threading

    from moto.dynamodb.responses import DynamoHandler

    lock = threading.RLock()
    call_action = DynamoHandler.call_action

    def serialized(self):
        with lock:
            return call_action(self)

    DynamoHandler.call_action = serialized


_serialize_moto_dynamodb()


async def _no_background_work(*_args, **_kwargs) -> None:
    """Stands in for a lifespan background worker."""


@pytest.fixture(autouse=True)
def _no_lifespan_background_workers(request, monkeypatch):
    """Every TestClient enters the app lifespan, which starts the background sweepers. Outside
    a moto context those reached real AWS with the dummy credentials (UnrecognizedClientException
    in the running_gauges loop and the pass-admission sweepers) and kept running after the test.
    Tests that drive a worker itself opt out with ``@pytest.mark.real_background_workers``."""
    if request.node.get_closest_marker("real_background_workers"):
        return
    import main
    from core.services import (
        browser_control,
        daily_digest,
        durable_activity,
        memory_api,
        needs_you_email,
        run_ledger,
        session_pickup,
        voice_calls,
    )

    monkeypatch.setattr(main, "run_scheduled_worker", _no_background_work)
    monkeypatch.setattr(main, "_running_count_gauge_loop", _no_background_work)
    for module, name in (
        (durable_activity, "run_trial_idle_sweeper"),
        (run_ledger, "run_ledger_sweeper"),
        (browser_control, "run_autosave_sweeper"),
        (voice_calls, "voice_call_sweeper"),
        (session_pickup, "run_pickup_sweeper"),
        (memory_api, "run_memory_sweeper"),
        (daily_digest, "run_digest_sweeper"),
        (needs_you_email, "run_needs_you_sweeper"),
    ):
        monkeypatch.setattr(module, name, _no_background_work)


@pytest.fixture(autouse=True)
def _reset_shutdown_flag():
    """Reset the process-global shutdown flag around every test.

    ``_shutting_down`` is module-global state. Tests that drive the lifespan
    shutdown set it True; with ShutdownAwareMiddleware mounted on the app, a
    leaked True would make every subsequent test get a 503. Reset before and
    after each test so the flag never leaks across the session.
    """
    shutdown_aware.set_shutting_down(False)
    yield
    shutdown_aware.set_shutting_down(False)


@pytest.fixture(autouse=True)
def _no_aws_profile(monkeypatch):
    # The dev acceptance harness (scripts/super_agent/acceptance/ctx.py aws_profile_default, reached from
    # validate_meeting_context and the acceptance tests) setdefaults AWS_PROFILE=isol8-admin. Owning the
    # variable here restores it after every test, so it never leaks to later tests in the xdist worker,
    # where boto3 then fails on any machine without that local SSO profile.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)


@pytest.fixture(autouse=True)
def _no_real_jev_key(monkeypatch):
    """The addressing stage (messaging.address) calls Jev on any human message that names someone,
    so a send in any test would read the real gateway key where AWS credentials exist. Default to
    "no key" (Jev off: the stage's deterministic fallback); test_jev patches its own reads."""
    from core.services import jev

    monkeypatch.setattr(jev, "_read_secret", lambda name: None)


@pytest.fixture(autouse=True)
def _runtime_session_gate_off():
    """Unit tests have no DynamoDB, so ``runtime_transport``'s single-flight wake gate would
    reach for a real table on every invoke (and every stop would clear a real row). Off by default (invoke directly, the gate's own
    fail-open shape); ``tests/unit/services/test_runtime_single_flight.py`` overrides this
    fixture to drive the real gate against moto."""
    from core.repositories import container_repo
    from core.services import runtime_transport

    async def _no_clear(owner_id, **kwargs):
        return None

    with (
        patch.object(runtime_transport, "_ensure_session", lambda owner_id, arn, deadline, wake: None),
        patch.object(container_repo, "clear_session", _no_clear),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_real_network_seed(monkeypatch):
    """Every invoke's boot header reads the owner's network seed from SecretsManager; unit tests
    have none (and real AWS where credentials exist). Tests of the seed itself patch over this."""
    from core.services import runtime_transport

    monkeypatch.setattr(runtime_transport, "_network_seed", lambda owner_id, row_id: None)
    monkeypatch.setattr(runtime_transport, "_NETWORK_SEEDS", {})


@pytest.fixture(autouse=True)
def _room_receipt_events_off():
    """Unit tests have no DynamoDB/AppSync, and read-cursor moves, run starts and delivery
    advances now publish receipts on the room channel. Stubbed by default; tests asserting
    on it re-patch ``core.services.messaging._publish_room_event``."""
    from core.services import messaging

    with patch.object(messaging, "_publish_room_event", AsyncMock()):
        yield


@pytest.fixture(autouse=True)
def _mail_changed_off():
    """Storing a mail or changing a meeting nudges the Mail panel over AppSync, which would reach
    for DynamoDB. Stubbed by default; tests asserting on it re-patch
    ``core.services.agent_email.mail_changed`` (the helper's own test binds the real one at import)."""
    from core.services import agent_email

    with patch.object(agent_email, "mail_changed", AsyncMock()):
        yield


def parse_sse_events(response_text: str) -> list[dict]:
    """Parse SSE events from response text into list of event dicts."""
    lines = [line for line in response_text.split("\n") if line.startswith("data:")]
    return [json.loads(line.replace("data: ", "")) for line in lines]


@pytest.fixture
def mock_user_payload() -> dict:
    """Default mock user JWT payload."""
    return {
        "sub": "user_test_123",
        "email": "test@example.com",
        "iss": "https://test.clerk.accounts.dev",
        "aud": "test-audience",
        "exp": 9999999999,
        "iat": 1234567890,
    }


@pytest.fixture
def mock_auth_context(mock_org_admin_context: AuthContext) -> AuthContext:
    """Default mock auth context for an active workspace admin."""
    return mock_org_admin_context


@pytest.fixture
def mock_current_user(mock_auth_context):
    """Dependency override for get_current_user with workspace-admin auth."""

    async def _mock_get_current_user():
        return mock_auth_context

    return _mock_get_current_user


@pytest.fixture
def mock_org_admin_context() -> AuthContext:
    """Mock auth context for org admin."""
    return AuthContext(
        user_id="user_test_123",
        org_id="org_test_456",
        org_role="org:admin",
        org_slug="test-org",
        org_permissions=["org:billing:manage"],
    )


@pytest.fixture
def mock_org_member_context() -> AuthContext:
    """Mock auth context for org member (non-admin)."""
    return AuthContext(
        user_id="user_test_789",
        org_id="org_test_456",
        org_role="org:member",
        org_slug="test-org",
    )


@pytest.fixture
def mock_org_admin_user(mock_org_admin_context):
    """Dependency override for get_current_user with org admin context."""

    async def _mock():
        return mock_org_admin_context

    return _mock


@pytest.fixture
def mock_org_member_user(mock_org_member_context):
    """Dependency override for get_current_user with org member context."""

    async def _mock():
        return mock_org_member_context

    return _mock


@pytest.fixture
def mock_jwks() -> dict:
    """Mock JWKS response for JWT verification tests."""
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": "test-key-id",
                "use": "sig",
                "n": "test-modulus",
                "e": "AQAB",
            }
        ]
    }


@pytest.fixture
def app():
    """Create a fresh FastAPI app instance for testing."""
    from main import app as fastapi_app

    return fastapi_app


@pytest.fixture
def client(app, mock_current_user) -> Generator:
    """Synchronous test client with mocked auth."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_current_user

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()


@pytest.fixture
async def async_client(app, mock_current_user) -> AsyncGenerator:
    """Async test client with mocked workspace-admin auth."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_current_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def async_client_org_admin(app, mock_org_admin_user) -> AsyncGenerator:
    """Async test client authenticated as org admin (user_test_123 in org_test_456)."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_org_admin_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def async_client_org_member(app, mock_org_member_user) -> AsyncGenerator:
    """Async test client authenticated as org member (user_test_789 in org_test_456)."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_org_member_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def unauthenticated_async_client(app) -> AsyncGenerator:
    """Async test client without auth mocking (for auth failure tests)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def mock_llm_stream():
    """Mock LLM streaming response generator."""

    async def _mock_stream():
        for chunk in ["Hello", " ", "world", "!"]:
            yield chunk

    return _mock_stream


@pytest.fixture
def mock_hf_sse_response() -> list[bytes]:
    """Mock HuggingFace SSE response data."""
    return [
        b'data: {"choices":[{"delta":{"content":"Hello"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":" world"}}]}\n\n',
        b'data: {"choices":[{"delta":{"content":"!"}}]}\n\n',
        b"data: [DONE]\n\n",
    ]


@pytest.fixture
def mock_settings():
    """Mock settings with test values."""
    with patch("core.config.settings") as mock:
        mock.CLERK_ISSUER = "https://test.clerk.accounts.dev"
        mock.clerk_authorized_parties_list = []
        mock.API_V1_STR = "/api/v1"
        mock.ENCRYPTION_KEY = "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3OA=="
        yield mock
