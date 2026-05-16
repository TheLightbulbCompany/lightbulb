"""
Node connection proxy — per-user tracking for local tool execution.

Manages dedicated upstream WebSocket connections for node clients.
Each connected desktop app gets its own NodeUpstreamConnection to the
shared container. Tracking is per-user (not per-owner) so that in an
org, Alice's Mac and Bob's Mac are independent.

State:
  _node_upstreams   connection_id -> NodeUpstreamConnection
  _user_nodes       user_id -> {
                        "cli": {deviceId, clientId, connection_id, owner_id, ...},
                        "mac": {deviceId, clientId, connection_id, owner_id, ...},
                    }
                    Each Mac has TWO openclaw nodes (the CLI subprocess +
                    the Tauri-mac companion). The bucket is selected by
                    the clientId from the connect handshake. See spec
                    docs/superpowers/specs/2026-05-15-desktop-node-architecture-migration-design.md.
  _node_count       owner_id -> int (active node connections)
  _patched_sessions session_key -> nodeId (in-memory cache)
"""

import logging
from uuid import uuid4

from core.gateway.node_connection import NodeUpstreamConnection
from core.containers import get_ecs_manager, get_gateway_pool
from core.services.config_patcher import patch_openclaw_config

logger = logging.getLogger(__name__)

# connection_id -> NodeUpstreamConnection
_node_upstreams: dict[str, NodeUpstreamConnection] = {}

# user_id -> {
#   "cli": {"deviceId": str, "clientId": "node-host",      "connection_id": str, "owner_id": str},
#   "mac": {"deviceId": str, "clientId": "openclaw-macos", "connection_id": str, "owner_id": str},
# }
# Per spec 2026-05-15-desktop-node-architecture-migration-design.md:
# each Mac has TWO openclaw nodes (cli subprocess + Tauri-mac companion).
# Slot by clientId from the connect handshake.
_user_nodes: dict[str, dict] = {}

# owner_id -> count of active node connections (for ref-counted config patching)
_node_count: dict[str, int] = {}

# session_key -> nodeId (tracks which sessions have been patched with execNode)
_patched_sessions: dict[str, str] = {}


def get_user_cli_node(user_id: str) -> dict | None:
    """Return the CLI subprocess node info for user, or None if not connected.

    Tolerates the legacy flat `{nodeId, connection_id, owner_id}` shape
    until Task 12 finishes the migration in handle_node_connect: any flat
    entry whose clientId is not `openclaw-macos` is treated as the CLI
    bucket.
    """
    entry = _user_nodes.get(user_id)
    if not entry:
        return None
    if "cli" in entry or "mac" in entry:
        return entry.get("cli")
    # Backward compat: legacy flat shape — treat as cli unless explicitly mac.
    return entry if entry.get("clientId") != "openclaw-macos" else None


def get_user_mac_node(user_id: str) -> dict | None:
    """Return the Tauri-mac companion node info for user, or None if not connected.

    Tolerates the legacy flat shape: an entry whose clientId is
    `openclaw-macos` is treated as the mac bucket.
    """
    entry = _user_nodes.get(user_id)
    if not entry:
        return None
    if "cli" in entry or "mac" in entry:
        return entry.get("mac")
    return entry if entry.get("clientId") == "openclaw-macos" else None


def get_user_node(user_id: str) -> dict | None:
    """Deprecated alias — prefers CLI node (exec-capable) when both kinds are present.

    Use get_user_cli_node / get_user_mac_node when you specifically need
    one or the other. Kept so legacy callers (session-binding,
    handle_node_disconnect's owning-close check) keep working until they
    migrate.
    """
    return get_user_cli_node(user_id) or get_user_mac_node(user_id)


def get_patched_session(session_key: str) -> str | None:
    """Return the nodeId a session is patched with, or None."""
    return _patched_sessions.get(session_key)


def set_patched_session(session_key: str, node_id: str) -> None:
    """Record that a session has been patched with execNode."""
    _patched_sessions[session_key] = node_id


def clear_patched_sessions_for_user(user_id: str) -> list[str]:
    """Remove all patched-session entries for a user. Returns the cleared session keys."""
    cleared = []
    for sk, nid in list(_patched_sessions.items()):
        # Session keys for org members: agent:<agentId>:<userId>
        # We match on the userId segment
        if sk.endswith(f":{user_id}"):
            del _patched_sessions[sk]
            cleared.append(sk)
    return cleared


