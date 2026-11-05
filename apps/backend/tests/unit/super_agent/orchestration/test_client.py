import pytest

from core.config import settings
from core.temporal import client as tc


def test_get_client_unset_raises():
    tc.set_client(None)
    with pytest.raises(tc.TemporalUnavailable):
        tc.get_client()


async def test_connect_args_cloud_vs_local(monkeypatch):
    calls = []

    async def fake_connect(address, **kwargs):
        calls.append((address, kwargs))
        return object()

    monkeypatch.setattr(tc.Client, "connect", fake_connect)

    monkeypatch.setattr(settings, "TEMPORAL_API_KEY", "k")
    monkeypatch.setattr(settings, "TEMPORAL_ADDRESS", "lightbulb-dev.nv5xx.tmprl.cloud:7233")
    monkeypatch.setattr(settings, "TEMPORAL_NAMESPACE", "lightbulb-dev.nv5xx")
    await tc.connect()
    address, kwargs = calls[-1]
    assert address == "lightbulb-dev.nv5xx.tmprl.cloud:7233"
    assert kwargs["api_key"] == "k"
    assert kwargs["tls"] is True
    assert kwargs["namespace"] == "lightbulb-dev.nv5xx"

    monkeypatch.setattr(settings, "TEMPORAL_API_KEY", None)
    monkeypatch.setattr(settings, "TEMPORAL_ADDRESS", "localhost:7233")
    monkeypatch.setattr(settings, "TEMPORAL_NAMESPACE", "default")
    await tc.connect()
    address, kwargs = calls[-1]
    assert address == "localhost:7233"
    assert kwargs["namespace"] == "default"
    assert "api_key" not in kwargs
    assert not kwargs.get("tls")
