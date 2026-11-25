"""
Shared test fixtures for the backend tests.

Tests use mocked DynamoDB (via moto or plain mocks) — no real database needed.
"""

import os

# Set required env vars for the test environment before any module imports.
# core.auth -> core.config constructs Settings() at module load and raises if
# CLERK_ISSUER is missing, so collection fails without these defaults.
os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
# The admin ids the router tests sign in as (require_platform_admin).
os.environ.setdefault("PLATFORM_ADMIN_USER_IDS", "user_platform_admin,admin_1,user_admin_123,user_admin")

import json
import zlib
from typing import AsyncGenerator, Generator
from unittest.mock import patch

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
    # A developer's local SSO profile must never reach a test: boto3 would then resolve it (and fail on
    # any machine without it). Owning the variable here also restores it after every test, so nothing
    # that sets it leaks to later tests in the xdist worker.
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_PROFILE", raising=False)


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
    return AuthContext(user_id="user_test_123", human_session=True)


@pytest.fixture
def mock_org_member_context() -> AuthContext:
    """Mock auth context for org member (non-admin)."""
    return AuthContext(user_id="user_test_123", human_session=True)


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
    """Async test client authenticated as org admin (user_test_123 in user_test_123)."""
    from core.auth import get_current_user

    app.dependency_overrides[get_current_user] = mock_org_admin_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture
async def async_client_org_member(app, mock_org_member_user) -> AsyncGenerator:
    """Async test client authenticated as org member (user_test_789 in user_test_123)."""
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
def mock_settings():
    """Mock settings with test values."""
    with patch("core.config.settings") as mock:
        mock.CLERK_ISSUER = "https://test.clerk.accounts.dev"
        mock.clerk_authorized_parties_list = []
        mock.API_V1_STR = "/api/v1"
        yield mock