async def handle_node_connect(
    owner_id: str,
    user_id: str,
    connection_id: str,
    connect_params: dict,
    management_api,
    display_name: str = "Desktop",
) -> dict | None:
    """
    Open a dedicated upstream to the container, complete the node handshake,
    and set up bidirectional relay. Returns the hello-ok dict on success.
    """
    ecs = get_ecs_manager()
    container, ip = await ecs.resolve_running_container(owner_id)

    # Build a displayable name for node.list. Prefer the name the desktop
    # client already sent in connect_params (set by the Tauri IPC from the
    # user's Clerk profile) so each member's node is individually
    # identifiable. Only fall back to `display_name` (or "Desktop") if the
    # client sent nothing.
    client_params = connect_params.get("client", {})
    incoming = (client_params.get("displayName") or "").strip()
    if not incoming:
        incoming = display_name.strip() or "Desktop"
    # Strip any pre-existing " | Isol8 Desktop" suffix so reconnects don't
    # compound "Alice | Isol8 Desktop | Isol8 Desktop".
    if incoming.endswith(" | Isol8 Desktop"):
        incoming = incoming[: -len(" | Isol8 Desktop")]
    client_params["displayName"] = f"{incoming} | Isol8 Desktop"
    connect_params["client"] = client_params

    # user_id (member) vs owner_id (org/container) -- scope the device
    # identity by user_id so org members each get a distinct nodeId. The
    # client (not the backend) signs the connect.challenge using a Keychain-
    # stored Ed25519 key; paired.json registration happens at pairing time
    # via the /desktop/pair endpoint (Task 7), not here.
    upstream = NodeUpstreamConnection(
        user_id=user_id,
        owner_id=owner_id,
        container_ip=ip,
        node_connect_params=connect_params,
        gateway_token=container["gateway_token"],
    )

    async def on_upstream_message(data: dict):
        management_api.send_message(connection_id, data)

    # Push callback delivers upstream frames DOWN to the client via the
    # Management API: container's connect.challenge during the handshake,
    # then node.invoke.request etc. once the handshake completes.
    upstream.set_client_push_callback(on_upstream_message)

    # If the upstream WS dies while the desktop side is still connected
    # (container restart, transient network drop, gateway close), tear
    # down per-user state so agent_chat stops binding sessions to the
    # now-dead nodeId. handle_node_disconnect is idempotent with the
    # eventual desktop-disconnect path.
    async def on_upstream_closed():
        logger.info(
            "Upstream closed for user=%s conn=%s; running cleanup",
            user_id,
            connection_id,
        )
        await handle_node_disconnect(connection_id, owner_id, user_id)

    upstream.set_on_upstream_closed(on_upstream_closed)

    # Register the upstream BEFORE awaiting connect(). connect() blocks on a
    # future that the relay's $default handler resolves via
    # submit_signed_connect when the client's signed connect frame arrives --
    # which requires _node_upstreams[connection_id] to be populated so the
    # routing can find this object. Pre-registering means submit_signed_connect
    # works during the handshake. handle_node_disconnect (idempotent) cleans
    # up on failure.
    _node_upstreams[connection_id] = upstream

    try:
        hello = await upstream.connect()
    except Exception:
        # Roll back the pre-registration so a failed handshake doesn't leave
        # a stale upstream in the registry. Use pop with default to stay
        # idempotent with the disconnect cleanup path.
        _node_upstreams.pop(connection_id, None)
        try:
            await upstream.close()
        except Exception:
            pass
        raise
    await upstream.start_reader()

    # The nodeId in OpenClaw's NodeRegistry is the device.id (SHA-256 hex of
    # the Ed25519 public key), set during the connect handshake inside
    # NodeUpstreamConnection.connect(). Read it back from the upstream.
    node_id = upstream.device_id or connection_id

    # Slot the device into the cli/mac bucket based on the connecting
    # client's advertised clientId. Anything other than "openclaw-macos"
    # goes into the cli bucket (catches "node-host" plus any
    # legacy/unknown clients — safe fallback). See spec
    # docs/superpowers/specs/2026-05-15-desktop-node-architecture-migration-design.md.
    client_id_raw = (connect_params.get("client") or {}).get("id") or "node-host"
    bucket = "mac" if client_id_raw == "openclaw-macos" else "cli"

    user_entry = _user_nodes.setdefault(user_id, {})
    user_entry[bucket] = {
        "deviceId": node_id,
        "clientId": client_id_raw,
        "connection_id": connection_id,
        "owner_id": owner_id,
    }

    # Reference-counted config patching: enable node tools on first connect
    prev_count = _node_count.get(owner_id, 0)
    _node_count[owner_id] = prev_count + 1
    if prev_count == 0:
        await patch_openclaw_config(owner_id, {"tools": {"deny": ["canvas"]}})

    # Per-user broadcast
    pool = get_gateway_pool()
    await pool.broadcast_to_member(
        owner_id,
        user_id,
        {"type": "node_status", "status": "connected"},
    )

    logger.info(
        "Node proxy established: user=%s owner=%s conn=%s nodeId=%s",
        user_id,
        owner_id,
        connection_id,
        node_id,
    )
    return hello


