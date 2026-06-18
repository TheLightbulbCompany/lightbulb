import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import oauth_dance  # noqa: E402


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance._isol8_oauth_client", new_callable=AsyncMock)
async def test_oauth_client_prefers_owner_byo_app(mock_isol8, mock_byo_get):
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": "owner_sec"}
    cid, sec = await oauth_dance._oauth_client("user_1", "google")
    assert (cid, sec) == ("owner_cid", "owner_sec")
    mock_isol8.assert_not_called()


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance._isol8_oauth_client", new_callable=AsyncMock)
async def test_oauth_client_falls_back_to_isol8(mock_isol8, mock_byo_get):
    mock_byo_get.return_value = None
    mock_isol8.return_value = ("isol8_cid", "isol8_sec")
    cid, sec = await oauth_dance._oauth_client("user_1", "google")
    assert (cid, sec) == ("isol8_cid", "isol8_sec")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance._isol8_oauth_client", new_callable=AsyncMock)
async def test_oauth_client_falls_back_when_byo_row_missing_secret(mock_isol8, mock_byo_get):
    # A malformed stored row (e.g. an out-of-band write) with a client_id but no
    # client_secret must not be honored as a usable BYO app — fall back to
    # Isol8's shared app rather than handing back an empty secret.
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": ""}
    mock_isol8.return_value = ("isol8_cid", "isol8_sec")
    cid, sec = await oauth_dance._oauth_client("user_1", "google")
    assert (cid, sec) == ("isol8_cid", "isol8_sec")
    mock_isol8.assert_awaited_once()


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance._isol8_oauth_client", new_callable=AsyncMock)
async def test_oauth_client_falls_back_when_byo_row_missing_client_id(mock_isol8, mock_byo_get):
    # Symmetric guard: a row missing the client_id (only a secret) is also
    # incomplete and falls back to the shared app.
    mock_byo_get.return_value = {"client_secret": "owner_sec"}
    mock_isol8.return_value = ("isol8_cid", "isol8_sec")
    cid, sec = await oauth_dance._oauth_client("user_1", "google")
    assert (cid, sec) == ("isol8_cid", "isol8_sec")
    mock_isol8.assert_awaited_once()
