"""
Operator connection relay — Paperclip adapter → per-user OpenClaw container.

A Paperclip agent's ``openclaw_gateway`` adapter connects to the Isol8 WS API
Gateway with ``clientId=paperclip-operator``. Unlike a browser / control-UI
operator (which only needs a cosmetic ``hello-ok`` because it talks to the
backend's RPC proxy, not the container directly), the adapter runs a REAL
OpenClaw device-auth handshake against the container: it waits for the
container's ``connect.challenge`` nonce, signs it with its
``devicePrivateKeyPem``, then drives agent runs over the same socket.

None of that works against a synthetic local ``hello-ok``: the container never
sees the handshake, so the adapter's ``agent`` request frames (the heartbeats /
wake runs) have nowhere to land. That synthetic-stub gap is exactly why
Paperclip agents could never connect to their container.

This module makes the backend a transparent protocol relay between the adapter
and the owner's container, reusing the proven verbatim relay built for the
desktop node-host (``NodeUpstreamConnection`` is role-agnostic — it forwards
whatever signed connect the client produces and never assumes ``role:"node"``).
The relay:

  1. opens an upstream WS to the owner's container on ``$connect``,
  2. forwards the container's real ``connect.challenge`` DOWN to the adapter,
  3. forwards the adapter's signed ``connect`` UP to the container,
  4. forwards ``hello-ok`` DOWN (rewritten to the adapter's connect request id
     so its pending-request map matches), then
  5. relays every subsequent frame verbatim in both directions — raw
     ``event:"agent"`` frames included. The adapter consumes the native
     OpenClaw event shape, so we deliberately do NOT run the webchat
     ``_transform_agent_event`` transform here.

It intentionally skips the node-host machinery (cli/mac device buckets,
exec/execNode session pinning, ``node_status`` UI broadcasts, ref-counted
``tools`` config patching): a Paperclip operator is a headless agent runner,
not a user's local tool host.
"""

import asyncio
import logging

from core.containers import get_ecs_manager
from core.gateway.node_connection import NodeUpstreamConnection
from core.observability.metrics import put_metric

logger = logging.getLogger(__name__)

# connection_id -> NodeUpstreamConnection (reused as a role-agnostic relay).
_operator_upstreams: dict[str, NodeUpstreamConnection] = {}


