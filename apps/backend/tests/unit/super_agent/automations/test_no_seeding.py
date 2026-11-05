"""Super-agent workspaces get no starter or feed routines (wave-2 lane A, Task 11; contracts-v2 §11)."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from core.services import paperclip_provisioning, user_provision


@pytest.fixture
def switch(monkeypatch):
    def set_(on: bool):
        flags = AsyncMock(return_value=on)
        monkeypatch.setattr(paperclip_provisioning, "flags_for", flags)
        monkeypatch.setattr(user_provision, "flags_for", flags)
        return flags

    return set_


def _provisioning():
    p = paperclip_provisioning.PaperclipProvisioning.__new__(paperclip_provisioning.PaperclipProvisioning)
    p._admin = MagicMock()
    return p


async def _seed(monkeypatch) -> AsyncMock:
    create = AsyncMock()
    monkeypatch.setattr(paperclip_provisioning, "create_agent_routines", create)
    starter = dict.fromkeys(paperclip_provisioning.STARTER_ROUTINES, "agent_x")
    await _provisioning()._seed_starter_routines(
        company_id="co_1", session_cookie="c", starter_agent_ids=starter, owner_id="org_1"
    )
    return create


async def test_super_agent_workspace_gets_no_starter_routines(monkeypatch, switch):
    flags = switch(True)
    assert not (await _seed(monkeypatch)).called
    flags.assert_awaited_once_with("org_1")


async def test_super_agent_workspace_gets_no_feed_routines(switch):
    switch(True)
    admin = MagicMock(list_routines=AsyncMock(return_value={"routines": []}))
    await user_provision._ensure_feed_routines(admin, "c", "org_1", SimpleNamespace(company_id="co_1"), "ceo1")
    assert not admin.list_routines.called


async def test_switch_off_seeding_unchanged(monkeypatch, switch):
    switch(False)
    create = await _seed(monkeypatch)
    assert create.await_count == len(paperclip_provisioning.STARTER_ROUTINES)
    admin = MagicMock(list_routines=AsyncMock(return_value={"routines": []}))
    await user_provision._ensure_feed_routines(admin, "c", "org_1", SimpleNamespace(company_id="co_1"), "ceo1")
    assert admin.list_routines.called
