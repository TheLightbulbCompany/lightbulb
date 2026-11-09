import time
from urllib.parse import parse_qs, urlsplit

import pytest

from core.repositories import oauth_state_repo
from core.services.credentials import oauth_dance


# --- oauth_state_repo (Task 5.1) -------------------------------------------


@pytest.fixture
def fake_state_table(monkeypatch):
    store = {}

    class _T:
        def put_item(self, Item):
            store[Item["state"]] = Item

        def get_item(self, Key):
            it = store.get(Key["state"])
            return {"Item": it} if it else {}

        def delete_item(self, Key):
            store.pop(Key["state"], None)

    monkeypatch.setattr(oauth_state_repo, "_table", lambda: _T())
    return store


@pytest.mark.asyncio
async def test_state_roundtrip(fake_state_table):
    await oauth_state_repo.put("st_1", owner_id="org_1", provider="google", verifier="v1")
    rec = await oauth_state_repo.consume("st_1")
    assert rec is not None
    assert rec["owner_id"] == "org_1"
    assert rec["provider"] == "google"
    assert rec["verifier"] == "v1"


@pytest.mark.asyncio
async def test_state_single_use(fake_state_table):
    await oauth_state_repo.put("st_1", owner_id="org_1", provider="google", verifier="v1")
    assert await oauth_state_repo.consume("st_1") is not None
    assert await oauth_state_repo.consume("st_1") is None


@pytest.mark.asyncio
async def test_state_expired_returns_none(fake_state_table):
    # Write a record whose ttl is already in the past (DDB TTL deletion can lag).
    fake_state_table["st_old"] = {
        "state": "st_old",
        "owner_id": "org_1",
        "provider": "google",
        "verifier": "v1",
        "ttl": int(time.time()) - 5,
    }
    assert await oauth_state_repo.consume("st_old") is None


@pytest.mark.asyncio
async def test_state_unknown_returns_none(fake_state_table):
    assert await oauth_state_repo.consume("missing") is None


# --- oauth_dance (Task 5.2) ------------------------------------------------

REDIRECT = "https://app.isol8.co/oauth/callback"


@pytest.mark.asyncio
async def test_start_builds_authorize_url(monkeypatch):
    stored = {}

    async def _fake_put(state, *, owner_id, provider, verifier, client_id=None, origin=None):
        stored.update(state=state, owner_id=owner_id, provider=provider, verifier=verifier, client_id=client_id)

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put)

    async def _fake_client(owner_id, provider_key):
        return ("cid", "secret")

    monkeypatch.setattr(oauth_dance, "_oauth_client", _fake_client)

    url = await oauth_dance.start("org_1", "google", redirect_uri=REDIRECT)

    parts = urlsplit(url)
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == "https://accounts.google.com/o/oauth2/v2/auth"
    q = parse_qs(parts.query)
    assert q["client_id"] == ["cid"]
    assert q["redirect_uri"] == [REDIRECT]
    assert q["response_type"] == ["code"]
    assert q["code_challenge_method"] == ["S256"]
    assert "code_challenge" in q and q["code_challenge"][0]
    # state stored matches the URL's state
    assert q["state"] == [stored["state"]]
    # joined scopes (google uses space separator)
    assert q["scope"] == [
        "openid email profile"
        " https://www.googleapis.com/auth/gmail.modify"
        " https://www.googleapis.com/auth/calendar"
        " https://www.googleapis.com/auth/drive"
    ]
    # google's extra authorize params
    assert q["access_type"] == ["offline"]
    assert q["prompt"] == ["consent"]
    # verifier was stored for later exchange
    assert stored["verifier"]
    assert stored["owner_id"] == "org_1"
    assert stored["provider"] == "google"
    # The resolved client_id is bound into the state so exchange() uses THIS client.
    assert stored["client_id"] == "cid"


