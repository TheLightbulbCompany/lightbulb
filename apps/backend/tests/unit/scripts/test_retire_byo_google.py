import asyncio
from unittest.mock import AsyncMock, patch

from scripts import retire_byo_google as s


def test_needs_reconnect_only_old_connected_google():
    base = {"provider": "google", "status": "connected", "connected_at": 100}
    assert s.needs_reconnect(base, 200)
    assert not s.needs_reconnect({**base, "connected_at": 300}, 200)  # reconnected after deploy
    assert not s.needs_reconnect({**base, "status": "reconnect_required"}, 200)
    assert not s.needs_reconnect({**base, "provider": "github"}, 200)


def test_classify_routines():
    routines = [
        {"id": "a", "status": "active", "title": "Google: New email (real-time push)"},
        {"id": "b", "status": "paused", "title": "Google: New email (checked on a schedule)"},
        {"id": "c", "status": "active", "title": "Mine", "description": "x Scheduled inbox check (account: a@b.c)"},
        {"id": "d", "status": "active", "title": "Google: Calendar changed"},
    ]
    pause, manual = s.classify_routines(routines)
    assert [r["id"] for r in pause] == ["a"]
    assert [r["id"] for r in manual] == ["c"]


def test_retire_grant_dry_run_writes_nothing_apply_marks_and_clears():
    with (
        patch.object(s, "_mark_reconnect", new=AsyncMock()) as mark,
        patch.object(s.hermes_cli, "clear", new=AsyncMock()) as clear,
        patch.object(s.integration_secret_store, "get", new=AsyncMock(return_value={})),
        patch.object(s.google_accounts_repo, "list_accounts", new=AsyncMock(return_value=[{"email": "a@b.c"}])),
    ):
        asyncio.run(s._retire_grant("org_1", apply=False))
        mark.assert_not_called()
        clear.assert_not_called()
        asyncio.run(s._retire_grant("org_1", apply=True))
        mark.assert_awaited_once_with("org_1", "google", "lightbulb_retired_grant")
        assert clear.await_args.args[3] == {"disconnect_all": True, "accounts": ["a@b.c"]}
