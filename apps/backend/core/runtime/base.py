"""Brand-blind runtime port: the contract every agent-runtime adapter fills.

Generic code depends on ``Runtime`` + ``Capabilities`` and never names a
runtime brand; every runtime-specific divergence (wire protocol, on-disk
formats) is contained inside ``core/runtime/<kind>/``.
"""

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class ContainerNotRunningError(RuntimeError):
    """Raised by ``Runtime.call_rpc`` when the owner has no running container."""


@dataclass(frozen=True)
class Capabilities:
    """What a runtime can do. Generic code asks a capability, never a brand."""

    desktop_pairing: bool
    device_exec_approval: bool
    resumable_stream: bool
    per_agent_workspace: bool
    multi_agent: bool
    durable_runs: bool
    structured_run_context: bool
    per_run_model: bool


@runtime_checkable
class SessionSurface(Protocol):
    """The live-session surface a runtime exposes to the chat path.

    Method names and signatures mirror ``GatewayConnectionPool`` exactly so
    the pool satisfies this Protocol structurally with zero wrappers; a
    second runtime implements the same surface over its own transport.
    Extend this Protocol (never the caller) if a migration surfaces a pool
    method not yet declared here.
    """

    def is_user_connected(self, user_id: str) -> bool: ...
    def mark_run_active(
        self, user_id: str, session_key: str, agent_id: str | None = None, member_id: str | None = None
    ) -> None: ...
    def clear_active_run(self, user_id: str, session_key: str) -> None: ...
    def mark_side_run(self, user_id: str, run_id: str) -> None: ...
    async def gate_chat(self, *, user_id: str, owner_id: str, session_key: str) -> dict: ...
    async def send_rpc(self, user_id: str, req_id: str, method: str, params: dict, ip: str, token: str) -> Any: ...
    async def ensure_connection(self, user_id: str, ip: str, token: str) -> Any: ...
    def add_frontend_connection(self, user_id: str, connection_id: str) -> None: ...
    def remove_frontend_connection(self, user_id: str, connection_id: str) -> None: ...
    async def close_user(self, user_id: str) -> None: ...
    async def broadcast_to_user(self, user_id: str, message: dict) -> None: ...
    async def broadcast_to_member(self, owner_id: str, member_user_id: str, message: dict) -> None: ...
    async def close_all(self) -> None: ...
    async def emit_running_gauges(self) -> None: ...


@runtime_checkable
class Runtime(Protocol):
    """One agent-runtime adapter (OpenClaw today, Hermes later)."""

    kind: str
    capabilities: Capabilities

    @property
    def sessions(self) -> SessionSurface: ...

    async def call_rpc(
        self,
        owner_id: str,
        method: str,
        params: dict | None = None,
        *,
        fresh: bool = False,
        timeout: float | None = None,
    ) -> Any: ...

    async def revoke_provider(self, owner_id: str, *, provider: str) -> bool: ...

    async def forward_hook(self, owner_id: str, path: str, headers: dict[str, str], body: bytes) -> Any: ...
