from unittest.mock import AsyncMock, patch

import pytest

from core.auth import AuthContext, require_platform_admin


@pytest.fixture
def admin_client(app, async_client):
    app.dependency_overrides[require_platform_admin] = lambda: AuthContext(user_id="user_admin")
    yield async_client
    app.dependency_overrides.pop(require_platform_admin, None)


@patch("core.services.ops.launch.compute", AsyncMock(return_value=({"phase": "live"}, "ok", [])))
async def test_ops_launch_envelope(admin_client):
    r = await admin_client.get("/api/v1/ops/launch")
    assert r.status_code == 200 and r.json()["data"]["phase"] == "live" and r.json()["source_state"] == "ok"


@patch("core.services.launch_revenue.refresh_all", AsyncMock(return_value=7))
async def test_ops_launch_refresh_forces(admin_client):
    r = await admin_client.post("/api/v1/ops/launch/refresh")
    assert r.status_code == 200 and r.json() == {"refreshed": 7}


async def test_ops_launch_requires_admin(async_client):
    r = await async_client.get("/api/v1/ops/launch")
    assert r.status_code in (401, 403)


async def test_ops_launch_delete_entry_removes_row_and_key_in_order(admin_client):
    # Key first: a dangling Secrets Manager entry with no owning row is worse
    # than a row that still points at an already-revoked key.
    calls: list[str] = []

    async def _delete_key(owner_id: str) -> None:
        calls.append(f"key:{owner_id}")

    async def _delete_row(owner_id: str) -> None:
        calls.append(f"row:{owner_id}")

    with (
        patch("core.services.launch_revenue.delete_key", AsyncMock(side_effect=_delete_key)),
        patch("core.repositories.launch_entries_repo.delete", AsyncMock(side_effect=_delete_row)),
    ):
        r = await admin_client.delete("/api/v1/ops/launch/entries/user_1")
    assert r.status_code == 200 and r.json() == {"deleted": "user_1"}
    assert calls == ["key:user_1", "row:user_1"]


async def test_ops_launch_delete_entry_requires_admin(async_client):
    r = await async_client.delete("/api/v1/ops/launch/entries/user_1")
    assert r.status_code in (401, 403)
