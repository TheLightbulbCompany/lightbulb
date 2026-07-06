"""Brand-blind runtime port: the contract every agent-runtime adapter fills.

Generic code depends on ``Runtime`` + ``Capabilities`` and never names a
runtime brand; every runtime-specific divergence (wire protocol, on-disk
formats) is contained inside ``core/runtime/<kind>/``.
"""

from collections.abc import Awaitable, Callable, Mapping, Sequence
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

    ``container_auth_token`` is the transport-neutral shared secret the backend
    uses to authenticate to the owner's container. OpenClaw maps it onto its
    gateway ``auth.mode = "token"`` secret; a runtime with no such shared token
    (Hermes authenticates per-container with an ``API_SERVER_KEY`` secret staged
    by provisioning, not written into config) simply leaves it unset.
    """

    provider_choice: str
    container_auth_token: str | None = None
    byo_provider: str | None = None
    hook_token: str | None = None
    owner_type: str = "personal"
    runtime_config: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OwnerFacts:
    """Brand-blind facts about ONE owner, gathered once and shared by every
    runtime's IdentitySeeder so no runtime re-does the Clerk / billing lookups.

    These are the FACTS the seeders render from — never the rendered output. The
    fact-gathering (Clerk ``get_user`` / ``get_organization``, owner-email
    lookup, billing ``resolve_provider_choice``) is runtime-neutral; only the
    per-runtime RENDERING of these facts into on-disk persona files is
    brand-specific (OpenClaw formats a ``provider_label`` string + IDENTITY/SOUL/
    USER markdown from them; Hermes writes a per-run instructions blob). So this
    DTO deliberately carries the RAW ``provider_choice`` / ``byo_provider`` (not a
    formatted label) and the resolved display/greeting strings, but no markdown.

    Fields:
      - ``owner_type`` — "personal" | "org".
      - ``provider_choice`` / ``byo_provider`` — the raw LLM-provider selection;
        each runtime formats its own human label from these.
      - ``display_name`` — how to refer to the owner in prose (personal: the
        owner's first name or a fallback; org: the org name).
      - ``first_name`` — the greeting handle (personal: first name, else the
        email local-part, else "there"; org: "team").
      - ``full_name`` — personal: "First Last" or "(unknown)"; org: the org name.
      - ``email`` — personal: the owner's primary email; org: the founding
        owner's email (both "(unknown)" when unresolved).
      - ``org_name`` — the org name for org owners, else ``None``.
    """

    owner_type: str
    provider_choice: str
    byo_provider: str | None
    display_name: str
    first_name: str
    full_name: str
    email: str
    org_name: str | None = None


@dataclass(frozen=True)
class OwnerProfile:
    """Runtime-neutral description of ONE owner and their seeded agent roster,
    handed to the runtime to seed each agent's persona/identity in whatever
    form it uses (OpenClaw writes IDENTITY/SOUL/USER markdown per agent).

    Deliberately minimal and brand-blind:
      - ``agents`` is the owner's Paperclip roster (each entry carries at least
        ``id`` and the ``role``/``name`` a runtime keys its per-agent seeding
        on). Paperclip is the same BFF for every runtime, so its agent records
        are runtime-neutral; this is the input that DRIVES which agents get
        seeded and under which workspace ids.
      - ``owner_type`` ("personal"/"org") is the brand-blind owner kind, mirrored
        from ``ProvisionCtx`` / ``ConfigProfile``.
      - ``facts`` is the shared, brand-blind ``OwnerFacts`` resolved once at the
        seed call site (``resolve_owner_facts``) so every runtime seeds an
        owner's persona from the SAME Clerk/billing lookup without re-doing it.
        Optional: it is ``None`` when the caller could not resolve facts (a Clerk
        outage) — a runtime seeder falls back to re-resolving or to placeholders,
        exactly as the OpenClaw path already degrades.

    It carries NO rendered persona strings (formatted provider label, markdown):
    those are produced per-runtime from ``facts``. The provider *label* formatting
    and greeting/display markdown stay inside each runtime's seeder.
    """

    agents: Sequence[Mapping[str, Any]] = ()
    owner_type: str = "personal"
    facts: OwnerFacts | None = None


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


# ---------------------------------------------------------------------------
# Chat-transport facet
#
# ``send_chat`` drives ONE user turn against a running container and streams
# translated events back through ``on_event``. The event dicts it emits are the
# internal ChatEvent shape the frontend already renders — the SAME shape
# ``GatewayConnectionPool._transform_agent_event`` / the pool's chat-terminal
# forwarding produce for OpenClaw (``{type:"chunk"|"thinking"|"tool_start"|
# "tool_end"|"tool_error"|"done"|"error", ...}``). Each runtime translates its
# own wire events into that shape so a second runtime renders identically.
# ---------------------------------------------------------------------------

# One translated ChatEvent handler: awaited once per emitted event. The dict is
# an internal ChatEvent (see module note above), NOT a raw runtime wire event.
ChatEventCallback = Callable[[dict], Awaitable[None]]


@dataclass(frozen=True)
class ChatResult:
    """Terminal outcome of one ``send_chat`` run.

    Returned once the run reaches a terminal state — either observed on the
    live event stream or resolved by the poll-recovery fallback after a dropped
    stream. Brand-neutral:
      - ``run_id`` is the runtime's handle for the run.
      - ``status`` is the terminal label ("completed" | "failed" | "cancelled"
        | "unknown"), independent of any brand's wire vocabulary.
      - ``output`` is the terminal payload the runtime reported, left opaque
        here (the H2.5 usage facet reads ``usage`` off the same terminal frame;
        this facet does not interpret it).
    """

    run_id: str
    status: str
    output: Any | None = None


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

    # --- Chat-transport facet: driving one user turn ----------------------

    async def send_chat(
        self,
        owner_id: str,
        agent_id: str,
        message: str,
        session_key: str,
        on_event: ChatEventCallback,
        context: str | None = None,
    ) -> "ChatResult | None": ...

    # --- Provisioner facet: launching / configuring a container -----------

    def container_spec(self, owner_id: str, ctx: ProvisionCtx) -> ContainerSpec: ...

    async def write_config(self, owner_id: str, profile: ConfigProfile) -> None: ...

    async def write_auth(self, owner_id: str, creds: ProviderCreds) -> None: ...

    # --- IdentitySeeder facet: seeding an owner's agents ------------------

    async def seed_owner_agents(self, owner_id: str, profile: OwnerProfile) -> None: ...