@pytest.mark.asyncio
async def test_exchange_consumes_state_and_returns_credential(monkeypatch):
    posted = {}

    async def _fake_consume(state):
        assert state == "st_1"
        return {"state": "st_1", "owner_id": "org_1", "provider": "google", "verifier": "ver123"}

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    async def _fake_client(owner_id, provider_key):
        return ("cid", "secret")

    monkeypatch.setattr(oauth_dance, "_oauth_client", _fake_client)

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "access_token": "at_tok",
                "refresh_token": "rt_tok",
                "expires_in": 3600,
            }

    async def _fake_post(self, url, data=None, **kwargs):
        posted["url"] = url
        posted["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    owner_id, provider, credential, _origin = await oauth_dance.exchange(
        await oauth_dance.oauth_state_repo.consume("st_1"), "code_abc", redirect_uri=REDIRECT
    )

    assert owner_id == "org_1"
    assert provider == "google"
    assert posted["url"] == "https://oauth2.googleapis.com/token"
    assert posted["data"]["grant_type"] == "authorization_code"
    assert posted["data"]["code"] == "code_abc"
    assert posted["data"]["code_verifier"] == "ver123"
    assert posted["data"]["client_id"] == "cid"
    assert posted["data"]["client_secret"] == "secret"
    assert credential["access_token"] == "at_tok"
    assert credential["refresh_token"] == "rt_tok"
    assert credential["client_id"] == "cid"
    # google is a platform app: its secret is posted but never kept in the credential.
    assert credential["client_secret"] == ""
    # Materializers that persist the CLI's own app registration (xurl_cli) read
    # this off the credential. Dropping it here silently degrades them to a
    # derived fallback, so the contract is asserted rather than assumed.
    assert credential["redirect_uri"] == REDIRECT
    assert credential["token_expiry"]  # populated ISO string


@pytest.mark.asyncio
async def test_exchange_invalid_state_raises(monkeypatch):
    async def _fake_consume(state):
        return None

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    with pytest.raises(ValueError):
        await oauth_dance.exchange(await oauth_dance.oauth_state_repo.consume("missing"), "code", redirect_uri=REDIRECT)


# --- Dynamic Client Registration (DCR) + public clients --------------------
#
# attio is the DCR provider in the registry: it has a registration_url and no BYO
# app, so start() registers a fresh PUBLIC (no-secret) client and exchange() omits
# the client_secret. google (BYO, with secret) must keep working unchanged.


@pytest.fixture
def fake_app_store(monkeypatch):
    """In-memory stand-in for oauth_app_store keyed by (owner, provider)."""
    store: dict[tuple[str, str], dict] = {}

    async def _get(owner_id, provider):
        return store.get((owner_id, provider))

    async def _put(
        owner_id,
        provider,
        *,
        client_id,
        client_secret,
        client_information=None,
        scope=None,
        redirect_uri=None,
    ):
        rec = {"client_id": client_id, "client_secret": client_secret}
        if client_information is not None:
            rec["client_information"] = client_information
        if scope is not None:
            rec["scope"] = scope
        if redirect_uri is not None:
            rec["redirect_uri"] = redirect_uri
        store[(owner_id, provider)] = rec
        return "arn:secret"

    monkeypatch.setattr(oauth_dance.oauth_app_store, "get", _get)
    monkeypatch.setattr(oauth_dance.oauth_app_store, "put", _put)
    return store


