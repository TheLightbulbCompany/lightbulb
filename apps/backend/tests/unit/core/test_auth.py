"""Unit tests for authentication module."""

import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
import jwt

from core.auth import AuthContext, _jwks_cache, get_current_user

TEST_ISSUER = "https://test.clerk.accounts.dev"


@pytest.fixture(autouse=True)
def _clear_jwks_cache():
    """Reset the module-level JWKS cache before each test to avoid cross-test pollution."""
    _jwks_cache["data"] = None
    _jwks_cache["expires_at"] = None
    yield
    _jwks_cache["data"] = None
    _jwks_cache["expires_at"] = None


TEST_RSA_N = (
    "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_"
    "BJECPebWKRXjBZCiFV4n3oknjhMstn64tZ_2W-5JsGY4Hc5n9yBXArwl93lqt7_RN5w6Cf0h4QyQ5v-65YGjQR0_"
    "FDW2QvzqY368QQMicAtaSqzs8KJZgnYb9c7d0zgdAZHzu6qMQvRL5hajrn1n91CbOpbISD08qNLyrdkt-bFTWhAI4"
    "vMQFh6WeZu0fM4lFd2NcRwr3XPksINHaQ-G_xBniIqbw0Ls1jF44-csFCur-kEgU8awapJzKnqDKgw"
)


def create_mock_httpx_client(jwks_response: dict = None, error: Exception = None) -> MagicMock:
    """Create a mock httpx.AsyncClient with configured JWKS response."""
    mock_client = MagicMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    if error:
        mock_client.get = AsyncMock(side_effect=error)
    else:
        mock_response = MagicMock()
        mock_response.json.return_value = jwks_response
        mock_client.get = AsyncMock(return_value=mock_response)

    return mock_client


class TestAuthContext:
    """Tests for AuthContext data class."""

    def test_auth_context_creation(self):
        ctx = AuthContext(user_id="user_123", email="a@b.co")
        assert (ctx.user_id, ctx.email, ctx.verified_issuer) == ("user_123", "a@b.co", None)


