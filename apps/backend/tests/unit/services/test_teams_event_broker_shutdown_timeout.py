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
