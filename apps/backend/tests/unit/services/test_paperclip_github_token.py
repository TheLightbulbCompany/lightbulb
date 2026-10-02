"""paperclip_github_token: Paperclip's GITHUB_TOKEN company secret follows the workspace's
GitHub connection, touches only a secret Lightbulb created, and never leaks the token."""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import paperclip_github_token as mod
from core.services.paperclip_admin_client import PaperclipApiError

# Bound at import: tests/conftest.py's autouse fixture replaces the module attribute.
sync_github_token = mod.sync_github_token

TOKEN = "not-a-real-github-token-0123456789"
DESCRIPTION = f"{mod.SECRET_DESCRIPTION} [fp:{mod.fingerprint(TOKEN)}]"
OURS = {"id": "sec_1", "name": "GITHUB_TOKEN", "description": DESCRIPTION, "status": "active"}
OURS_OLD_TOKEN = {**OURS, "description": f"{mod.SECRET_DESCRIPTION} [fp:{mod.fingerprint('an-older-token')}]"}
OURS_LEGACY = {**OURS, "description": mod.SECRET_DESCRIPTION}  # synced before the fingerprint existed
THEIRS = {"id": "sec_9", "name": "GITHUB_TOKEN", "description": "my own PAT", "status": "active"}


@pytest.fixture
def admin(monkeypatch):
    fake = MagicMock()
    fake.list_company_secrets = AsyncMock(return_value=[{"id": "sec_0", "name": "OTHER", "description": None}])
    fake.create_company_secret = AsyncMock(return_value={"id": "sec_1"})
    fake.rotate_secret = AsyncMock(return_value={"id": "sec_1"})
    fake.update_secret_description = AsyncMock(return_value={"id": "sec_1"})
    fake.delete_secret = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(mod, "PaperclipAdminClient", lambda http_client: fake)
    monkeypatch.setattr(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(mod, "resolve_company_id_for_owner", AsyncMock(return_value="co_1"))
    # The stored dict also carries the platform app's client_secret: only the token may leave.
    stored = {"access_token": TOKEN, "client_id": "cid", "client_secret": "platform-app-secret"}
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(return_value=stored))
    return fake


def _sent(admin) -> str:
    return repr(admin.mock_calls)


@pytest.mark.asyncio
async def test_creates_the_secret_when_the_company_has_none(admin):
    assert await sync_github_token("org_a") == "created"
    admin.create_company_secret.assert_awaited_once_with(
        session_cookie="cookie",
        company_id="co_1",
        name="GITHUB_TOKEN",
        value=TOKEN,
        description=DESCRIPTION,
    )
    admin.rotate_secret.assert_not_awaited()
    assert "platform-app-secret" not in _sent(admin)


@pytest.mark.asyncio
async def test_an_unchanged_token_writes_nothing(admin):
    # Every reconcile lands here: no new secret version, no activity row.
    admin.list_company_secrets.return_value = [OURS]
    assert await sync_github_token("org_a") == "unchanged"
    admin.rotate_secret.assert_not_awaited()
    admin.update_secret_description.assert_not_awaited()
    admin.create_company_secret.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("existing", [OURS_OLD_TOKEN, OURS_LEGACY], ids=["new-token", "legacy-bare-description"])
async def test_a_changed_token_rotates_then_records_the_fingerprint(admin, existing):
    admin.list_company_secrets.return_value = [existing]
    assert await sync_github_token("org_a") == "rotated"
    # Rotate first: a failed description write must cost an extra rotate, never skip one.
    assert [c[0] for c in admin.mock_calls] == ["list_company_secrets", "rotate_secret", "update_secret_description"]
    admin.rotate_secret.assert_awaited_once_with(session_cookie="cookie", secret_id="sec_1", value=TOKEN)
    admin.update_secret_description.assert_awaited_once_with(
        session_cookie="cookie", secret_id="sec_1", description=DESCRIPTION
    )
    admin.create_company_secret.assert_not_awaited()
    assert TOKEN not in DESCRIPTION


@pytest.mark.asyncio
async def test_a_409_on_create_relists_and_rotates(admin):
    admin.list_company_secrets.side_effect = [[], [OURS_OLD_TOKEN]]
    admin.create_company_secret.side_effect = PaperclipApiError("POST /api/companies/co_1/secrets -> 409", 409, "{}")
    assert await sync_github_token("org_a") == "rotated"
    admin.rotate_secret.assert_awaited_once_with(session_cookie="cookie", secret_id="sec_1", value=TOKEN)
    admin.update_secret_description.assert_awaited_once()


@pytest.mark.asyncio
async def test_deletes_our_secret_when_lightbulb_holds_no_token(admin, monkeypatch):
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(return_value=None))
    admin.list_company_secrets.return_value = [OURS]
    assert await sync_github_token("org_a") == "deleted"
    admin.delete_secret.assert_awaited_once_with(session_cookie="cookie", secret_id="sec_1")

    admin.list_company_secrets.return_value = []
    assert await sync_github_token("org_a") == "absent"
    admin.delete_secret.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("stored", [{"access_token": TOKEN}, None])
