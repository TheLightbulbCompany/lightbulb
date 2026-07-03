"""OpenClaw runtime adapter: wraps the existing gateway pool + one-shot RPC.

The adapter delegates 1:1 — the pool IS the session surface (structural
typing). It never filters or reshapes gateway traffic.
"""

import logging
import uuid
from typing import Any

from core.gateway.operator_rpc import gateway_rpc_oneshot
from core.runtime.base import Capabilities, ContainerNotRunningError, SessionSurface

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

    # forward_hook lands with the hooks migration.
