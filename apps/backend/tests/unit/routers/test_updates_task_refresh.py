from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from core.auth import AuthContext
from routers import updates


@pytest.mark.asyncio
async def test_admin_fleet_task_refresh_schedules_durable_rollout():
    request = updates.AdminUpdateRequest(
        owner_id="all",
        update_type=updates.TASK_DEFINITION_REFRESH_KIND,
        description="Config-lock protocol rollout",
        changes={},
    )

    with patch.object(
        updates,
        "queue_fleet_task_definition_refresh",
        new=AsyncMock(return_value=(12, 3)),
    ) as queue:
        result = await updates.admin_create_update(request, MagicMock())

    assert result == {"status": "scheduled", "queued": 12, "skipped": 3}
    queue.assert_awaited_once_with("Config-lock protocol rollout")


@pytest.mark.asyncio
async def test_fleet_task_refresh_is_mounted_at_container_updates_path():
    from main import app

    app.dependency_overrides[updates.require_platform_admin] = lambda: AuthContext(
        user_id="user_admin",
        email="admin@isol8.co",
    )
    body = {
        "owner_id": "all",
        "update_type": updates.TASK_DEFINITION_REFRESH_KIND,
        "description": "Config-lock protocol rollout",
        "changes": {},
    }
    try:
        with patch.object(
            updates,
            "queue_fleet_task_definition_refresh",
            new=AsyncMock(return_value=(12, 3)),
        ):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
                response = await client.post("/api/v1/container/updates", json=body)
                wrong_path = await client.post("/api/v1/updates", json=body)
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {"status": "scheduled", "queued": 12, "skipped": 3}
    assert wrong_path.status_code == 404


@pytest.mark.asyncio
async def test_single_owner_task_refresh_queues_eligible_row():
    request = updates.AdminUpdateRequest(
        owner_id="user_one",
        update_type=updates.TASK_DEFINITION_REFRESH_KIND,
        description="Targeted refresh",
        changes={},
    )
    row = {"owner_id": "user_one", "status": "running"}
    created = {"owner_id": "user_one", "update_id": "u1"}

    with (
        patch.object(updates.container_repo, "get_by_owner_id", new=AsyncMock(return_value=row)),
        patch.object(
            updates,
            "queue_task_definition_refresh_for_owner",
            new=AsyncMock(return_value=created),
        ) as queue,
    ):
        result = await updates.admin_create_update(request, MagicMock())

    assert result == {"status": "created", "update": created}
    queue.assert_awaited_once_with(row, description="Targeted refresh")


@pytest.mark.asyncio
async def test_single_owner_task_refresh_reports_skip_instead_of_silent_noop():
    request = updates.AdminUpdateRequest(
        owner_id="user_one",
        update_type=updates.TASK_DEFINITION_REFRESH_KIND,
        description="",
        changes={},
    )
    row = {"owner_id": "user_one", "status": "running"}

    with (
        patch.object(updates.container_repo, "get_by_owner_id", new=AsyncMock(return_value=row)),
        patch.object(
            updates,
            "queue_task_definition_refresh_for_owner",
            new=AsyncMock(return_value=None),
        ),
    ):
        result = await updates.admin_create_update(request, MagicMock())

    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_single_owner_task_refresh_404s_without_container():
    from fastapi import HTTPException

    request = updates.AdminUpdateRequest(
        owner_id="user_missing",
        update_type=updates.TASK_DEFINITION_REFRESH_KIND,
        description="",
        changes={},
    )

    with patch.object(updates.container_repo, "get_by_owner_id", new=AsyncMock(return_value=None)):
        with pytest.raises(HTTPException) as exc:
            await updates.admin_create_update(request, MagicMock())

    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_admin_release_queues_only_a_registered_single_owner_target():
    request = updates.AdminUpdateRequest(
        owner_id="owner_founder",
        update_type="release",
        description="Founder 9.3 qualification",
        changes={"target_release_id": "qualified-release"},
    )
    created = {"owner_id": "owner_founder", "update_id": "release-update"}

    with (
        patch("core.services.openclaw_release.release_by_id", return_value=MagicMock(release_id="qualified-release")),
        patch.object(updates.update_repo, "create", new=AsyncMock(return_value=created)) as create,
    ):
        result = await updates.admin_create_update(request, MagicMock())

    assert result == {"status": "created", "update": created}
    create.assert_awaited_once_with(
        owner_id="owner_founder",
        update_type="release",
        description="Founder 9.3 qualification",
        changes={"target_release_id": "qualified-release"},
        force_by=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "owner_id,changes",
    [
        ("all", {"target_release_id": "qualified-release"}),
        ("owner_founder", {"target_release_id": "qualified-release", "image": "mutable:tag"}),
    ],
)
async def test_admin_release_rejects_fleet_or_request_artifact_claims(owner_id, changes):
    from fastapi import HTTPException

    request = updates.AdminUpdateRequest(
        owner_id=owner_id,
        update_type="release",
        description="refused",
        changes=changes,
    )

    with pytest.raises(HTTPException) as exc:
        await updates.admin_create_update(request, MagicMock())

    assert exc.value.status_code == 400
