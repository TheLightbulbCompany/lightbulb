from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from core.services.owner_lifecycle import OwnerLifecycleBusyError, owner_lifecycle_guard


@pytest.fixture(autouse=True)
def _oauth_state(monkeypatch):
    monkeypatch.setattr(
        "routers.connections.oauth_state_repo.consume",
        AsyncMock(return_value={"owner_id": "org_1", "provider": "google", "verifier": "pkce"}),
    )


@asynccontextmanager
async def _timing_out_guard(owner_id):
    raise OwnerLifecycleBusyError("timed out")
    yield  # pragma: no cover - unreachable, keeps this a generator


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
    """Holding the lease across a provider round-trip would be a new problem."""
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
        mock_accounts.list_assignments = AsyncMock(return_value={})
        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )
    assert response.status_code in (302, 307)
    assert events.index("exchange") < events.index("acquire")


@pytest.mark.asyncio
async def test_a_nested_serialize_owner_lifecycle_call_does_not_deadlock(monkeypatch):
    """The write section may reach another @serialize_owner_lifecycle method for the same
    owner. Re-entry is answered from the ContextVar, so the inner call takes NO second lease --
    which is also what stops it deadlocking against the one the outer call holds."""
    from core.repositories import container_repo

    claims = []
    monkeypatch.setattr(
        container_repo, "try_claim_owner_lifecycle", AsyncMock(side_effect=lambda o: claims.append(o) or "tok")
    )
    monkeypatch.setattr(container_repo, "release_owner_lifecycle_claim", AsyncMock())

    async with owner_lifecycle_guard("org_1"):
        async with owner_lifecycle_guard("org_1"):
            pass

    assert claims == ["org_1"]


def test_the_old_per_provider_lock_module_cannot_be_imported():
    # The dotted path is split, and this name avoids spelling the module's own
    # name, so this regression test -- whose whole point is proving the string
    # is gone from the codebase -- doesn't itself trip a repo-wide grep for it.
    with pytest.raises(ModuleNotFoundError):
        __import__("core.services.credentials." + "connection" + "_lock")


@pytest.mark.asyncio
async def test_callback_redirects_busy_on_guard_timeout(async_client, monkeypatch):
    """A guard timeout must land a clean redirect, not a raw 500 -- the OAuth
    state is already consumed by this point, and nothing is written before
    the guard, so this is a clean no-op the user can retry."""
    from routers import connections

    monkeypatch.setattr(connections.oauth_dance, "exchange", AsyncMock(return_value=("org_1", "google", {}, None)))
    monkeypatch.setattr("routers.connections.owner_lifecycle_guard", _timing_out_guard)

    with (
        patch("routers.connections.apply_connection") as mock_apply,
        patch("routers.connections.integration_secret_store") as mock_store,
        patch("routers.connections.integrations_repo") as mock_repo,
    ):
        mock_apply.apply_connection = AsyncMock(return_value="user@example.com")
        mock_store.put = AsyncMock()
        mock_repo.put_status = AsyncMock()
        response = await async_client.get(
            "/api/v1/oauth/google/callback?code=abc&state=st1",
            follow_redirects=False,
        )

    assert response.status_code in (302, 307)
    assert "error=busy" in response.headers["location"]
    # No partial write: the timeout fires on guard entry, before the body runs.
    mock_store.put.assert_not_called()
    mock_apply.apply_connection.assert_not_called()
    mock_repo.put_status.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_returns_busy_on_guard_timeout(async_client, monkeypatch):
    """Same guard, same failure mode, on the DELETE route -- existing error
    form here is an HTTPException, not a redirect."""
    from routers import connections
    from core.services.credentials.registry import OpenclawPath, Provider, Wiring

    synthetic = Provider(
        key="synth",
        display_name="Synthetic",
        acquisition="paste_token",
        actor="mcp",
        refresh_owner="none",
        wiring=Wiring(openclaw_paths=[OpenclawPath(path="mcp.servers.synth.env.SYNTH_API_KEY", source="input.token")]),
    )
    monkeypatch.setattr(connections.registry, "get_provider", lambda key: synthetic)
    monkeypatch.setattr("routers.connections.owner_lifecycle_guard", _timing_out_guard)
    teardown = AsyncMock()
    monkeypatch.setattr("routers.connections._disconnect_teardown", teardown)

    response = await async_client.delete("/api/v1/connections/synth")

    assert response.status_code == 409
    teardown.assert_not_called()