async def handle_node_message(connection_id: str, message: dict) -> None:
    """Relay a message from the node client to the upstream container."""
    upstream = _node_upstreams.get(connection_id)
    if upstream:
        await upstream.relay_to_upstream(message)
    else:
        logger.warning("No node upstream for connection %s", connection_id)


async def handle_node_disconnect(
    connection_id: str,
    owner_id: str,
    user_id: str,
) -> None:
    """Close the upstream connection and update per-user tracking.

    Idempotent: calling twice for the same connection_id is a no-op on
    the second call. This matters because there are now two paths that
    trigger disconnect — the desktop WS dropping (via ws_disconnect) and
    the upstream WS dropping (via NodeUpstreamConnection.on_upstream_closed
    callback). Without idempotency we'd double-decrement _node_count and
    the ref counter would go out of sync.
    """
    upstream = _node_upstreams.pop(connection_id, None)
    # Whoever popped first does the cleanup; everyone else returns here.
    # Safe because _node_upstreams.pop is atomic.
    if upstream is None:
        return
    # Best-effort close: if the upstream errors here (e.g. broken pipe
    # mid-shutdown) we still need to fall through to per-user state
    # teardown so chat stops binding to the dead nodeId. Without this
    # guard the exception would unwind past the _user_nodes cleanup
    # below and leak state.
    try:
        await upstream.close()
    except Exception:
        logger.warning(
            "Error closing upstream for %s; continuing with cleanup",
            connection_id,
            exc_info=True,
        )

    # Only clear per-user state when the stored mapping still points at THIS
    # connection. If the user reconnected first, _user_nodes[user_id] now holds
    # the new connection_id — the old socket's close event must not clobber it,
    # clear its patched sessions, or emit a false "disconnected" to the UI.
    # Traverse both cli/mac buckets to find the bucket whose connection_id
    # matches; only that bucket gets cleared.
    user_entry = _user_nodes.get(user_id) or {}
    matching_bucket = None
    for bkey, binfo in user_entry.items():
        if isinstance(binfo, dict) and binfo.get("connection_id") == connection_id:
            matching_bucket = bkey
            break
    is_owning_close = matching_bucket is not None

    if is_owning_close:
        user_entry.pop(matching_bucket, None)
        if not user_entry:
            _user_nodes.pop(user_id, None)

        # Patched sessions reference the CLI device for exec routing
        # (websocket_chat.py session-binding pins execNode to the CLI
        # bucket — see Task 13). A Mac-only disconnect must NOT clear
        # the pin: the CLI device is still alive and the exec target
        # is still valid.
        if matching_bucket == "cli":
            cleared_sessions = clear_patched_sessions_for_user(user_id)
        else:
            cleared_sessions = []
        if cleared_sessions:
            pool = get_gateway_pool()
            # Best-effort: clear execNode on sessions. If container is down, skip.
            for sk in cleared_sessions:
                try:
                    container, ip = await get_ecs_manager().resolve_running_container(owner_id)
                    # req_id MUST be unique per call — the connection pool's
                    # pending-response map is keyed on it and a duplicate ID
                    # orphans the earlier future, hanging one caller 30s.
                    await pool.send_rpc(
                        user_id=owner_id,
                        req_id=f"clear-exec-{uuid4()}",
                        method="sessions.patch",
                        # OpenClaw's sessions.patch takes `key`, not `sessionKey`
                        # (openclaw/src/gateway/server-methods/sessions.ts:1262).
                        params={"key": sk, "execNode": None, "execHost": None},
                        ip=ip,
                        token=container["gateway_token"],
                    )
                except Exception:
                    logger.debug(
                        "Failed to clear execNode on session %s (container may be down)",
                        sk,
                    )

    # Reference-counted config patching: each upstream socket contributes one,
    # so we always decrement even if a newer connection has taken ownership of
    # the user mapping (the newer one has its own +1 counted at connect time).
    count = _node_count.get(owner_id, 1)
    new_count = max(0, count - 1)
    _node_count[owner_id] = new_count
    if new_count == 0:
        try:
            await patch_openclaw_config(owner_id, {"tools": {"deny": ["canvas", "nodes"]}})
        except Exception:
            logger.debug("Failed to re-disable node tools for %s (container may be down)", owner_id)
        _node_count.pop(owner_id, None)

    # Only broadcast "disconnected" when this close actually ended the user's
    # active node — otherwise the UI flickers to disconnected even though the
    # new socket is live.
    if is_owning_close:
        pool = get_gateway_pool()
        await pool.broadcast_to_member(
            owner_id,
            user_id,
            {"type": "node_status", "status": "disconnected"},
        )

    logger.info(
        "Node proxy closed: user=%s conn=%s owning=%s",
        user_id,
        connection_id,
        is_owning_close,
    )


