"""Live gateway-RPC push for desktop device pairings.

Since the OpenClaw 8.x image the gateway's device trust store lives in the
container's SQLite state DB. ``devices/paired.json`` and ``nodes/paired.json``
are RETIRED files that a one-time boot migration imports (fork:
``src/infra/device-pairing-migration.ts`` + ``node-pairing-migration.ts``) and
then archives with a ``.migrated`` suffix. The backend's ``desktop_pair`` only
writes those JSON files, so pairing a new desktop device against a RUNNING
container was inert until the next container boot — the desktop looped on
NOT_PAIRED forever (prod incident 2026-08-30).

The gateway exposes no bulk "import a paired device" RPC. The only live doors
are upstream's own two-gate approval flow:

1. ``device.pair.approve {requestId}`` — approves the PENDING device request
   that a rejected connect creates (``requestDevicePairing`` runs inside the
   gateway's connect handler). Approving a node-role request requires an
   admin caller; the backend's operator connection carries ``operator.admin``.
2. ``node.pair.approve {requestId}`` — approves the PENDING node command
   surface (``pendingNodeSurface``) that a *paired* node's connect files.
   Requires ``operator.pairing``(+``operator.write``/``operator.admin`` per
   the declared commands) — all implied by ``operator.admin``
   (fork: ``operator-scope-compat.ts`` ``operatorScopeSatisfied``).

Both doors need a pending request to exist, so the live push is event-shaped,
not a one-shot import:

- ``push_desktop_pairing_live`` (POST /desktop/pair) approves whatever is
  ALREADY pending — the re-pair case where the desktop is looping on
  NOT_PAIRED and refreshing its pending request.
- ``heal_desktop_device_not_paired`` (node relay NOT_PAIRED rejection in
  ``node_proxy``) approves the pending request the rejected connect just
  created — the fresh-pair case where the desktop connects after pairing.
  Twin of the Paperclip operator relay's self-heal (``operator_proxy``).
- ``approve_pending_node_surface`` (successful node connect) approves the
  node's pending command surface — the second gate, which the gateway files
  during the connect handshake and which otherwise strips the node's live
  commands to [] until the next boot folds ``nodes/paired.json``.

TRUST BOUNDARY: every push is gated on the backend-written pairing files.
Only a deviceId the Clerk-authed ``/desktop/pair`` flow wrote is ever
approved (deviceId is the SHA-256 of the device public key, and the gateway
verifies key possession before creating a pending request, so a deviceId
match proves the connecting device holds the owner-approved key). A node
surface is only approved when its declared commands stay inside the command
set the owner's ``DesktopPermissionGrant`` derived — a client declaring more
than it was granted is left pending, not approved.

All best-effort: a failed push logs + metrics and leaves the boot import as
the fallback. Nothing here may fail the user-visible pair/connect flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os

from core.observability.metrics import put_metric
from core.services import container_rpc
from core.services.desktop_pair import NODE_COMMANDS_DECLARED_UNCONDITIONALLY
from core.services.paperclip_openclaw_pairing import approve_pending_device_from_rejection

logger = logging.getLogger(__name__)

# Per-RPC response budget. `container_rpc.call`'s default (30s) is sized for
# contended agents.create calls; these are cheap list/approve reads and a sick
# container should fail the push fast — the boot import remains behind it.
_RPC_TIMEOUT_S = 10


def _read_backend_pairing_file(owner_id: str, subdir: str) -> dict:
    """Plain read of the backend-written ``<owner>/{subdir}/paired.json``.

    Lockless by design: every writer swaps the file in with ``os.rename``
    (see ``desktop_pair.EfsWorkspaceAdapter``), so a read never observes a
    partial write. Missing/unparseable file reads as {} — which fails the
    trust gate closed (nothing gets approved).
    """
    from core.containers import get_workspace

    path = os.path.join(str(get_workspace().user_path(owner_id)), subdir, "paired.json")
    try:
        with open(path) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


async def _pool_rpc(*, owner_id: str, method: str, params: dict) -> dict:
    """One admin-scoped RPC to the owner's container."""
    payload = await container_rpc.call(owner_id, method, params, timeout_s=_RPC_TIMEOUT_S)
    return payload if isinstance(payload, dict) else {}


