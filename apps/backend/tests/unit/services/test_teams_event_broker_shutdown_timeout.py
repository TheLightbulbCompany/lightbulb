import asyncio
import pytest

from core.services import teams_event_broker as teb


class _HangingClient:
    def __init__(self):
        self.close_started = False

    async def close(self):
        self.close_started = True
        await asyncio.sleep(3600)  # never returns


class _FastClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_hanging_client_does_not_block_shutdown(monkeypatch):
    monkeypatch.setattr(teb, "_CLIENT_CLOSE_TIMEOUT", 0.1)
    broker = teb.TeamsEventBroker.__new__(teb.TeamsEventBroker)
    broker._grace_tasks = {}
    hanging, fast = _HangingClient(), _FastClient()
    broker._clients = {"a": hanging, "b": fast}
    broker._subscribers = {}
    await asyncio.wait_for(broker.shutdown(), timeout=2.0)  # must not hang
    assert hanging.close_started is True
    assert fast.closed is True


@pytest.mark.asyncio
async def test_many_hanging_clients_close_concurrently(monkeypatch):
    """N wedged clients must close CONCURRENTLY: total wall-clock stays ~one
    timeout, not N * timeout (which would starve the rest of lifespan shutdown)."""
    timeout = 0.2
    n = 8
    monkeypatch.setattr(teb, "_CLIENT_CLOSE_TIMEOUT", timeout)
    broker = teb.TeamsEventBroker.__new__(teb.TeamsEventBroker)
    broker._grace_tasks = {}
    clients = [_HangingClient() for _ in range(n)]
    broker._clients = {str(i): c for i, c in enumerate(clients)}
    broker._subscribers = {}

    loop = asyncio.get_event_loop()
    start = loop.time()
    await broker.shutdown()
    elapsed = loop.time() - start

    # Concurrent: ~one timeout. Serial would be n * timeout (1.6s here).
    assert elapsed < timeout * 3, f"shutdown took {elapsed:.2f}s — not concurrent"
    assert all(c.close_started for c in clients)