def is_node_connection(connection_id: str) -> bool:
    """Check if a connection ID has a registered node upstream."""
    return connection_id in _node_upstreams


def submit_signed_connect(connection_id: str, signed_payload: dict) -> bool:
    """Route a client's signed-connect frame to the pending upstream handshake.

    Called from the relay's $default handler when a method=="connect" frame
    arrives on an already-known node connection (the wake-up first connect
    pre-registered the upstream). Returns True if routed, False if no
    upstream exists for this connection.
    """
    upstream = _node_upstreams.get(connection_id)
    if upstream is None:
        return False
    upstream.submit_signed_connect(signed_payload)
    return True


async def force_close_device(device_id: str) -> bool:
    """Force-close the live upstream WS for a given deviceId.

    Routes through handle_node_disconnect so cleanup of per-user state
    (_node_upstreams, _user_nodes bucket, _patched_sessions, _node_count,
    node_status broadcast) happens in exactly one place. A direct
    _node_upstreams.pop here would race the reader's on_upstream_closed
    callback: pop wins, callback re-enters handle_node_disconnect, sees
    upstream is None, returns early — leaving _user_nodes / _node_count
    populated and chat still binding sessions to the dead execNode until
    process restart.

    handle_node_disconnect is idempotent (it does the atomic
    _node_upstreams.pop check at the top) so it's safe whether the
    reader callback or this revoke path runs first.

    Best-effort: errors during cleanup are swallowed; the revocation
    flow shouldn't fail because of a transient socket error. Used by
    the desktop-device revoke endpoint to kick live sessions after
    paired.json removal (security review HIGH finding F).

    Returns True if a paired device with this ID was found and the
    cleanup ran; False otherwise.
    """
    # Find the user+bucket that owns this device_id. _user_nodes is keyed
    # user_id -> {"cli": {...}, "mac": {...}}; legacy flat shape is also
    # tolerated for backward compat with stale entries on a hot path.
    for user_id, slots in list(_user_nodes.items()):
        if not isinstance(slots, dict):
            continue
        # Bucketed shape (post-Task 11): walk cli/mac slots.
        bucketed = "cli" in slots or "mac" in slots
        candidate_slots = [slots.get("cli"), slots.get("mac")] if bucketed else [slots]
        for slot in candidate_slots:
            if not isinstance(slot, dict):
                continue
            if slot.get("deviceId") != device_id:
                continue
            connection_id = slot.get("connection_id")
            owner_id = slot.get("owner_id")
            if not connection_id or not owner_id:
                continue
            try:
                await handle_node_disconnect(
                    connection_id=connection_id,
                    owner_id=owner_id,
                    user_id=user_id,
                )
            except Exception:
                logger.warning(
                    "force_close: error tearing down user=%s conn=%s for device=%s",
                    user_id,
                    connection_id,
                    device_id,
                    exc_info=True,
                )
            return True
    return False
