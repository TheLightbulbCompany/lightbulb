import pytest
from botocore.exceptions import ClientError

from core.services.credentials import integration_secret_store, oauth_app_store


@pytest.mark.asyncio
async def test_get_returns_none_on_client_error(monkeypatch):
    # A non-not-found ClientError (e.g. no creds / throttling) must NOT propagate:
    # list_connections calls this per byo-required provider on every catalog GET,
    # so an unhandled error would 500 the whole connections list. Degrade to None.
    async def boom(*_):
        raise ClientError({"Error": {"Code": "UnrecognizedClientException", "Message": "invalid token"}}, "GetItem")

    monkeypatch.setattr(integration_secret_store, "get", boom)
    assert await oauth_app_store.get("user_1", "google") is None
