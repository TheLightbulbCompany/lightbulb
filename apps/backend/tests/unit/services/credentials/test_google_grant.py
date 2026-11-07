"""The backend reads Google from the grant gog already holds: no new consent, gog untouched."""

from unittest.mock import AsyncMock

import pytest

from core.services.credentials import access_token, google_grant, hermes_cli
from core.services.credentials.materializers import base, gog_cli

OWNER = "org_g"


@pytest.fixture(autouse=True)
def _clear():
    google_grant._grants.clear()
    yield
    google_grant._grants.clear()


@pytest.fixture
def wired(monkeypatch):
    monkeypatch.setattr(
        google_grant.google_accounts_repo, "list_accounts", AsyncMock(return_value=[{"email": "a@biz.com"}])
    )
    monkeypatch.setattr(hermes_cli, "_roster", AsyncMock(return_value=["ceo"]))
    export = []
    monkeypatch.setattr(google_grant, "_export_sync", lambda o, agents, email: export.append(email) or "rt-1")
    refresh = AsyncMock(return_value={"refresh_token": "rt-1", "access_token": "at-1", "expires_in": 3599})
    monkeypatch.setattr(google_grant.oauth_dance, "refresh", refresh)
    return export, refresh


async def test_exports_once_then_serves_the_cached_token(wired):
    export, refresh = wired
    assert (await google_grant.access_token(OWNER))[0] == "at-1"
    assert (await google_grant.access_token(OWNER))[0] == "at-1"
    assert export == ["a@biz.com"] and refresh.await_count == 1
    refresh.assert_awaited_with(OWNER, "google", {"refresh_token": "rt-1"})


async def test_a_failed_refresh_is_a_value_error_and_drops_the_grant(wired):
    _, refresh = wired
    refresh.side_effect = RuntimeError("invalid_grant")
    with pytest.raises(ValueError):
        await google_grant.access_token(OWNER)
    assert OWNER not in google_grant._grants


async def test_access_token_get_routes_google_and_never_flips_the_row(wired, monkeypatch):
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value={"status": "connected"}))
    mark = AsyncMock()
    monkeypatch.setattr(access_token, "_mark_reconnect", mark)
    assert await access_token.get(OWNER, "google") == "at-1"
    monkeypatch.setattr(access_token.integrations_repo, "get", AsyncMock(return_value=None))
    with pytest.raises(ValueError):
        await access_token.get(OWNER, "google")
    assert OWNER not in google_grant._grants and mark.await_count == 0


def test_export_reads_a_scratch_keyring_seeded_from_s3(monkeypatch):
    seen = {}

    def fetch(owner, agent, prefixes, dest):
        keyring = dest / "home/.local/share/gogcli/keyring"
        keyring.parent.mkdir(parents=True)
        keyring.write_bytes(b"jwe")

    def export(owner, email, env):
        seen["root"], seen["email"] = base.materializer_root(), email
        return "rt-x"

    from core.services import hermes_profiles

    monkeypatch.setattr(hermes_profiles, "fetch_profile_subtree", fetch)
    monkeypatch.setattr(gog_cli, "_export_refresh_token", export)
    assert google_grant._export_sync(OWNER, ["ceo"], "a@biz.com") == "rt-x"
    assert seen["root"].name == "home" and seen["email"] == "a@biz.com"
