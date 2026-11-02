from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest


@pytest.fixture(autouse=True)
def _no_live_openrouter_allowance(monkeypatch):
    """GET /billing/account reads a capped owner's allowance from OpenRouter; keep router tests offline."""
    monkeypatch.setattr(
        "routers.billing.openrouter_keys", SimpleNamespace(owner_allowance=AsyncMock(return_value=None))
    )


# A2A router and conformance tests: the moto messaging table, and the hosted-agent dispatch mocked.
from tests.unit.services.conftest import repos  # noqa: E402, F401


@pytest.fixture
def a2a_env(monkeypatch, repos):  # noqa: F811
    """Signing key, the roster lookup and the DM room for the A2A routes; yields the dispatch mock."""
    import uuid

    monkeypatch.setattr("core.config.settings.NETWORK_PROOF_KEY", "test-seed")
    monkeypatch.setattr(
        "core.services.messaging.load_agent_roster", AsyncMock(return_value=[{"id": "friday", "name": "Friday"}])
    )
    room = str(uuid.uuid4())
    dm = AsyncMock(return_value=room)
    monkeypatch.setattr("core.services.hosted_mcp.person_dm", dm)
    sent: list[str] = []  # the dispatched message ids, in order

    def _send(*a, **k):
        sent.append(str(uuid.uuid4()))
        return SimpleNamespace(message_id=sent[-1])

    send = AsyncMock(side_effect=_send)
    monkeypatch.setattr("core.services.messaging.create_human_message", send)
    yield SimpleNamespace(room=room, send=send, dm=dm, sent=sent)
