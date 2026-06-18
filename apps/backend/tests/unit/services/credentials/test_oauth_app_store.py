import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import oauth_app_store  # noqa: E402


def test_secret_name_under_integrations_prefix():
    name = oauth_app_store._secret_name("user_1", "google")
    # MUST be under the RW-granted integrations/ prefix (service-stack.ts:453-455).
    assert "/integrations/user_1/oauth-app/google" in name


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_app_store._client")
@patch("core.services.credentials.oauth_app_store.create_or_update_secret")
async def test_put_then_get_round_trips(mock_cou, mock_client):
    sm = MagicMock()
    mock_client.return_value = sm
    sm.describe_secret.return_value = {"ARN": "arn:aws:...:secret"}
    await oauth_app_store.put(
        "user_1", "google", client_id="cid", client_secret="csec", acknowledged_at="2026-06-18T00:00:00Z"
    )
    # create_or_update_secret received a JSON payload with the three fields.
    payload = mock_cou.call_args.args[2]
    import json

    data = json.loads(payload)
    assert data == {"client_id": "cid", "client_secret": "csec", "acknowledged_at": "2026-06-18T00:00:00Z"}

    sm.get_secret_value.return_value = {"SecretString": payload}
    got = await oauth_app_store.get("user_1", "google")
    assert got["client_id"] == "cid"


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_app_store._client")
async def test_get_missing_returns_none(mock_client):
    sm = MagicMock()
    mock_client.return_value = sm
    sm.exceptions.ResourceNotFoundException = Exception
    sm.get_secret_value.side_effect = sm.exceptions.ResourceNotFoundException()
    assert await oauth_app_store.get("user_1", "google") is None
