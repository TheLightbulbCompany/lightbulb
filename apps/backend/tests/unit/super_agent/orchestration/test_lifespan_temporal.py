"""Review focus 1 and 2 (wave-1 plan T): Temporal never blocks or breaks backend startup, and an env
with the master off never tries to connect."""

import asyncio
import time
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from core.config import settings
from core.temporal import client as tc


@pytest.fixture(autouse=True)
def _quiet_startup(monkeypatch):
    monkeypatch.setattr("core.services.openrouter_keys.check_account_balance", AsyncMock())
    tc.set_client(None)
    yield
    tc.set_client(None)


def test_lifespan_skips_temporal_when_env_master_off(app, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", False)

    async def must_not_connect():
        pytest.fail("connect called with the env master off")

    monkeypatch.setattr(tc, "connect", must_not_connect)
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        with pytest.raises(tc.TemporalUnavailable):
            tc.get_client()


def test_lifespan_connect_failure_does_not_block_startup(app, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)
    monkeypatch.setattr(tc, "connect", AsyncMock(side_effect=RuntimeError("cloud down")))
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        with pytest.raises(tc.TemporalUnavailable):
            tc.get_client()


def test_lifespan_sets_and_closes_client(app, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)
    fake = object()
    monkeypatch.setattr(tc, "connect", AsyncMock(return_value=fake))
    with TestClient(app):
        deadline = time.monotonic() + 5  # the connect runs in the background, after startup
        while tc._client is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert tc.get_client() is fake
    with pytest.raises(tc.TemporalUnavailable):
        tc.get_client()


def test_lifespan_does_not_wait_for_a_hanging_connect(app, monkeypatch):
    monkeypatch.setattr(settings, "SUPER_AGENT_ENV_ENABLED", True)

    async def hang():
        await asyncio.sleep(3600)

    monkeypatch.setattr(tc, "connect", hang)
    started = time.monotonic()
    with TestClient(app) as client:
        assert client.get("/health").status_code == 200
        with pytest.raises(tc.TemporalUnavailable):
            tc.get_client()
    assert time.monotonic() - started < 9  # neither startup nor shutdown waits on the connect
