"""Brand-blind runtime port: the contract every agent-runtime adapter fills.

Generic code depends on ``Runtime`` + ``Capabilities`` and never names a
runtime brand; every runtime-specific divergence (wire protocol, on-disk
formats) is contained inside ``core/runtime/<kind>/``.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
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
    def mark_run_active(self, user_id: str, session_key: str, agent_id: str | None = None) -> None: ...
    def clear_active_run(self, user_id: str, session_key: str) -> None: ...
    def mark_side_run(self, user_id: str, run_id: str) -> None: ...
    async def gate_chat(self, *, user_id: str, owner_id: str) -> dict: ...
    async def send_rpc(self, user_id: str, req_id: str, method: str, params: dict, ip: str, token: str) -> Any: ...
    async def ensure_connection(self, user_id: str, ip: str, token: str) -> Any: ...
    def add_frontend_connection(self, user_id: str, connection_id: str) -> None: ...
    def remove_frontend_connection(self, user_id: str, connection_id: str) -> None: ...
    async def close_user(self, user_id: str) -> None: ...
    async def broadcast_to_user(self, user_id: str, message: dict) -> None: ...
    async def broadcast_to_member(self, owner_id: str, member_user_id: str, message: dict) -> None: ...
    async def close_all(self) -> None: ...
    async def emit_running_gauges(self) -> None: ...


# ---------------------------------------------------------------------------
# Provisioner facet DTOs
#
# The types below describe *launching / configuring* one owner's container,
# the counterpart to the session/RPC surface above which is about *talking to*
# an already-running one. They stay brand-blind: only fields any runtime would
# need, never a runtime's internal task-definition/on-disk mechanics. The
# runtime adapter supplies the values; a launcher (ECS today, k8s later) maps
# them onto its own primitives.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Healthcheck:
    """How generic code decides a freshly-launched container is serving.

    ``kind`` is the probe transport ("tcp" is all any runtime needs today);
    ``port`` + ``timeout_s`` are its parameters. OpenClaw's readiness is a
    bare TCP-accept on the gateway port.
    """

    kind: str
    port: int
    timeout_s: float


@dataclass(frozen=True)
class EfsMountLayout:
    """Per-owner persistent-filesystem layout the runtime's durable state
    lives under.

    Generic code needs these to create the per-owner mount (EFS access point
    today) and to reach the owner's files from the backend. Brand-blind: a
    non-EFS runtime fills the same shape for its own persistent store.
    """

    mount_root: str
    owner_subpath: str
    posix_uid: int
    posix_gid: int
    root_permissions: str


@dataclass(frozen=True)
class ContainerSpec:
    """Everything generic provisioning code needs to launch ONE owner's
    container, with zero runtime-brand knowledge.

    Values are supplied by the runtime adapter; the launcher maps them onto
    its own primitives. Runtime-only task-definition mechanics (base-family
    cloning, per-provider secret/env layering) stay inside the adapter — they
    are deliberately NOT modelled here.
    """

    image: str
    container_port: int
    cpu: str
    memory_mib: str
    env: Mapping[str, str]
    healthcheck: Healthcheck
    efs_mount: EfsMountLayout


@dataclass(frozen=True)
class ProvisionCtx:
    """Per-provision inputs that shape ONE owner's container launch,
    independent of runtime brand.

    ``hook_token`` is the per-owner inbound-webhook secret the runtime injects
    into the container environment; ``owner_type`` ("personal"/"org") tags the
    per-owner filesystem mount.
    """

    hook_token: str
    owner_type: str = "personal"


@dataclass(frozen=True)
class ConfigProfile:
    """Runtime-neutral description of ONE owner's agent config, handed to the
    runtime to serialize into whatever form it uses (OpenClaw writes
    ``openclaw.json``).

    The scalar fields are brand-neutral (LLM provider selection, the
    container's auth secrets, owner kind). ``runtime_config`` is an opaque bag
    of already-gathered, runtime-specific config inputs the adapter forwards
    verbatim — generic code never inspects it, so no runtime-brand shape leaks
    onto this DTO.
    """

    provider_choice: str
    gateway_token: str
    byo_provider: str | None = None
    hook_token: str | None = None
    owner_type: str = "personal"
    runtime_config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderCreds:
    """Runtime-neutral provider credential to stage into ONE owner's container
    before launch.

    ``kind`` selects the credential family ("oauth" today — the only provider
    whose material is staged onto the container's filesystem; BYO keys ride the
    task-definition secret path and Bedrock uses IAM, so neither is a
    ``write_auth``). ``secrets`` is the opaque credential material the adapter
    maps onto its own staging; generic code never reads it.
    """

    kind: str
    secrets: Mapping[str, Any]


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
        open_timeout: float | None = None,
        recv_timeout: float | None = None,
        rpc_timeout: float | None = None,
    ) -> Any: ...

    async def revoke_provider(self, owner_id: str, *, provider: str) -> bool: ...

    async def forward_hook(
        self, owner_id: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]: ...

    # --- Provisioner facet: launching / configuring a container -----------

    def container_spec(self, owner_id: str, ctx: ProvisionCtx) -> ContainerSpec: ...

    async def write_config(self, owner_id: str, profile: ConfigProfile) -> None: ...

    async def write_auth(self, owner_id: str, creds: ProviderCreds) -> None: ...
