"""
HTTP routes for API Gateway WebSocket integration.

API Gateway WebSocket converts WebSocket frames into HTTP POST requests:
- $connect  -> POST /ws/connect
- $disconnect -> POST /ws/disconnect
- $default (messages) -> POST /ws/message

Responses are pushed via Management API, not returned in HTTP response body.
"""

import asyncio
import json
import logging
import secrets
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Response

from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool
from core.observability.metrics import put_metric
from core.services.connection_service import ConnectionService, ConnectionServiceError
from core.services.management_api_client import ManagementApiClient
from core.services.node_proxy import (
    handle_node_connect,
    handle_node_message,
    handle_node_disconnect,
    is_node_connection,
    submit_signed_connect as submit_node_signed_connect,
)
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import PaperclipAdminClient, PaperclipApiError
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_agent_lifecycle import (
    create_paperclip_for_chat,
    delete_paperclip_for_chat,
)
from core.services.paperclip_meta import PaperclipMeta, read_from_agent_entry
from core.services.paperclip_openclaw_pairing import (
    pair_with_openclaw,
    resolve_company_id_for_owner,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


# Client IDs that handle their own connect.challenge via the upstream-relay
# path (handle_node_connect). For these clients we MUST NOT send a synthetic
# nonce on $connect -- it would race the real challenge from the upstream
# OpenClaw container and could cause the subprocess to sign the wrong nonce.
NODE_HOST_CLIENT_IDS = frozenset({"node-host", "openclaw-macos"})


# Singleton instances (created lazily with async lock for thread safety)
_connection_service: Optional[ConnectionService] = None
_management_api_client: Optional[ManagementApiClient] = None
_singleton_lock = asyncio.Lock()


async def get_connection_service() -> ConnectionService:
    """Get or create ConnectionService singleton (async-safe)."""
    global _connection_service
    if _connection_service is None:
        async with _singleton_lock:
            if _connection_service is None:
                _connection_service = ConnectionService()
    return _connection_service


async def get_management_api_client() -> ManagementApiClient:
    """Get or create ManagementApiClient singleton (async-safe)."""
    global _management_api_client
    if _management_api_client is None:
        async with _singleton_lock:
            if _management_api_client is None:
                _management_api_client = ManagementApiClient()
    return _management_api_client


async def _safe_record_usage(**kwargs) -> None:
    """Record usage, catching errors so they never propagate."""
    try:
        from core.services.usage_service import record_usage

        await record_usage(**kwargs)
    except Exception:
        logger.exception("Usage recording failed for owner %s", kwargs.get("owner_id"))


async def _send_connect_challenge(connection_id: str) -> None:
    """Send OpenClaw connect.challenge to a newly connected client.

    The control UI SPA expects this event before sending its connect
    handshake.  The chat frontend ignores it (no ``type`` field).
    """
    try:
        management_api = await get_management_api_client()
        management_api.send_message(
            connection_id,
            {
                "type": "event",
                "event": "connect.challenge",
                "payload": {"nonce": secrets.token_urlsafe(16)},
            },
        )
    except Exception as e:
        logger.warning("Failed to send connect.challenge to %s: %s", connection_id, e)


@router.post(
    "/connect",
    status_code=200,
    summary="Handle WebSocket connect",
    description="Called by API Gateway on $connect. Stores connection state in DynamoDB.",
    operation_id="ws_connect",
    responses={
        400: {"description": "Missing x-connection-id header"},
        401: {"description": "Missing x-user-id header"},
    },
)
async def ws_connect(
    background_tasks: BackgroundTasks,
    x_connection_id: Optional[str] = Header(None, alias="x-connection-id"),
    x_user_id: Optional[str] = Header(None, alias="x-user-id"),
    x_org_id: Optional[str] = Header(None, alias="x-org-id"),
    x_client_id: Optional[str] = Header(None, alias="x-client-id"),
) -> Response:
    if not x_connection_id:
        raise HTTPException(status_code=400, detail="Missing x-connection-id header")
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing x-user-id header")

    logger.info(
        "WS connect: conn=%s user=%s org=%s owner=%s",
        x_connection_id[:12],
        x_user_id,
        x_org_id or "-",
        x_org_id or x_user_id,
    )

    is_node_host = x_client_id in NODE_HOST_CLIENT_IDS
    # Route by owner_id: org_id for org members, user_id for personal.
    owner_id = x_org_id or x_user_id

    connection_service = await get_connection_service()
    connection_service.store_connection(
        connection_id=x_connection_id,
        user_id=x_user_id,
        org_id=x_org_id,
        connection_type="node" if is_node_host else "chat",
    )

    try:
        pool = get_gateway_pool()
        # Pass member_id so the pool can route streaming events to the
        # specific org member who initiated each chat, not broadcast to
        # all members. For personal users member_id == owner_id so it's
        # harmless (the filter matches all connections).
        pool.add_frontend_connection(owner_id, x_connection_id, member_id=x_user_id)
    except Exception as e:
        logger.warning("Failed to register frontend connection with pool: %s", e)

    # Branch on client type:
    #
    # - Browser / control UI: send a synthetic connect.challenge so the
    #   SPA can complete its handshake. Chat frontend silently ignores it.
    #
    # - node-host clients (Tauri proxy / openclaw subprocess): start the
    #   upstream-container handshake ourselves. The subprocess uses the
    #   OpenClaw v3 server-initiated handshake (it waits for
    #   `connect.challenge` from the server before sending anything), so
    #   SOMETHING has to open the upstream container WS to surface that
    #   challenge. Previously the Tauri proxy injected a wake-up `connect`
    #   to trigger this — but that frame collided with the subprocess's
    #   own signed `connect`, with the backend keying `hello-ok` to the
    #   wake-up id instead of the subprocess's pending id, silently
    #   timing out the handshake (Codex round-3 P1 on PR #667). The
    #   proxy is now a transparent relay; we kick off
    #   `handle_node_connect` here on `$connect` and the subprocess's
    #   signed-connect frame is the FIRST and ONLY `connect` frame the
    #   `$default` handler sees.
    if is_node_host:
        background_tasks.add_task(
            _kick_off_node_connect,
            connection_id=x_connection_id,
            owner_id=owner_id,
            user_id=x_user_id,
            client_id=x_client_id or "node-host",
        )
    else:
        background_tasks.add_task(_send_connect_challenge, x_connection_id)

    return Response(status_code=200)


async def _kick_off_node_connect(
    *,
    connection_id: str,
    owner_id: str,
    user_id: str,
    client_id: str,
) -> None:
    """Start the per-user node-host upstream handshake on `$connect`.

    Replaces the legacy "wake-up connect" trigger. See ws_connect comment
    for the rationale.

    Errors are swallowed (logged): a failed handshake should not bring
    down the WS connection. The subprocess will reconnect via its own
    retry loop, and the next `$connect` will retry this path.
    """
    try:
        management_api = await get_management_api_client()
        # The handler builds the connect_params shape itself from a minimal
        # seed. `client.id` is the discriminator that slots the device into
        # the cli/mac bucket in `_user_nodes` — feed the value we already
        # have from the `$connect` header.
        connect_params = {
            "role": "node",
            "client": {"id": client_id, "mode": "node"},
        }
        await handle_node_connect(
            owner_id=owner_id,
            user_id=user_id,
            connection_id=connection_id,
            connect_params=connect_params,
            management_api=management_api,
        )
    except Exception as e:
        logger.warning(
            "node-host connect failed for user=%s conn=%s: %s",
            user_id,
            connection_id[:12],
            e,
        )


@router.post(
    "/disconnect",
    status_code=200,
    summary="Handle WebSocket disconnect",
    description="Called by API Gateway on $disconnect. Removes connection state from DynamoDB.",
    operation_id="ws_disconnect",
)
async def ws_disconnect(
    x_connection_id: Optional[str] = Header(None, alias="x-connection-id"),
) -> Response:
    if not x_connection_id:
        return Response(status_code=200)

    logger.info("WebSocket disconnect: connection_id=%s", x_connection_id)

    # Unregister from gateway connection pool
    try:
        connection_service = await get_connection_service()
        connection = connection_service.get_connection(x_connection_id)
        if connection:
            owner_id = connection.get("org_id") or connection["user_id"]
            user_id = connection["user_id"]

            # Always unregister from the frontend fanout pool: every WS
            # (node OR chat) gets added in ws_connect, so every close path
            # must remove it. Previously the node branch skipped this call
            # and dead sockets lingered in _frontend_connections, causing
            # every subsequent broadcast to retry a doomed send.
            pool = get_gateway_pool()
            pool.remove_frontend_connection(owner_id, x_connection_id)

            # Tear down any teams-event subscription this conn had. Without
            # this, a tab crash / network drop leaves a dead conn-id in the
            # broker's subscriber set, blocking grace teardown of the
            # backing Paperclip WS. The browser SHOULD send teams.unsubscribe
            # on unmount but can't be relied on.
            from core.services import teams_event_broker_singleton

            broker = teams_event_broker_singleton.get_broker()
            if broker is not None:
                try:
                    await broker.unsubscribe(user_id, x_connection_id)
                except Exception:
                    logger.exception(
                        "teams broker disconnect cleanup failed user=%s conn=%s",
                        user_id,
                        x_connection_id[:12] if x_connection_id else "?",
                    )

            # Additionally tear down the node upstream + per-user state
            # if this was a node connection.
            if is_node_connection(x_connection_id):
                await handle_node_disconnect(x_connection_id, owner_id, user_id)
    except Exception as e:
        logger.warning("Failed to unregister frontend connection from pool: %s", e)

    try:
        connection_service = await get_connection_service()
        connection_service.delete_connection(x_connection_id)
    except ConnectionServiceError as e:
        logger.warning("Failed to delete connection %s: %s", x_connection_id, e)
    except Exception as e:
        logger.exception("Unexpected error deleting connection %s: %s", x_connection_id, e)

    return Response(status_code=200)


@router.post(
    "/message",
    status_code=200,
    summary="Handle WebSocket message",
    description="Called by API Gateway on $default. Processes agent chat messages and pushes responses via Management API.",
    operation_id="ws_message",
    responses={
        400: {"description": "Missing x-connection-id header"},
        401: {"description": "Unknown connection (not in DynamoDB)"},
    },
)
async def ws_message(
    body: Dict[str, Any],
    background_tasks: BackgroundTasks,
    x_connection_id: Optional[str] = Header(None, alias="x-connection-id"),
) -> Response:
    if not x_connection_id:
        raise HTTPException(status_code=400, detail="Missing x-connection-id header")

    connection_service = await get_connection_service()
    connection = connection_service.get_connection(x_connection_id)

    if not connection:
        raise HTTPException(status_code=401, detail="Unknown connection")

    user_id = connection["user_id"]
    owner_id = connection.get("org_id") or user_id
    msg_type = body.get("type")

    if msg_type == "ping":
        management_api = await get_management_api_client()
        management_api.send_message(x_connection_id, {"type": "pong"})
        return Response(status_code=200)

    # Teams BFF realtime: browser opts into / out of Paperclip event fanout.
    # Best-effort — if the broker singleton isn't configured (local dev
    # without Paperclip env), 200 silently and /teams just runs without
    # live updates.
    if msg_type in ("teams.subscribe", "teams.unsubscribe"):
        from core.services import teams_event_broker_singleton

        broker = teams_event_broker_singleton.get_broker()
        if broker is None:
            put_metric(
                "teams.dispatch",
                dimensions={"type": msg_type, "outcome": "broker_disabled"},
            )
        else:
            try:
                if msg_type == "teams.subscribe":
                    await broker.subscribe(user_id, x_connection_id)
                else:
                    await broker.unsubscribe(user_id, x_connection_id)
                put_metric(
                    "teams.dispatch",
                    dimensions={"type": msg_type, "outcome": "ok"},
                )
            except Exception:
                logger.exception(
                    "teams broker dispatch failed type=%s user=%s conn=%s",
                    msg_type,
                    user_id,
                    x_connection_id[:12] if x_connection_id else "?",
                )
                put_metric(
                    "teams.dispatch",
                    dimensions={"type": msg_type, "outcome": "error"},
                )
        return Response(status_code=200)

    # Lazy-register the frontend connection with the gateway pool. The
    # WS connection registry is in-memory per backend task. /ws/connect
    # only fires once when the client first opens the WS — but the
    # client's WS lives on API Gateway and survives backend task rollovers
    # (deploys, scaling). Each /ws/message can land on a different task,
    # and any task that handled the original /ws/connect may since be
    # gone. Without this, openclaw streaming events arrive at a task
    # whose pool has empty `_frontend_connections` and the `chunk` /
    # `done` events get silently dropped — chat appears stuck loading.
    #
    # add_frontend_connection is idempotent (set semantics on conn_id);
    # cheap to call on every message.
    try:
        pool = get_gateway_pool()
        pool.add_frontend_connection(owner_id, x_connection_id, member_id=user_id)
    except Exception as e:
        logger.warning(
            "Failed to lazy-register frontend connection user=%s conn=%s: %s",
            user_id,
            x_connection_id[:12] if x_connection_id else "?",
            e,
        )

    # Log all non-ping message types so chat/RPC flow is visible at INFO
    logger.info(
        "[%s] ws msg type=%s owner=%s conn=%s method=%s agent=%s",
        user_id,
        msg_type,
        owner_id,
        x_connection_id[:12] if x_connection_id else "?",
        body.get("method", "-") if msg_type == "req" else "-",
        body.get("agent_id", "-") if msg_type == "agent_chat" else "-",
    )

    if msg_type == "pong":
        return Response(status_code=200)

    if msg_type == "req":
        req_id = body.get("id")
        method = body.get("method")
        params = body.get("params", {})

        if not req_id or not method:
            management_api = await get_management_api_client()
            management_api.send_message(
                x_connection_id,
                {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": {"message": "Missing id or method"},
                },
            )
            return Response(status_code=200)

        # OpenClaw connect handshake.
        if method == "connect":
            connect_params = params or {}
            role = connect_params.get("role", "operator")

            # Node connection: post-PR #667 round-3 pure-relay flow.
            #
            # The subprocess only ever sends ONE `connect` frame: the signed
            # reply to the upstream container's `connect.challenge` (the
            # OpenClaw v3 handshake is server-initiated, see
            # ~/Desktop/openclaw/src/gateway/client.ts:880). The backend
            # started the upstream handshake itself on `$connect` via
            # `_kick_off_node_connect`, so by the time this signed-connect
            # frame arrives there's an awaiting NodeUpstreamConnection
            # ready to consume it. Route it through `submit_signed_connect`
            # and ACK synchronously — `handle_node_connect` is what
            # eventually forwards `hello-ok` DOWN to the client (with the
            # subprocess's own request id, captured during the handshake).
            #
            # The legacy "frame 1: wake-up" path that opened the upstream
            # WS in response to a proxy-injected unsigned `connect` is
            # gone — that frame collided with the subprocess's real
            # signed connect and silently broke JSON-RPC id correlation
            # (Codex round-3 P1 on PR #667).
            if role == "node":
                if is_node_connection(x_connection_id):
                    submit_node_signed_connect(x_connection_id, body)
                    return Response(status_code=200)
                # No upstream pending — either `_kick_off_node_connect`
                # hasn't run yet (very tight race window: the subprocess
                # got past `connect.challenge` before $connect's
                # BackgroundTask scheduled) or it errored out. Log and
                # drop: the subprocess's reconnect loop will retry.
                logger.warning(
                    "Node-role connect frame for connection %s with no upstream — "
                    "dropping (subprocess will retry on next reconnect)",
                    x_connection_id[:12] if x_connection_id else "?",
                )
                return Response(status_code=200)

            # Operator connect — respond with hello-ok locally.
            # Auth is already handled by the Lambda authorizer.
            management_api = await get_management_api_client()
            management_api.send_message(
                x_connection_id,
                {
                    "type": "res",
                    "id": req_id,
                    "ok": True,
                    "payload": {"protocol": 4},
                },
            )
            return Response(status_code=200)

        # Node connections: relay all non-connect messages to upstream
        if is_node_connection(x_connection_id):
            await handle_node_message(x_connection_id, body)
            return Response(status_code=200)

        background_tasks.add_task(
            _process_rpc_background,
            connection_id=x_connection_id,
            user_id=user_id,
            owner_id=owner_id,
            req_id=req_id,
            method=method,
            params=params,
        )
        return Response(status_code=200)

    if msg_type == "usage":
        asyncio.create_task(
            _safe_record_usage(
                owner_id=owner_id,
                user_id=user_id,
                model=body.get("model", "unknown"),
                input_tokens=body.get("inputTokens", 0),
                output_tokens=body.get("outputTokens", 0),
                cache_read=body.get("cacheRead", 0),
                cache_write=body.get("cacheWrite", 0),
            )
        )
        return Response(status_code=200)

    if msg_type == "user_active":
        # Legacy heartbeat from useActivityPing (frontend hook is deleted in
        # the flat-fee cutover but a stray client may still send this). 200 OK
        # so we don't 4xx old browsers; the ping does nothing post-cutover.
        return Response(status_code=200)

    if msg_type == "agent_chat":
        agent_id = body.get("agent_id")
        message = body.get("message")
        # Frontend-minted UUID per user turn. Forwarded to OpenClaw as
        # idempotencyKey so the frontend can match the chat.final event
        # to the user's send and drain its local chat queue. Mirrors
        # upstream ~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:502.
        # Optional for backwards-compat with old frontend builds — when
        # absent, the backend mints a UUID below so OpenClaw's chat.send
        # always carries an idempotencyKey (the field is required by
        # upstream chat.ts:2002 which does `const clientRunId = p.idempotencyKey`).
        client_run_id = body.get("clientRunId")

        if not agent_id or not message:
            management_api = await get_management_api_client()
            # No agent_id to tag — this is a client-validation error,
            # untagged by necessity. Frontend treats untagged errors as
            # broadcast so they still clear the streaming state.
            management_api.send_message(
                x_connection_id,
                {"type": "error", "message": "Missing agent_id or message"},
            )
            return Response(status_code=200)

        # The gateway connection pool's gate_chat (called inside the
        # forwarder) is the authoritative pre-chat budget gate for card-3
        # (bedrock_claude) users; cards 1 + 2 carry their own provider
        # auth and don't need a backend gate.
        background_tasks.add_task(
            _process_agent_chat_background,
            connection_id=x_connection_id,
            user_id=user_id,
            owner_id=owner_id,
            agent_id=agent_id,
            message=message,
            client_run_id=client_run_id,
        )
        return Response(status_code=200)

    # Unknown message type
    management_api = await get_management_api_client()
    management_api.send_message(
        x_connection_id,
        {"type": "error", "message": f"Unknown message type: {msg_type}"},
    )
    return Response(status_code=200)


# =============================================================================
# OpenClaw RPC Proxy
# =============================================================================


async def _intercept_agents_create(
    *,
    connection_id: str,
    req_id: str,
    user_id: str,
    owner_id: str,
    params: dict,
    ip: str,
    token: str,
) -> bool:
    """Intercept /chat's agents.create RPC.

    Performs the Paperclip-first pairing flow:
    1. Create a Paperclip agent (under the owner's company), getting
       a UUID.
    2. Call ``pair_with_openclaw`` to register it in the OWNER's
       container.
    3. Send a ``res`` frame back to the client over the management API
       with the new agent's UUID as ``payload.agentId``.

    Returns ``True`` when the call has been fully consumed (the
    dispatcher should NOT forward to the gateway). Returns ``False``
    when the interception decides to fall through (e.g. the create
    call has no name — let OpenClaw's validator reject so we don't
    duplicate validation here).

    user_id is the authenticated member (creator); owner_id is the
    container owner — these differ for org members.

    Symmetric with the /teams pairing flow in ``routers/teams/agents.py``;
    see ``_resolve_owner_id`` / ``_container_is_reachable`` there for
    rationale on the owner-vs-member split.
    """
    name = (params or {}).get("name", "").strip() if isinstance((params or {}).get("name"), str) else ""
    role = (params or {}).get("role")
    model = (params or {}).get("model")
    if not name:
        # Let OpenClaw's validator reject — we don't duplicate validation.
        return False

    # Pull Paperclip meta out of the RPC params' own ``params`` sub-dict.
    # The chat UI builds this from its AgentPaperclipFields form. Absent
    # for legacy clients → meta=None → role defaults to "general".
    rpc_params_inner = (params or {}).get("params") or {}
    meta: PaperclipMeta | None = (
        read_from_agent_entry({"params": rpc_params_inner})
        if isinstance(rpc_params_inner, dict) and rpc_params_inner.get("paperclip")
        else None
    )

    paperclip_agent = await create_paperclip_for_chat(
        creator_user_id=user_id,
        owner_id=owner_id,
        name=name,
        role=role,
        model=model,
        meta=meta,
    )

    # pair_with_openclaw issues both agents.create and agents.update RPCs
    # to the gateway, registering the agent under its Paperclip UUID
    # with the friendly display name.
    await pair_with_openclaw(
        owner_id=owner_id,
        paperclip_agent=paperclip_agent,
        gateway_pool=get_gateway_pool(),
        ip=ip,
        token=token,
    )

    # Persist the Paperclip meta into openclaw.json so the marketplace
    # publish slice extraction picks it up later. Best-effort: failures
    # log but don't break the chat-side agent create flow (Paperclip
    # already has the data via the create call above, which is the
    # user-visible source of truth for Friday's roster).
    if meta is not None:
        try:
            from core.services.config_patcher import apply_paperclip_meta

            meta_block: dict = {"role": meta.role}
            if meta.title:
                meta_block["title"] = meta.title
            if meta.capabilities:
                meta_block["capabilities"] = meta.capabilities
            if meta.icon:
                meta_block["icon"] = meta.icon
            await apply_paperclip_meta(
                owner_id,
                agent_id=paperclip_agent["id"],
                meta=meta_block,
            )
        except Exception as exc:
            logger.warning(
                "[%s] config_patcher apply_paperclip_meta failed agent=%s err=%s",
                owner_id,
                paperclip_agent.get("id"),
                exc,
            )

    # Surface the new agent's UUID back to the frontend over the
    # management API. The frontend treats this as the resolution of the
    # original ``agents.create`` req — it will use ``agentId`` to start
    # chats against the new agent.
    #
    # SECURITY: Paperclip echoes the full stored agent record (including
    # ``adapterConfig.authToken`` — the freshly minted OpenClaw service
    # token) on create. We MUST redact those adapter fields before
    # sending over the WS to the browser; otherwise the token leaks
    # into the DOM. Mirrors the ``_redact_agent`` strip used by every
    # /teams read path — see ``routers/teams/agents.py:_redact_agent``.
    from routers.teams.agents import _redact_agent

    redacted_agent = _redact_agent(paperclip_agent)
    management_api = await get_management_api_client()
    management_api.send_message(
        connection_id,
        {
            "type": "res",
            "id": req_id,
            "ok": True,
            "payload": {
                "agentId": paperclip_agent["id"],
                "agent": redacted_agent,
            },
        },
    )
    return True


async def _intercept_agents_update(
    *,
    owner_id: str,
    params: dict,
) -> bool:
    """Mirror Paperclip-relevant fields from an agents.update RPC over
    to Paperclip's PATCH /agents.

    Does NOT consume the RPC — returns False so the dispatcher still
    forwards to OpenClaw (the user's update probably includes
    OpenClaw-only fields like model / identity / skills which only
    OpenClaw cares about).

    Best-effort: a Paperclip PATCH failure is logged but never raised.
    OpenClaw is the source of truth for the agent's runtime state; if
    Paperclip's row drifts, the next manual edit or a reconciliation
    job can re-sync.
    """
    rpc_params_inner = (params or {}).get("params") or {}
    if not isinstance(rpc_params_inner, dict) or not rpc_params_inner.get("paperclip"):
        return False  # No meta in payload — nothing to mirror.

    meta = read_from_agent_entry({"params": rpc_params_inner})
    agent_id = (params or {}).get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return False

    try:
        repo = PaperclipRepo(table_name="paperclip-companies")
        company_id = await resolve_company_id_for_owner(owner_id=owner_id, repo=repo)
        if company_id:
            async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as http:
                admin = PaperclipAdminClient(http_client=http)
                cookie = await get_admin_session_cookie(http)
                await admin.update_agent(
                    session_cookie=cookie,
                    company_id=company_id,
                    agent_id=agent_id,
                    role=meta.role,
                    title=meta.title,
                    capabilities=meta.capabilities,
                    icon=meta.icon,
                )
    except (PaperclipApiError, httpx.HTTPError, RuntimeError) as e:
        logger.warning(
            "[%s] paperclip update_agent mirror failed agent=%s err=%s",
            owner_id,
            agent_id,
            e,
        )

    # Persist params.paperclip into openclaw.json on EFS so the
    # marketplace publish slice extraction picks up the latest meta.
    # Best-effort like the Paperclip PATCH — failures log but don't
    # block the user-facing OpenClaw update.
    try:
        from core.services.config_patcher import apply_paperclip_meta

        meta_block: dict = {"role": meta.role}
        if meta.title:
            meta_block["title"] = meta.title
        if meta.capabilities:
            meta_block["capabilities"] = meta.capabilities
        if meta.icon:
            meta_block["icon"] = meta.icon
        await apply_paperclip_meta(
            owner_id,
            agent_id=agent_id,
            meta=meta_block,
        )
    except Exception as exc:
        logger.warning(
            "[%s] config_patcher apply_paperclip_meta failed agent=%s err=%s",
            owner_id,
            agent_id,
            exc,
        )

    # Strip the Paperclip-only ``params`` block before the dispatcher
    # forwards this RPC to OpenClaw. OpenClaw's ``agents.update`` AJV
    # schema is ``additionalProperties: false`` and only accepts
    # ``agentId, name, workspace, model, emoji, avatar`` — leaving
    # ``params`` in would 422 the whole call and surface as a save
    # error to the user. After stripping, if the user only changed
    # paperclip fields the forwarded RPC is just ``{agentId}`` which
    # OpenClaw accepts as a no-op. Codex P1 on PR #720.
    if isinstance(params, dict) and "params" in params:
        del params["params"]
    return False


async def _process_rpc_background(
    connection_id: str,
    user_id: str,
    owner_id: str,
    req_id: str,
    method: str,
    params: dict,
) -> None:
    """Process an OpenClaw RPC request via the gateway connection pool."""
    # Log non-noisy RPCs at INFO (skip health/channels.status which fire every 3s)
    if method not in ("health", "channels.status"):
        logger.info("[%s] rpc %s owner=%s conn=%s", user_id, method, owner_id, connection_id[:12])
    management_api = await get_management_api_client()

    try:
        ecs_manager = get_ecs_manager()
        container, ip = await ecs_manager.resolve_running_container(owner_id)

        if not container:
            management_api.send_message(
                connection_id,
                {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": {"message": "No container provisioned."},
                },
            )
            return

        if not ip:
            management_api.send_message(
                connection_id,
                {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": {"message": "Container is starting up. Try again in a moment."},
                },
            )
            return

        # Paperclip-first pairing for /chat agents.create. The interception
        # mints a Paperclip UUID, calls pair_with_openclaw to register it
        # in the owner's container, and emits the WS response itself. When
        # it consumes the call we must NOT forward to the gateway — pairing
        # already issued the underlying agents.create + agents.update RPCs
        # under the canonical UUID, and forwarding the original (browser-
        # supplied, friendly-named) request would create a phantom second
        # agent. Symmetric with /teams's create_agent route.
        if method == "agents.create":
            try:
                consumed = await _intercept_agents_create(
                    connection_id=connection_id,
                    req_id=req_id,
                    user_id=user_id,
                    owner_id=owner_id,
                    params=params,
                    ip=ip,
                    token=container["gateway_token"],
                )
            except Exception as exc:
                logger.exception(
                    "[%s] agents.create interception failed owner=%s err=%s",
                    user_id,
                    owner_id,
                    exc,
                )
                try:
                    management_api.send_message(
                        connection_id,
                        {
                            "type": "res",
                            "id": req_id,
                            "ok": False,
                            "error": {"message": f"Failed to create agent: {exc}"},
                        },
                    )
                except Exception:
                    pass
                return
            if consumed:
                return
            # else fall through to forward the original RPC (validator-reject path).

        if method == "agents.update":
            try:
                consumed = await _intercept_agents_update(
                    owner_id=owner_id,
                    params=params,
                )
            except Exception as exc:
                # Defensive: never let our mirror code crash the RPC.
                logger.exception(
                    "[%s] agents.update interception failed: %s",
                    user_id,
                    exc,
                )
                consumed = False
            # update interceptor always returns False — fall through
            # to the gateway forwarding below.
            if consumed:
                return

        pool = get_gateway_pool()
        result = await pool.send_rpc(
            user_id=owner_id,
            req_id=req_id,
            method=method,
            params=params,
            ip=ip,
            token=container["gateway_token"],
        )
        management_api.send_message(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": True,
                "payload": result,
            },
        )

        # Reconcile EFS after a successful agents.delete: OpenClaw's
        # moveToTrashBestEffort silently fails on Linux containers (cross-device
        # rename from EFS to the local overlay's $HOME/.Trash), leaking the
        # agent's on-EFS dirs forever. Clean them up from the backend.
        #
        # `cleanup_agent_dirs` is sync (`shutil.rmtree`) and EFS-backed dirs
        # can be large enough to block the event loop and stall unrelated
        # WS chat traffic, so it runs on a worker thread via `to_thread`.
        if method == "agents.delete":
            agent_id = (params or {}).get("agentId")
            if isinstance(agent_id, str) and agent_id:
                try:
                    from core.containers import get_workspace

                    await asyncio.to_thread(get_workspace().cleanup_agent_dirs, owner_id, agent_id)
                except Exception as cleanup_exc:
                    logger.warning(
                        "[%s] post-delete cleanup failed for agent=%s: %s",
                        owner_id,
                        agent_id,
                        cleanup_exc,
                    )

                # Cascade-delete the Paperclip row. Container provisioning
                # repopulates ``openclaw.json`` from
                # ``fetch_paperclip_agents_for_owner``, so leaving the
                # Paperclip record behind would resurrect the supposedly-
                # deleted agent on the next reprovision. Idempotent: a
                # 404 (legacy 'main' agent that was never in Paperclip,
                # or already-deleted row) is swallowed by
                # ``PaperclipAdminClient.delete_agent`` itself.
                try:
                    await delete_paperclip_for_chat(
                        owner_id=owner_id,
                        agent_id=agent_id,
                    )
                except Exception as paperclip_exc:
                    # Non-fatal: OpenClaw + EFS already cleaned. The next
                    # reprovision rewrites openclaw.json from the live
                    # Paperclip list — if the row truly is still there,
                    # the resurrected agent will surface in /teams and a
                    # subsequent delete attempt can reconcile.
                    logger.warning(
                        "[%s] post-delete paperclip delete failed for agent=%s: %s",
                        owner_id,
                        agent_id,
                        paperclip_exc,
                    )

    except RuntimeError as e:
        # RuntimeError comes from OpenClaw rejecting the RPC — forward full error object.
        # connection_pool serializes the gateway error dict as JSON in the message.
        logger.warning("RPC %s rejected for user %s: %s", method, user_id, e)
        try:
            try:
                error_obj = json.loads(str(e))
            except (ValueError, TypeError):
                error_obj = {"message": str(e)}
            management_api.send_message(
                connection_id,
                {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": error_obj,
                },
            )
        except Exception:
            pass
    except Exception as e:
        # Enriched failure log so we can distinguish transient vs persistent
        # gateway issues across requests. Include error class + last known
        # heartbeat so a wedged container is obvious in CloudWatch Insights:
        # "where last_gateway_handshake_at older than 5 min and error_class
        # is TimeoutError".
        last_handshake = "?"
        try:
            from core.repositories import container_repo as _cr

            _row = await _cr.get_by_owner_id(owner_id)
            if _row:
                last_handshake = _row.get("last_gateway_handshake_at") or "never"
        except Exception:
            pass
        logger.error(
            "RPC %s failed for user %s error_class=%s last_handshake=%s error=%s",
            method,
            user_id,
            type(e).__name__,
            last_handshake,
            e,
        )
        try:
            management_api.send_message(
                connection_id,
                {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": {"message": f"Internal error: {e}"},
                },
            )
        except Exception:
            pass


