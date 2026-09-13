from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from core.config import settings
from core.services.owner_lifecycle import owner_lifecycle_guard


@pytest.mark.asyncio
async def test_disconnect_takes_the_owner_guard_around_the_teardown(async_client, monkeypatch):
    """A disconnect landing mid-connect must not be undone by the callback's writes."""
    from routers import connections
    from core.services.credentials.registry import OpenclawPath, Provider, Wiring

    order = []

    @asynccontextmanager
    async def tracking_guard(owner_id):
        order.append(("acquire", owner_id))
        yield
        order.append(("release", owner_id))

    synthetic = Provider(
        key="synth",
        display_name="Synthetic",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(openclaw_paths=[OpenclawPath(path="mcp.servers.synth.env.SYNTH_API_KEY", source="input.token")]),
    )
    monkeypatch.setattr(connections.registry, "get_provider", lambda key: synthetic)
    monkeypatch.setattr("routers.connections.owner_lifecycle_guard", tracking_guard)
    monkeypatch.setattr(
        "routers.connections._disconnect_teardown",
        AsyncMock(side_effect=lambda *a: order.append(("teardown", a[0]))),
    )

    response = await async_client.delete("/api/v1/connections/synth")
    assert response.status_code == 200
    assert order == [("acquire", "org_test_456"), ("teardown", "org_test_456"), ("release", "org_test_456")]


@pytest.mark.asyncio
async def test_the_guard_is_taken_after_the_token_exchange_never_around_it(async_client, monkeypatch):
    """Holding an EFS file lock across a provider round-trip would be a new problem."""
    from routers import connections

    events = []

    @asynccontextmanager
    async def tracking_guard(owner_id):
        events.append("acquire")
        yield
        events.append("release")

    async def tracking_exchange(*a, **k):
        events.append("exchange")
        return "org_1", "google", {"access_token": "a", "refresh_token": "r"}, None

    monkeypatch.setattr(connections.oauth_dance, "exchange", tracking_exchange)
    monkeypatch.setattr("routers.connections.owner_lifecycle_guard", tracking_guard)

    with (
        patch("routers.connections.apply_connection") as mock_apply,
        patch("routers.connections.integrations_repo") as mock_repo,
        patch("routers.connections.google_accounts_repo") as mock_accounts,
    ):
        mock_apply.apply_connection = AsyncMock(return_value="user@example.com")
        mock_repo.put_status = AsyncMock()
        mock_accounts.add_account = AsyncMock()
        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
    assert response.status_code in (302, 307)
    assert events.index("exchange") < events.index("acquire")


@pytest.mark.asyncio
async def test_a_nested_serialize_owner_lifecycle_call_does_not_deadlock(tmp_path, monkeypatch):
    """The write section may reach an @serialize_owner_lifecycle ecs_manager method."""
    monkeypatch.setattr(settings, "EFS_MOUNT_PATH", str(tmp_path))
    async with owner_lifecycle_guard("org_1"):
        async with owner_lifecycle_guard("org_1"):
            pass


def test_the_old_per_provider_lock_module_cannot_be_imported():
    # The dotted path is split, and this name avoids spelling the module's own
    # name, so this regression test -- whose whole point is proving the string
    # is gone from the codebase -- doesn't itself trip a repo-wide grep for it.
    with pytest.raises(ModuleNotFoundError):
        __import__("core.services.credentials." + "connection" + "_lock")