@pytest.mark.asyncio
async def test_start_registers_dcr_client_when_absent(monkeypatch, fake_app_store):
    async def _fake_put_state(state, *, owner_id, provider, verifier, client_id=None):
        pass

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put_state)

    registered = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "client_id": "dcr_cid",
                "redirect_uris": [REDIRECT],
                "token_endpoint_auth_method": "none",
            }

    async def _fake_post(self, url, json=None, data=None, **kwargs):
        registered["url"] = url
        registered["json"] = json
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    url = await oauth_dance.start("org_1", "attio", redirect_uri=REDIRECT)

    # Registration was POSTed to attio's registration endpoint with a public-client body.
    assert registered["url"] == "https://app.attio.com/oauth/register"
    body = registered["json"]
    assert body["token_endpoint_auth_method"] == "none"
    assert body["redirect_uris"] == [REDIRECT]
    assert body["grant_types"] == ["authorization_code", "refresh_token"]
    assert body["scope"] == "openid offline_access mcp"

    # The registered client was cached (id + empty secret marks it public) with the
    # full registration JSON for the materializer to write as clientInformation.
    cached = fake_app_store[("org_1", "attio")]
    assert cached["client_id"] == "dcr_cid"
    assert cached["client_secret"] == ""
    assert cached["client_information"]["token_endpoint_auth_method"] == "none"
    # The requested scope is stored so start() can detect a later scope change.
    assert cached["scope"] == "openid offline_access mcp"
    # ...and so is the redirect uri, for the same reason (DCR pins redirect_uris).
    assert cached["redirect_uri"] == REDIRECT

    # The authorize URL uses the freshly registered client_id.
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["dcr_cid"]
    assert q["scope"] == ["openid offline_access mcp"]


@pytest.mark.asyncio
async def test_start_reuses_cached_dcr_client(monkeypatch, fake_app_store):
    # A previously registered client with the SAME scope must NOT trigger a second
    # registration.
    fake_app_store[("org_1", "attio")] = {
        "client_id": "existing_cid",
        "client_secret": "",
        "client_information": {"client_id": "existing_cid"},
        # Matches attio's current registry scope list, so start() reuses it.
        "scope": "openid offline_access mcp",
    }

    async def _fake_put_state(state, *, owner_id, provider, verifier, client_id=None):
        pass

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put_state)

    posts = []

    async def _fake_post(self, url, **kwargs):
        posts.append(url)
        raise AssertionError("registration must not be called when a client is cached")

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    url = await oauth_dance.start("org_1", "attio", redirect_uri=REDIRECT)
    assert posts == []
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["existing_cid"]


@pytest.mark.asyncio
async def test_start_reregisters_dcr_client_when_scope_changed(monkeypatch, fake_app_store):
    # A cached client registered under an OLD scope set must be re-registered
    # (overwritten) so authorize uses a client carrying the current scopes. This is
    # the no-delete path that replaces clearing the secret on disconnect.
    fake_app_store[("org_1", "attio")] = {
        "client_id": "stale_cid",
        "client_secret": "",
        "client_information": {"client_id": "stale_cid"},
        "scope": "openid offline_access",  # missing 'mcp' vs the current registry list
    }

    async def _fake_put_state(state, *, owner_id, provider, verifier, client_id=None):
        pass

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put_state)

    posts = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"client_id": "fresh_cid", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}

    async def _fake_post(self, url, json=None, data=None, **kwargs):
        posts.append((url, json))
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    url = await oauth_dance.start("org_1", "attio", redirect_uri=REDIRECT)

    # Re-registration ran once, with the CURRENT scope, overwriting the cached client.
    assert len(posts) == 1
    assert posts[0][1]["scope"] == "openid offline_access mcp"
    cached = fake_app_store[("org_1", "attio")]
    assert cached["client_id"] == "fresh_cid"
    assert cached["scope"] == "openid offline_access mcp"
    q = parse_qs(urlsplit(url).query)
    assert q["client_id"] == ["fresh_cid"]


@pytest.mark.asyncio
async def test_start_reregisters_dcr_client_when_redirect_uri_changed(monkeypatch, fake_app_store):
    """DCR PINS ``redirect_uris`` at registration, so after a callback-domain
    move every cached client rejects the new uri — and comparing scope alone
    means no amount of "the owner reconnects" ever heals it."""
    fake_app_store[("org_1", "attio")] = {
        "client_id": "old_domain_cid",
        "client_secret": "",
        "client_information": {"client_id": "old_domain_cid"},
        "scope": "openid offline_access mcp",  # unchanged — only the uri moved
        "redirect_uri": "https://api.old-domain.example/api/v1/connections/callback",
    }

    async def _fake_put_state(state, *, owner_id, provider, verifier, client_id=None):
        pass

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put_state)

    posts = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"client_id": "new_domain_cid", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}

    async def _fake_post(self, url, json=None, data=None, **kwargs):
        posts.append((url, json))
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    url = await oauth_dance.start("org_1", "attio", redirect_uri=REDIRECT)

    assert len(posts) == 1
    assert posts[0][1]["redirect_uris"] == [REDIRECT]
    cached = fake_app_store[("org_1", "attio")]
    assert cached["client_id"] == "new_domain_cid"
    assert cached["redirect_uri"] == REDIRECT
    assert parse_qs(urlsplit(url).query)["client_id"] == ["new_domain_cid"]


