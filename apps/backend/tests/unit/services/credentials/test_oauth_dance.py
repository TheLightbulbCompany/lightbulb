import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import oauth_dance  # noqa: E402


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_uses_owner_byo_app(mock_byo_get):
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": "owner_sec"}
    cid, sec = await oauth_dance._oauth_client("user_1", "google")
    assert (cid, sec) == ("owner_cid", "owner_sec")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_raises_when_no_byo_app(mock_byo_get):
    mock_byo_get.return_value = None
    with pytest.raises(ValueError):
        await oauth_dance._oauth_client("user_1", "google")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_raises_when_byo_row_incomplete(mock_byo_get):
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": ""}
    with pytest.raises(ValueError):
        await oauth_dance._oauth_client("user_1", "google")
