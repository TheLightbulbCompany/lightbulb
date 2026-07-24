"""The /container/status running-container branch must dispatch
ensure_user_ready for EVERY org-context user (admins included) so a co-admin
joining an already-running org gets their Paperclip workspace — and it must
pass provision_container=False so the running box is never force-restarted."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.auth import AuthContext
from routers import container as container_mod


def _org_admin_auth() -> AuthContext:
    return AuthContext(user_id="user_admin2", org_id="org_xyz", org_role="org:admin", email="a@example.com")


def _org_member_auth() -> AuthContext:
    return AuthContext(user_id="user_member1", org_id="org_xyz", org_role="org:member", email="m@example.com")


def _running():
    return {
        "service_name": "svc",
        "status": "running",
        "substatus": None,
        "created_at": None,
        "updated_at": None,
        "last_error": None,
        "last_error_at": None,
    }


@pytest.mark.asyncio
async def test_running_container_dispatches_ensure_user_ready_for_org_admin():
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(_running(), "10.0.0.1"))
    calls = {}

    def capture_task(coro):
        # ensure_user_ready(...) returns a coroutine; capture its name then close.
        calls["qualname"] = coro.__qualname__
        coro.close()

    with (
        patch("routers.container.get_ecs_manager", return_value=ecs),
        patch("routers.container.resolve_owner_id", return_value="org_xyz"),
        patch("routers.container._safe_phase", return_value="ready"),
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
    ecs = MagicMock()
    ecs.resolve_running_container = AsyncMock(return_value=(_running(), "10.0.0.1"))
    dispatched: list[str] = []

    def capture_task(coro):
        dispatched.append(coro.__qualname__)
        coro.close()

    with (
        patch("routers.container.get_ecs_manager", return_value=ecs),
        patch("routers.container.resolve_owner_id", return_value="org_xyz"),
        patch("routers.container._safe_phase", return_value="ready"),
        patch("routers.container.asyncio.create_task", side_effect=capture_task),
    ):
        await container_mod.container_status(auth=_org_member_auth())

    assert "ensure_user_ready" in dispatched, (
        f"org MEMBER on a running container must dispatch ensure_user_ready, got {dispatched}"
    )
