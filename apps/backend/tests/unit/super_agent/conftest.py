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
