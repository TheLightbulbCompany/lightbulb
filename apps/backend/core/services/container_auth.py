"""Revoke a provider's auth credential from a RUNNING OpenClaw container.

On 6.6, the live credential lives in the container's per-agent SQLite auth
store (imported from ``credentials/oauth.json`` on first boot). Deleting the
EFS seed file does NOT clear that store, so a disconnect / switch-away must
also tell the running gateway to drop the credential — otherwise the already
imported refresh token stays usable until the container is reprovisioned.

The gateway exposes a built-in ``models.authLogout`` RPC (scope
``operator.admin``) that removes the provider's auth profiles across the owner
stores, invalidates the provider-auth cache, and aborts in-flight runs using
that provider (``stopReason: "auth-revoked"``) — a true revoke with no restart.

NOTE: the WS connect+RPC pattern below is also in routers/container_rpc.py and
routers/container_recover.py. It is duplicated here (rather than imported) to
avoid a service -> router import cycle; a shared gateway RPC client is a
reasonable future cleanup.
"""

import asyncio
import json
import logging
import uuid

import websockets

from core.containers import get_ecs_manager
from core.containers.ecs_manager import GATEWAY_PORT

logger = logging.getLogger(__name__)

_WS_TIMEOUT = 10


async def _logout_provider_via_gateway(ip: str, token: str, provider: str) -> None:
    """Open a short-lived WS to the gateway, complete the operator.admin connect
    handshake, and call ``models.authLogout`` for ``provider``. Raises on any
    handshake/RPC failure (caller treats failure as best-effort)."""
    uri = f"ws://{ip}:{GATEWAY_PORT}"
    async with websockets.connect(uri, open_timeout=_WS_TIMEOUT, close_timeout=5) as ws:
        # Handshake: connect.challenge -> connect (operator.admin) -> hello-ok.
        raw = await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT)
        if json.loads(raw).get("event") != "connect.challenge":
            raise RuntimeError("expected connect.challenge from gateway")
        await ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": str(uuid.uuid4()),
                    "method": "connect",
                    "params": {
                        "minProtocol": 4,
                        "maxProtocol": 4,
                        "client": {
                            "id": "cli",
                            "version": "1.0.0",
                            "platform": "linux",
                            "mode": "cli",
                        },
                        "role": "operator",
                        "scopes": ["operator.admin"],
                        "auth": {"token": token},
                    },
                }
            )
        )
        hello = json.loads(await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT))
        if not hello.get("ok"):
            raise RuntimeError(f"gateway connect failed: {hello.get('error', {}).get('message')}")

        # models.authLogout — drop the provider's credential from the live store.
        req_id = str(uuid.uuid4())
        await ws.send(
            json.dumps(
                {
                    "type": "req",
                    "id": req_id,
                    "method": "models.authLogout",
                    "params": {"provider": provider},
                }
            )
        )
        # Skip event broadcasts until the matching response arrives.
        while True:
            data = json.loads(await asyncio.wait_for(ws.recv(), timeout=_WS_TIMEOUT))
            if data.get("type") == "res" and data.get("id") == req_id:
                if not data.get("ok"):
                    raise RuntimeError(data.get("error", {}).get("message", "models.authLogout rejected"))
                return


async def revoke_provider_auth_in_container(owner_id: str, *, provider: str = "openai") -> bool:
    """Best-effort: clear ``provider``'s credential from the owner's RUNNING
    container via ``models.authLogout``, so disconnect / switch-away revokes the
    live token immediately without a restart.

    Returns ``True`` if the RPC succeeded, ``False`` otherwise. NEVER raises —
    when no container is running (cold) there is no live store to clear and the
    EFS seed-file delete is the authoritative cleanup; a transient RPC failure
    is logged and self-heals on the next reprovision.
    """
    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("revoke_provider_auth: resolve container failed for %s: %s", owner_id, exc)
        return False

    if not container or not ip:
        return False  # cold container — nothing live to clear
    token = container.get("gateway_token")
    if not token:
        return False

    try:
        await _logout_provider_via_gateway(ip, token, provider)
        logger.info(
            "revoked_provider_auth_in_container",
            extra={"owner_id": owner_id, "provider": provider},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — best-effort
        logger.warning("revoke_provider_auth: models.authLogout failed for %s: %s", owner_id, exc)
        return False
