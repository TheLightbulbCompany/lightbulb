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


@pytest.mark.asyncio
async def test_growth_moves_off_the_success_number_text(monkeypatch, capsys):
    """Dev walk 15: Growth's old persona ("propose one experiment with a success number") put invented targets in
    its summaries. Its starter is patched like the CEO; an edited one stays."""
    admin, refresh = _setup(monkeypatch, {"c1": NEW, "c2": NEW})
    texts = {"g1": mod.OLD_GROWTH_CAPABILITIES, "g2": "our own Growth", "c1": NEW, "c2": NEW}
    admin.get_agent.side_effect = lambda session_cookie, agent_id: {"capabilities": texts[agent_id]}
    repo = (await mod.build_paperclip_provisioning())._repo
    rows = {
        "org_c1": SimpleNamespace(status="active", org_id="org_c1", starter_agent_ids={"ceo": "c1", "growth": "g1"}),
        "org_c2": SimpleNamespace(status="active", org_id="org_c2", starter_agent_ids={"ceo": "c2", "growth": "g2"}),
    }
    repo.resolve_owner_row.side_effect = lambda owner_id: rows[owner_id]

    assert await mod.main(["--all"]) == 0  # dry run first
    admin.update_agent.assert_not_awaited()
    assert "org_c1: skipped (already current) ceo=c1; would update growth=g1" in capsys.readouterr().out

    assert await mod.main(["--all", "--apply"]) == 0
    admin.update_agent.assert_awaited_once_with(
        session_cookie="cookie", agent_id="g1", capabilities=mod.STARTER_CAPABILITIES["growth"]
    )
    assert ("org_c1", "g1") in [c.args for c in refresh.await_args_list]
    out = capsys.readouterr().out
    assert "updated growth=g1" in out and "skipped (capabilities edited) growth=g2" in out
    assert "total: " in out and "updated=1" in out