async def test_a_members_own_github_token_secret_is_left_alone(admin, monkeypatch, stored):
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(return_value=stored))
    admin.list_company_secrets.return_value = [THEIRS]
    assert await sync_github_token("org_a") == "foreign_secret"
    admin.create_company_secret.assert_not_awaited()
    admin.rotate_secret.assert_not_awaited()
    admin.update_secret_description.assert_not_awaited()
    admin.delete_secret.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_company_yet_reads_nothing_and_calls_nothing(admin, monkeypatch):
    monkeypatch.setattr(mod, "resolve_company_id_for_owner", AsyncMock(return_value=None))
    assert await sync_github_token("org_a") == "no_company"
    mod.integration_secret_store.get.assert_not_awaited()
    assert admin.mock_calls == []


@pytest.mark.asyncio
async def test_dry_run_only_lists(admin):
    assert await sync_github_token("org_a", dry_run=True) == "would_create"
    admin.list_company_secrets.return_value = [OURS_OLD_TOKEN]
    assert await sync_github_token("org_a", dry_run=True) == "would_rotate"
    admin.list_company_secrets.return_value = [OURS]
    assert await sync_github_token("org_a", dry_run=True) == "unchanged"
    admin.create_company_secret.assert_not_awaited()
    admin.rotate_secret.assert_not_awaited()
    admin.update_secret_description.assert_not_awaited()


# --- overlapping connect / disconnect: the sync runs outside the owner lifecycle lease ------


def _reads(monkeypatch, *values):
    """The workspace token as successive reads see it; the last value repeats."""
    seen = list(values)
    get = AsyncMock(side_effect=lambda *a: seen.pop(0) if len(seen) > 1 else seen[0])
    monkeypatch.setattr(mod.integration_secret_store, "get", get)
    return get


CONNECTED = {"access_token": TOKEN}


@pytest.mark.asyncio
@pytest.mark.parametrize("existing,outcome", [([], "absent"), ([OURS_OLD_TOKEN], "deleted")])
async def test_a_connect_sync_whose_token_is_gone_before_its_write_leaves_no_secret(
    admin, monkeypatch, existing, outcome
):
    # A disconnect landed between this sync's first read and its write: the delayed connect
    # sync must not hand Paperclip a token the workspace no longer holds.
    _reads(monkeypatch, CONNECTED, None)
    admin.list_company_secrets.return_value = existing
    assert await sync_github_token("org_a") == outcome
    admin.create_company_secret.assert_not_awaited()
    admin.rotate_secret.assert_not_awaited()
    assert admin.delete_secret.await_count == len(existing)


@pytest.mark.asyncio
async def test_a_disconnect_sync_whose_token_reappears_before_its_write_keeps_the_secret(admin, monkeypatch):
    _reads(monkeypatch, None, CONNECTED)
    admin.list_company_secrets.return_value = [OURS]
    assert await sync_github_token("org_a") == "unchanged"
    admin.delete_secret.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_disconnect_sync_whose_token_reappears_after_its_delete_puts_the_secret_back(admin, monkeypatch):
    # Reads: the plan (gone), the pre-write check (still gone), then the post-write check.
    _reads(monkeypatch, None, None, CONNECTED)
    admin.list_company_secrets.side_effect = [[OURS], []]
    assert await sync_github_token("org_a") == "created"
    admin.delete_secret.assert_awaited_once()
    assert admin.create_company_secret.await_args.kwargs["value"] == TOKEN


@pytest.mark.asyncio
async def test_a_token_that_never_settles_stops_after_the_pass_bound(admin, monkeypatch):
    flips = iter([CONNECTED, None] * 20)
    monkeypatch.setattr(mod.integration_secret_store, "get", AsyncMock(side_effect=lambda *a: next(flips)))
    assert await sync_github_token("org_a") == "unsettled"
    assert admin.list_company_secrets.await_count == mod._MAX_PASSES
    admin.create_company_secret.assert_not_awaited()
    admin.delete_secret.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_stale_admin_session_is_refreshed_once(admin, monkeypatch):
    invalidate = MagicMock()
    monkeypatch.setattr(mod, "invalidate_admin_session", invalidate)
    admin.list_company_secrets.side_effect = [PaperclipApiError("GET /x -> 401", 401, ""), []]
    assert await sync_github_token("org_a") == "created"
    invalidate.assert_called_once()


@pytest.mark.asyncio
async def test_the_token_never_reaches_a_log_line_or_the_outcome(admin, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setattr(mod, "put_metric", MagicMock())
    monkeypatch.setattr(mod, "sync_github_token", sync_github_token)

    assert TOKEN not in await sync_github_token("org_a")
    await mod.sync_best_effort("org_a")

    # Paperclip echoing the request back in an error body must not reach the log either.
    admin.list_company_secrets.return_value = [OURS_OLD_TOKEN]
    admin.rotate_secret.side_effect = PaperclipApiError("POST /api/secrets/sec_1/rotate -> 422", 422, f"value={TOKEN}")
    await mod.sync_best_effort("org_a")  # never raises

    mod.put_metric.assert_called_once_with("paperclip.github_token_sync.error")
    assert "FAILED" in caplog.text
    assert TOKEN not in caplog.text