# =============================================================================
# Agent Chat (Streaming, Plaintext)
# =============================================================================


async def _process_agent_chat_background(
    connection_id: str,
    user_id: str,
    owner_id: str,
    agent_id: str,
    message: str,
    client_run_id: str | None = None,
) -> None:
    """
    Process agent chat message via OpenClaw's native chat.send RPC.

    Sends the message through the persistent WebSocket connection pool.
    The RPC returns immediately with an ack; streaming response events
    (text_delta, turn_completed, etc.) are handled by the pool's reader
    task and forwarded to the frontend automatically.

    client_run_id (optional): frontend-minted UUID for this user turn.
    When provided, forwarded to OpenClaw's chat.send as ``idempotencyKey``
    so the frontend can match the eventual chat.final / aborted / error
    event back to the originating send (drains the client-side chat queue).
    Absent → backend mints a UUID instead so the chat.send call always
    carries an idempotencyKey (preserves upstream's dedupe contract).
    """
    logger.info(
        "[%s] chat.send START agent=%s owner=%s conn=%s msg_len=%d",
        user_id,
        agent_id,
        owner_id,
        connection_id[:12],
        len(message),
    )

    management_api = await get_management_api_client()

    try:
        # Look up user's container and discover task IP
        ecs_manager = get_ecs_manager()
        container, ip = await ecs_manager.resolve_running_container(owner_id)

        if not container:
            put_metric("chat.error", dimensions={"reason": "container_unreachable"})
            logger.warning("[%s] chat.send FAIL: no container for owner=%s", user_id, owner_id)
            management_api.send_message(
                connection_id,
                {
                    "type": "error",
                    "agent_id": agent_id,
                    "message": "No container provisioned. Please subscribe to start chatting.",
                },
            )
            return

        if not ip:
            put_metric("chat.error", dimensions={"reason": "container_unreachable"})
            logger.warning("[%s] chat.send FAIL: no IP for owner=%s (container starting)", user_id, owner_id)
            management_api.send_message(
                connection_id,
                {
                    "type": "error",
                    "agent_id": agent_id,
                    "message": "Your agent is starting up. Please try again in a moment.",
                },
            )
            return

        pool = get_gateway_pool()
        req_id = str(uuid4())

        # Session key format: agent:{agentId}:{userId}
        # Every user gets their own session, isolating their chat history
        # from cron jobs, channels, and other system activity.
        session_key = f"agent:{agent_id}:{user_id}"

        logger.info(
            "[%s] chat.send RPC agent=%s sessionKey=%s ip=%s",
            user_id,
            agent_id,
            session_key,
            ip,
        )

        # --- Node binding: pin this session to the user's CLI device if connected ---
        # Task 13: exec is bound to the CLI bucket specifically. The Mac
        # companion (clientId=openclaw-macos) is a sibling node for
        # passkey/UI affordances, but execNode must point at the CLI.
        from core.services.node_proxy import (
            get_user_cli_node,
            get_patched_session,
            set_patched_session,
        )

        node_info = get_user_cli_node(user_id)
        if node_info:
            node_id = node_info["deviceId"]
            cached = get_patched_session(session_key)
            if cached != node_id:
                # Patch the session so the agent KNOWS which node to use
                # when it chooses host=node — but DON'T force every exec
                # through the node. Setting execHost="node" pinned the
                # whole session to the user's Mac, which:
                #   1. routed trivial commands (echo, whoami) over the
                #      WS to the Mac for no benefit, and
                #   2. forced every exec through openclaw's exec-approval
                #      flow, which is upstream-broken for the webchat
                #      surface (APPROVAL_CLIENT_MISMATCH — see upstream
                #      bug filed by us).
                # Setting execNode without execHost lets exec.host
                # default to "auto" (exec-defaults.ts:47): the agent
                # picks per-command. When it explicitly targets node,
                # execNode is already bound. When it picks sandbox or
                # gateway, no node round-trip + no approval gate.
                # req_id MUST be unique per call — the connection pool keys
                # pending-response futures by req_id and a duplicate ID
                # orphans the earlier future, hanging one caller 30s.
                try:
                    await pool.send_rpc(
                        user_id=owner_id,
                        req_id=f"bind-node-{uuid4()}",
                        method="sessions.patch",
                        # OpenClaw's sessions.patch takes `key` (see
                        # openclaw/src/gateway/server-methods/sessions.ts:1262).
                        # chat.send still uses `sessionKey` — that's a separate
                        # schema that didn't change.
                        params={
                            "key": session_key,
                            "execNode": node_id,
                            # Explicitly clear any stale execHost pin.
                            # Sessions that were patched before this
                            # change still have execHost="node" on
                            # disk; session.patch only updates fields
                            # present in params, so omitting this
                            # would leave them pinned. None resets
                            # back to global default (auto).
                            "execHost": None,
                        },
                        ip=ip,
                        token=container["gateway_token"],
                    )
                    set_patched_session(session_key, node_id)
                    logger.info("Bound session %s to node %s (host=auto)", session_key, node_id[:16])
                except Exception:
                    logger.warning("Failed to bind session %s to node", session_key)

        # Plan 3 Task 4 + audit M1: card-3 hard-stop when balance ≤ 0,
        # AND any provider hard-stop when subscription is inactive.
        # provider_choice + credit balance live on the Clerk user row,
        # so we pass user_id; subscription_status lives on the billing
        # row (org_id in org context, user_id in personal), so we also
        # pass owner_id. Cards 1 + 2 still skip the credit-balance gate
        # — they aren't billed against prepaid credits — but they DO
        # have to honor the subscription gate so a canceled-sub user
        # can't keep talking through the gateway indefinitely.
        gate = await pool.gate_chat(user_id=user_id, owner_id=owner_id)
        if gate.get("blocked"):
            put_metric("chat.error", dimensions={"reason": gate.get("code", "blocked")})
            logger.info(
                "[%s] chat.send BLOCKED agent=%s code=%s",
                user_id,
                agent_id,
                gate.get("code"),
            )
            try:
                management_api.send_message(
                    connection_id,
                    {
                        "type": "error",
                        "code": gate.get("code", "blocked"),
                        "message": gate.get("message", "Chat blocked"),
                        "agent_id": agent_id,
                    },
                )
            except Exception:
                logger.warning("[%s] failed to deliver blocked-chat error event", user_id)
            return

        # Open the active-run window BEFORE forwarding chat.send. If the WS
        # to OpenClaw tears down mid-turn (ping timeout during a long Bedrock
        # call), the pool's parked-vs-dead decision in `_on_reader_loop_exit`
        # keys on whether `_active_runs` is non-empty — so marking here, not
        # after the ack, is what keeps the connection in the PARKED branch
        # during the streaming window. Idempotent for the cancel-and-retry
        # race where the same sessionKey starts a fresh run.
        pool.mark_run_active(owner_id, session_key, agent_id)

        # Idempotency key for OpenClaw's chat.send. We always send one so
        # upstream's dedupe (`context.dedupe.get(`chat:${clientRunId}`)`) and
        # in-flight detection (`context.chatAbortControllers.get(clientRunId)`)
        # in ~/Desktop/openclaw/src/gateway/server-methods/chat.ts:2037-2052
        # have a stable key to match against. When the frontend supplies
        # `clientRunId`, we use it so the frontend can correlate the eventual
        # chat.final back to its send (drives the local chat queue drain).
        # When absent (old client), we mint a backend UUID — matches the
        # pre-clientRunId behavior. Upstream chat.ts:2174 calls
        # `context.addChatRun(clientRunId, ...)` with whichever value we pass.
        chat_send_params: dict[str, str] = {
            "sessionKey": session_key,
            "message": message,
            "idempotencyKey": client_run_id or str(uuid4()),
        }

        result = await pool.send_rpc(
            user_id=owner_id,
            req_id=req_id,
            method="chat.send",
            params=chat_send_params,
            ip=ip,
            token=container["gateway_token"],
        )
        logger.info("[%s] chat.send ACK agent=%s result=%s", user_id, agent_id, result)
        # Streaming response events are forwarded by the connection pool's reader task

    except asyncio.TimeoutError:
        chat_err = "RPC timed out"
        put_metric("chat.error", dimensions={"reason": "timeout"})
        logger.error("[%s] chat.send TIMEOUT agent=%s", user_id, agent_id)
    except ConnectionError as e:
        chat_err = str(e)
        put_metric("chat.error", dimensions={"reason": "container_unreachable"})
        logger.error("[%s] chat.send CONNECT FAIL agent=%s: %s", user_id, agent_id, e)
    except RuntimeError as e:
        chat_err = str(e)
        put_metric("chat.error", dimensions={"reason": "gateway_error"})
        logger.error("[%s] chat.send GATEWAY ERROR agent=%s: %s", user_id, agent_id, e)
    except Exception as e:
        chat_err = str(e)
        put_metric("chat.error", dimensions={"reason": "unknown"})
        logger.error("[%s] chat.send FAIL agent=%s: %s", user_id, agent_id, e)
    else:
        return  # success — no error to report

    # All error branches fall through here to notify the client
    try:
        management_api.send_message(
            connection_id,
            {
                "type": "error",
                "agent_id": agent_id,
                "message": f"Failed to send message: {chat_err}",
            },
        )
    except Exception:
        pass
