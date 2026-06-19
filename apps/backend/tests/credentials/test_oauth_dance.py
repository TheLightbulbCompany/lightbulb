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

    async def _fake_put(state, *, owner_id, provider, verifier):
        stored.update(state=state, owner_id=owner_id, provider=provider, verifier=verifier)

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
        "https://www.googleapis.com/auth/gmail.modify"
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

    owner_id, provider, credential = await oauth_dance.exchange("st_1", "code_abc", redirect_uri=REDIRECT)

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
    assert credential["client_secret"] == "secret"
    assert credential["token_expiry"]  # populated ISO string


@pytest.mark.asyncio
async def test_exchange_invalid_state_raises(monkeypatch):
    async def _fake_consume(state):
        return None

    monkeypatch.setattr(oauth_dance.oauth_state_repo, "consume", _fake_consume)

    with pytest.raises(ValueError):
        await oauth_dance.exchange("missing", "code", redirect_uri=REDIRECT)
