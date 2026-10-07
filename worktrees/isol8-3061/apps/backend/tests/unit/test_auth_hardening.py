"""Tests for auth.py security hardening — JWKS stale cap + JWT leeway."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core import auth


class TestJWKSStaleFallback:
    """JWKS cache should serve stale data up to 15 min, then fail closed."""

    @pytest.mark.asyncio
    async def test_serves_stale_within_15_min(self):
        """If JWKS fetch fails and cache is <15 min stale, serve stale."""
        test_keys = {"keys": [{"kid": "test", "kty": "RSA", "n": "x", "e": "y", "use": "sig"}]}
        now = datetime.utcnow()

        # Seed cache with data that expired 10 minutes ago (within 15 min limit)
        original_cache = dict(auth._jwks_cache)
        auth._jwks_cache["data"] = test_keys
        auth._jwks_cache["expires_at"] = now - timedelta(minutes=10)

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("network error")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        try:
            with patch("core.auth.httpx.AsyncClient", return_value=mock_client):
                result = await auth._get_cached_jwks("https://test/.well-known/jwks.json")
                assert result == test_keys  # Should serve stale
        finally:
            auth._jwks_cache.update(original_cache)

    @pytest.mark.asyncio
    async def test_fails_closed_after_15_min(self):
        """If JWKS fetch fails and cache is >15 min stale, raise (fail closed)."""
        test_keys = {"keys": [{"kid": "test", "kty": "RSA", "n": "x", "e": "y", "use": "sig"}]}
        now = datetime.utcnow()

        # Seed cache with data that expired 20 minutes ago (beyond 15 min limit)
        original_cache = dict(auth._jwks_cache)
        auth._jwks_cache["data"] = test_keys
        auth._jwks_cache["expires_at"] = now - timedelta(minutes=20)

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("network error")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        try:
            with patch("core.auth.httpx.AsyncClient", return_value=mock_client):
                with pytest.raises(httpx.ConnectError):
                    await auth._get_cached_jwks("https://test/.well-known/jwks.json")
        finally:
            auth._jwks_cache.update(original_cache)

    @pytest.mark.asyncio
    async def test_fails_closed_with_no_cache(self):
        """If JWKS fetch fails and there's no cached data at all, raise."""
        original_cache = dict(auth._jwks_cache)
        auth._jwks_cache["data"] = None
        auth._jwks_cache["expires_at"] = None

        mock_client = AsyncMock()
        mock_client.get.side_effect = httpx.ConnectError("network error")
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        try:
            with patch("core.auth.httpx.AsyncClient", return_value=mock_client):
                with pytest.raises(httpx.ConnectError):
                    await auth._get_cached_jwks("https://test/.well-known/jwks.json")
        finally:
            auth._jwks_cache.update(original_cache)


class TestJWKSTTL:
    """JWKS cache TTL should be 5 minutes, not 1 hour."""

    def test_ttl_is_5_minutes(self):
        assert auth.JWKS_CACHE_TTL == timedelta(minutes=5)

    def test_max_stale_is_15_minutes(self):
        assert auth._JWKS_MAX_STALE == timedelta(minutes=15)


class TestJWTLeeway:
    """jwt.decode should be called with leeway=30."""

    @pytest.mark.asyncio
    async def test_decode_uses_leeway(self):
        """Verify leeway parameter is passed to jwt.decode."""
        with (
            patch("core.auth._get_cached_jwks", new_callable=AsyncMock) as mock_jwks,
            patch("core.auth.jwt") as mock_jwt,
        ):
            mock_jwks.return_value = {"keys": [{"kid": "k1", "kty": "RSA", "n": "x", "e": "y", "use": "sig"}]}
            mock_jwt.get_unverified_header.return_value = {"kid": "k1"}
            mock_jwt.PyJWK.return_value.key = "fake-key"
            mock_jwt.decode.return_value = {"sub": "user_123"}

            await auth._decode_token("fake-token")

            # Verify leeway=30 was passed
            call_kwargs = mock_jwt.decode.call_args
            assert call_kwargs.kwargs.get("leeway") == 30 or call_kwargs[1].get("leeway") == 30