async def heal_desktop_device_not_paired(
    *,
    owner_id: str,
    device_id: str | None,
    request_id: str | None,
) -> bool:
    """Approve the pending pairing request a rejected desktop connect created.

    Gate: the deviceId must be present in the owner's backend-written
    ``devices/paired.json`` — the artifact of the Clerk-authed /desktop/pair
    flow. A device the owner never paired stays NOT_PAIRED; auto-approving it
    would let any org member's raw WS connect mint its own trust entry and
    bypass the server-derived permission grant.

    Never raises (called from an already-failing connect path). Returns True
    iff an approval RPC landed.
    """
    if not device_id:
        return False
    try:
        devices = await asyncio.to_thread(_read_backend_pairing_file, owner_id, "devices")
    except Exception:  # noqa: BLE001 — gate read failure fails closed
        logger.warning("heal_desktop_device_not_paired: pairing-file read failed for owner=%s", owner_id, exc_info=True)
        return False
    if device_id not in devices:
        logger.info(
            "heal_desktop_device_not_paired: device=%s owner=%s is not backend-paired — leaving NOT_PAIRED alone",
            device_id[:16],
            owner_id,
        )
        return False
    return await approve_pending_device_from_rejection(
        owner_id=owner_id,
        device_id=device_id,
        request_id=request_id,
    )


async def approve_pending_node_surface(
    *,
    owner_id: str,
    device_id: str,
) -> bool:
    """Approve the pending node command surface for one backend-paired device.

    The 8.x gateway gates a node's live commands behind a SECOND approval:
    after device pairing, the connecting node declares its surface and the
    gateway parks it as ``pendingNodeSurface`` until an operator approves it —
    until then the node connects but its commands are stripped, so agents see
    "node requires system.run". Approving live updates the connected session
    in place (``node.pair.approve`` handler → ``nodeRegistry.updateSurface``),
    no reconnect needed.

    Gates: the device must have an entry in the backend-written
    ``nodes/paired.json``, and the declared pending commands must be a subset
    of the commands the owner's permission grant derived there. Never raises.
    Returns True iff an approval RPC landed.
    """
    try:
        nodes = await asyncio.to_thread(_read_backend_pairing_file, owner_id, "nodes")
        entry = nodes.get(device_id)
        if not isinstance(entry, dict):
            return False
        payload = await _pool_rpc(owner_id=owner_id, method="node.pair.list", params={})
        pending = next(
            (
                p
                for p in payload.get("pending") or []
                if isinstance(p, dict) and str(p.get("nodeId", "")).strip() == device_id
            ),
            None,
        )
        if not pending:
            # Nothing pending: surface already approved (boot fold or a prior
            # approval), or the node hasn't declared one yet. Idempotent no-op.
            return False
        allowed = set(entry.get("commands") or [])
        declared = set(pending.get("commands") or [])
        excess = declared - allowed
        # The 2026.8.1 sidecar declares several commands unconditionally,
        # regardless of the owner's grant (NODE_COMMANDS_DECLARED_UNCONDITIONALLY
        # -- see its comment for why declared <= allowed cannot hold for a
        # partial grant even when the node behaves exactly as configured).
        # Refusing on ANY excess left every partial grant permanently pending,
        # even one that granted real commands (e.g. browser-only): the
        # always-declared noise alone tripped this gate.
        #
        # Tolerate exactly that known noise as long as the owner granted
        # something for this device; an all-denied grant still gets nothing,
        # and an excess containing anything unrecognised (a future plugin
        # declaring its own unconditional command, e.g. google-meet/ollama)
        # still leaves the surface pending rather than silently widen it.
        #
        # This is an approximation, not an exact per-command grant.
        # node.pair.approve's params are a closed {requestId} schema (no
        # commands field -- packages/gateway-protocol/src/schema/nodes.ts
        # NodePairApproveParamsSchema), so approving hands the node everything
        # it declared, filtered only by the container-wide command allowlist,
        # never by this per-owner grant. Two of the five unconditional
        # commands (system.execApprovals.get/set) are inert regardless: the
        # node.invoke handler hard-refuses them directly (points callers at
        # exec.approvals.node.*), and that RPC family requires operator.admin,
        # which only this backend's own pooled connection holds -- the agent
        # can never reach them. The other three (fs.listDir, terminal.upload,
        # browser.proxy.upload.v1) ARE agent-invocable, so on a partial grant
        # missing their family they can ride along live beyond what was
        # granted until the next container boot, when the SQLite import
        # applies this device's exact entry commands with no such slack.
        if excess and (not allowed or excess - NODE_COMMANDS_DECLARED_UNCONDITIONALLY):
            logger.warning(
                "approve_pending_node_surface: node=%s owner=%s declares commands beyond the "
                "owner's grant (%s) — leaving the surface pending",
                device_id[:16],
                owner_id,
                sorted(excess),
            )
            put_metric("desktop_pair.node_surface_exceeds_grant")
            return False
        await _pool_rpc(
            owner_id=owner_id,
            method="node.pair.approve",
            params={"requestId": pending["requestId"]},
        )
        logger.info(
            "approve_pending_node_surface: approved surface for node=%s owner=%s (%d commands)",
            device_id[:16],
            owner_id,
            len(declared),
        )
        put_metric("desktop_pair.node_surface_approved")
        return True
    except Exception as e:  # noqa: BLE001 — never fail the connect; boot fold remains the fallback
        logger.warning(
            "approve_pending_node_surface: failed for node=%s owner=%s: %s",
            device_id[:16],
            owner_id,
            e,
        )
        put_metric("desktop_pair.node_surface_approve_failed")
        return False


