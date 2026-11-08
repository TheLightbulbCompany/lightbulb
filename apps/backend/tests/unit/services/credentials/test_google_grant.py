"""The backend reads Google from the grant gog already holds: no new consent, gog untouched."""

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest

from core.services.credentials import access_token, google_grant, hermes_cli
from core.services.credentials.materializers import base, gog_cli

OWNER = "org_g"


@pytest.fixture(autouse=True)
def _clear():
    for d in (google_grant._grants, google_grant._failed, google_grant._inflight):
        d.clear()
    yield
    for d in (google_grant._grants, google_grant._failed, google_grant._inflight):
        d.clear()


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


async def test_a_broken_grant_is_fetched_once_and_remembered(wired, monkeypatch, caplog):
    """8 Nov prod: an unexportable grant re-ran the export on every phone turn, 7 at once (one per profile day), and
    the Business profile timed out 18 times in 16 minutes. Now: one fetch for all of them, then 5 min of fast
    failures, and one warning for the workspace."""
    export = []

    def slow_export(o, agents, email):
        time.sleep(0.05)
        export.append(email)

    monkeypatch.setattr(google_grant, "_export_sync", slow_export)
    with caplog.at_level(logging.WARNING, logger=google_grant.__name__):
        got = await asyncio.gather(*(google_grant.access_token(OWNER) for _ in range(7)), return_exceptions=True)
        assert all(isinstance(e, ValueError) for e in got) and len(export) == 1
        with pytest.raises(ValueError, match="no exportable grant"):
            await google_grant.access_token(OWNER)
        assert len(export) == 1  # the failure is remembered
        google_grant._failed[OWNER] = (0.0, google_grant._failed[OWNER][1])  # the 5 min are up
        with pytest.raises(ValueError):
            await google_grant.access_token(OWNER)
    assert len(export) == 2
    assert [r.getMessage() for r in caplog.records].count(
        f"google grant needs reconnecting owner={OWNER} (google: no exportable grant in the workspace keyring); "
        "phone bookings stay in Nova's calendar"
    ) == 1


async def test_a_fetch_its_caller_stopped_waiting_for_still_lands(wired, monkeypatch):
    """The export is a thread asyncio cannot cancel: a turn's 2 s bound gives up waiting, never the fetch, so the
    next turn reads its result instead of starting another export."""
    export, _ = wired
    gate = asyncio.Event()

    async def slow_refresh(*_a):
        await gate.wait()
        return {"refresh_token": "rt-1", "access_token": "at-2", "expires_in": 3599}

    monkeypatch.setattr(google_grant.oauth_dance, "refresh", slow_refresh)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(google_grant.access_token(OWNER), 0.01)
    gate.set()
    assert (await google_grant.access_token(OWNER))[0] == "at-2"
    assert export == ["a@biz.com"]  # one export, though the first caller stopped waiting


async def test_any_fetch_error_is_a_value_error_and_a_success_clears_it(wired, monkeypatch):
    _, refresh = wired
    monkeypatch.setattr(google_grant.hermes_cli, "_roster", AsyncMock(side_effect=OSError("s3 secret-ish detail")))
    with pytest.raises(ValueError, match=r"^google: OSError$"):
        await google_grant.access_token(OWNER)
    google_grant._failed[OWNER] = (0.0, "x")
    monkeypatch.setattr(google_grant.hermes_cli, "_roster", AsyncMock(return_value=["ceo"]))
    assert (await google_grant.access_token(OWNER))[0] == "at-1"
    assert OWNER not in google_grant._failed
