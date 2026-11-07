from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import cancel_paperclip_approvals as mod

ROWS = {
    "approvals": [
        {"id": "a1", "companyId": "co", "status": "pending", "type": "hire_agent"},
        {"id": "a2", "companyId": "co", "status": "revision_requested", "type": "request_board_approval"},
        {"id": "a3", "companyId": "co", "status": "approved"},
        {"id": "a4", "companyId": "other", "status": "pending"},
    ]
}


@pytest.fixture
def admin(monkeypatch):
    admin = SimpleNamespace(list_approvals=AsyncMock(return_value=ROWS), reject_approval=AsyncMock())
    monkeypatch.setattr(mod, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(mod, "get_admin_session_cookie", AsyncMock(return_value="ck"))
    ws = SimpleNamespace(company_id="co")
    monkeypatch.setattr(mod.PaperclipRepo, "resolve_owner_row", AsyncMock(return_value=ws))
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev")
    return admin


async def test_dry_run_lists_only_open_approvals_of_the_company(admin, capsys):
    assert await mod.main(["--owner-id", "org_1"]) == 0
    out = capsys.readouterr().out
    assert "a1" in out and "a2" in out and "a3" not in out and "a4" not in out
    assert "2 open approvals to cancel (dry run)" in out
    admin.reject_approval.assert_not_awaited()


async def test_apply_rejects_each_open_approval_with_the_note(admin, capsys):
    assert await mod.main(["--owner-id", "org_1", "--apply"]) == 0
    assert [c.kwargs["approval_id"] for c in admin.reject_approval.await_args_list] == ["a1", "a2"]
    assert all(c.kwargs["reason"] == mod.NOTE for c in admin.reject_approval.await_args_list)


async def test_refuses_prod_without_allow_prod(admin, monkeypatch):
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod")
    assert await mod.main(["--all", "--apply"]) == 2
    admin.list_approvals.assert_not_awaited()
    admin.reject_approval.assert_not_awaited()