async def handle_operator_connect(
    *,
    owner_id: str,
    user_id: str,
    connection_id: str,
    connect_params: dict,
    management_api,
) -> dict | None:
    """Open the upstream relay to the owner's container and run the handshake.

    Returns the ``hello-ok`` dict on success. On any failure the
    pre-registered upstream is rolled back and the error re-raised to the
    caller (``_kick_off_operator_connect``, which logs + swallows — the
    adapter reconnects via its own retry loop).
    """
    ecs = get_ecs_manager()
    container, ip = await ecs.resolve_running_container(owner_id)

    upstream = NodeUpstreamConnection(
        user_id=user_id,
        owner_id=owner_id,
        container_ip=ip,
        node_connect_params=connect_params,
        gateway_token=container["gateway_token"],
    )

    async def on_upstream_message(data: dict):
        # send_message returns False when the Management API raises GoneException
        # — the downstream adapter WS is gone. This is the load-bearing reaper
        # for the cross-task disconnect case: API Gateway can route $disconnect
        # to a different backend task than the one holding this upstream, so the
        # explicit teardown is skipped there; the next container->adapter push
        # is what reveals the loss here. Defer the teardown via create_task so
        # we don't cancel the reader loop from inside its own push callback.
        delivered = management_api.send_message(connection_id, data)
        if delivered is False:
            # Gate the reap on confirmed Gone: a lone GoneException can be a
            # spurious API Gateway blip on a live adapter WS, and tearing the
            # relay down on it kills a healthy container->adapter link. An
            # unconfirmed blip just drops this frame; a genuinely gone
            # adapter confirms on a subsequent push.
            if not management_api.gone_confirmed(connection_id):
                logger.warning(
                    "Unconfirmed Gone for operator conn=%s — frame dropped, relay kept",
                    connection_id,
                )
                # Parity with the other two kill-sites
                # (gateway.frontend.gone_unconfirmed,
                # teams.broker.fanout outcome=gone_unconfirmed) so the
                # spurious-410 rate is visible on every path, not two of three.
                put_metric("operator.relay.gone_unconfirmed")
                return
            logger.info(
                "Operator adapter gone for conn=%s; reaping orphaned relay upstream",
                connection_id,
            )
            asyncio.create_task(handle_operator_disconnect(connection_id))

    # Deliver upstream frames DOWN to the adapter: the container's
    # connect.challenge during the handshake, then res/event frames once the
    # handshake completes.
    upstream.set_client_push_callback(on_upstream_message)

    async def on_upstream_closed():
        # If the upstream dies while the adapter WS is still open (container
        # restart, transient drop), tear down our registry entry so the next
        # adapter frame doesn't relay into a dead socket. Idempotent with the
        # adapter-disconnect path via the atomic pop in handle_operator_disconnect.
        logger.info(
            "Operator upstream closed for owner=%s conn=%s; cleaning up",
            owner_id,
            connection_id,
        )
        # The container dropped the relay upstream — the load-bearing signal that
        # a per-user container is too unhealthy to hold a Paperclip agent
        # connection. Surface it as a fleet metric so the relay-churn alarm can
        # fire (owner is a denied high-cardinality dimension; it stays in the
        # structured log above for per-owner drill-down).
        put_metric("operator.upstream.closed")
        await handle_operator_disconnect(connection_id)

    upstream.set_on_upstream_closed(on_upstream_closed)

    # Register BEFORE awaiting connect(): connect() blocks on the adapter's
    # signed connect, which the $default handler routes via
    # submit_operator_signed_connect — that lookup needs
    # _operator_upstreams[connection_id] populated. The adapter provably
    # cannot send its signed connect until it has received the
    # connect.challenge we push from inside connect(), by which point this
    # registration is already in place.
    _operator_upstreams[connection_id] = upstream

    try:
        hello = await upstream.connect()
    except Exception:
        # Roll back so a failed handshake doesn't leave a stale upstream.
        # pop-with-default stays idempotent with the disconnect path.
        _operator_upstreams.pop(connection_id, None)
        # Handshake never completed (connect.challenge timeout, version
        # mismatch, dead container). This is the connect-time counterpart to
        # operator.upstream.closed.
        put_metric("operator.connect.failed")
        try:
            await upstream.close()
        except Exception:
            pass
        raise

    # Forward hello-ok DOWN keyed to the adapter's OWN connect request id. The
    # adapter correlates res.id against the uuid it minted for the connect
    # frame; forwarding the container's own id verbatim would miss that
    # pending-request map and silently time out the handshake.
    if upstream.signed_req_id and hello:
        try:
            management_api.send_message(connection_id, {**hello, "id": upstream.signed_req_id})
        except Exception:
            logger.warning(
                "Failed to deliver operator hello-ok owner=%s conn=%s",
                owner_id,
                connection_id,
                exc_info=True,
            )

    await upstream.start_reader()

    logger.info(
        "Operator relay established: owner=%s user=%s conn=%s device=%s",
        owner_id,
        user_id,
        connection_id,
        (upstream.device_id or "?")[:16],
    )
    # Pairs with operator.upstream.closed: a healthy relay establishes once and
    # stays; a high established rate is churn — the container keeps dropping and
    # the adapter keeps reconnecting.
    put_metric("operator.relay.established")
    return hello


async def handle_operator_message(connection_id: str, message: dict) -> None:
    """Relay one adapter frame to the upstream container."""
    upstream = _operator_upstreams.get(connection_id)
    if upstream:
        await upstream.relay_to_upstream(message)
    else:
        logger.warning("No operator upstream for connection %s", connection_id)


async def handle_operator_disconnect(connection_id: str) -> None:
    """Close the upstream relay for a connection.

    Idempotent: the atomic ``pop`` means whichever of the adapter-disconnect
    path and the upstream-died callback runs first does the cleanup; the
    second call is a no-op.
    """
    upstream = _operator_upstreams.pop(connection_id, None)
    if upstream is None:
        return
    try:
        await upstream.close()
    except Exception:
        logger.warning(
            "Error closing operator upstream for %s; continuing",
            connection_id,
            exc_info=True,
        )
    logger.info("Operator relay closed: conn=%s", connection_id)


def is_operator_connection(connection_id: str) -> bool:
    """True if this connection has a registered operator relay upstream."""
    return connection_id in _operator_upstreams


def submit_operator_signed_connect(connection_id: str, signed_payload: dict) -> bool:
    """Route the adapter's signed-connect frame into the pending handshake.

    Called from the relay's $default handler when a ``method=="connect"`` frame
    arrives on an operator relay connection. Returns True if routed, False if
    no upstream exists for this connection.
    """
    upstream = _operator_upstreams.get(connection_id)
    if upstream is None:
        return False
    upstream.submit_signed_connect(signed_payload)
    return True
