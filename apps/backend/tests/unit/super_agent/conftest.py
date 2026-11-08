"""Shared by every super-agent suite: the tasks table fixtures here create only the tasks table under moto,
and `tasks_repo.create_task` stamps `wsChannel` from the messaging table (`ensure_workspace_id`). A deterministic
stand-in keeps those suites table-local; `test_tasks_repo.py::test_create_task_stamps_ws_channel` pins the call."""

import uuid

import pytest


@pytest.fixture(autouse=True)
def _ws_channel_stub(monkeypatch):
    from core.repositories import messaging_repo

    async def ensure_workspace_id(owner_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"test-ws-channel|{owner_id}"))

    monkeypatch.setattr(messaging_repo, "ensure_workspace_id", ensure_workspace_id)


@pytest.fixture(autouse=True)
def _workspace_session_by_default(request, monkeypatch):
    """Every attempt asks `token_custody.task_vm_allowed` now that the switch is gone. Unless a test says otherwise,
    the workspace's credentials are VM-rotated, so its tasks run in the workspace session (what these suites
    assumed). tests/unit/super_agent/runtime/ tests the gate itself and keeps the real one."""
    if "/super_agent/runtime/" in str(request.node.fspath):
        return
    from unittest.mock import AsyncMock as _AsyncMock

    from core.services import token_custody

    monkeypatch.setattr(token_custody, "task_vm_allowed", _AsyncMock(return_value=False))


@pytest.fixture(autouse=True)
def _no_connected_apps(monkeypatch):
    """A Worker brief lists the workspace's connected apps (`integrations_repo`, a table these suites do not create)."""
    from unittest.mock import AsyncMock as _AsyncMock

    from core.repositories import integrations_repo

    monkeypatch.setattr(integrations_repo, "list_for_owner", _AsyncMock(return_value=[]))
