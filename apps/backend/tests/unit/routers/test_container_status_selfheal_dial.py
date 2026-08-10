"""The status poll's pool self-heal dial is the scale-from-zero wake's ONLY
readiness writer.

runtime_phase "ready" requires a gateway-liveness row that is written solely
by a successful pool connect, and every pool dial is demand-driven. During a
cold-start wake the frontend renders the stepper and sends no gateway RPCs —
so without this dial nothing ever connects, the poll reports "starting"
forever, and only a page reload (fresh mount RPC burst) breaks the deadlock.
These tests pin the dispatch conditions: dial exactly when the poll observes
"starting" with an IP + token in hand (regardless of the wake param — wake
governs starting a PARKED container, not repairing a running one), and never
stacked (a global single-flight plus a backoff armed only by expensive
failures pace the background repair — failure TYPE is the classifier).
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
    container_router._self_heal_backoff_at.clear()
    yield
    container_router._self_heal_inflight.clear()
    container_router._self_heal_backoff_at.clear()


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
    # can dial again, and success must not arm the backoff (a post-success
    # drop re-dials on the next poll).
    assert OWNER not in container_router._self_heal_inflight
    assert OWNER not in container_router._self_heal_backoff_at


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_wake0_poll_still_dials_a_starting_container(
    mock_get_ecs, mock_gateway_connected, mock_pool, async_client
):
    """The dial is NOT gated on the wake param: wake governs starting a
    PARKED container, and this dial only completes the handshake for an
    already-running one. The frontend demotes to wake=0 after 120s without
    input — a user silently watching the stepper crosses that line before
    a normal gateway boot finishes, and is exactly who this dial serves."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False

    response = await async_client.get("/api/v1/container/status?wake=0")
    assert response.status_code == 200

    await _drain_background_tasks()
    mock_pool.ensure_connection.assert_awaited_once_with(OWNER, IP, TOKEN)


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
async def test_successful_dial_is_not_repeated(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """After a successful dial the pool holds the connection; a later poll's
    helper fast-paths on is_user_connected instead of re-dialing."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False
    mock_pool.is_user_connected = MagicMock(side_effect=[False, True])

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
    must clear the in-flight marker so the next poll retries."""
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


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_fast_boot_refusal_retries_on_next_poll(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """ConnectionRefusedError (gateway still booting — the kernel rejected
    the connect, no lock time consumed) must NOT arm the backoff: the next
    ~5s poll retries and is the one that heals."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False
    mock_pool.ensure_connection = AsyncMock(side_effect=ConnectionRefusedError("booting"))

    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()
    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()

    assert mock_pool.ensure_connection.await_count == 2


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_pool_reconnect_throttle_retries_on_next_poll(
    mock_get_ecs, mock_gateway_connected, mock_pool, async_client
):
    """GatewayReconnectThrottled (the pool's own 5s reconnect floor, raised
    before any I/O) is transient — it must NOT arm the 60s backoff, or a 5s
    pool throttle would suppress status-driven repair for a minute."""
    from core.gateway.connection_pool import GatewayReconnectThrottled

    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False
    mock_pool.ensure_connection = AsyncMock(side_effect=GatewayReconnectThrottled("floor"))

    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()
    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()

    assert mock_pool.ensure_connection.await_count == 2


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_connection_reset_retries_on_next_poll(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """ConnectionResetError (gateway accepted TCP then reset mid-boot) is a
    kernel-level connection-phase failure like a refusal — transient, no
    backoff, retried by the next poll."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False
    mock_pool.ensure_connection = AsyncMock(side_effect=ConnectionResetError("mid-boot reset"))

    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()
    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()

    assert mock_pool.ensure_connection.await_count == 2


@pytest.mark.asyncio
@patch("core.services.connection_service.is_gateway_connected", new_callable=AsyncMock)
@patch("routers.container.get_ecs_manager")
async def test_expensive_failure_arms_the_backoff(mock_get_ecs, mock_gateway_connected, mock_pool, async_client):
    """A handshake timeout — a real wait on the global connect lock — arms
    the per-owner backoff: no re-dial on the next poll. TimeoutError
    subclasses OSError, so this also pins the except-branch ORDER: swapping
    the tiers would silently re-dial 10s hangs at poll cadence."""
    mock_ecs = AsyncMock()
    mock_get_ecs.return_value = mock_ecs
    mock_ecs.resolve_running_container = AsyncMock(return_value=(_running_row(), IP))
    mock_gateway_connected.return_value = False
    mock_pool.ensure_connection = AsyncMock(side_effect=TimeoutError("handshake"))

    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()
    await async_client.get("/api/v1/container/status?wake=1")
    await _drain_background_tasks()

    assert mock_pool.ensure_connection.await_count == 1