class TestGetCurrentUser:
    """Tests for get_current_user authentication function."""

    @pytest.fixture
    def mock_credentials(self):
        """Mock HTTP authorization credentials."""
        mock = MagicMock()
        mock.credentials = "test_token"
        return mock

    @pytest.fixture
    def valid_jwks(self) -> dict:
        """Valid JWKS response with test key."""
        return {"keys": [{"kty": "RSA", "kid": "test-key-id", "use": "sig", "n": TEST_RSA_N, "e": "AQAB"}]}

    @pytest.mark.asyncio
    async def test_valid_token_calls_jwt_decode(self, mock_credentials, valid_jwks):
        """Valid JWT token triggers jwt.decode with correct parameters."""
        payload = {
            "sub": "user_123",
            "email": "test@example.com",
            "iss": TEST_ISSUER,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }

        with (
            patch("core.auth.httpx.AsyncClient") as mock_client_class,
            patch("core.auth.jwt.get_unverified_header") as mock_header,
            patch("core.auth.jwt.decode") as mock_decode,
            patch("core.auth.settings") as mock_settings,
        ):
            mock_settings.CLERK_ISSUER = TEST_ISSUER
            mock_settings.clerk_authorized_parties_list = []
            mock_client_class.return_value = create_mock_httpx_client(valid_jwks)
            mock_header.return_value = {"kid": "test-key-id", "alg": "RS256"}
            mock_decode.return_value = payload

            result = await get_current_user(mock_credentials)

            assert isinstance(result, AuthContext)
            assert result.user_id == "user_123"
            assert mock_decode.call_count == 1

    @pytest.mark.asyncio
    async def test_expired_token_raises_401(self, mock_credentials, valid_jwks):
        """Expired token raises 401 HTTPException."""
        with (
            patch("core.auth.httpx.AsyncClient") as mock_client_class,
            patch("core.auth.jwt.get_unverified_header") as mock_header,
            patch("core.auth.jwt.decode") as mock_decode,
            patch("core.auth.settings") as mock_settings,
        ):
            mock_settings.CLERK_ISSUER = TEST_ISSUER
            mock_settings.clerk_authorized_parties_list = []
            mock_client_class.return_value = create_mock_httpx_client(valid_jwks)
            mock_header.return_value = {"kid": "test-key-id", "alg": "RS256"}
            mock_decode.side_effect = jwt.ExpiredSignatureError("Token expired")

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(mock_credentials)

            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Token expired"

    @pytest.mark.asyncio
    async def test_invalid_claims_raises_401(self, mock_credentials, valid_jwks):
        """Invalid claims raises 401 HTTPException."""
        with (
            patch("core.auth.httpx.AsyncClient") as mock_client_class,
            patch("core.auth.jwt.get_unverified_header") as mock_header,
            patch("core.auth.jwt.decode") as mock_decode,
            patch("core.auth.settings") as mock_settings,
        ):
            mock_settings.CLERK_ISSUER = TEST_ISSUER
            mock_settings.clerk_authorized_parties_list = []
            mock_client_class.return_value = create_mock_httpx_client(valid_jwks)
            mock_header.return_value = {"kid": "test-key-id", "alg": "RS256"}
            mock_decode.side_effect = jwt.InvalidAudienceError("Invalid claims")

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(mock_credentials)

            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Invalid claims"

    @pytest.mark.asyncio
    async def test_invalid_kid_raises_401(self, mock_credentials, valid_jwks):
        """Token with unknown key ID raises 401 HTTPException."""
        with (
            patch("core.auth.httpx.AsyncClient") as mock_client_class,
            patch("core.auth.jwt.get_unverified_header") as mock_header,
            patch("core.auth.settings") as mock_settings,
        ):
            mock_settings.CLERK_ISSUER = TEST_ISSUER
            mock_settings.clerk_authorized_parties_list = []
            mock_client_class.return_value = create_mock_httpx_client(valid_jwks)
            mock_header.return_value = {"kid": "unknown-key-id", "alg": "RS256"}

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(mock_credentials)

            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Invalid token headers"

    @pytest.mark.asyncio
    async def test_jwks_fetch_failure_raises_401(self, mock_credentials):
        """JWKS fetch failure raises 401 HTTPException."""
        with patch("core.auth.httpx.AsyncClient") as mock_client_class, patch("core.auth.settings") as mock_settings:
            mock_settings.CLERK_ISSUER = TEST_ISSUER
            mock_client_class.return_value = create_mock_httpx_client(error=Exception("Network error"))

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(mock_credentials)

            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Could not validate credentials"

    @pytest.mark.asyncio
    async def test_generic_exception_raises_401(self, mock_credentials, valid_jwks):
        """Generic exception during validation raises 401."""
        with (
            patch("core.auth.httpx.AsyncClient") as mock_client_class,
            patch("core.auth.jwt.get_unverified_header") as mock_header,
            patch("core.auth.settings") as mock_settings,
        ):
            mock_settings.CLERK_ISSUER = TEST_ISSUER
            mock_client_class.return_value = create_mock_httpx_client(valid_jwks)
            mock_header.side_effect = Exception("Unexpected error")

            with pytest.raises(HTTPException) as exc_info:
                await get_current_user(mock_credentials)

            assert exc_info.value.status_code == 401
            assert exc_info.value.detail == "Could not validate credentials"

    @pytest.mark.asyncio
    async def test_returns_auth_context(self, mock_credentials, valid_jwks):
        """get_current_user returns AuthContext object."""
        payload = {
            "sub": "user_123",
            "email": "test@example.com",
            "iss": TEST_ISSUER,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
        }

        with (
            patch("core.auth.httpx.AsyncClient") as mock_client_class,
            patch("core.auth.jwt.get_unverified_header") as mock_header,
            patch("core.auth.jwt.decode") as mock_decode,
            patch("core.auth.settings") as mock_settings,
        ):
            mock_settings.CLERK_ISSUER = TEST_ISSUER
            mock_settings.clerk_authorized_parties_list = []
            mock_client_class.return_value = create_mock_httpx_client(valid_jwks)
            mock_header.return_value = {"kid": "test-key-id", "alg": "RS256"}
            mock_decode.return_value = payload

            result = await get_current_user(mock_credentials)

            assert isinstance(result, AuthContext)
            assert result.user_id == "user_123"

    @pytest.mark.asyncio
    async def test_ignores_org_claims(self, mock_credentials, valid_jwks):
        """A leftover organisation claim changes nothing: the context is the person's."""
        # Use Clerk v2 compact format with nested 'o' object
        payload = {
            "sub": "user_123",
            "email": "test@example.com",
            "iss": TEST_ISSUER,
            "exp": int(time.time()) + 3600,
            "iat": int(time.time()),
            "o": {
                "id": "org_456",
                "rol": "admin",  # Without "org:" prefix - code adds it
                "slg": "acme-corp",
                "per": "org:read,org:write",  # Comma-separated string
            },
        }

        with (
            patch("core.auth.httpx.AsyncClient") as mock_client_class,
            patch("core.auth.jwt.get_unverified_header") as mock_header,
            patch("core.auth.jwt.decode") as mock_decode,
            patch("core.auth.settings") as mock_settings,
        ):
            mock_settings.CLERK_ISSUER = TEST_ISSUER
            mock_settings.clerk_authorized_parties_list = []
            mock_client_class.return_value = create_mock_httpx_client(valid_jwks)
            mock_header.return_value = {"kid": "test-key-id", "alg": "RS256"}
            mock_decode.return_value = payload

            result = await get_current_user(mock_credentials)

            assert isinstance(result, AuthContext)
            assert result.user_id == "user_123"
