"""sync_paperclip_github_tokens: dry-run by default, every owner with a Paperclip company."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import paperclip_github_token as tokens
from scripts import sync_paperclip_github_tokens as script


@pytest.fixture
def sync(monkeypatch):
    # The repo scan is the target list: active workspaces, GitHub-connected or not.
    repo = MagicMock()
    repo.list_active_workspace_members = AsyncMock(return_value={"org_b": set(), "org_a": {"user_1"}})
    monkeypatch.setattr(tokens, "PaperclipRepo", lambda table_name: repo)
    monkeypatch.setattr(tokens, "put_metric", MagicMock())
    fake = AsyncMock(return_value="would_delete")
    monkeypatch.setattr(tokens, "sync_github_token", fake)
    return fake


@pytest.mark.asyncio
async def test_all_is_a_dry_run_over_every_owner_with_a_company(sync, capsys):
    assert await script.main(["--all"]) == 0
    assert [(c.args, c.kwargs) for c in sync.await_args_list] == [
        (("org_a",), {"dry_run": True}),
        (("org_b",), {"dry_run": True}),
    ]
    assert capsys.readouterr().out == "org_a would_delete\norg_b would_delete\n"


@pytest.mark.asyncio
async def test_apply_writes_and_one_failing_owner_does_not_stop_the_run(sync, capsys):
    sync.side_effect = [RuntimeError("paperclip 503"), "deleted"]
    assert await script.main(["--all", "--apply"]) == 1
    assert [(c.args, c.kwargs) for c in sync.await_args_list] == [(("org_a",), {}), (("org_b",), {})]
    assert capsys.readouterr().out == "org_a failed\norg_b deleted\n"


@pytest.mark.asyncio
async def test_one_owner_skips_the_scan(sync, capsys):
    assert await script.main(["--owner-id", "org_z"]) == 0
    sync.assert_awaited_once_with("org_z", dry_run=True)
    assert capsys.readouterr().out == "org_z would_delete\n"
