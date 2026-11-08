from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import workspace_name as mod


@pytest.fixture
def sources(monkeypatch):
    ws = SimpleNamespace(name=None)
    monkeypatch.setattr(mod.WorkspaceRepo, "get_workspace", AsyncMock(return_value=ws))
    clerk = AsyncMock(return_value=None)
    monkeypatch.setattr(mod.clerk_admin, "get_organization", clerk)
    return ws, clerk


async def test_name_order_meta_then_clerk_then_owner(sources):
    ws, clerk = sources
    assert await mod.workspace_name("org_1") == "org_1"
    clerk.return_value = {"name": "  Clerk Co "}
    assert await mod.workspace_name("org_1") == "Clerk Co"
    ws.name = "Meta Co"
    assert await mod.workspace_name("org_1") == "Meta Co"


async def test_personal_owner_skips_clerk(sources):
    _, clerk = sources
    assert await mod.workspace_name("user_1") == "user_1"
    clerk.assert_not_awaited()
