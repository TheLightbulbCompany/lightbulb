import os
from unittest.mock import AsyncMock, patch

os.environ.setdefault("CLERK_ISSUER", "https://test.clerk.accounts.dev")

import pytest  # noqa: E402

from core.services.credentials import oauth_dance  # noqa: E402


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_uses_owner_byo_app(mock_byo_get):
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": "owner_sec"}
    cid, sec = await oauth_dance._oauth_client("user_1", "x")
    assert (cid, sec) == ("owner_cid", "owner_sec")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_raises_when_no_byo_app(mock_byo_get):
    mock_byo_get.return_value = None
    with pytest.raises(ValueError):
        await oauth_dance._oauth_client("user_1", "x")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_allows_empty_secret_for_public_client(mock_byo_get):
    # A DCR-registered PUBLIC client legitimately has an empty secret (PKCE is its
    # only proof), so a row with a client_id and an empty secret is valid — only a
    # missing client_id is an error.
    mock_byo_get.return_value = {"client_id": "owner_cid", "client_secret": ""}
    cid, sec = await oauth_dance._oauth_client("user_1", "attio")
    assert (cid, sec) == ("owner_cid", "")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_raises_when_client_id_missing(mock_byo_get):
    mock_byo_get.return_value = {"client_secret": "owner_sec"}
    with pytest.raises(ValueError):
        await oauth_dance._oauth_client("user_1", "x")


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance.platform_oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_platform_app_skips_owner_store(mock_platform_get, mock_byo_get):
    # instagram declares oauth.platform_app — the client is Isol8's shared app,
    # and the per-owner BYO/DCR store must never be consulted.
    mock_platform_get.return_value = {"client_id": "ig_cid", "client_secret": "ig_sec"}
    cid, sec = await oauth_dance._oauth_client("user_1", "instagram")
    assert (cid, sec) == ("ig_cid", "ig_sec")
    mock_platform_get.assert_awaited_once_with("meta-instagram")
    mock_byo_get.assert_not_awaited()


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.platform_oauth_app_store.get", new_callable=AsyncMock)
async def test_oauth_client_platform_app_unprovisioned_is_operator_error(mock_platform_get):
    mock_platform_get.return_value = None
    with pytest.raises(ValueError, match="not provisioned"):
        await oauth_dance._oauth_client("user_1", "instagram")


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class _FakeAsyncClient:
    """Stub httpx.AsyncClient capturing the IG exchange sequence: POST (code
    exchange -> short-lived token + user_id) then GET (long-lived upgrade)."""

    # Last POST's (data, auth), so a test can assert how the client authenticated
    # at the token endpoint without threading the instance out of exchange().
    last_post: dict = {}
    # Same for the long-lived upgrade GET — the two Meta surfaces send different
    # URLs and parameter names, and getting either wrong yields a token that dies
    # in hours with no refresh path.
    last_get: dict = {}

    def __init__(self, *args, **kwargs):
        self.get_params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def post(self, url, data=None, headers=None, auth=None):
        _FakeAsyncClient.last_post = {"data": data, "auth": auth}
        return _FakeResponse(
            {"access_token": "SHORT", "user_id": 178414123, "permissions": ["instagram_business_basic"]}
        )

    async def get(self, url, params=None):
        self.get_params = params
        _FakeAsyncClient.last_get = {"url": url, "params": params}
        return _FakeResponse({"access_token": "LONG60D", "token_type": "bearer", "expires_in": 5184000})


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.httpx.AsyncClient", _FakeAsyncClient)
@patch("core.services.credentials.oauth_dance._oauth_client", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance.oauth_state_repo.consume", new_callable=AsyncMock)
async def test_exchange_instagram_upgrades_to_long_lived_and_blanks_platform_secret(mock_consume, mock_client):
    mock_consume.return_value = {
        "owner_id": "user_1",
        "provider": "instagram",
        "verifier": "v" * 43,
        "client_id": "ig_cid",
    }
    mock_client.return_value = ("ig_cid", "ig_platform_secret")
    owner, provider, cred, _origin = await oauth_dance.exchange(
        await oauth_dance.oauth_state_repo.consume("state123"), "code456", redirect_uri="https://api/cb"
    )
    assert (owner, provider) == ("user_1", "instagram")
    # Long-lived upgrade replaced the short token and set the ~60d expiry.
    assert cred["access_token"] == "LONG60D"
    assert cred["expires_in"] == 5184000
    assert cred["token_expiry"]
    # IG's app-scoped user id is carried for the materializer.
    assert cred["provider_user_id"] == "178414123"
    # The platform app secret never leaves the dance in the credential payload.
    assert cred["client_secret"] == ""
    assert cred["client_information"] == {"client_id": "ig_cid"}


@pytest.mark.asyncio
@patch("core.services.credentials.oauth_dance.httpx.AsyncClient", _FakeAsyncClient)
@patch("core.services.credentials.oauth_dance._oauth_client", new_callable=AsyncMock)
@patch("core.services.credentials.oauth_dance.oauth_state_repo.consume", new_callable=AsyncMock)
async def test_exchange_meta_ads_upgrades_via_fb_exchange_token(mock_consume, mock_client):
    """Meta Ads must take the fb_exchange_token upgrade, not Instagram's.

    Without it the code exchange leaves a token measured in hours, and since Meta
    issues no refresh_token the MCP SDK has nothing to renew from — the ads server
    starts 401ing the same afternoon. The parameter names differ from Instagram's
    (fb_exchange_token, plus client_id), so assert the exact request.
    """
    mock_consume.return_value = {
        "owner_id": "org_1",
        "provider": "meta_ads",
        "verifier": "v" * 43,
        "client_id": "meta_app_id",
    }
    mock_client.return_value = ("meta_app_id", "meta_platform_secret")

    owner, provider, cred, _origin = await oauth_dance.exchange(
        await oauth_dance.oauth_state_repo.consume("state123"), "code456", redirect_uri="https://api/cb"
    )

    assert (owner, provider) == ("org_1", "meta_ads")
    # The upgrade reuses the code-exchange endpoint, so the Graph version tracks
    # providers.yaml rather than a second hardcoded copy.
    assert _FakeAsyncClient.last_get["url"] == "https://graph.facebook.com/v25.0/oauth/access_token"
    assert _FakeAsyncClient.last_get["params"] == {
        "grant_type": "fb_exchange_token",
        "client_id": "meta_app_id",
        "client_secret": "meta_platform_secret",
        "fb_exchange_token": "SHORT",
    }
    # The ~60d token replaced the short one.
    assert cred["access_token"] == "LONG60D"
    assert cred["expires_in"] == 5184000
    # Meta issues no refresh_token — mcp_oauth omits the key entirely rather than
    # writing an empty one, which is why refresh_owner is `none`, not `actor`.
    assert cred["refresh_token"] == ""
    # Platform secret never rides out in the credential payload.
    assert cred["client_secret"] == ""


async def _exchange_for(provider: str) -> dict:
    """Run exchange() for a BYO confidential client and return the captured POST."""
    with (
        patch("core.services.credentials.oauth_dance.httpx.AsyncClient", _FakeAsyncClient),
        patch("core.services.credentials.oauth_dance.oauth_app_store.get", new_callable=AsyncMock) as store_get,
        patch("core.services.credentials.oauth_dance._oauth_client", new_callable=AsyncMock) as client,
        patch("core.services.credentials.oauth_dance.oauth_state_repo.consume", new_callable=AsyncMock) as consume,
    ):
        store_get.return_value = None
        client.return_value = ("cid", "sec")
        consume.return_value = {"owner_id": "user_1", "provider": provider, "verifier": "v" * 43, "client_id": "cid"}
        await oauth_dance.exchange(
            await oauth_dance.oauth_state_repo.consume("state123"), "code456", redirect_uri="https://api/cb"
        )
    return _FakeAsyncClient.last_post


@pytest.mark.asyncio
async def test_exchange_uses_basic_auth_when_provider_declares_it():
    # X rejects a confidential client that posts its secret: the code exchange has
    # to carry HTTP Basic instead, and the secret must not also ride in the body.
    post = await _exchange_for("x")
    assert isinstance(post["auth"], oauth_dance.httpx.BasicAuth)
    assert post["auth"]._auth_header == oauth_dance.httpx.BasicAuth("cid", "sec")._auth_header
    assert "client_secret" not in post["data"]


@pytest.mark.asyncio
async def test_exchange_posts_secret_by_default():
    # Every other confidential provider keeps the pre-X behaviour: secret in the
    # form body, no Authorization header.
    post = await _exchange_for("google")
    assert post["auth"] is None
    assert post["data"]["client_secret"] == "sec"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["quickbooks", "x"])
async def test_exchange_omits_body_client_id_under_basic(provider):
    # The Basic header already identifies the client. Intuit counts a body
    # client_id as a second client-authentication method and answers 401
    # invalid_client (verified against its live token endpoint: identical
    # request, dropping only this key, 401 -> 200). X documents the same
    # expectation, so this holds for every Basic provider rather than one.
    post = await _exchange_for(provider)
    assert isinstance(post["auth"], oauth_dance.httpx.BasicAuth)
    assert "client_id" not in post["data"]
    assert "client_secret" not in post["data"]


@pytest.mark.asyncio
async def test_exchange_keeps_body_client_id_without_basic():
    # A client_secret_post provider has no Basic header, so the body client_id is
    # the only thing identifying the client — dropping it there would break them.
    post = await _exchange_for("stripe")
    assert post["auth"] is None
    assert post["data"]["client_id"] == "cid"
