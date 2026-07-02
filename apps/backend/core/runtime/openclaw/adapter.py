"""OpenClaw runtime adapter: wraps the existing gateway pool + one-shot RPC.

The adapter delegates 1:1 — the pool IS the session surface (structural
typing). It never filters or reshapes gateway traffic.
"""

from core.runtime.base import Capabilities, SessionSurface

OPENCLAW_CAPABILITIES = Capabilities(
    desktop_pairing=True,
    device_exec_approval=True,
    resumable_stream=True,
    per_agent_workspace=True,
    multi_agent=True,
    durable_runs=True,
    structured_run_context=True,
    per_run_model=True,
)


def _get_pool():
    # Lazy: core.containers builds AWS clients at import; the runtime package
    # must stay import-light (same constraint as operator_rpc's port constant).
    from core.containers import get_gateway_pool

    return get_gateway_pool()


class OpenClawRuntime:
    kind = "openclaw"
    capabilities = OPENCLAW_CAPABILITIES

    @property
    def sessions(self) -> SessionSurface:
        return _get_pool()

    # call_rpc / revoke_provider / forward_hook are added in Tasks 4 and 7.
