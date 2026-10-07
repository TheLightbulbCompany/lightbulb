import json
import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import platform_oauth_app_store as store  # noqa: E402


@pytest.fixture(autouse=True)
def _clear_cache():
    store._cache.clear()
    yield
    store._cache.clear()


@pytest.mark.asyncio
@patch("core.services.credentials.platform_oauth_app_store._client")
async def test_get_passes_signing_secret_through(mock_client):
    sm = MagicMock()
    mock_client.return_value = sm
    sm.get_secret_value.return_value = {
        "SecretString": json.dumps({"client_id": "cid", "client_secret": "cs", "signing_secret": "ss"})
    }
    got = await store.get("slack")
    assert got == {"client_id": "cid", "client_secret": "cs", "signing_secret": "ss"}


@pytest.mark.asyncio
@patch("core.services.credentials.platform_oauth_app_store._client")
async def test_get_without_signing_secret_is_empty_string(mock_client):
    sm = MagicMock()
    mock_client.return_value = sm
    sm.get_secret_value.return_value = {"SecretString": json.dumps({"client_id": "cid", "client_secret": "cs"})}
    got = await store.get("meta-instagram")
    assert got["signing_secret"] == ""
