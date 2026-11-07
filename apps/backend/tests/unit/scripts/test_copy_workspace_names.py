from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import copy_workspace_names as mod

ROWS = {
    "org_old": SimpleNamespace(company_id="co_1", name=None),
    "org_new": SimpleNamespace(company_id="", name=None),
    "org_named": SimpleNamespace(company_id="co_3", name="Already"),
}


@pytest.fixture
def env(monkeypatch):
    admin = SimpleNamespace(get_company=AsyncMock(return_value={"name": "Acme", "description": "anvils"}))
    monkeypatch.setattr(mod, "PaperclipAdminClient", lambda http_client: admin)
    monkeypatch.setattr(mod, "get_admin_session_cookie", AsyncMock(return_value="ck"))
    monkeypatch.setattr(mod.PaperclipRepo, "list_active_workspace_members", AsyncMock(return_value=dict.fromkeys(ROWS)))
    monkeypatch.setattr(mod.PaperclipRepo, "resolve_owner_row", AsyncMock(side_effect=lambda o, **_: ROWS[o]))
    set_profile = AsyncMock()
    monkeypatch.setattr(mod.PaperclipRepo, "set_profile", set_profile)
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "dev")
    return admin, set_profile


async def test_copy_names_dry_run_writes_nothing(env, capsys):
    admin, set_profile = env
    assert await mod.main(["--all"]) == 0
    out = capsys.readouterr().out
    assert "org_old: would copy 'Acme'" in out and "org_new" not in out and "org_named" not in out
    set_profile.assert_not_awaited()
    assert [c.kwargs["company_id"] for c in admin.get_company.await_args_list] == ["co_1"]


async def test_copy_names_apply_writes_name_and_description(env):
    _, set_profile = env
    assert await mod.main(["--all", "--apply"]) == 0
    set_profile.assert_awaited_once()
    assert set_profile.await_args.args == ("org_old",)
    assert set_profile.await_args.kwargs == {"name": "Acme", "description": "anvils"}


async def test_refuses_prod_without_allow_prod(env, monkeypatch):
    monkeypatch.setattr(mod.settings, "ENVIRONMENT", "prod")
    assert await mod.main(["--all", "--apply"]) == 2
    env[1].assert_not_awaited()
