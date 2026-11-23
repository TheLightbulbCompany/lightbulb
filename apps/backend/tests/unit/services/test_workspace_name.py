from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from core.services import workspace_name as mod


@pytest.fixture
def ws(monkeypatch):
    row = SimpleNamespace(name=None)
    monkeypatch.setattr(mod.WorkspaceRepo, "get_workspace", AsyncMock(return_value=row))
    return row


async def test_name_is_the_saved_name_else_the_owner_id(ws):
    assert await mod.saved_name("user_1") is None
    assert await mod.workspace_name("user_1") == "user_1"
    ws.name = "  Meta Co "
    assert await mod.workspace_name("user_1") == "Meta Co"
