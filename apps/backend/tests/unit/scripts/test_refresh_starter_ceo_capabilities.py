"""refresh_starter_ceo_capabilities: patch a starter CEO only while its capabilities are exactly the
old seeded text, refresh its SOUL, and leave owner-edited text alone."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from scripts import refresh_starter_ceo_capabilities as mod

NEW = "Answers the owner and does quick work itself."


def _setup(monkeypatch, capabilities_by_ceo: dict[str, str]):
    monkeypatch.setitem(mod.STARTER_CAPABILITIES, "ceo", NEW)
    rows = {
        f"org_{c}": SimpleNamespace(status="active", org_id=f"org_{c}", starter_agent_ids={"ceo": c})
        for c in capabilities_by_ceo
    }
    admin = SimpleNamespace(
        _http=object(),
        get_agent=AsyncMock(
            side_effect=lambda session_cookie, agent_id: {"capabilities": capabilities_by_ceo[agent_id]}
        ),
        update_agent=AsyncMock(),
    )
    repo = SimpleNamespace(resolve_owner_row=AsyncMock(side_effect=lambda owner_id: rows[owner_id]))
    monkeypatch.setattr(
        mod, "build_paperclip_provisioning", AsyncMock(return_value=SimpleNamespace(_admin=admin, _repo=repo))
    )
    monkeypatch.setattr(mod, "close_paperclip_provisioning", AsyncMock())
    monkeypatch.setattr(mod, "get_admin_session_cookie", AsyncMock(return_value="cookie"))
    monkeypatch.setattr(mod.container_repo, "scan_all", AsyncMock(return_value=[{"owner_id": o} for o in rows]))
    refresh = AsyncMock()
    monkeypatch.setattr(mod, "refresh_agent_soul", refresh)
    return admin, refresh


@pytest.mark.asyncio
async def test_apply_patches_old_text_and_skips_edited(monkeypatch, capsys):
    admin, refresh = _setup(monkeypatch, {"old": mod.OLD_CEO_CAPABILITIES, "edited": "my own words", "current": NEW})

    assert await mod.main(["--all", "--apply"]) == 0

    admin.update_agent.assert_awaited_once_with(session_cookie="cookie", agent_id="old", capabilities=NEW)
    # Old text: patched + SOUL; already NEW: SOUL only (repairs an earlier failed refresh).
    assert sorted(c.args for c in refresh.await_args_list) == [("org_current", "current"), ("org_old", "old")]
    out = capsys.readouterr().out
    assert "org_old: updated" in out and "org_edited: skipped (capabilities edited)" in out


@pytest.mark.asyncio
async def test_dry_run_patches_nothing(monkeypatch, capsys):
    admin, refresh = _setup(monkeypatch, {"old": mod.OLD_CEO_CAPABILITIES})

    assert await mod.main(["--owner-id", "org_old"]) == 0

    admin.update_agent.assert_not_awaited()
    refresh.assert_not_awaited()
    assert "org_old: would update" in capsys.readouterr().out
