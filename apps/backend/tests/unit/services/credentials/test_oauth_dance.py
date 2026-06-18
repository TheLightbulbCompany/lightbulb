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
