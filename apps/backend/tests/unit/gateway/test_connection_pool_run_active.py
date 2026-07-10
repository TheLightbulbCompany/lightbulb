import json
import pytest
from core.gateway.connection_pool import GatewayConnection


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_subscribe_session_events_sends_sessions_subscribe():
    conn = GatewayConnection.__new__(GatewayConnection)  # skip __init__ networking
    conn._ws = FakeWS()
    await conn._subscribe_session_events()
    methods = [m.get("method") for m in conn._ws.sent]
    assert "sessions.subscribe" in methods
    frame = next(m for m in conn._ws.sent if m.get("method") == "sessions.subscribe")
    assert frame["type"] == "req"
    assert frame["params"] == {}
    assert frame.get("id")
