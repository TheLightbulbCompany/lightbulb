"""GW-2a: resolve_running_container must offload its blocking AWS/socket calls.

``discover_ip`` (boto3 Cloud Map) and ``is_healthy`` (a blocking TCP connect)
are synchronous. ``resolve_running_container`` is on the /container/status +
chat-send hot path, so calling them inline blocks the event loop for every other
request. They now run in worker threads via ``asyncio.to_thread``.
"""

import threading
from unittest.mock import AsyncMock, patch

import pytest

from core.containers.ecs_manager import EcsManager


@pytest.mark.asyncio
async def test_discover_and_health_run_off_the_event_loop_thread():
    main_thread = threading.current_thread()
    threads: dict = {}

    mgr = EcsManager.__new__(EcsManager)

    def fake_discover(service_name):
        threads["discover"] = threading.current_thread()
        return "10.0.0.5"

    def fake_healthy(ip):
        threads["healthy"] = threading.current_thread()
        return True

    mgr.discover_ip = fake_discover
    mgr.is_healthy = fake_healthy

    with patch(
        "core.repositories.container_repo.get_by_owner_id",
        new_callable=AsyncMock,
    ) as mock_get:
        mock_get.return_value = {"status": "running", "service_name": "svc-1"}
        container, ip = await mgr.resolve_running_container("user_1")

    assert ip == "10.0.0.5"
    assert threads["discover"] is not main_thread
    assert threads["healthy"] is not main_thread
