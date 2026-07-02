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
    ) -> Any:
        """Send an RPC to the owner's running container.

        Default path reuses the persistent pooled connection. ``fresh=True``
        opens a short-lived signed one-shot connection instead — required for
        ``operator.admin``-scoped calls and for RPCs that must not ride a
        connection the call itself may tear down (e.g. gateway restart).
        ``timeout`` applies only to ``fresh=True`` one-shot calls; the pooled
        path uses the pool's own timeouts, so passing one there raises
        ``ValueError`` rather than silently ignoring it.
        Raises ``ContainerNotRunningError`` when the container is cold.
        """
        if timeout is not None and not fresh:
            raise ValueError("timeout applies only to fresh=True one-shot calls")
        ip, token = await self._resolve(owner_id)
        if fresh:
            t = timeout if timeout is not None else 10.0
            return await gateway_rpc_oneshot(
                ip,
                owner_id=owner_id,
                token=token,
                method=method,
                params=params,
                open_timeout=t,
                recv_timeout=t,
                rpc_timeout=t,
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
            await self.call_rpc(owner_id, "models.authLogout", {"provider": provider}, fresh=True)
        except ContainerNotRunningError:
            return False  # cold container — nothing live to clear
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