@pytest.mark.asyncio
async def test_start_reregisters_on_redirect_drift_seen_only_in_the_echoed_response(monkeypatch, fake_app_store):
    """Records predate the stored ``redirect_uri`` field, so the registration
    response's own echo of ``redirect_uris`` is the fallback evidence."""
    fake_app_store[("org_1", "attio")] = {
        "client_id": "echoed_cid",
        "client_secret": "",
        "client_information": {
            "client_id": "echoed_cid",
            "redirect_uris": ["https://api.old-domain.example/api/v1/connections/callback"],
        },
        "scope": "openid offline_access mcp",
    }

    async def _fake_put_state(state, *, owner_id, provider, verifier, client_id=None):
        pass

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put_state)

    posts = []

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"client_id": "fresh_cid", "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none"}

    async def _fake_post(self, url, json=None, data=None, **kwargs):
        posts.append(url)
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    await oauth_dance.start("org_1", "attio", redirect_uri=REDIRECT)
    assert len(posts) == 1


@pytest.mark.asyncio
async def test_start_does_not_churn_a_client_whose_redirect_uri_is_unknown(monkeypatch, fake_app_store):
    """No stored uri and no echo means UNKNOWN, not mismatched — re-registering
    on a guess would mint a new client_id for every legacy owner on connect."""
    fake_app_store[("org_1", "attio")] = {
        "client_id": "unknown_cid",
        "client_secret": "",
        "client_information": {"client_id": "unknown_cid"},
        "scope": "openid offline_access mcp",
    }

    async def _fake_put_state(state, *, owner_id, provider, verifier, client_id=None):
        pass

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put_state)

    async def _fake_post(self, url, **kwargs):
        raise AssertionError("must not re-register when the registered uri is unknown")

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    url = await oauth_dance.start("org_1", "attio", redirect_uri=REDIRECT)
    assert parse_qs(urlsplit(url).query)["client_id"] == ["unknown_cid"]


@pytest.mark.asyncio
async def test_exchange_public_client_omits_client_secret(monkeypatch, fake_app_store):
    # A cached public client (empty secret) drives a secret-less token exchange and
    # the credential carries the cached client_information for the materializer.
    fake_app_store[("org_1", "attio")] = {
        "client_id": "dcr_cid",
        "client_secret": "",
        "client_information": {"client_id": "dcr_cid", "token_endpoint_auth_method": "none"},
    }

    async def _fake_consume(state):
        return {"state": state, "owner_id": "org_1", "provider": "attio", "verifier": "ver123"}

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    posted = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "at_tok", "refresh_token": "rt_tok", "expires_in": 3600}

    async def _fake_post(self, url, data=None, **kwargs):
        posted["url"] = url
        posted["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    owner_id, provider, credential, _origin = await oauth_dance.exchange(
        await oauth_dance.oauth_state_repo.consume("st_1"), "code_abc", redirect_uri=REDIRECT
    )

    assert owner_id == "org_1" and provider == "attio"
    assert posted["url"] == "https://app.attio.com/oidc/token"
    # The public client proves itself with PKCE only — NO client_secret in the body.
    assert "client_secret" not in posted["data"]
    assert posted["data"]["client_id"] == "dcr_cid"
    assert posted["data"]["code_verifier"] == "ver123"
    assert posted["data"]["grant_type"] == "authorization_code"
    # Credential carries the token fields + an int expires_in + the cached client_information.
    assert credential["access_token"] == "at_tok"
    assert credential["refresh_token"] == "rt_tok"
    assert credential["expires_in"] == 3600
    assert credential["client_information"]["token_endpoint_auth_method"] == "none"


