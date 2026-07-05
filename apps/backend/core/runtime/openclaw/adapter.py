"""OpenClaw runtime adapter: wraps the existing gateway pool + one-shot RPC.

The adapter delegates 1:1 — the pool IS the session surface (structural
typing). It never filters or reshapes gateway traffic.
"""

import logging
import uuid
from pathlib import Path
from typing import Any

import httpx

from core.config import settings
from core.gateway.operator_rpc import GATEWAY_PORT, gateway_rpc_oneshot
from core.runtime.base import (
    Capabilities,
    ConfigProfile,
    ContainerNotRunningError,
    ContainerSpec,
    EfsMountLayout,
    Healthcheck,
    ProviderCreds,
    ProvisionCtx,
    SessionSurface,
)

logger = logging.getLogger(__name__)

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


def _get_ecs():
    # Lazy for the same import-lightness reason as _get_pool.
    from core.containers import get_ecs_manager

    return get_ecs_manager()


def _hook_client_factory() -> httpx.AsyncClient:
    """Build the upstream HTTP client for hook forwarding. Module-level so
    tests can swap in a stub transport."""
    return httpx.AsyncClient(timeout=httpx.Timeout(30.0))


class OpenClawRuntime:
    kind = "openclaw"
    capabilities = OPENCLAW_CAPABILITIES

    @property
    def sessions(self) -> SessionSurface:
        return _get_pool()

    async def _resolve(self, owner_id: str) -> tuple[str, str]:
        container, ip = await _get_ecs().resolve_running_container(owner_id)
        token = container.get("gateway_token") if container else None
        if not container or not ip or not token:
            # A container row without a gateway token is as unreachable as a
            # cold container — callers get the port's declared error either
            # way, and no connection attempt is made.
            raise ContainerNotRunningError(f"no running container or gateway token for {owner_id}")
        return ip, token

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
        """Send an RPC to the owner's running container.

        Default path reuses the persistent pooled connection. ``fresh=True``
        opens a short-lived signed one-shot connection instead — required for
        ``operator.admin``-scoped calls and for RPCs that must not ride a
        connection the call itself may tear down (e.g. gateway restart).
        The timeouts apply only to ``fresh=True`` one-shot calls: ``timeout``
        fans out to all three connection stages, while ``open_timeout`` /
        ``recv_timeout`` / ``rpc_timeout`` override individual stages (each
        falls back to ``timeout`` where unset, else 10s). The pooled path
        uses the pool's own timeouts, so passing any of them there raises
        ``ValueError`` rather than silently ignoring it.
        Raises ``ContainerNotRunningError`` when the container is cold.
        """
        timeouts_given = (timeout, open_timeout, recv_timeout, rpc_timeout)
        if any(t is not None for t in timeouts_given) and not fresh:
            raise ValueError("timeout/open_timeout/recv_timeout/rpc_timeout apply only to fresh=True one-shot calls")
        ip, token = await self._resolve(owner_id)
        if fresh:
            default = timeout if timeout is not None else 10.0
            return await gateway_rpc_oneshot(
                ip,
                owner_id=owner_id,
                token=token,
                method=method,
                params=params,
                open_timeout=open_timeout if open_timeout is not None else default,
                recv_timeout=recv_timeout if recv_timeout is not None else default,
                rpc_timeout=rpc_timeout if rpc_timeout is not None else default,
            )
        return await _get_pool().send_rpc(
            user_id=owner_id,
            req_id=f"rt-{method}-{uuid.uuid4().hex[:12]}",
            method=method,
            params=params or {},
            ip=ip,
            token=token,
        )

    async def revoke_provider(self, owner_id: str, *, provider: str) -> bool:
        """Best-effort: drop ``provider``'s credential from the running
        container's live auth store via ``models.authLogout``.

        Returns ``True`` if the RPC succeeded, ``False`` otherwise. NEVER
        raises — when the container is cold there is no live store to clear
        (the EFS seed-file delete is the authoritative cleanup), and a
        transient RPC failure is logged and self-heals on reprovision.
        """
        try:
            container, ip = await _get_ecs().resolve_running_container(owner_id)
            if not container or not ip:
                return False  # cold container — nothing live to clear
            token = container.get("gateway_token")
            if not token or not container.get("operator_priv_key_enc"):
                # A row never paired with a signed operator device (or missing
                # its gateway token) cannot complete the signed handshake, so
                # skip the live logout without any network I/O — the EFS
                # seed-file delete remains the authoritative cleanup.
                logger.warning(
                    "missing gateway_token/operator seed for %s — skipping live logout",
                    owner_id,
                )
                return False
            await gateway_rpc_oneshot(
                ip,
                owner_id=owner_id,
                token=token,
                method="models.authLogout",
                params={"provider": provider},
                open_timeout=10.0,
                recv_timeout=10.0,
                rpc_timeout=10.0,
            )
        except Exception as exc:  # noqa: BLE001 — best-effort
            # Neutral wording: this branch catches both container-resolution
            # failures and the RPC itself failing.
            logger.warning("revoke_provider failed for %s: %s", owner_id, exc)
            return False
        logger.info(
            "revoked_provider_auth_in_container",
            extra={"owner_id": owner_id, "provider": provider},
        )
        return True

    async def forward_hook(
        self, owner_id: str, path: str, headers: dict[str, str], body: bytes
    ) -> tuple[int, dict[str, str], bytes]:
        """POST a webhook payload to the owner's container ``/hooks`` ingress.

        ``path`` may carry the caller's raw query string (``wake?challenge=x``)
        — it is spliced into the target URL verbatim, because webhook senders
        put signature/verification material there. The container-side
        ``Authorization: Bearer <hook_token>`` is attached here from the
        container row (hooks ingress auth is the hook token, NOT the gateway
        token); callers supply everything else in ``headers``.

        Returns ``(status, headers, body)`` with hop-by-hop response headers
        stripped so the caller can relay the tuple verbatim. An unreachable
        upstream maps to a ``502`` tuple rather than raising. Raises
        ``ContainerNotRunningError`` when the container is cold or its row
        has no hook token (equally unforwardable).
        """
        container, ip = await _get_ecs().resolve_running_container(owner_id)
        hook_token = container.get("hook_token") if container else None
        if not container or not ip or not hook_token:
            raise ContainerNotRunningError(f"no running container or hook token for {owner_id}")
        target = f"http://{ip}:{GATEWAY_PORT}/hooks/{path}"
        upstream_headers = {**headers, "Authorization": f"Bearer {hook_token}"}
        try:
            async with _hook_client_factory() as client:
                upstream = await client.post(target, headers=upstream_headers, content=body)
        except httpx.RequestError as exc:
            logger.warning("hook proxy upstream error owner=%s err=%s", owner_id, exc)
            return 502, {}, b'{"detail":"upstream_unreachable"}'
        return (
            upstream.status_code,
            {
                k: v
                for k, v in upstream.headers.items()
                if k.lower() not in ("content-length", "transfer-encoding", "connection")
            },
            upstream.content,
        )

    # ------------------------------------------------------------------
    # Provisioner facet: launching / configuring a container
    # ------------------------------------------------------------------

    def container_spec(self, owner_id: str, ctx: ProvisionCtx) -> ContainerSpec:
        """Describe the container to launch for ``owner_id``, brand-blind.

        Returns the SAME launch values the ECS provisioning path uses today:
        the port is the shared gateway port (18789); ``cpu``/``memory_mib`` are
        the per-user task-size constants; ``image`` is the CDK base task-def
        family the per-user clone inherits its image from; ``env`` is the one
        per-user variable the backend injects (``OPENCLAW_HOOK_TOKEN``); the
        healthcheck mirrors ``EcsManager.is_healthy`` (a TCP-accept on the
        gateway port); and the EFS layout mirrors ``_create_access_point``.
        This facet only DESCRIBES those values — the ECS path still owns the
        launch today (a future task makes it consume this).
        """
        # Lazy: PER_USER_* + the base task-def family live in core.containers
        # (AWS clients at import). Same import-lightness reason as _get_pool.
        from core.containers.ecs_manager import (
            PER_USER_CPU,
            PER_USER_MEMORY_MIB,
            EcsManager,
        )

        base_family = EcsManager._BASE_TASK_DEF_FAMILY.format(env=settings.ENVIRONMENT or "dev")
        return ContainerSpec(
            image=base_family,
            container_port=GATEWAY_PORT,
            cpu=PER_USER_CPU,
            memory_mib=PER_USER_MEMORY_MIB,
            env={"OPENCLAW_HOOK_TOKEN": ctx.hook_token},
            healthcheck=Healthcheck(kind="tcp", port=GATEWAY_PORT, timeout_s=5.0),
            efs_mount=EfsMountLayout(
                mount_root=settings.EFS_MOUNT_PATH,
                owner_subpath=f"/users/{owner_id}",
                posix_uid=1000,
                posix_gid=1000,
                root_permissions="0755",
            ),
        )

    async def write_config(self, owner_id: str, profile: ConfigProfile) -> None:
        """Serialize ``profile`` into the owner's ``openclaw.json`` on EFS.

        Thin delegation to ``config.write_openclaw_config`` — the brand-neutral
        scalars map to its named params, and ``runtime_config`` (already-gathered
        OpenClaw-specific inputs: ``paperclip_agents``, ``integration_mcp_servers``,
        ``channel_accounts``, ``aip_arns``, ``acp_enabled``) is forwarded verbatim.
        """
        # Lazy import: core.containers.config pulls the container package
        # (AWS clients at import). Same import-lightness reason as _get_pool.
        from core.containers.config import write_openclaw_config

        config_path = Path(settings.EFS_MOUNT_PATH) / owner_id / "openclaw.json"
        await write_openclaw_config(
            config_path=config_path,
            gateway_token=profile.gateway_token,
            provider_choice=profile.provider_choice,
            user_id=owner_id,
            byo_provider=profile.byo_provider,
            hook_token=profile.hook_token,
            owner_type=profile.owner_type,
            **dict(profile.runtime_config),
        )

    async def write_auth(self, owner_id: str, creds: ProviderCreds) -> None:
        """Stage ``creds`` into the owner's container before launch.

        OpenClaw stages only OAuth material onto the container filesystem (the
        container-global ``credentials/oauth.json`` EFS seed) — BYO keys ride
        the task-definition secret path and Bedrock uses IAM, so neither is a
        ``write_auth``. Thin delegation to ``workspace.pre_stage_auth_profile_store``.
        """
        if creds.kind != "oauth":
            raise ValueError(
                "OpenClaw write_auth stages only 'oauth' credentials to the container "
                f"filesystem; got kind={creds.kind!r} (BYO keys ride task-def secrets, "
                "Bedrock uses IAM)"
            )
        # Lazy import: core.containers.workspace pulls the container package
        # (AWS clients at import). Same import-lightness reason as _get_pool.
        from core.containers.workspace import pre_stage_auth_profile_store

        await pre_stage_auth_profile_store(user_id=owner_id, oauth_tokens=dict(creds.secrets))
