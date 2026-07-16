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
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")

import json
from typing import AsyncGenerator, Generator
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext
from core.middleware import shutdown_aware


@pytest.fixture(autouse=True)
def _bedrock_gate_url_direct_default():
    """Default every test to bedrock-gate BREAK-GLASS 'direct' mode.

    ``core.billing.bedrock_gate_url.gate_url`` reads a deploy-published SSM
    parameter and RAISES when it is unreadable (deliberate: a silently-direct
    config would AccessDeny at inference). Unit tests have no SSM, so without
    this default every config-building test would raise. Returning "" here is
    the explicit break-glass value ("route direct"), which keeps all
    direct-baseUrl assertions meaningful; gateway-specific tests patch the
    resolver (or settings.BEDROCK_GATE_URL) with a URL instead.
    """
    with patch("core.billing.bedrock_gate_url.gate_url", return_value=""):
        yield


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
def mock_auth_context() -> AuthContext:
    """Default mock auth context for personal mode (no org)."""
    return AuthContext(user_id="user_test_123")


@pytest.fixture
def mock_current_user(mock_auth_context):
    """Dependency override for get_current_user with mock AuthContext (personal mode)."""

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
    """Async test client with mocked auth (personal mode)."""
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
        mock.CLERK_AUDIENCE = None
        mock.HUGGINGFACE_TOKEN = "hf_test_token"
        mock.HF_API_URL = "https://router.huggingface.co/v1"
        mock.PROJECT_NAME = "Isol8 Test"
        mock.API_V1_STR = "/api/v1"
        mock.ENCRYPTION_KEY = "dGVzdC1lbmNyeXB0aW9uLWtleS0xMjM0NTY3OA=="
        yield mock