# --- RFC 8707 resource indicator ------------------------------------------
#
# A remote MCP server (mcp_remote: attio) requires the canonical server URI in a
# `resource` parameter on BOTH the authorize request and the token request so the
# token is audienced to it. A BYO OAuth provider with no mcp_remote (google) must
# NOT receive the param.


@pytest.mark.asyncio
async def test_start_includes_resource_for_mcp_remote_provider(monkeypatch, fake_app_store):
    # A cached client skips registration; assert the authorize URL carries the
    # canonical MCP server URL as the resource indicator.
    fake_app_store[("org_1", "attio")] = {
        "client_id": "dcr_cid",
        "client_secret": "",
        "client_information": {"client_id": "dcr_cid"},
        "scope": "openid offline_access mcp",
    }

    async def _unexpected_registration(self, url, **kwargs):
        raise AssertionError("matching cached scope must reuse the existing client")

    monkeypatch.setattr("httpx.AsyncClient.post", _unexpected_registration)

    async def _fake_put_state(state, *, owner_id, provider, verifier, client_id=None):
        pass

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put_state)

    url = await oauth_dance.start("org_1", "attio", redirect_uri=REDIRECT)
    q = parse_qs(urlsplit(url).query)
    assert q["resource"] == ["https://mcp.attio.com/mcp"]


@pytest.mark.asyncio
async def test_start_omits_resource_for_byo_provider(monkeypatch):
    # google has no mcp_remote — the authorize request must not carry a resource param.
    async def _fake_put(state, *, owner_id, provider, verifier, client_id=None, origin=None):
        pass

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "put", _fake_put)

    async def _fake_client(owner_id, provider_key):
        return ("cid", "secret")

    monkeypatch.setattr(oauth_dance, "_oauth_client", _fake_client)

    url = await oauth_dance.start("org_1", "google", redirect_uri=REDIRECT)
    q = parse_qs(urlsplit(url).query)
    assert "resource" not in q


@pytest.mark.asyncio
async def test_exchange_includes_resource_for_mcp_remote_provider(monkeypatch, fake_app_store):
    # The token POST mirrors the authorize request: a mcp_remote provider sends the
    # canonical server URL as the resource indicator.
    fake_app_store[("org_1", "attio")] = {
        "client_id": "dcr_cid",
        "client_secret": "",
        "client_information": {"client_id": "dcr_cid"},
    }

    async def _fake_consume(state):
        return {"state": state, "owner_id": "org_1", "provider": "attio", "verifier": "ver123"}

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    posted = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "at_tok", "refresh_token": "rt_tok", "expires_in": 3600}

    async def _fake_post(self, url, data=None, **kwargs):
        posted["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    await oauth_dance.exchange(await oauth_dance.oauth_state_repo.consume("st_1"), "code_abc", redirect_uri=REDIRECT)
    assert posted["data"]["resource"] == "https://mcp.attio.com/mcp"


@pytest.mark.asyncio
async def test_exchange_omits_resource_for_byo_provider(monkeypatch, fake_app_store):
    # x has no mcp_remote — the token POST must not carry a resource param.
    fake_app_store[("org_1", "x")] = {"client_id": "cid", "client_secret": "secret"}

    async def _fake_consume(state):
        return {"state": state, "owner_id": "org_1", "provider": "x", "verifier": "ver123"}

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    posted = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "at_tok", "refresh_token": "rt_tok", "expires_in": 3600}

    async def _fake_post(self, url, data=None, **kwargs):
        posted["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    await oauth_dance.exchange(await oauth_dance.oauth_state_repo.consume("st_1"), "code_abc", redirect_uri=REDIRECT)
    assert "resource" not in posted["data"]


@pytest.mark.asyncio
async def test_exchange_byo_client_still_sends_secret(monkeypatch, fake_app_store):
    # The x (BYO, confidential) path is unchanged: a stored secret is sent in
    # the token POST and the credential's client_information is the minimal pair.
    fake_app_store[("org_1", "x")] = {"client_id": "cid", "client_secret": "secret"}

    async def _fake_consume(state):
        return {"state": state, "owner_id": "org_1", "provider": "x", "verifier": "ver123"}

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    posted = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "at_tok", "refresh_token": "rt_tok", "expires_in": 3600}

    async def _fake_post(self, url, data=None, **kwargs):
        posted["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    _, _, credential, _ = await oauth_dance.exchange(
        await oauth_dance.oauth_state_repo.consume("st_1"), "code_abc", redirect_uri=REDIRECT
    )

    # x is client_secret_basic: the secret rides the Authorization header, not the body.
    assert "client_secret" not in posted["data"]
    assert credential["client_id"] == "cid"
    assert credential["client_secret"] == "secret"
    # No cached client_information for a BYO app — synthesize the minimal shape.
    assert credential["client_information"] == {"client_id": "cid", "client_secret": "secret"}


