"""The status poll's pool self-heal dial is the scale-from-zero wake's ONLY
readiness writer.

runtime_phase "ready" requires a gateway-liveness row that is written solely
by a successful pool connect, and every pool dial is demand-driven. During a
cold-start wake the frontend renders the stepper and sends no gateway RPCs —
so without this dial nothing ever connects, the poll reports "starting"
forever, and only a page reload (fresh mount RPC burst) breaks the deadlock.
These tests pin the dispatch conditions: dial exactly when the poll observes
"starting" with an IP + token in hand, never on read-only polls, and never
stacked (in-flight marker + cooldown pace the background repair).
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from routers import container as container_router

OWNER = "user_test_123"
IP = "10.0.1.5"
TOKEN = "gw-token"


def _running_row(token: str | None = TOKEN) -> dict:
    row = {
        "service_name": "openclaw-abc",
        "status": "running",
        "created_at": "2026-01-15T12:00:00+00:00",
        "updated_at": "2026-01-15T12:00:00+00:00",
    }
    if token is not None:
        row["gateway_token"] = token
    return row


@pytest.fixture(autouse=True)
def _reset_dial_guards():
    container_router._self_heal_inflight.clear()
    container_router._self_heal_last_dial.clear()
    yield
    container_router._self_heal_inflight.clear()
    container_router._self_heal_last_dial.clear()


@pytest.fixture
def mock_pool():
    pool = MagicMock()
    pool.is_user_connected = MagicMock(return_value=False)
    pool.ensure_connection = AsyncMock()
    with patch("routers.container.get_gateway_pool", return_value=pool):
        yield pool


async def _drain_background_tasks():
    # The dial is fired with asyncio.create_task from the request handler;
    # yield the loop a few times so it runs to completion before asserting.
    for _ in range(5):
        await asyncio.sleep(0)


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_starting_phase_dials_the_pool(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """running row + IP + token + no liveness -> background ensure_connection."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False

    response = await async_client.get("/api/v1/container/status?wake=1")
    assert response.status_code == 200
    assert response.json()["runtime_phase"] == "starting"

    await _drain_background_tasks()
    mock_pool.ensure_connection.assert_awaited_once_with(OWNER, IP, TOKEN)
    # A successful dial may have created a connection with no frontend WS
    # registered on this task — the dial must arm the idle teardown itself
    # (nothing else ever will), or a trial container stays up forever.
    mock_pool.arm_idle_teardown_if_unattended.assert_called_once_with(OWNER)
    # The helper's finally must clear the in-flight marker so a later poll
    # (past the cooldown) can dial again.
    assert OWNER not in container_router._self_heal_inflight


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_wake0_read_only_poll_never_dials(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False

    response = await async_client.get("/api/v1/container/status?wake=0")
    assert response.status_code == 200

    await _drain_background_tasks()
    mock_pool.ensure_connection.assert_not_awaited()


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_ready_phase_never_dials(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """A live gateway-liveness row means there is nothing to heal."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = True

    response = await async_client.get("/api/v1/container/status?wake=1")
    assert response.status_code == 200
    assert response.json()["runtime_phase"] == "ready"

    await _drain_background_tasks()
    mock_pool.ensure_connection.assert_not_awaited()


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_missing_token_never_dials(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(token=None), IP))
    mock_gateway_connected.return_value = False

    response = await async_client.get("/api/v1/container/status?wake=1")
    assert response.status_code == 200

    await _drain_background_tasks()
    mock_pool.ensure_connection.assert_not_awaited()


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_cooldown_paces_back_to_back_polls(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """The ~5s poll cadence must not stack dials: one dial per cooldown."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False

    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()
    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()

    assert mock_pool.ensure_connection.await_count == 1


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_dial_failure_is_swallowed_and_clears_inflight(
    mock_get_ecs, mock_gateway_connected, mock_pool, async_client
):
    """A refused dial (gateway still booting) must not surface anywhere and
    must clear the in-flight marker so the next post-cooldown poll retries."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False
    mock_pool.ensure_connection = AsyncMock(side_effect=ConnectionRefusedError("boom"))

    response = await async_client.get("/api/v1/container/status?wake=1")
    assert response.status_code == 200

    await _drain_background_tasks()
    mock_pool.ensure_connection.assert_awaited_once()
    # No connection was created, so nothing to arm teardown for.
    mock_pool.arm_idle_teardown_if_unattended.assert_not_called()
    assert OWNER not in container_router._self_heal_inflight


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_global_single_flight_blocks_other_owners(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """The in-flight set is a process-wide single-flight: while ANY owner's
    dial is outstanding, no other owner may queue one — the pool serializes
    all connect I/O behind one lock, so a wake burst must not stack serial
    handshake holds in front of chat RPCs."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False
    container_router._self_heal_inflight.add("user_someone_else")

    response = await async_client.get("/api/v1/container/status?wake=1")
    assert response.status_code == 200

    await _drain_background_tasks()
    mock_pool.ensure_connection.assert_not_awaited()


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_already_connected_pool_skips_redial(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """A racing dial (chat send) may win between dispatch and run — fast-path out."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False
    mock_pool.is_user_connected = MagicMock(return_value=True)

    response = await async_client.get("/api/v1/container/status?wake=1")
    assert response.status_code == 200

    await _drain_background_tasks()
    mock_pool.ensure_connection.assert_not_awaited()
