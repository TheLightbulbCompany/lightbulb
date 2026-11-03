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
def a2a_env(app, monkeypatch, repos):  # noqa: F811
    """Signing key, the roster lookup and the DM room for the A2A routes; yields the dispatch mock.
    The door's caller is the signed-in person `get_current_user` (overridden by the client fixtures)
    returns, and the card's OAuth URLs are Clerk's fallback paths (no network)."""
    import uuid

    import httpx
    from fastapi import Depends

    from core.auth import get_current_user
    from core.services import a2a_auth

    async def _session(auth=Depends(get_current_user)):
        return a2a_auth.session_caller(auth)

    async def _no_clerk():
        raise httpx.ConnectError("no network in tests")

    app.dependency_overrides[a2a_auth.a2a_caller] = _session
    monkeypatch.setattr(a2a_auth, "_endpoints", {})
    monkeypatch.setattr(a2a_auth, "_clerk_metadata", _no_clerk)

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
    app.dependency_overrides.pop(a2a_auth.a2a_caller, None)