# --- code↔client binding (concurrent-DCR race) ----------------------------
#
# start() binds the issued client_id into the OAuth state. exchange() must use
# THAT client_id, not whatever the app store currently holds — a concurrent
# connect could have re-registered a different DCR client between start() and the
# callback. The binding makes the code↔client pairing atomic per-authorization.


@pytest.mark.asyncio
async def test_exchange_uses_state_bound_client_id_over_store(monkeypatch, fake_app_store):
    # The app store was overwritten by a concurrent connect AFTER this authorization
    # started: it now holds a different DCR client. exchange() must use the client_id
    # bound into the state (the one this code was issued to), not the store's.
    fake_app_store[("org_1", "attio")] = {
        "client_id": "newer_concurrent_cid",
        "client_secret": "",
        "client_information": {"client_id": "newer_concurrent_cid"},
    }

    async def _fake_consume(state):
        # The state carries the client_id start() resolved for THIS authorization.
        return {
            "state": state,
            "owner_id": "org_1",
            "provider": "attio",
            "verifier": "ver123",
            "client_id": "this_auth_cid",
        }

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    posted = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "at_tok", "refresh_token": "rt_tok", "expires_in": 3600}

    async def _fake_post(self, url, data=None, **kwargs):
        posted["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    _, _, credential, _ = await oauth_dance.exchange(
        await oauth_dance.oauth_state_repo.consume("st_1"), "code_abc", redirect_uri=REDIRECT
    )

    # The token POST + credential use the state's client_id, NOT the store's newer one.
    assert posted["data"]["client_id"] == "this_auth_cid"
    assert credential["client_id"] == "this_auth_cid"


@pytest.mark.asyncio
async def test_exchange_falls_back_to_store_client_id_for_legacy_state(monkeypatch, fake_app_store):
    # Back-compat: a state minted before the binding existed has no client_id, so
    # exchange() falls back to the app store's client_id (the prior behavior).
    fake_app_store[("org_1", "attio")] = {
        "client_id": "store_cid",
        "client_secret": "",
        "client_information": {"client_id": "store_cid"},
    }

    async def _fake_consume(state):
        # No client_id key — an in-flight authorization from before this change.
        return {"state": state, "owner_id": "org_1", "provider": "attio", "verifier": "ver123"}

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    posted = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"access_token": "at_tok", "refresh_token": "rt_tok", "expires_in": 3600}

    async def _fake_post(self, url, data=None, **kwargs):
        posted["data"] = data
        return _Resp()

    monkeypatch.setattr("httpx.AsyncClient.post", _fake_post)

    _, _, credential, _ = await oauth_dance.exchange(
        await oauth_dance.oauth_state_repo.consume("st_1"), "code_abc", redirect_uri=REDIRECT
    )

    assert posted["data"]["client_id"] == "store_cid"
    assert credential["client_id"] == "store_cid"
