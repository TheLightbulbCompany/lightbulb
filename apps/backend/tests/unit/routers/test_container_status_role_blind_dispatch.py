"""The /container/status running-container branch must dispatch
ensure_user_ready for EVERY org-context user (admins included) so a co-admin
joining an already-running org gets their Paperclip workspace. Safe on a
running box without a flag: ensure_user_ready reads the container row and
resolves it to a seed reconcile instead of an executor dispatch (see
user_provision._provision_or_reconcile)."""

from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext
from routers import container as container_mod


def _org_admin_auth() -> AuthContext:
    return AuthContext(user_id="user_admin2", org_id="org_xyz", org_role="org:admin", email="a@example.com")


def _org_member_auth() -> AuthContext:
    return AuthContext(user_id="user_member1", org_id="org_xyz", org_role="org:member", email="m@example.com")


def _running():
    return {
        "owner_id": "org_xyz",
        "agentcore_runtime_arn": "arn:aws:bedrock-agentcore:us-east-1:1:runtime/r1",
        "status": "running",
        "substatus": None,
        "created_at": None,
        "updated_at": None,
        "last_error": None,
        "last_error_at": None,
    }


@pytest.mark.asyncio
async def test_running_container_dispatches_ensure_user_ready_for_org_admin():
    calls = {}

    def capture_task(coro):
        # ensure_user_ready(...) returns a coroutine; capture its name then close.
        calls["qualname"] = coro.__qualname__
        coro.close()

    with (
        patch("routers.container.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=_running()),
        patch("routers.container.resolve_owner_id", return_value="org_xyz"),
        patch("core.services.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}),
        patch("routers.container.asyncio.create_task", side_effect=capture_task),
    ):
        await container_mod.container_status(auth=_org_admin_auth())

    assert calls.get("qualname") == "ensure_user_ready", (
        f"org admin on a running container must dispatch ensure_user_ready, got {calls}"
    )


@pytest.mark.asyncio
async def test_running_container_dispatches_ensure_user_ready_for_org_member():
    """The 200 (shared-container-resolved) path must dispatch the idempotent
    ensure_user_ready membership-add for a NON-ADMIN org member too — the
    orphan-member incident family is a member who resolves the org's running
    container fine but never gets the Paperclip member-add."""
    dispatched: list[str] = []

    def capture_task(coro):
        dispatched.append(coro.__qualname__)
        coro.close()

    with (
        patch("routers.container.container_repo.get_by_owner_id", new_callable=AsyncMock, return_value=_running()),
        patch("routers.container.resolve_owner_id", return_value="org_xyz"),
        patch("core.services.container_rpc.call", new_callable=AsyncMock, return_value={"ok": True}),
        patch("routers.container.asyncio.create_task", side_effect=capture_task),
    ):
        await container_mod.container_status(auth=_org_member_auth())

    assert "ensure_user_ready" in dispatched, (
        f"org MEMBER on a running container must dispatch ensure_user_ready, got {dispatched}"
    )
