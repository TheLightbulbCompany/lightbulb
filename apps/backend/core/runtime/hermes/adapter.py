"""Hermes runtime adapter: the second agent-runtime behind the brand-blind port.

This is the H2.1 SKELETON — the class, its verified capability flags, and its
registry wiring only. Every port method is a stub that raises
``NotImplementedError``; the real transport / provisioner / seeder / usage
bodies land across H2.2–H2.5. The stubs still satisfy the ``Runtime`` Protocol
(``runtime_checkable`` only checks member PRESENCE), so a registered
``HermesRuntime`` is importable and Protocol-shaped without being functional
yet.

Import-lightness is load-bearing: registering Hermes means importing
``core.runtime`` now imports this module, and the subprocess tripwire in the
runtime tests asserts ``core.containers`` (AWS clients at import) never leaks
into that path. Keep module-level imports to stdlib/typing + ``core.runtime.base``;
any future httpx/aiohttp/container imports go INSIDE methods, lazily, exactly as
the OpenClaw adapter does.
"""

from typing import Any

from core.runtime.base import (
    Capabilities,
    ConfigProfile,
    ContainerSpec,
    OwnerProfile,
    ProviderCreds,
    ProvisionCtx,
    SessionSurface,
)

# The H1.1-confirmed Hermes capability set. Hermes has no desktop-pairing /
# device exec-approval, no resumable stream, and no durable/structured/per-run
# run model; it does give each agent its own workspace and supports multiple
# agents. Divergence from OpenClaw's all-True set is intentional and verified.
HERMES_CAPABILITIES = Capabilities(
    desktop_pairing=False,
    device_exec_approval=False,
    resumable_stream=False,
    per_agent_workspace=True,
    multi_agent=True,
    durable_runs=False,
    structured_run_context=False,
    per_run_model=False,
)


class HermesRuntime:
    kind = "hermes"
    capabilities = HERMES_CAPABILITIES

    @property
    def sessions(self) -> SessionSurface:
        raise NotImplementedError("HermesRuntime.sessions lands in H2.x")

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
    ) -> Any:
        raise NotImplementedError("HermesRuntime.call_rpc lands in H2.x")

    async def revoke_provider(self, owner_id: str, *, provider: str) -> bool:
        raise NotImplementedError("HermesRuntime.revoke_provider lands in H2.x")

    async def forward_hook(
        self, owner_id: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        raise NotImplementedError("HermesRuntime.forward_hook lands in H2.x")

    # --- Provisioner facet: launching / configuring a container -----------

    def container_spec(self, owner_id: str, ctx: ProvisionCtx) -> ContainerSpec:
        raise NotImplementedError("HermesRuntime.container_spec lands in H2.x")

    async def write_config(self, owner_id: str, profile: ConfigProfile) -> None:
        raise NotImplementedError("HermesRuntime.write_config lands in H2.x")

    async def write_auth(self, owner_id: str, creds: ProviderCreds) -> None:
        raise NotImplementedError("HermesRuntime.write_auth lands in H2.x")

    # --- IdentitySeeder facet: seeding an owner's agents ------------------

    async def seed_owner_agents(self, owner_id: str, profile: OwnerProfile) -> None:
        raise NotImplementedError("HermesRuntime.seed_owner_agents lands in H2.x")
