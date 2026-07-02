import pytest

from core.runtime import get_runtime, get_runtime_for_owner
from core.runtime.base import Capabilities


def test_get_runtime_openclaw():
    rt = get_runtime("openclaw")
    assert rt.kind == "openclaw"
    # Full-Protocol isinstance(rt, Runtime) is deferred: the adapter grows
    # call_rpc / revoke_provider / forward_hook later in the refactor, so
    # assert the surface that exists today instead.
    assert isinstance(rt.capabilities, Capabilities)
    assert isinstance(type(rt).sessions, property)
    assert rt.capabilities.desktop_pairing is True
    assert rt.capabilities.per_run_model is True


def test_get_runtime_unknown_kind_raises():
    with pytest.raises(ValueError, match="unknown runtime kind"):
        get_runtime("hermes")  # not registered until the Hermes adapter lands


def test_get_runtime_for_owner_defaults_openclaw():
    rt = get_runtime_for_owner("org_anything")
    assert rt.kind == "openclaw"


def test_get_runtime_is_singleton_per_kind():
    assert get_runtime("openclaw") is get_runtime("openclaw")


def test_sessions_is_the_gateway_pool(monkeypatch):
    sentinel = object()
    import core.runtime.openclaw.adapter as adapter_mod

    monkeypatch.setattr(adapter_mod, "_get_pool", lambda: sentinel)
    assert get_runtime("openclaw").sessions is sentinel
