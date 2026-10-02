"""sync_paperclip_github_tokens: dry-run by default, github-connected owners only."""

from unittest.mock import AsyncMock

import pytest

from scripts import sync_paperclip_github_tokens as script

ROWS = [
    {"owner_id": "org_b", "provider": "github", "status": "connected"},
    {"owner_id": "org_a", "provider": "github", "status": "connected"},
    {"owner_id": "org_c", "provider": "slack", "status": "connected"},
    {"owner_id": "org_d", "provider": "github", "status": "pending"},
]


@pytest.fixture
def sync(monkeypatch):
    monkeypatch.setattr(script.integrations_repo, "scan_all", AsyncMock(return_value=ROWS))
    fake = AsyncMock(return_value="would_create")
    monkeypatch.setattr(script, "sync_github_token", fake)
    return fake


@pytest.mark.asyncio
async def test_all_is_a_dry_run_over_github_connected_owners(sync, capsys):
    assert await script.main(["--all"]) == 0
    assert [(c.args, c.kwargs) for c in sync.await_args_list] == [
        (("org_a",), {"dry_run": True}),
        (("org_b",), {"dry_run": True}),
    ]
    assert capsys.readouterr().out == "org_a would_create\norg_b would_create\n"


@pytest.mark.asyncio
async def test_apply_writes_and_one_failing_owner_does_not_stop_the_run(sync):
    sync.side_effect = [RuntimeError("paperclip 503"), "created"]
    assert await script.main(["--all", "--apply"]) == 1
    assert sync.await_args_list[1].kwargs == {"dry_run": False}
    assert sync.await_count == 2
