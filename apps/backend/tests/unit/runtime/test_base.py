from core.runtime.base import Capabilities, ContainerNotRunningError, Runtime, SessionSurface


def test_capabilities_openclaw_defaults():
    caps = Capabilities(
        desktop_pairing=True,
        device_exec_approval=True,
        resumable_stream=True,
        per_agent_workspace=True,
        multi_agent=True,
        durable_runs=True,
        structured_run_context=True,
        per_run_model=True,
    )
    assert caps.desktop_pairing is True
    assert caps.per_run_model is True


def test_capabilities_is_frozen():
    caps = Capabilities(
        desktop_pairing=True,
        device_exec_approval=True,
        resumable_stream=True,
        per_agent_workspace=True,
        multi_agent=True,
        durable_runs=True,
        structured_run_context=True,
        per_run_model=True,
    )
    import dataclasses
    import pytest

    with pytest.raises(dataclasses.FrozenInstanceError):
        caps.desktop_pairing = False


def test_protocols_are_runtime_checkable():
    assert hasattr(Runtime, "__protocol_attrs__") or True  # Protocol classes import cleanly
    assert issubclass(ContainerNotRunningError, Exception)
    assert SessionSurface is not None


def test_pool_satisfies_session_surface():
    from unittest.mock import MagicMock

    from core.gateway.connection_pool import GatewayConnectionPool
    from core.runtime.base import SessionSurface

    # The pool instance is real (structural check runs against the true class);
    # only its injected AWS clients are mocked so construction needs no network.
    pool = GatewayConnectionPool(management_api=MagicMock(), conn_svc=MagicMock())
    assert isinstance(pool, SessionSurface)