async def push_desktop_revoke_live(*, owner_id: str, device_id: str) -> bool:
    """Evict a revoked device from the RUNNING gateway's live trust store.

    The boot import is ADD-ONLY (existing SQLite records win over the JSON),
    so removing a device from the backend paired.json never evicts it from
    the gateway — not even at the next container boot. The containers-DDB
    projection removal still blocks the platform WS door, but without this
    the container-side trust entry (tokens and all) survives indefinitely.
    ``device.pair.remove`` also invalidates + disconnects the device's live
    gateway clients server-side, which is stronger than our own force-close.

    Never raises — revoke already force-closes the platform WS and removes
    the authorizer projection, so a failed eviction here degrades, not
    breaks. Returns True iff the device is gone from the live store (removed
    now, or already absent).
    """
    try:
        from core.containers import get_ecs_manager

        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        if not container or not ip:
            return False
        try:
            await _pool_rpc(owner_id=owner_id, method="device.pair.remove", params={"deviceId": device_id})
        except RuntimeError as e:
            if "unknown deviceId" in str(e):
                return True  # already absent — the desired end state
            raise
        logger.info(
            "push_desktop_revoke_live: removed device=%s from live trust store owner=%s",
            device_id[:16],
            owner_id,
        )
        return True
    except Exception as e:  # noqa: BLE001 — never fail the revoke request
        logger.warning(
            "push_desktop_revoke_live: failed for device=%s owner=%s: %s",
            device_id[:16],
            owner_id,
            e,
        )
        put_metric("desktop_pair.live_revoke_failed")
        return False


async def push_desktop_pairing_live(*, owner_id: str, device_ids: set[str]) -> bool:
    """Approve any PENDING gateway pairing requests for freshly paired devices.

    Called by POST /desktop/pair right after the JSON writes succeed. Covers
    the re-pair case: the desktop is already looping on NOT_PAIRED (its retry
    keeps a pending device request alive) and, once device-paired, files a
    pending node surface. The connect-time hooks in ``node_proxy`` cover the
    other ordering (desktop connects after pairing).

    Never raises — a failed push must not fail the pair request; the boot
    import remains the fallback. Returns True iff the push ran to completion
    against a running container (even if there was nothing pending to
    approve).
    """
    from core.containers import get_ecs_manager

    try:
        container, ip = await get_ecs_manager().resolve_running_container(owner_id)
        if not container or not ip:
            logger.info(
                "push_desktop_pairing_live: no running container for owner=%s — boot import will pick the pairing up",
                owner_id,
            )
            return False

        payload = await _pool_rpc(owner_id=owner_id, method="device.pair.list", params={})
        for req in payload.get("pending") or []:
            if not isinstance(req, dict):
                continue
            req_device_id = str(req.get("deviceId", "")).strip()
            request_id = req.get("requestId")
            if req_device_id not in device_ids or not request_id:
                continue
            try:
                await _pool_rpc(owner_id=owner_id, method="device.pair.approve", params={"requestId": request_id})
                logger.info(
                    "push_desktop_pairing_live: approved pending device=%s owner=%s",
                    req_device_id[:16],
                    owner_id,
                )
            except Exception as e:  # noqa: BLE001 — one device's failure must not drop the other's approval
                logger.warning(
                    "push_desktop_pairing_live: device approve failed device=%s owner=%s: %s",
                    req_device_id[:16],
                    owner_id,
                    e,
                )

        for device_id in device_ids:
            await approve_pending_node_surface(owner_id=owner_id, device_id=device_id)

        put_metric("desktop_pair.live_push")
        return True
    except Exception as e:  # noqa: BLE001 — never fail the pair request
        logger.warning("push_desktop_pairing_live: failed for owner=%s: %s", owner_id, e)
        put_metric("desktop_pair.live_push_failed")
        return False
