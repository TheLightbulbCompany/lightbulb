"""
HTTP routes for API Gateway WebSocket integration.

API Gateway WebSocket converts WebSocket frames into HTTP POST requests:
- $connect  -> POST /ws/connect
- $disconnect -> POST /ws/disconnect
- $default (messages) -> POST /ws/message

Responses are pushed via Management API, not returned in HTTP response body.
"""

import asyncio
import hmac
import json
import logging
import re
import secrets
from typing import Any, Dict, Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Response

from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool
from core.observability.logging import bind_log_context
from core.observability.metrics import put_metric
from core.services import run_analytics
from core.services.connection_service import ConnectionService, ConnectionServiceError
from core.services.management_api_client import ManagementApiClient
from core.services.node_proxy import (
    handle_node_connect,
    handle_node_message,
    handle_node_disconnect,
    is_node_connection,
    submit_signed_connect as submit_node_signed_connect,
)
from core.services.operator_proxy import (
    handle_operator_connect,
    handle_operator_message,
    handle_operator_disconnect,
    is_operator_connection,
    submit_operator_signed_connect,
)
from core.services.paperclip_adapter_config import PAPERCLIP_OPERATOR_CLIENT_ID
from core.repositories.paperclip_repo import PaperclipRepo
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipAgentCreateReplayMiss,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_agent_lifecycle import (
    create_paperclip_for_chat,
    delete_paperclip_for_chat,
    delete_runtime_agent_with_lock_retry,
    prepare_paperclip_agent_delete,
)
from core.services.paperclip_meta import PaperclipMeta, read_from_agent_entry
from core.services.paperclip_openclaw_pairing import (
    ensure_operator_device_paired,
    is_seeded_agent,
    pair_with_openclaw,
    resolve_company_id_for_owner,
)

logger = logging.getLogger(__name__)

# Strict lowercase RFC-4122 v4 UUID. This is the load-bearing security guard for
# the trailing session-id segment: OpenClaw lowercases the ENTIRE session key
# before store/lookup, so a mixed-case id would alias-merge two sessions under
# the case-fold. It also blocks colon child-key forks (`:thread:`/`:subagent:`)
# and reserved tokens (`heartbeat` -> session reap; `group`/`channel`/`direct`/
# `dashboard` -> kind misclassification) — none of those can match this regex.
#
# The pattern ends with ``\Z`` (absolute end), NOT ``$``: Python's ``$`` also
# matches just before a trailing newline, so ``"<uuid>\n"`` would slip through
# ``$`` and inject a newline into the session key. ``\Z`` has no such carve-out.
_SESSION_ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")


def build_chat_session_key(agent_id: str, user_id: str, session_id: str | None) -> str:
    """Construct the OpenClaw session key for a /chat conversation.

    - ``session_id is None`` -> legacy 3-part ``agent:{agent_id}:{user_id}`` (the
      "General" session) so existing single-session history is never orphaned.
    - Otherwise -> 4-part ``agent:{agent_id}:{user_id}:{session_id}`` where
      ``session_id`` MUST be a strict lowercase v4 UUID (client-minted via
      ``crypto.randomUUID()``).

    ``agent_id`` and ``user_id`` are server-supplied (``user_id`` from the JWT),
    never client-influenced — the client only ever sends the opaque trailing id.
    """
    if session_id is None:
        return f"agent:{agent_id}:{user_id}"
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError(f"invalid session_id: {session_id!r}")
    return f"agent:{agent_id}:{user_id}:{session_id}"


def _user_owns_session_key(key, user_id: str) -> bool:
    """True iff the OpenClaw session ``key`` belongs to ``user_id``.

    A key is owned when, after the ``agent:<agentId>:`` prefix, the remainder
    is exactly the caller's user_id (legacy "General" 3-part key) or begins
    with ``<user_id>:`` (a 4-part named session). The ``<user_id>:`` boundary
    check (not a bare startswith) stops ``user_1`` from matching ``user_10``.

    Non-``agent:`` keys (``paperclip:``/``cron:``/``channel``) and other org
    members' keys are NOT owned — this is the ownership predicate that both the
    sessions.list post-filter and the pre-forward mutation guard rely on.

    The comparison is case-insensitive: OpenClaw lowercases the whole
    sessionKey, so the ``rest`` segment parsed here is already lowercase, while
    ``user_id`` is the original mixed-case Clerk id (base58, almost always has
    uppercase). Comparing case-sensitively would drop every real user's own
    rows. Mirrors the lowercased key/member comparisons in connection_pool.py.
    """
    if not isinstance(key, str):
        return False
    parts = key.split(":", 2)
    if len(parts) < 3 or parts[0] != "agent":
        return False
    rest_l = parts[2].lower()  # everything after agent:<agentId>:, lowercased
    uid_l = user_id.lower()
    return rest_l == uid_l or rest_l.startswith(f"{uid_l}:")


def _filter_sessions_for_user(result, user_id: str):
    """Drop sessions.list rows the caller doesn't own.

    sessions.list returns EVERY session on the shared per-owner container —
    paperclip:/cron:/channel and other org members' rows included — all
    collapsed to kind='direct'. Keep only rows the caller owns per
    ``_user_owns_session_key``. Ownership guard, not cosmetic.
    """
    if not isinstance(result, dict):
        return result
    sessions = result.get("sessions")
    if not isinstance(sessions, list):
        return result
    kept = [s for s in sessions if isinstance(s, dict) and _user_owns_session_key(s.get("key", ""), user_id)]
    return {**result, "sessions": kept}


def _filter_search_hits_for_user(result, user_id: str):
    """Drop sessions.search hits from sessions the caller doesn't own.

    A key-less ``sessions.search`` indexes EVERY session of the agent on the
    shared per-owner container, so its snippets would surface other org
    members' (and paperclip:/cron:) transcripts. Same ownership guard as
    ``_filter_sessions_for_user``.
    """
    if not isinstance(result, dict):
        return result
    hits = result.get("results")
    if not isinstance(hits, list):
        return result
    kept = [h for h in hits if isinstance(h, dict) and _user_owns_session_key(h.get("sessionKey", ""), user_id)]
    return {**result, "results": kept}


# API Gateway's Management API rejects PostToConnection payloads over 128KiB
# (413), and the whole res frame counts. Budget the chat.history payload well
# below that so the envelope ({"type":"res","id":...}) always fits.
_WS_FRAME_BUDGET_BYTES = 110_000


def _elide_oversized_message(message, budget: int):
    """Replace long text blocks in one message with a short marker.

    Last resort for a message that cannot fit a frame even alone (raw tool
    output — file dumps, stdout floods). Dropping it instead would stall the
    client's cursor on this page forever. The UI renders tool results as
    collapsed cards, so eliding the body costs little and the marker keeps the
    omission honest rather than silent.
    """
    if not isinstance(message, dict):
        return message
    content = message.get("content")
    if not isinstance(content, list):
        return message
    elided = []
    for block in content:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            raw = block["text"]
            if len(raw.encode()) > budget // 4:
                omitted = len(raw.encode())
                elided.append({**block, "text": f"[… {omitted} bytes omitted — too large to display …]"})
                continue
        elided.append(block)
    return {**message, "content": elided}


def _hard_truncate_message(message, original_bytes: int) -> dict:
    """Guaranteed-fit fallback when per-block elision isn't enough.

    ``_elide_oversized_message`` only shrinks a content block once that block
    ALONE crosses ``budget // 4`` — two shapes defeat it: several
    sub-threshold blocks whose SUM still exceeds the budget (each passes
    through unchanged), and non-list ``content`` (a plain string), where it's
    a no-op entirely. This replaces the whole message body with one marker
    block, keeping bounded native attribution and lineage when present.
    Called only after a post-elision size check has confirmed
    elision alone didn't fit — this is what makes "the returned payload
    always fits" an enforced guarantee rather than a heuristic.
    """
    stub = {"content": [{"type": "text", "text": f"[… {original_bytes} bytes omitted — too large to display …]"}]}
    if isinstance(message, dict) and "role" in message:
        stub["role"] = message["role"]
    if isinstance(message, dict):
        for key in ("sender", "id", "entryId", "parentId", "runId", "timestamp"):
            if key in message and len(json.dumps(message[key], default=str).encode()) <= 4096:
                stub[key] = message[key]
    return stub


def _trim_chat_history_for_frame(result, budget: int = _WS_FRAME_BUDGET_BYTES):
    """Drop the OLDEST chat.history messages until the payload fits one frame.

    OpenClaw caps chat.history by its own byte ceiling, but that ceiling is
    above API Gateway's frame limit — a tool-heavy session's history 413s on
    the push and the browser renders a blank transcript. Bisect for the
    longest newest-suffix that fits and flag the result ``truncated``.
    """
    if not isinstance(result, dict):
        return result
    messages = result.get("messages")
    if not isinstance(messages, list):
        return result

    def _size(candidate) -> int:
        return len(json.dumps(candidate, separators=(",", ":"), default=str).encode())

    if _size(result) <= budget:
        return result

    # Smallest drop count d such that messages[d:] fits.
    lo, hi = 0, len(messages)
    while lo < hi:
        mid = (lo + hi) // 2
        if _size({**result, "messages": messages[mid:], "truncated": True}) <= budget:
            hi = mid
        else:
            lo = mid + 1
    # Degenerate page: not even the newest single message fits. Ship it
    # elided (or, failing that, hard-truncated) so the cursor still advances
    # by one instead of stalling on this page forever. This applies to BOTH
    # paged and unpaged requests — gating it on `offset` would leave the
    # legacy unpaged shape falling through to an empty transcript, which is
    # the exact bug this guard exists to eliminate. Only the cursor fields
    # (nextOffset/hasMore) are conditioned on this being a paged request.
    if lo >= len(messages) and messages:
        last = messages[-1]
        elided = _elide_oversized_message(last, budget)
        out = {**result, "messages": [elided], "truncated": True}
        if _size(out) > budget:
            # Elision alone didn't shrink it enough (many sub-threshold
            # blocks, or non-list `content`). Fall back to a hard truncation
            # that is guaranteed to fit.
            stub = _hard_truncate_message(last, _size(last))
            out = {**result, "messages": [stub], "truncated": True}
        offset = result.get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool):
            out["nextOffset"] = offset + 1
            out["hasMore"] = True
        return out
    kept = messages[lo:]
    out = {**result, "messages": kept, "truncated": True}
    # A paged read: the bisect above dropped the OLDEST messages of this page,
    # which sit at the FAR end of the cursor (offset counts backward from
    # newest). The server's nextOffset assumed the full page was delivered, so
    # resuming from it would skip exactly what we just dropped. Re-anchor the
    # cursor to what actually shipped, and force hasMore — messages were
    # dropped, so more definitely remain.
    offset = result.get("offset")
    if isinstance(offset, int) and not isinstance(offset, bool):
        out["nextOffset"] = offset + len(kept)
        out["hasMore"] = True
    return out


# Methods whose result payload the frontend DISCARDS (it revalidates via a
# follow-up read instead). When such a result is too big for one frame, the
# payload can be replaced with a marker rather than letting the push 413 —
# which would report failure for an operation that actually persisted.
# sessions.patch echoes ~180KB (entry + model catalog) on some containers.
_RPC_RESULT_DISCARDED_METHODS = {"sessions.patch"}

# Gateway methods that DISPATCH AN AGENT RUN — their container-side handler
# enters OpenClaw's chat.send dispatch boundary and starts a new inference turn.
# Verified against the pinned fork's gateway server-methods:
#   chat.send       chat.ts      — the canonical dispatch
#   sessions.send   sessions.ts  — runSessionSendLike() -> chatHandlers["chat.send"]
#   sessions.steer  sessions.ts  — same helper, same dispatch
#   agent           agent.ts     — "shares the chat.send / sessions.send boundary"
# Deliberately NOT here: chat.abort (terminates a run), agent.wait (awaits an
# existing runId), chat.inject (transcript write), tools.invoke (tool exec, no
# model turn). Container-side dispatch is exact-match on the method string, so a
# variant that isn't in this set cannot reach a run-dispatching handler.
#
# Chat has exactly ONE entrance from a client: the typed `agent_chat` WS message
# (`agent_steer` for a steer). That path gates the send, opens the parked-vs-dead
# run window (`mark_run_active`), and drives the streaming UX. The generic RPC
# proxy below does none of that, so forwarding a run-dispatching method through
# it starts a run the backend does not know about — a second, unowned door into
# chat. This is NOT a cost fence (the expensive lanes — agent tool loops, cron,
# Paperclip, channels — never touch this proxy); it removes a duplication. Every
# other method the proxy forwards untouched.
_RUN_DISPATCH_METHODS = frozenset({"chat.send", "sessions.send", "sessions.steer", "agent"})

# The typed WS message that owns each refused method's job.
_RUN_DISPATCH_ALTERNATIVE = {
    "chat.send": "agent_chat",
    "sessions.send": "agent_chat",
    "sessions.steer": "agent_steer",
    "agent": "agent_chat",
}

# Wake-admission gate scope. Deliberately the SAME set as above: that frozenset
# is this codebase's single answer to "does this method start an agent run", and
# the two doors must never disagree about it. The proxy REFUSES these outright
# (they belong to the typed chat path); the operator relay ADMITS them subject
# to budget — same question, different verdicts.
#
# In practice the Paperclip openclaw-gateway adapter only ever sends `agent`
# (paperclip/packages/adapters/openclaw-gateway/src/server/execute.ts).
# `agent.wait` is the separate call that AWAITS an already-started run
# and is deliberately NOT gated — refusing it would strand a live run rather
# than prevent one, and the spend it represents is already committed.
_WAKE_GATED_METHODS = _RUN_DISPATCH_METHODS

# DEFAULT-DENY: the only gateway methods the browser/mobile RPC proxy will forward.
#
# `core-descriptors.ts` at the pinned fork tag declares ~390 dotted methods, plus
# plugin-provided ones. The product sends 44. Everything else was reachable from any
# org member's browser against the SHARED per-owner container — and the container has
# no opinion about it: our operator device holds `operator.admin`
# (core/crypto/operator_device.py), and OpenClaw's `roleVisibilityFilter` only engages
# behind `hasOperatorBoundary` (fork src/gateway/operator-role-policy.ts), which needs a
# configured operator role. We configure none.
#
# A session-key ownership check cannot be the fence here: roughly half the dangerous
# surface (cron writes, device pairing, audit reads, config apply) carries no session
# key at all. This table is the fence; _SESSION_KEY_PARAM_BY_METHOD is depth behind it.
#
# WHAT MAY BE ADDED: a method the product demonstrably sends, or one already carrying an
# individual ownership guard. Nothing else. Adding a line here re-opens a door, so the
# justification belongs in the PR, not in a comment.
#
# HOW TO SPOT AN OMISSION: a refusal emits put_metric("rpc.refused", reason="not_allowlisted")
# with the method name. Watch it on dev after the merge, before the prod promotion.
#
# Deliberately NOT here (no client caller; the near-misses that look like they belong):
#   chat.abort / agent.wait / tools.invoke / cron.list — aborts ride the typed
#   `agent_abort` frame, not this proxy.
#   sessions.reset — key-guarded in _SESSION_KEY_PARAM_BY_METHOD, but nothing sends it
#   and it destructively wipes a session. Already-guarded != needed; deny is safer.
# Deliberately NOT here (different door, diverted upstream of this function):
#   connect, node.invoke.result (desktop node host, handle_node_message);
#   agent, agent.wait (Paperclip operator relay).
_PROXY_ALLOWED_METHODS = frozenset(
    {
        # agents + identity — AgentOverviewTab.tsx, useAgents.ts, mobile control/agents.tsx
        "agent.identity.get",
        "agents.create",
        "agents.delete",
        "agents.list",
        "agents.update",
        # browser live-view panel (plugin-provided; absent from core-descriptors.ts)
        # BrowserDockPanel.tsx
        "browser.request",
        # channels — awaitChannelReady.ts (prop-drilled callRpc), channels panels
        "channels.status",
        # chat reads (key-guarded) — useAgentChat.ts
        "chat.history",
        "chat.message.get",
        "commands.list",
        # config — control/llm.tsx and the settings panels
        "config.get",
        "config.patch",
        "config.set",
        # dreams / memory doctor — DreamsPanel.tsx dispatches five of these from its
        # module-level ADVANCED_ACTIONS table; .status and .dreamDiary are hook literals
        "doctor.memory.backfillDreamDiary",
        "doctor.memory.dedupeDreamDiary",
        "doctor.memory.dreamDiary",
        "doctor.memory.repairDreamingArtifacts",
        "doctor.memory.resetDreamDiary",
        "doctor.memory.resetGroundedShortTerm",
        "doctor.memory.status",
        # approvals — useExecApprovals.tsx picks between these two by entry kind
        "exec.approval.resolve",
        "plugin.approval.resolve",
        # health / models — OverviewPanel, useSystemHealth, ProvisioningStepper
        "health",
        "models.authStatus",
        "models.list",
        # progress card — useProgressCard.ts
        "progressCard.get",
        # questions
        "question.list",
        "question.resolve",
        # sessions (all key-guarded or response post-filtered)
        "sessions.branches.list",
        "sessions.branches.switch",
        "sessions.delete",
        "sessions.list",
        "sessions.patch",
        "sessions.rewind",
        "sessions.search",
        # skills — SkillsTab.tsx, SkillProposalsPanel.tsx
        "skills.install",
        "skills.proposals.apply",  # `skills.proposals.${action}` template literal
        "skills.proposals.inspect",
        "skills.proposals.list",
        "skills.proposals.reject",  # same template literal
        "skills.search",
        "skills.status",
        "skills.update",
        # tools
        "tools.catalog",
    }
)


def _fit_rpc_result_for_frame(method: str, result, budget: int = _WS_FRAME_BUDGET_BYTES):
    """Keep any RPC res frame deliverable through the Management API.

    chat.history gets a content-aware trim; discarded-payload methods get a
    marker stub; anything else oversized passes through with a warning so we
    learn about the next offender from logs instead of silent blank UIs.

    sessions.list needs no special case: its preview field is the top-level
    `lastMessagePreview`, which the container already caps at 240 chars.
    """
    if method == "chat.history":
        return _trim_chat_history_for_frame(result, budget)
    size = len(json.dumps(result, separators=(",", ":"), default=str).encode())
    if size <= budget:
        return result
    if method in _RPC_RESULT_DISCARDED_METHODS:
        logger.info("RPC %s result (%d bytes) exceeds frame budget; payload omitted", method, size)
        return {"payloadOmitted": True}
    logger.warning(
        "RPC %s result is %d bytes — over the WS frame budget; push will likely 413",
        method,
        size,
    )
    return result


def require_ws_internal_caller(
    x_lightbulb_ws_internal: Optional[str] = Header(None, alias="x-lightbulb-ws-internal"),
    x_isol8_ws_internal: Optional[str] = Header(None, alias="x-isol8-ws-internal"),
) -> None:
    """Authenticate the CALLER of the /ws/* endpoints — the trusted WebSocket
    Lambdas — not the user identity in x-user-id (which the caller chooses, so
    validating its value would be pointless).

    These endpoints have no get_current_user: they derive identity from request
    headers and are meant to be reached only via the WS Lambdas, which run after
    the API Gateway WS authorizer validates the Clerk JWT. The public HTTP API
    Gateway $default route, by contrast, proxies every path to the same ALB with
    no authorizer. The Lambdas attach settings.WS_INTERNAL_SECRET as a header the
    public route cannot supply; this dependency rejects callers that lack it.

    Off by default: enforcement activates only once WS_ENFORCE_INTERNAL_SECRET is
    true, so the secret can be deployed to both the Lambdas and the backend before
    the switch is flipped (zero-downtime rollout). When enforcement is on but no
    secret is configured we fail closed (503) rather than silently allow forged
    callers.

    TRANSITIONAL — accepts the retired `x-isol8-ws-internal` spelling as well as
    the current `x-lightbulb-ws-internal`. The Lambdas (ApiStack) and the backend
    (ServiceStack) are separate CloudFormation stacks in one pipeline run, and
    the backend rolls at maxHealthyPercent 200, so during a deploy one side is on
    the new name while the other is still on the old one for minutes. A hard swap
    would 403 every WS connect/message in that window. Both names carry the SAME
    secret, so accepting either widens no trust boundary — only the spelling.
    FOLLOW-UP: once this release is deployed to dev AND promoted to prod, delete
    the `x_isol8_ws_internal` parameter and this paragraph. No sender emits it.
    """
    if not settings.WS_ENFORCE_INTERNAL_SECRET:
        return
    secret = settings.WS_INTERNAL_SECRET
    if not secret:
        logger.error(
            "WS_ENFORCE_INTERNAL_SECRET is on but WS_INTERNAL_SECRET is empty; "
            "rejecting /ws/* calls until the secret is configured"
        )
        raise HTTPException(status_code=503, detail="WebSocket gateway not configured")
    presented = x_lightbulb_ws_internal or x_isol8_ws_internal
    # Compare bytes, not str: compare_digest raises TypeError on non-ASCII str.
    if not presented or not hmac.compare_digest(presented.encode("utf-8"), secret.encode("utf-8")):
        raise HTTPException(status_code=403, detail="Forbidden")


router = APIRouter(tags=["websocket"], dependencies=[Depends(require_ws_internal_caller)])


# Client IDs that handle their own connect.challenge via the upstream-relay
# path (handle_node_connect). For these clients we MUST NOT send a synthetic
# nonce on $connect -- it would race the real challenge from the upstream
# OpenClaw container and could cause the subprocess to sign the wrong nonce.
NODE_HOST_CLIENT_IDS = frozenset({"node-host", "openclaw-macos"})


# Singleton instances (created lazily with async lock for thread safety)
_connection_service: Optional[ConnectionService] = None
_management_api_client: Optional[ManagementApiClient] = None
_singleton_lock = asyncio.Lock()

# Module-level handle used by _guard_seeded_delete. Left None in production so
# the lazy get_management_api_client() singleton is used (constructing the
# client eagerly raises when WS_MANAGEMENT_API_URL is unset); tests patch it.
management_api: Optional[ManagementApiClient] = None


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
        await management_api.send_message_async(
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
    x_lightbulb_ws_internal: Optional[str] = Header(None, alias="x-lightbulb-ws-internal"),
) -> Response:
    if not x_connection_id:
        raise HTTPException(status_code=400, detail="Missing x-connection-id header")
    if not x_user_id:
        raise HTTPException(status_code=401, detail="Missing x-user-id header")

    from core.gateway.native_rooms import CLIENT_ID, HumanIdentity, RoomError, qualification_for, require_current_member

    if x_client_id == CLIENT_ID:
        _require_native_internal(x_lightbulb_ws_internal)
        # Lambda already stored verified authorizer facts. Never overwrite them
        # with inbound issuer/user/owner/scope headers, including on backend retry.
        service = await get_connection_service()
        row = await asyncio.to_thread(service.get_connection, x_connection_id)
        try:
            identity = HumanIdentity.from_connection(x_connection_id, row or {})
            qualification_for(identity.owner_id)
            await require_current_member(identity, service)
        except RoomError as exc:
            raise HTTPException(status_code=403, detail=exc.error) from exc
        background_tasks.add_task(_send_connect_challenge, x_connection_id)
        return Response(status_code=200)

    logger.info(
        "WS connect: conn=%s user=%s org=%s owner=%s",
        x_connection_id[:12],
        x_user_id,
        x_org_id or "-",
        x_org_id or x_user_id,
    )

    is_node_host = x_client_id in NODE_HOST_CLIENT_IDS
    is_operator_relay = x_client_id == PAPERCLIP_OPERATOR_CLIENT_ID
    # Route by owner_id: org_id for org members, user_id for personal.
    # Paperclip service tokens resolve to userId=<owner_id>, orgId="" at the
    # authorizer, so owner_id is the container owner either way.
    owner_id = x_org_id or x_user_id

    connection_service = await get_connection_service()
    connection_service.store_connection(
        connection_id=x_connection_id,
        user_id=x_user_id,
        org_id=x_org_id,
        connection_type=("operator" if is_operator_relay else "node" if is_node_host else "chat"),
    )

    # Operator-relay connections (Paperclip adapter) are NOT frontend chat
    # connections: their frames flow through the dedicated upstream relay's
    # push callback, never the pool's broadcast fanout. Keep them out of
    # _frontend_connections so owner chat broadcasts don't bleed onto the
    # adapter socket.
    if not is_operator_relay:
        try:
            pool = get_gateway_pool()
            # Registers the connection for the owner's idle-teardown signal and
            # busts the recipient cache so this tab is picked up on its next
            # fan-out. Per-member demux is sourced from the by-owner-id GSI's
            # userId, so no member_id is tracked in-process.
            pool.add_frontend_connection(owner_id, x_connection_id)
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
    #   challenge. A naive wake-up `connect` injected by the proxy collides
    #   with the subprocess's own signed `connect`, with the backend keying
    #   `hello-ok` to the wake-up id instead of the subprocess's pending id,
    #   silently timing out the handshake. The proxy is now a transparent
    #   relay; we kick off `handle_node_connect` here on `$connect` and the
    #   subprocess's signed-connect frame is the FIRST and ONLY `connect`
    #   frame the `$default` handler sees.
    #
    # - paperclip-operator clients (the Paperclip agent adapter): same
    #   upstream-relay shape as node-host, but a headless operator with no
    #   local tool host. The adapter waits for the container's real
    #   `connect.challenge` to compute its device signature, so a synthetic
    #   nonce / cosmetic hello-ok would brick the handshake — kick off the
    #   transparent operator relay here instead.
    if is_operator_relay:
        background_tasks.add_task(
            _kick_off_operator_connect,
            connection_id=x_connection_id,
            owner_id=owner_id,
            user_id=x_user_id,
        )
    elif is_node_host:
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


async def _kick_off_operator_connect(
    *,
    connection_id: str,
    owner_id: str,
    user_id: str,
) -> None:
    """Start the Paperclip-operator upstream relay handshake on `$connect`.

    Mirrors `_kick_off_node_connect`: the adapter (an OpenClaw gateway client)
    blocks on the container's `connect.challenge` before it signs, so SOMETHING
    must open the upstream container WS to surface that nonce. Errors are
    swallowed (logged); the adapter retries via its own reconnect loop.
    """
    try:
        management_api = await get_management_api_client()
        await handle_operator_connect(
            owner_id=owner_id,
            user_id=user_id,
            connection_id=connection_id,
            connect_params={
                "role": "operator",
                "client": {"id": PAPERCLIP_OPERATOR_CLIENT_ID, "mode": "backend"},
            },
            management_api=management_api,
        )
    except Exception as e:
        logger.warning(
            "paperclip-operator connect failed for owner=%s conn=%s: %s",
            owner_id,
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
    # Row-independent: the disconnect Lambda deletes the row before notifying.
    try:
        await get_gateway_pool().native_rooms.close_connection(x_connection_id)
    except Exception:
        logger.exception("Native human disconnect cleanup failed")

    # Tear down the operator relay upstream FIRST — independent of the
    # connection row. The $disconnect lambda (apps/infra/lambda/ws-disconnect)
    # DELETES the DynamoDB connection row BEFORE it POSTs here, so the
    # get_connection() lookup below can return None and skip the row-gated
    # cleanup. handle_operator_disconnect needs only the connection_id and is
    # idempotent, so key it off the in-process relay map instead. (A cross-task
    # orphan — the upstream stranded on the $connect task while $disconnect
    # lands on a different task — is reaped by the failed-push path in
    # operator_proxy.on_upstream_message.)
    if is_operator_connection(x_connection_id):
        try:
            await handle_operator_disconnect(x_connection_id)
        except Exception as e:  # noqa: BLE001 — best-effort relay teardown
            logger.warning("operator relay teardown failed for %s: %s", x_connection_id, e)

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

            # Additionally tear down the node upstream + per-user state if this
            # was a node connection. (Operator relay teardown happened above,
            # row-independent — the $disconnect lambda may already have deleted
            # the row, so it must not be gated on this lookup.)
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


def _wake_refusal_payload(gate: dict, *, req_id: Any) -> dict:
    """Build the frame that refuses a budget-blocked agent wake.

    THE ONE PLACE this wire shape is constructed — every wake refusal goes
    through here so retuning it is a single edit.

    Shape CONFIRMED against the deployed fork (`isol8/2026.6-circuit-breaker`)
    at both layers that could re-arm a retry:

    1. **The adapter.** `GatewayWsClient.handleMessage` rejects the pending
       request on `ok: false`, reading `error.message` / `error.code` /
       `error.details` into a thrown `GatewayResponseError`. Its retry
       classifier is a SUBSTRING match on that message — it retries only
       `econnrefused` / `econnreset` / `socket hang up` / `timeout`. Our gate
       messages contain none of those (pinned by a test), so the adapter does
       not retry a refusal; it returns `exitCode: 1`.
    2. **Paperclip's wake scheduler.** `scheduleBoundedRetryForRun` fires only
       when `errorFamily == "transient_upstream"` (or the codex/claude
       transient errorCodes, or max-turn exhaustion). The openclaw-gateway
       adapter NEVER sets `errorFamily`/`retryNotBefore` — verified zero
       occurrences in its `execute.ts` — so its failures are terminal by
       construction. A refusal PARKS; nothing re-dispatches it.

    Downstream consequence worth knowing: the routine circuit breaker counts
    the parked run toward `consecutiveFailureCount`, so enough refusals pause
    the whole routine (`autoPauseReason="consecutive_failures"`), which then
    needs a manual resume even after the cap resets.

    Keep the shape here regardless — one edit site is the point.
    """
    code = gate.get("code", "blocked")
    return {
        "type": "res",
        "id": req_id,
        "ok": False,
        "error": {
            "code": code,
            "message": gate.get("message", "Agent wake blocked"),
            "details": {"reason": "wake_admission_gate", "code": code},
        },
    }


async def _admit_operator_wake(
    *,
    connection_id: str,
    user_id: str,
    owner_id: str,
    body: Dict[str, Any],
) -> Optional[dict]:
    """Budget-check a Paperclip-originated agent wake.

    Paperclip's seed agents (Friday/Atlas) drive their runs through the operator
    relay, which short-circuits ABOVE both the RPC proxy and the typed
    `agent_chat` handler. That made the agent-wake door the one entrance to
    inference that was never budget-checked: a capped-out free-trial owner got a
    wall of instantly-dead runs (12 in ~3 minutes) instead of one clear
    "out for today, upgrade" signal.

    This calls the SAME ``pool.gate_chat`` the human ``agent_chat`` path calls, so
    wakes and human sends obey one contract. Reusing it is load-bearing for
    provider safety: gate_chat is already provider-aware, so ChatGPT-OAuth and
    BYOK owners — who consume no Isol8 budget — can never be blocked on one,
    while paid Bedrock owners are blocked only on genuinely exhausted credits.
    A trial-only predicate here would have been the WRONG primitive.

    Returns ``None`` to admit the wake, or the refusal frame to send instead of
    relaying it upstream.
    """
    if body.get("method") not in _WAKE_GATED_METHODS:
        return None

    params = body.get("params")
    session_key = params.get("sessionKey") if isinstance(params, dict) else None

    try:
        pool = get_gateway_pool()
        gate = await pool.gate_chat(
            user_id=user_id,
            owner_id=owner_id,
            session_key=session_key or "",
        )
    except Exception:
        # FAIL OPEN — the deliberate inverse of the chat path, where gate_chat's
        # raise propagates and the send is refused. The failure modes differ: a
        # dropped chat send is a visible error the human retries, but a dropped
        # wake is silently lost work for a paying customer. And spend is not
        # actually fenced here — the bedrock-gate's synchronous pre-flight
        # reservation is the real fence — so a transient DDB/network blip must
        # not cost an owner their automation.
        logger.warning(
            "[%s] wake gate read failed owner=%s conn=%s — ADMITTING wake (fail-open)",
            user_id,
            owner_id,
            connection_id[:12] if connection_id else "?",
            exc_info=True,
        )
        put_metric("wake.admission", dimensions={"outcome": "gate_error"})
        return None

    if not gate.get("blocked"):
        return None

    code = gate.get("code", "blocked")
    logger.info(
        "[%s] agent wake BLOCKED owner=%s conn=%s code=%s",
        user_id,
        owner_id,
        connection_id[:12] if connection_id else "?",
        code,
    )
    put_metric("wake.admission", dimensions={"outcome": "blocked", "reason": code})
    return _wake_refusal_payload(gate, req_id=body.get("id"))


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
    x_lightbulb_ws_internal: Optional[str] = Header(None, alias="x-lightbulb-ws-internal"),
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

    if connection.get("connection_type") == "native_human":
        _require_native_internal(x_lightbulb_ws_internal)
        background_tasks.add_task(_process_native_room_background, x_connection_id, body)
        return Response(status_code=200)

    if msg_type == "ping":
        management_api = await get_management_api_client()
        await management_api.send_message_async(x_connection_id, {"type": "pong"})

        async def _touch_liveness() -> None:
            # Fire-and-forget liveness stamp -- never awaited by the caller
            # below, so the pong above never gains a DDB round-trip. Nothing
            # awaits this task, so it must swallow its own errors: an
            # exception here would otherwise only ever surface as an
            # asyncio "Task exception was never retrieved" warning.
            # touch_connection already logs real DynamoDB failures
            # internally (see its docstring) -- this is a last-resort net.
            try:
                await connection_service.touch_connection(x_connection_id)
            except Exception:
                logger.debug("touch_connection task failed for %s", x_connection_id, exc_info=True)

        asyncio.create_task(_touch_liveness())

        # Layer 3 of scale-to-zero (Task 13): reset this owner's per-owner
        # client-liveness deadline. If API Gateway ever loses this owner's
        # $disconnect, nothing else clears `_frontend_connections` for
        # them -- this deadline is what eventually recovers it. Purely
        # in-process (dict lookups + a timer reschedule, no I/O), so unlike
        # `_touch_liveness` above it is cheap enough to call inline rather
        # than via a background task -- but still swallow errors so a pool
        # lookup hiccup can never fail the pong.
        try:
            pool = get_gateway_pool()
            pool.note_client_activity(owner_id)
        except Exception:
            logger.debug("note_client_activity failed for owner=%s", owner_id, exc_info=True)

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

    # Paperclip operator relay: forward every adapter frame verbatim to the
    # upstream container. This is a transparent pipe, so it short-circuits BEFORE
    # the frontend-pool lazy-register and RPC-proxy dispatch below — the adapter
    # is not a chat client.
    #
    # Identify operator traffic by the PERSISTED connection_type (stored in
    # DynamoDB at $connect), NOT just the in-process _operator_upstreams map.
    # The client's WS lives on API Gateway and survives backend task rollovers,
    # so a $default frame can land on a different task than $connect — where the
    # relay upstream doesn't exist. Routing on the durable type keeps these
    # frames out of the cosmetic operator hello-ok stub and the RPC proxy on
    # EVERY task; without it a connect frame on the wrong task would get a fake
    # hello-ok while the real upstream (on the $connect task) waits and times out.
    if connection.get("connection_type") == "operator":
        if is_operator_connection(x_connection_id):
            # The relay upstream lives on THIS task: complete the handshake (the
            # signed `connect` frame) or relay a steady-state frame verbatim.
            if msg_type == "req" and body.get("method") == "connect":
                submit_operator_signed_connect(x_connection_id, body)
            else:
                # Wake-admission gate: a relay `req` that would START an
                # agent run is budget-checked with the same predicate as a human
                # chat send. Everything else — `agent.wait`, status reads,
                # history fetches, non-`req` frames — relays untouched, so the
                # Teams surface is undisturbed.
                refusal = (
                    await _admit_operator_wake(
                        connection_id=x_connection_id,
                        user_id=user_id,
                        owner_id=owner_id,
                        body=body,
                    )
                    if msg_type == "req"
                    else None
                )
                if refusal is not None:
                    management_api = await get_management_api_client()
                    await management_api.send_message_async(x_connection_id, refusal)
                    return Response(status_code=200)
                await handle_operator_message(x_connection_id, body)
        else:
            # Relay upstream isn't on this task (cross-task / post-rollover).
            # Don't silently blackhole the frame — that hangs the adapter's
            # request for the full connect timeout. Reply with an explicit error
            # res keyed to the frame's request id so the adapter fails FAST and
            # retries on its next wake (which re-runs $connect). At our
            # single-task steady state $connect + $default co-locate, so this
            # only bites during a brief deploy-overlap window. We still never
            # fake a hello-ok or forward into the RPC path.
            logger.warning(
                "operator frame for conn=%s (method=%s) with no relay upstream on this task — "
                "returning explicit error so the adapter retries",
                x_connection_id[:12] if x_connection_id else "?",
                body.get("method") if msg_type == "req" else msg_type,
            )
            management_api = await get_management_api_client()
            await management_api.send_message_async(
                x_connection_id,
                {
                    "type": "res",
                    "id": body.get("id"),
                    "ok": False,
                    "error": {"message": "operator relay unavailable on this node — reconnect"},
                },
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
        pool.add_frontend_connection(owner_id, x_connection_id)
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
            await management_api.send_message_async(
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

            # Node connection: pure-relay flow.
            #
            # The only `connect` frame that means anything here is the signed
            # reply to the upstream container's `connect.challenge` (the
            # OpenClaw v3 handshake is server-initiated, see upstream
            # src/gateway/client.ts). The backend started the upstream
            # handshake itself on `$connect` via `_kick_off_node_connect`, so
            # by the time that frame arrives there's an awaiting
            # NodeUpstreamConnection ready to consume it. Route it through
            # `submit_signed_connect` and ACK synchronously —
            # `handle_node_connect` is what eventually forwards `hello-ok`
            # DOWN to the client (keyed to the client's own request id,
            # captured during the handshake).
            if role == "node":
                # Only a SIGNED connect may resolve the pending handshake.
                #
                # `submit_signed_connect` forwards whatever it is handed
                # upstream VERBATIM as the signed reply. Desktop builds up to
                # 0.4.86 open with a deviceless "wake-up" connect — a relic of
                # the pre-`$connect` trigger — which races the real signed
                # frame and usually wins, so the container answers
                # NOT_PAIRED/DEVICE_IDENTITY_REQUIRED, the desktop tears the
                # socket down, and local tools never connect. Measured on the
                # owner's prod install 2026-08-31: 282 of 286 handshakes.
                #
                # The upstream handshake already started at `$connect`, so a
                # deviceless frame has nothing left to do. Drop it and let the
                # signed frame (which is still coming) do its job. This is the
                # half of the fix that reaches installed DMGs.
                device = connect_params.get("device")
                if not (isinstance(device, dict) and device.get("id")):
                    logger.info(
                        "Dropping deviceless node connect on %s (legacy wake-up frame)",
                        x_connection_id[:12] if x_connection_id else "?",
                    )
                    return Response(status_code=200)
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
            await management_api.send_message_async(
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
        # upstream ui/src/ui/controllers/chat.ts.
        # Optional for backwards-compat with old frontend builds — when
        # absent, the backend mints a UUID below so OpenClaw's chat.send
        # always carries an idempotencyKey (the field is read by upstream
        # src/gateway/server-methods/chat.ts which does `const clientRunId = p.idempotencyKey`).
        client_run_id = body.get("clientRunId")
        # Side-channel (`/btw`) send. Upstream's webchat routes a `/btw`/`/side`
        # message through the SAME wire call as a normal chat.send (deliver:false
        # + fresh idempotencyKey); the OpenClaw container detects the `/btw`
        # prefix in the message text and runs it as a non-blocking side run that
        # never enters the session's active-run window (see upstream
        # auto-reply/reply/btw-command.ts BTW_COMMAND_RE). We forward it as a
        # normal chat.send but skip mark_run_active so it does NOT claim the
        # active-run window — that keeps the busy gate clear for the primary run.
        side = bool(body.get("side"))

        if not agent_id or not message:
            management_api = await get_management_api_client()
            # No agent_id to tag — this is a client-validation error,
            # untagged by necessity. Frontend treats untagged errors as
            # broadcast so they still clear the streaming state.
            await management_api.send_message_async(
                x_connection_id,
                {"type": "error", "message": "Missing agent_id or message"},
            )
            return Response(status_code=200)

        # Optional client-minted session id (snake_case on the wire). None ->
        # legacy "General" session. Reject a malformed id at the handler so a
        # crafted trailing segment never reaches the key builder or gets queued.
        session_id = body.get("session_id")
        if session_id is not None:
            try:
                build_chat_session_key(agent_id, user_id, session_id)
            except ValueError:
                management_api = await get_management_api_client()
                await management_api.send_message_async(
                    x_connection_id,
                    {"type": "error", "agent_id": agent_id, "message": "Invalid session_id"},
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
            side=side,
            session_id=session_id,
        )
        return Response(status_code=200)

    if msg_type == "agent_abort":
        # Cancel the in-flight run for this session. Forwarded as upstream's
        # chat.abort: {sessionKey, runId} when the client knows the runId, else
        # {sessionKey} (mirrors openclaw chat.ts abortChatRun). Fire-and-don't-
        # wait-for-terminal: the run's aborted/run_ended arrives via the normal
        # event stream, where the existing terminal handler clears the active
        # run — so we deliberately do NOT clear it here.
        agent_id = body.get("agent_id")
        run_id = body.get("runId")
        if not agent_id:
            management_api = await get_management_api_client()
            await management_api.send_message_async(
                x_connection_id,
                {"type": "error", "message": "Missing agent_id"},
            )
            return Response(status_code=200)

        # Optional client-minted session id (snake_case on the wire). None ->
        # legacy "General" session. Reject a malformed id at the handler so a
        # crafted trailing segment never reaches the key builder or gets queued.
        session_id = body.get("session_id")
        if session_id is not None:
            try:
                build_chat_session_key(agent_id, user_id, session_id)
            except ValueError:
                management_api = await get_management_api_client()
                await management_api.send_message_async(
                    x_connection_id,
                    {"type": "error", "agent_id": agent_id, "message": "Invalid session_id"},
                )
                return Response(status_code=200)

        background_tasks.add_task(
            _process_agent_abort_background,
            connection_id=x_connection_id,
            user_id=user_id,
            owner_id=owner_id,
            agent_id=agent_id,
            run_id=run_id,
            session_id=session_id,
        )
        return Response(status_code=200)

    if msg_type == "agent_steer":
        # Inject `text` into the in-flight run for this session. Upstream's
        # sendSteerChatMessage (ui/controllers/chat.ts -> requestChatSend)
        # is just a chat.send with deliver:false + a freshly-generated
        # idempotencyKey on the SAME sessionKey; the container's queue-mode
        # "steer" merges it into the active run. There is no separate steer RPC
        # at the chat layer (sessions.steer is the distinct hard-redirect path,
        # used only by the /redirect slash command — not what we want here).
        agent_id = body.get("agent_id")
        text = body.get("text")
        if not agent_id or not text:
            management_api = await get_management_api_client()
            await management_api.send_message_async(
                x_connection_id,
                {"type": "error", "message": "Missing agent_id or text"},
            )
            return Response(status_code=200)

        # Optional client-minted session id (snake_case on the wire). None ->
        # legacy "General" session. Reject a malformed id at the handler so a
        # crafted trailing segment never reaches the key builder or gets queued.
        session_id = body.get("session_id")
        if session_id is not None:
            try:
                build_chat_session_key(agent_id, user_id, session_id)
            except ValueError:
                management_api = await get_management_api_client()
                await management_api.send_message_async(
                    x_connection_id,
                    {"type": "error", "agent_id": agent_id, "message": "Invalid session_id"},
                )
                return Response(status_code=200)

        background_tasks.add_task(
            _process_agent_steer_background,
            connection_id=x_connection_id,
            user_id=user_id,
            owner_id=owner_id,
            agent_id=agent_id,
            text=text,
            session_id=session_id,
        )
        return Response(status_code=200)

    # Unknown message type
    management_api = await get_management_api_client()
    await management_api.send_message_async(
        x_connection_id,
        {"type": "error", "message": f"Unknown message type: {msg_type}"},
    )
    return Response(status_code=200)


# =============================================================================
# OpenClaw RPC Proxy
# =============================================================================


def _require_native_internal(presented: str | None) -> None:
    # Native identity is never available through the legacy unenforced mode.
    secret = settings.WS_INTERNAL_SECRET
    if not secret or not isinstance(presented, str) or not hmac.compare_digest(presented.encode(), secret.encode()):
        raise HTTPException(status_code=403, detail="Authenticated WebSocket edge required")


async def _process_native_room_background(connection_id: str, body: dict) -> None:
    from core.gateway.native_rooms import (
        HumanIdentity,
        RoomError,
        UNCERTAIN_AFTER_SUCCESS,
        admit_rpc,
        filter_rpc_result,
        is_terminal_agent_wait_result,
        native_execution_run_id,
        qualification_for,
        require_append_result,
        require_current_member,
    )

    management = await get_management_api_client()
    service = await get_connection_service()
    pool = get_gateway_pool()
    req_id = body.get("id")
    method = body.get("method")
    conn = None
    try:
        if body == {"type": "ping"}:
            row = await asyncio.to_thread(service.get_connection, connection_id)
            identity = HumanIdentity.from_connection(connection_id, row or {})
            qualification_for(identity.owner_id)
            await require_current_member(identity, service)
            await service.touch_connection(connection_id)
            await management.send_message_async(connection_id, {"type": "pong"})
            return
        if body.get("type") != "req" or not isinstance(req_id, str) or not req_id or len(req_id) > 512:
            raise RoomError("INVALID_REQUEST", "Native room client requires a request id and method")
        if set(body) - {"type", "id", "method", "params"} or not isinstance(method, str):
            raise RoomError("INVALID_REQUEST", "Unexpected native room envelope fields")
        row = await asyncio.to_thread(service.get_connection, connection_id)
        identity = HumanIdentity.from_connection(connection_id, row or {})
        qualification = qualification_for(identity.owner_id)
        params = body.get("params", {})
        if method == "connect":
            if params != {}:
                raise RoomError("INVALID_REQUEST", "Native human handshake is server-owned")
            admitted = {}
        else:
            admitted = admit_rpc(method, params, identity, qualification)
        container, ip = await get_ecs_manager().resolve_running_container(identity.owner_id)
        if not container or not ip:
            raise RoomError("ROOM_UNAVAILABLE", "Qualified room runtime unavailable")
        conn = await pool.native_rooms.ensure(identity, container, ip)
        if method == "connect":
            result = {
                "protocol": 4,
                # The RPC contract is present, but rollout requires live runtime,
                # auth, coexistence, and legacy-privacy qualification evidence.
                "companyRooms": {"enabled": False},
            }
        else:
            # Re-check both durable credential epochs/tombstones and current
            # Clerk membership immediately before every upstream side effect.
            await conn.require_current()
            key = admitted.get("sessionKey") or admitted.get("key")
            # The independent native human socket owns the room ACL decision.
            native_run = None
            native_run_created = False
            if method == "sessions.execution.dispatch":
                gate = await pool.gate_chat(
                    user_id=identity.subject,
                    owner_id=identity.owner_id,
                    session_key=admitted["sessionKey"],
                )
                if gate.get("blocked"):
                    raise RoomError(gate.get("code", "blocked"), gate.get("message", "Chat blocked"))
                native_run, native_run_created = await pool.native_rooms.begin_run(
                    identity.owner_id,
                    admitted["sessionKey"],
                    native_execution_run_id(conn.device.profile_id, admitted),
                )
            try:
                result = await conn.rpc(method, admitted)
            except RoomError as exc:
                if native_run is not None and native_run_created and getattr(exc, "native_rejection", False):
                    await pool.native_rooms.finish_run(*native_run)
                raise
            if native_run is not None:
                try:
                    wait_result = await conn.rpc("agent.wait", {"runId": native_run[2], "timeoutMs": 0})
                    if is_terminal_agent_wait_result(wait_result, native_run[2], admitted["expectedSessionId"]):
                        await pool.native_rooms.finish_run(*native_run)
                except Exception:
                    # Reconciliation is observational. Unknown/transport
                    # outcomes retain the durable hold and cannot rewrite the
                    # already-successful dispatch response.
                    logger.warning("Native dispatch reconciliation remains unresolved", exc_info=True)
            try:
                await conn.require_current()
            except RoomError:
                await pool.native_rooms.close_connection(connection_id)
                if method in UNCERTAIN_AFTER_SUCCESS:
                    raise RoomError(
                        "ROOM_UNAVAILABLE",
                        "Native request outcome is uncertain; reconcile canonical room state",
                    )
                raise
            if method == "sessions.message.append":
                require_append_result(result, admitted)
            if isinstance(result, dict):
                for field in ("key", "sessionKey", "roomKey"):
                    if field in result and result[field] != key:
                        raise RoomError("FORBIDDEN", "Native canonical target changed")
            result = filter_rpc_result(method, result, identity, qualification)
            if method == "sessions.messages.subscribe":
                conn.subscriptions.add(admitted["key"])
            elif method == "sessions.messages.unsubscribe":
                conn.subscriptions.discard(admitted["key"])
            elif method == "sessions.presence.subscribe":
                conn.presence_subscriptions.add(admitted["sessionKey"])
            elif method == "sessions.presence.unsubscribe":
                conn.presence_subscriptions.discard(admitted["sessionKey"])
            result = _fit_rpc_result_for_frame(method, result)
            if len(json.dumps(result, separators=(",", ":"), default=str).encode()) > _WS_FRAME_BUDGET_BYTES:
                raise RoomError("ROOM_UNAVAILABLE", "Native room result exceeds transport limit")
        await management.send_message_async(connection_id, {"type": "res", "id": req_id, "ok": True, "payload": result})
    except RoomError as exc:
        if exc.error.get("code") == "FORBIDDEN":
            await pool.native_rooms.close_connection(connection_id)
        await management.send_message_async(
            connection_id, {"type": "res", "id": req_id, "ok": False, "error": exc.error}
        )
    except Exception:
        logger.exception("Native room request failed method=%s", method)
        # Timeout/transport errors are not replayed; the client reconciles its
        # deterministic idempotency key against native history after reconnect.
        await management.send_message_async(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": False,
                "error": {"code": "ROOM_UNAVAILABLE", "message": "Restore canonical history before retrying"},
            },
        )


async def _guard_seeded_delete(
    *,
    owner_id: str,
    method: str,
    params: dict | None,
    connection_id: str,
    req_id: str,
) -> bool:
    """Reject deletion of seeded agents (Friday/Atlas) BEFORE forwarding the
    RPC to OpenClaw (the post-forward cleanup is too late — the container has
    already deleted the agent by then).

    Returns True if it consumed the request (sent an error res; caller must
    return without forwarding). False otherwise (caller forwards normally).
    """
    if method != "agents.delete":
        return False
    agent_id = (params or {}).get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return False
    if not await is_seeded_agent(owner_id=owner_id, agent_id=agent_id):
        return False
    mgmt = management_api or await get_management_api_client()
    mgmt.send_message(
        connection_id,
        {
            "type": "res",
            "id": req_id,
            "ok": False,
            "error": {
                "message": "Friday and Atlas are managed by Lightbulb and can't be deleted.",
            },
        },
    )
    return True


# Methods that carry a CLIENT-SUPPLIED session key which MUST be
# ownership-checked before we forward it to the shared per-owner container.
# Maps method -> the param name that holds the key. sessions.patch/reset/delete
# use ``key``; the chat.history read and OpenClaw 8.1's transcript-DAG RPCs use
# ``sessionKey``.
#
# ANY new gateway method that takes a caller-supplied session key belongs in
# this table. Omitting one is a cross-tenant IDOR on every org: every member
# shares ONE per-owner container, so an unguarded key param lets member A name
# member B's session (or a ``paperclip:``/``cron:`` one).
_SESSION_KEY_PARAM_BY_METHOD = {
    "sessions.patch": "key",
    "sessions.reset": "key",
    "sessions.delete": "key",
    "chat.history": "sessionKey",
    # sessions.rewind and sessions.branches.switch destructively repoint the
    # transcript; sessions.branches.list leaks another member's branch
    # headlines and message counts.
    "sessions.rewind": "sessionKey",
    "sessions.branches.list": "sessionKey",
    "sessions.branches.switch": "sessionKey",
    "chat.message.get": "sessionKey",
}

# Methods whose client-supplied session keys arrive as a LIST. Every key
# must be owned or the call is refused; a key-less call is scoped by the
# response post-filter instead (``_filter_search_hits_for_user``).
_SESSION_KEYS_LIST_PARAM_BY_METHOD = {
    "sessions.search": "sessionKeys",
}


# Params a client may NEVER set, whatever session they aim at. This is a
# separate axis from the key tables above: those ask "is this key yours?", and
# for these the answer is YES — the caller patches their OWN session, so every
# ownership check passes. The danger is the field, not the target.
#
# ``permissionMode`` maps straight onto core-tool policy in OpenClaw
# (src/agents/session-permission-exec-mode.ts): "full" yields execMode "full",
# workspaceOnly false, applyPatchWorkspaceOnly false and
# bypassHostApprovalFloors true. Writing it requires ``operator.admin``, which
# our proxy supplies on every member's behalf — so on a shared per-owner
# container one hand-crafted frame buys exec outside the caller's workspace,
# which on a shared EFS mount means every other member's ``workspace-*`` and
# the container environment (where a BYO LLM key lives).
#
# Reject the FIELD, not a blocklist of values: the enum belongs to upstream and
# a value-based guard would go quietly stale on the next base bump.
#
# Nothing of ours sends it — a repo-wide grep finds only an unrelated ACP block
# in core/containers/config.py — so this costs no functionality.
_FORBIDDEN_PARAMS_BY_METHOD: dict[str, frozenset[str]] = {
    "sessions.patch": frozenset({"permissionMode"}),
}


async def _guard_session_key_ownership(
    *,
    user_id: str,
    method: str,
    params: dict | None,
    connection_id: str,
    req_id: str,
) -> bool:
    """Reject session-key-bearing RPCs whose key the caller doesn't own, BEFORE
    forwarding to OpenClaw.

    In an org, EVERY member shares ONE per-owner container, so member A could
    hand-craft a ``key`` targeting member B's — or a ``paperclip:``/``cron:`` —
    session and destroy/mutate it (sessions.reset/delete/patch) or read its
    transcript (chat.history). That is a cross-tenant IDOR. sessions.list is
    already post-filtered (``_filter_sessions_for_user``); this guards the
    key-carrying mutators + the history read on the way IN, using the same
    ownership predicate.

    It guards TWO axes, which fail differently:

    1. **Ownership** — is this ``key`` yours? Catches member A aiming at member
       B's session, or at a ``paperclip:``/``cron:`` one.
    2. **Privileged params** (``_FORBIDDEN_PARAMS_BY_METHOD``) — fields a client
       may never set even on a session they DO own. Ownership cannot catch these
       precisely because the check passes: the caller escalates their own
       session. See that table for why ``permissionMode`` is on it.

    Returns True if it consumed the request (sent an error res; caller must
    return without forwarding). False otherwise (caller forwards normally):
    - method carries no forbidden param AND is not in the key-bearing set, OR
    - the call carries NO client key param (a key-less variant — nothing to
      forge; leave it to the container / the sessions.list post-filter).
    """
    forbidden = _FORBIDDEN_PARAMS_BY_METHOD.get(method)
    if forbidden:
        present = sorted(forbidden & set(params or {}))
        if present:
            logger.warning(
                "[%s] rejected %s carrying privileged param(s) %s",
                user_id,
                method,
                ",".join(present),
            )
            mgmt = management_api or await get_management_api_client()
            mgmt.send_message(
                connection_id,
                {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    "error": {"message": "That session setting cannot be changed here."},
                },
            )
            return True

    param_name = _SESSION_KEY_PARAM_BY_METHOD.get(method)
    list_param_name = _SESSION_KEYS_LIST_PARAM_BY_METHOD.get(method)
    if param_name is None and list_param_name is None:
        return False
    if param_name is not None:
        key = (params or {}).get(param_name)
        if not isinstance(key, str) or not key:
            # No client-supplied key — nothing to validate. Forward as-is.
            return False
        if _user_owns_session_key(key, user_id):
            return False
    else:
        keys = (params or {}).get(list_param_name)
        if not isinstance(keys, list) or not keys:
            # Key-less search: the container scans the whole agent; the
            # response post-filter scopes the hits to the caller.
            return False
        foreign = [k for k in keys if not _user_owns_session_key(k, user_id)]
        if not foreign:
            return False
        key = foreign[0]
    logger.warning(
        "[%s] rejected %s targeting non-owned session key=%s",
        user_id,
        method,
        key,
    )
    mgmt = management_api or await get_management_api_client()
    mgmt.send_message(
        connection_id,
        {
            "type": "res",
            "id": req_id,
            "ok": False,
            "error": {"message": "You don't have access to that session."},
        },
    )
    return True


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

    # At the plan cap, Paperclip is allowed to replay this exact logical create
    # but atomically refuses a missing key. That lets a lost-response retry heal
    # its original UUID without opening an 11th roster slot.
    from core.services.agent_budget import AGENT_BUDGET_EXCEEDED_DETAIL, is_at_agent_budget

    at_agent_budget = await is_at_agent_budget(owner_id)
    try:
        paperclip_agent = await create_paperclip_for_chat(
            creator_user_id=user_id,
            owner_id=owner_id,
            name=name,
            role=role,
            model=model,
            meta=meta,
            idempotency_replay_only=at_agent_budget,
        )
    except PaperclipAgentCreateReplayMiss:
        management_api = await get_management_api_client()
        await management_api.send_message_async(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": False,
                "error": {
                    "code": "agent_budget_exceeded",
                    "message": AGENT_BUDGET_EXCEEDED_DETAIL,
                    "details": {"reason": "agent_budget_exceeded"},
                },
            },
        )
        return True

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

    # Register the new agent's operator device into the RUNNING container's
    # paired.json + bind its authToken to the gateway token, so its first
    # relayed connect isn't rejected NOT_PAIRED. The provision-time
    # register_paperclip_agent_devices only covers agents that exist at
    # provision; this closes the gap for post-provision creates. Best-effort.
    await ensure_operator_device_paired(
        owner_id=owner_id,
        paperclip_agent=paperclip_agent,
        gateway_token=token,
        ip=ip,
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
    await management_api.send_message_async(
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


# Non-fatal retry budget for the inner Paperclip mirror PATCH. The
# mirror is best-effort (the reconciler
# ``scripts.reconcile_paperclip_profile_meta`` is the durable backstop),
# so this only smooths over transient blips — it never changes the
# best-effort contract.
_PAPERCLIP_MIRROR_MAX_ATTEMPTS = 3


async def _mirror_paperclip_meta_with_retry(
    *,
    admin: "PaperclipAdminClient",
    cookie: str,
    company_id: str,
    owner_id: str,
    agent_id: str,
    role: str,
    title: str | None,
    capabilities: str | None,
    icon: str | None,
) -> None:
    """Confirm the agent belongs to ``company_id`` then PATCH its
    Paperclip profile-meta, with a small non-fatal retry on transient
    failures.

    Retries only on transient errors — a ``PaperclipApiError`` whose
    ``retryable`` flag is set (5xx / 429) or any ``httpx.HTTPError``
    (transport blip). Permanent 4xx state errors (e.g. a 422 bad role)
    are NOT retried — they re-raise immediately. After the attempt
    budget is exhausted the last error re-raises so the caller's outer
    ``except`` logs it once; this helper NEVER swallows on its own, so
    the caller keeps full control of save-success/failure semantics.

    The cross-company scope check (the PATCH hits the unscoped
    ``/api/agents/{id}`` with the bootstrap admin cookie) runs once,
    before any retry — a foreign-tenant agent id is skipped, not
    retried.
    """
    target = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
    if target.get("companyId") != company_id:
        logger.warning(
            "[%s] paperclip meta mirror skipped: agent=%s not in company=%s",
            owner_id,
            agent_id,
            company_id,
        )
        return

    for attempt in range(_PAPERCLIP_MIRROR_MAX_ATTEMPTS):
        try:
            await admin.update_agent(
                session_cookie=cookie,
                agent_id=agent_id,
                role=role,
                title=title,
                capabilities=capabilities,
                icon=icon,
            )
            return
        except (PaperclipApiError, httpx.HTTPError) as e:
            retryable = isinstance(e, httpx.HTTPError) or getattr(e, "retryable", False)
            is_last = attempt == _PAPERCLIP_MIRROR_MAX_ATTEMPTS - 1
            if not retryable or is_last:
                # Permanent error, or out of attempts — surface it so the
                # caller's outer best-effort ``except`` logs it once.
                raise
            await asyncio.sleep(0.2 * 2**attempt)


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
    if not isinstance(rpc_params_inner, dict):
        return False  # No meta in payload — nothing to mirror.
    pc_block = rpc_params_inner.get("paperclip")
    if not isinstance(pc_block, dict):
        return False

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
                # title/capabilities/icon come from the RAW pc_block
                # (not from ``meta``, which normalizes "" → None via
                # read_from_agent_entry). The UI sends ``""`` to clear
                # a field; that empty string has to reach Paperclip's
                # PATCH as an explicit value for the clear to take
                # effect. ``role`` stays normalized because invalid /
                # blank role values must fall back to "general" — an
                # empty role would 422 Paperclip's enum.
                pc_title = pc_block.get("title") if isinstance(pc_block.get("title"), str) else None
                pc_capabilities = (
                    pc_block.get("capabilities") if isinstance(pc_block.get("capabilities"), str) else None
                )
                pc_icon = pc_block.get("icon") if isinstance(pc_block.get("icon"), str) else None
                # The mirror PATCH hits the unscoped /api/agents/{id} with
                # the bootstrap admin cookie, which can reach any
                # company's agent. agent_id is client-supplied, so the
                # helper confirms the target belongs to THIS owner's
                # company before mutating it — otherwise a caller could
                # pass another tenant's agent id and have us write to
                # their row. The helper retries transient failures but
                # NEVER swallows — it re-raises so this block's best-effort
                # except is the single place the mirror error is logged.
                await _mirror_paperclip_meta_with_retry(
                    admin=admin,
                    cookie=cookie,
                    company_id=company_id,
                    owner_id=owner_id,
                    agent_id=agent_id,
                    role=meta.role,
                    title=pc_title,
                    capabilities=pc_capabilities,
                    icon=pc_icon,
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
    #
    # Order matters: this MUST run BEFORE the ``params`` strip below.
    # We read straight from the raw ``pc_block`` here (not the
    # ``meta`` dataclass) so the UI's empty-string-clear contract
    # round-trips: a field sent as ``""`` lands in openclaw.json as
    # ``""`` and is then pruned post-merge by ``apply_paperclip_meta``
    # so the file never carries empty values. ``meta``'s read path
    # already normalized whitespace strings to ``None``, which would
    # break the "explicitly clear this field" semantic.
    try:
        from core.services.config_patcher import apply_paperclip_meta

        meta_block: dict = {"role": meta.role}
        for key in ("title", "capabilities", "icon"):
            raw = pc_block.get(key)
            if isinstance(raw, str):
                meta_block[key] = raw.strip()
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
    #
    # NOTE: the file write + Paperclip PATCH above already captured
    # everything they need from ``params`` before this point. Do NOT
    # move this strip ahead of those calls.
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
    # Re-bind correlation ids onto the log contextvars: this runs in a detached
    # BackgroundTask, so the HTTP-middleware binds did not propagate here. Use
    # the WS request id as the run_id so all log lines for this RPC are
    # queryable. (See bind_log_context docstring.)
    bind_log_context(user_id=user_id, run_id=req_id)
    # Log non-noisy RPCs at INFO (skip health/channels.status which fire every 3s)
    if method not in ("health", "channels.status"):
        logger.info("[%s] rpc %s owner=%s conn=%s", user_id, method, owner_id, connection_id[:12])
    management_api = await get_management_api_client()

    # One entrance per run: the raw RPC proxy does not dispatch agent runs.
    # See _RUN_DISPATCH_METHODS. Refuse before touching ECS or the pool so the
    # call is a no-op on the container, and point the caller at the typed
    # message that owns this job.
    if method in _RUN_DISPATCH_METHODS:
        use = _RUN_DISPATCH_ALTERNATIVE[method]
        logger.warning(
            "[%s] rpc %s refused (run dispatch is not proxied; use %s) owner=%s conn=%s",
            user_id,
            method,
            use,
            owner_id,
            connection_id[:12],
        )
        put_metric("rpc.refused", dimensions={"method": method, "reason": "run_dispatch"})
        await management_api.send_message_async(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": (
                        f'"{method}" is not available over the RPC proxy — it would start an '
                        "agent run outside the chat path. Send a WebSocket message of type "
                        f'"{use}" instead.'
                    ),
                    "details": {"method": method, "use": use},
                },
            },
        )
        return

    # Default-deny. See _PROXY_ALLOWED_METHODS. Refuse before touching ECS or the pool
    # so a denied call is a no-op on the container. This runs AFTER the run-dispatch
    # check above so chat.send keeps its more useful "use agent_chat" message.
    if method not in _PROXY_ALLOWED_METHODS:
        logger.warning(
            "[%s] rpc %s refused (not on the proxy allowlist) owner=%s conn=%s",
            user_id,
            method,
            owner_id,
            connection_id[:12],
        )
        put_metric("rpc.refused", dimensions={"method": method, "reason": "not_allowlisted"})
        await management_api.send_message_async(
            connection_id,
            {
                "type": "res",
                "id": req_id,
                "ok": False,
                "error": {
                    "code": "INVALID_REQUEST",
                    "message": f'"{method}" is not available over the RPC proxy.',
                    "details": {"method": method},
                },
            },
        )
        return

    try:
        ecs_manager = get_ecs_manager()
        container, ip = await ecs_manager.resolve_running_container(owner_id)

        if not container:
            await management_api.send_message_async(
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
            await management_api.send_message_async(
                connection_id,
                {
                    "type": "res",
                    "id": req_id,
                    "ok": False,
                    # A task with no IP is USUALLY waking (still pulling the
                    # image) but can also be one that died and is being
                    # replaced — resolve_running_container cannot tell them
                    # apart, so `retryable` here is a hint, not a liveness
                    # proof. Clients must bound how long they present it as
                    # loading (see useWarmingWindow); nothing downstream may
                    # treat it as "wait forever".
                    #
                    # Deliberately NO `code`: shipped mobile builds string-join
                    # an error's `code` into the message they display, so
                    # adding one would regress the installed base to
                    # "...try again in a moment. — [UNAVAILABLE]" — the exact
                    # class of message this change exists to remove.
                    # `retryable` and `retryAfterMs` are ignored by those
                    # older clients.
                    "error": {
                        "message": "Container is starting up. Try again in a moment.",
                        "retryable": True,
                        "retryAfterMs": 3000,
                    },
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
                    await management_api.send_message_async(
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

        if await _guard_seeded_delete(
            owner_id=owner_id,
            method=method,
            params=params,
            connection_id=connection_id,
            req_id=req_id,
        ):
            return

        # Cross-tenant IDOR guard: reject sessions.patch/reset/delete +
        # chat.history whose client-supplied key belongs to another member or a
        # paperclip:/cron: session on this shared per-owner container.
        if await _guard_session_key_ownership(
            user_id=user_id,
            method=method,
            params=params,
            connection_id=connection_id,
            req_id=req_id,
        ):
            return

        # Preserve durable business state before OpenClaw removes the agent.
        # Paperclip's native delete otherwise hard-deletes assigned routines
        # and unassigns open issues. This preflight is idempotent, so the Teams
        # REST path and a retried gateway delete converge on the same state.
        if method == "agents.delete":
            agent_id = (params or {}).get("agentId")
            if isinstance(agent_id, str) and agent_id:
                await prepare_paperclip_agent_delete(owner_id=owner_id, agent_id=agent_id)

        pool = get_gateway_pool()
        agent_id = (params or {}).get("agentId") if method == "agents.delete" else None
        if isinstance(agent_id, str) and agent_id:
            result = await delete_runtime_agent_with_lock_retry(
                gateway=pool,
                owner_id=owner_id,
                agent_id=agent_id,
                ip=ip,
                token=container["gateway_token"],
            )
        else:
            result = await pool.send_rpc(
                user_id=owner_id,
                req_id=req_id,
                method=method,
                params=params,
                ip=ip,
                token=container["gateway_token"],
            )
        # Scope sessions.list to the caller — the container returns every
        # session on the shared per-owner box (paperclip/cron/other members).
        if method == "sessions.list":
            result = _filter_sessions_for_user(result, user_id)
        elif method == "sessions.search":
            result = _filter_search_hits_for_user(result, user_id)
        # Keep every res frame deliverable — see _fit_rpc_result_for_frame.
        result = _fit_rpc_result_for_frame(method, result)
        await management_api.send_message_async(
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
        # `cleanup_deleted_agent` runs the sync EFS `shutil.rmtree` on a worker
        # thread — those dirs
        # can be large enough to block the event loop and stall unrelated
        # WS chat traffic, so it runs on a worker thread via `to_thread`.
        if method == "agents.delete":
            agent_id = (params or {}).get("agentId")
            if isinstance(agent_id, str) and agent_id:
                try:
                    from core.containers.workspace import cleanup_deleted_agent

                    await cleanup_deleted_agent(owner_id, agent_id)
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
            # Deliberately NOT classified as transient here. An earlier
            # attempt substring-matched this error's text for "still coming
            # up" markers and was wrong three ways: `str(e)` is the whole
            # serialized OpenClaw error dict (so `details` text could flip a
            # real rejection to retryable, and it kept its `code`); the
            # dominant early cold-start failure is ConnectionRefusedError, an
            # OSError that never reaches this handler at all; and two of the
            # markers ("Gateway connection lost", "Connection closed") are
            # raised immediately AFTER the pool emits GATEWAY_DOWN_DEAD — the
            # death signal, labelled as birth. "Is the container coming up?"
            # is answered by /container/status, which owns that question.
            await management_api.send_message_async(
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
            await management_api.send_message_async(
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
    side: bool = False,
    session_id: str | None = None,
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

    side (`/btw` side-channel): the wire call is an ordinary chat.send — the
    container recognizes the `/btw` prefix in ``message`` and runs it as a
    non-blocking side run that never enters the session's active-run window.
    On our side that means we must NOT call ``mark_run_active`` for a side
    send: a side run does not own the parked-vs-dead window, so it is tracked
    separately (``mark_side_run``) and leaves the primary run's window intact.
    """
    # Re-bind correlation ids onto the log contextvars: this runs in a detached
    # BackgroundTask, so the HTTP-middleware binds did not propagate here. Bind
    # the frontend-minted client_run_id as the run_id when present so every log
    # line for this turn is queryable. (See bind_log_context docstring.)
    bind_log_context(user_id=user_id, run_id=client_run_id)
    logger.info(
        "[%s] chat.send START agent=%s owner=%s conn=%s msg_len=%d",
        user_id,
        agent_id,
        owner_id,
        connection_id[:12],
        len(message),
    )

    management_api = await get_management_api_client()

    # Tracks whether mark_run_active opened the busy-gate window below. Bound
    # before the try so the error fall-through can safely decide whether to
    # tear the window back down even if an exception fires before `pool` is
    # assigned. Only ever True for non-side sends past the mark_run_active line.
    run_active_marked = False

    # Every error frame this function hand-builds must carry the request's
    # session_id (when the send targeted a named session): the frontend
    # demuxes error frames by session and DROPS frames whose session doesn't
    # match the active one — without this, a named-session user never sees
    # the error and their busy gate sticks. General-session sends
    # (session_id None) omit the key, which the demux reads as null.
    err_session = {"session_id": session_id} if session_id else {}

    try:
        # Look up user's container and discover task IP
        ecs_manager = get_ecs_manager()
        container, ip = await ecs_manager.resolve_running_container(owner_id)

        if not container:
            # resolve_running_container returns (None, None) for BOTH a truly
            # absent row and a row that exists but is scaled to zero
            # ("stopped" — the free-tier reaper's scale-to-zero path). Re-fetch
            # the row to tell them apart: a stopped box is merely asleep, and
            # the next GET /container/status poll wakes it (ensure_user_ready),
            # so telling the user to "subscribe" is both wrong and alarming.
            # Emit a distinct `container_waking` frame + metric reason so the
            # client can trigger that wake and dashboards can separate "asleep"
            # from "never had one". Best-effort: a re-fetch failure falls back
            # to the legacy copy (mirrors the enriched-log re-fetch below).
            row = None
            try:
                from core.repositories import container_repo as _cr

                row = await _cr.get_by_owner_id(owner_id)
            except Exception:
                logger.warning(
                    "[%s] chat.send: container status re-fetch failed for owner=%s",
                    user_id,
                    owner_id,
                )
            if row and row.get("status") == "stopped":
                # Server-side wake: a message IS intent, and it must wake the
                # container on every client version ever shipped — the client
                # cannot be trusted to follow up (a hidden tab's polling is
                # paused, an old bundle knows nothing about wake gating). The
                # dispatch is lease-idempotent and stands down on
                # conversion_pending and trial gates; the frame below is
                # emitted regardless — the wake is a side effect, not the
                # reply. This makes the historical "the WS path never wakes"
                # invariant intentionally FALSE for the stopped branch.
                from core.services import container_wake as _cw

                await _cw.request_wake_silent(owner_id)
                put_metric("chat.error", dimensions={"reason": "container_waking", "reclassified": "false"})
                logger.info(
                    "[%s] chat.send: container stopped (scaled to zero) for owner=%s — wake dispatched",
                    user_id,
                    owner_id,
                )
                await management_api.send_message_async(
                    connection_id,
                    {
                        "type": "error",
                        "agent_id": agent_id,
                        "code": "container_waking",
                        "message": "Waking your agent — this takes about a minute.",
                        **err_session,
                    },
                )
                return

            put_metric("chat.error", dimensions={"reason": "container_unreachable", "reclassified": "false"})
            logger.warning("[%s] chat.send FAIL: no container for owner=%s", user_id, owner_id)
            await management_api.send_message_async(
                connection_id,
                {
                    "type": "error",
                    "agent_id": agent_id,
                    "message": "No container provisioned. Please subscribe to start chatting.",
                    **err_session,
                },
            )
            return

        if not ip:
            put_metric("chat.error", dimensions={"reason": "container_unreachable", "reclassified": "false"})
            logger.warning("[%s] chat.send FAIL: no IP for owner=%s (container starting)", user_id, owner_id)
            await management_api.send_message_async(
                connection_id,
                {
                    "type": "error",
                    "agent_id": agent_id,
                    "message": "Your agent is starting up. Please try again in a moment.",
                    **err_session,
                },
            )
            return

        pool = get_gateway_pool()
        req_id = str(uuid4())

        # Session key: server-built agent:{agentId}:{userId}[:{sessionId}].
        # Every user gets their own session, isolating their chat history from
        # cron jobs, channels, and other system activity. session_id (client-
        # minted UUID) opens a distinct conversation; None = the "General" session.
        session_key = build_chat_session_key(agent_id, user_id, session_id)

        logger.info(
            "[%s] chat.send RPC agent=%s sessionKey=%s ip=%s",
            user_id,
            agent_id,
            session_key,
            ip,
        )

        # --- Node binding: pin this session to the user's CLI device if connected ---
        # Exec is bound to the CLI bucket specifically. The Mac companion
        # (clientId=openclaw-macos) is a sibling node for passkey/UI
        # affordances, but execNode must point at the CLI.
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
                # default to "auto" (exec-defaults.ts): the agent
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
                        # openclaw/src/gateway/server-methods/sessions.ts).
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

        # Card-3 hard-stop when balance ≤ 0, AND any provider hard-stop
        # when subscription is inactive.
        # provider_choice + credit balance live on the Clerk user row,
        # so we pass user_id; subscription_status lives on the billing
        # row (org_id in org context, user_id in personal), so we also
        # pass owner_id. Cards 1 + 2 still skip the credit-balance gate
        # — they aren't billed against prepaid credits — but they DO
        # have to honor the subscription gate so a canceled-sub user
        # can't keep talking through the gateway indefinitely.
        gate = await pool.gate_chat(user_id=user_id, owner_id=owner_id, session_key=session_key)
        if gate.get("blocked"):
            put_metric("chat.error", dimensions={"reason": gate.get("code", "blocked"), "reclassified": "false"})
            logger.info(
                "[%s] chat.send BLOCKED agent=%s code=%s",
                user_id,
                agent_id,
                gate.get("code"),
            )
            try:
                await management_api.send_message_async(
                    connection_id,
                    {
                        "type": "error",
                        "code": gate.get("code", "blocked"),
                        "message": gate.get("message", "Chat blocked"),
                        "agent_id": agent_id,
                        **err_session,
                    },
                )
            except Exception:
                logger.warning("[%s] failed to deliver blocked-chat error event", user_id)
            return

        # NO server-side serialize gate. We mirror upstream's concurrency model:
        # the frontend QUEUES sends while a run is active (clientRunIdRef +
        # observed-run adoption) and never fires a second, and the container's
        # per-session JSONL write-lock serializes anything that still races (it
        # retries up to 60s — see upstream session-write-lock.ts). Upstream's web
        # UI has no server gate either; an earlier Isol8 `agent_busy` gate was an
        # extra layer that diverged from that model (and caused side-run / steer
        # / multi-tab interaction bugs the single-client UI never has), so it is
        # removed in favor of the upstream three-layer approach.

        # Record the run for the parked-vs-dead reconnect decision — this is NOT
        # a gate. If the WS to OpenClaw tears down mid-turn (ping timeout during
        # a long Bedrock call), `_on_reader_loop_exit` keys on whether
        # `_active_runs` is non-empty to PARK the connection (reopen + preserve
        # state) instead of treating it as dead. mark_run_active no-ops without a
        # connection and send_rpc creates it lazily, so on the FIRST send to a
        # fresh task we ensure_connection first; send_rpc routes through the same
        # method, so this is idempotent (the second resolution hits the fast
        # path). Skip for side (`/btw`) runs — they're tracked separately
        # (mark_side_run) so their terminal can't clear the primary run's
        # parked-vs-dead window.
        #
        # The runId this send is bound to: == the chat.send idempotencyKey ==
        # the runId OpenClaw stamps on every event for this run (src/gateway/server-methods/chat.ts).
        # Computed up-front so a side run can be registered under the SAME id its
        # terminal will carry.
        run_id_for_send = client_run_id or str(uuid4())
        # Stamp the run's actor at dispatch: a chat.send off a client WS frame
        # (normal or /btw side send) is human-initiated. Read back by the
        # gateway terminal's agent_run_completed emit. See run_analytics.
        run_analytics.record_run_actor(owner_id, run_id_for_send, "human")

        if not side:
            await pool.ensure_connection(owner_id, ip, container["gateway_token"])
            # user_id is the authenticated MEMBER (JWT sub, canonical/mixed-case);
            # carry it so the run-completion push targets the users-table PK
            # (NOT the lowercased member parsed from the echoed session key).
            pool.mark_run_active(owner_id, session_key, agent_id, member_id=user_id)
            run_active_marked = True
        else:
            # Side (`/btw`) run: register its runId so the agent-event handler
            # never clears the PRIMARY run's session window when this side run
            # ends (both share the session_key). ensure_connection first so the
            # registration lands on the live connection the events arrive on.
            await pool.ensure_connection(owner_id, ip, container["gateway_token"])
            pool.mark_side_run(owner_id, run_id_for_send)

        # Idempotency key for OpenClaw's chat.send. Everything below lives in
        # upstream src/gateway/server-methods/chat.ts.
        #
        # We always send one so upstream's dedupe
        # (`context.dedupe.get(`chat:${clientRunId}`)`) and its in-flight
        # detection (`context.chatAbortControllers.get(clientRunId)`) have a
        # stable key to match against. When the frontend supplies `clientRunId`
        # we use it, so the frontend can correlate the eventual chat.final back
        # to its send (drives the local chat queue drain). When absent (old
        # client) we mint a backend UUID — matches the pre-clientRunId
        # behavior. Upstream calls `context.addChatRun(clientRunId, ...)` with
        # whichever value we pass, and reads it back as the runId.
        #
        # deliver:false matches upstream's web-UI requestChatSend
        # (ui/src/ui/controllers/chat.ts) for EVERY web send — normal, steer,
        # and /btw alike: a direct streaming client gets the reply over the
        # socket and must NOT also re-broadcast it to external channels. The
        # server only delivers externally on `params.deliver === true`, so this
        # is the explicit form of the previous omitted default (verified
        # behaviorally identical), shared by normal + side sends.
        chat_send_params: dict[str, object] = {
            "sessionKey": session_key,
            "message": message,
            "idempotencyKey": run_id_for_send,
            "deliver": False,
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

        # The chat.e2e.latency SLO span (alarm W9) opens here implicitly: the
        # mark_run_active call above stamped the run's started_at on the pool's
        # active-run window (BEFORE this chat.send RPC was forwarded). The END
        # is emitted from that same window on the terminal chat event in the
        # gateway reader loop (connection_pool._handle_message), so no separate
        # start-record is needed here — the pool already tracks the start.

    except asyncio.TimeoutError:
        chat_err = "RPC timed out"
        put_metric("chat.error", dimensions={"reason": "timeout", "reclassified": "false"})
        logger.error("[%s] chat.send TIMEOUT agent=%s", user_id, agent_id)
    except ConnectionError as e:
        chat_err = str(e)
        put_metric("chat.error", dimensions={"reason": "container_unreachable", "reclassified": "false"})
        logger.error("[%s] chat.send CONNECT FAIL agent=%s: %s", user_id, agent_id, e)
    except RuntimeError as e:
        chat_err = str(e)
        put_metric("chat.error", dimensions={"reason": "gateway_error", "reclassified": "false"})
        logger.error("[%s] chat.send GATEWAY ERROR agent=%s: %s", user_id, agent_id, e)
    except Exception as e:
        chat_err = str(e)
        put_metric("chat.error", dimensions={"reason": "unknown", "reclassified": "false"})
        logger.error("[%s] chat.send FAIL agent=%s: %s", user_id, agent_id, e)
    else:
        return  # success — no error to report

    # All error branches fall through here to notify the client.
    #
    # mark_run_active above opened the parked-vs-dead window for this session
    # (non-side sends only). chat.send never reached OpenClaw, so no terminal
    # event will ever arrive to close that window — clear it here, otherwise a
    # stale entry would keep the connection parking on a WS drop indefinitely.
    # The flag guarantees pool + session_key are bound (the mark is past both).
    if run_active_marked:
        pool.clear_active_run(owner_id, session_key)

    try:
        await management_api.send_message_async(
            connection_id,
            {
                "type": "error",
                "agent_id": agent_id,
                "message": f"Failed to send message: {chat_err}",
                **err_session,
            },
        )
    except Exception:
        pass


async def _process_agent_abort_background(
    connection_id: str,
    user_id: str,
    owner_id: str,
    agent_id: str,
    run_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Cancel the in-flight run for ``agent:{agent_id}:{user_id}``.

    Forwards upstream's ``chat.abort``. Param shape mirrors openclaw's
    abortChatRun (ui/src/ui/controllers/chat.ts): ``{sessionKey, runId}``
    when the runId is known, else ``{sessionKey}`` alone.

    Fire-and-don't-wait-for-terminal: chat.abort only requests cancellation.
    The run's ``aborted`` / ``run_ended`` event arrives via the normal event
    stream, and the pool's existing terminal handler clears the active-run
    window there — so this function deliberately does NOT clear it.
    """
    logger.info(
        "[%s] chat.abort agent=%s owner=%s runId=%s conn=%s",
        user_id,
        agent_id,
        owner_id,
        run_id or "-",
        connection_id[:12],
    )

    async def _notify_aborted_terminal() -> None:
        """Tell the client the run is over so cancelMessage's server-reconciled
        wait unblocks (it keeps isStreaming + the active runId until a SERVER
        terminal arrives). Synthesizes the SAME ``error`` frame a real abort
        produces — connection_pool._transform_agent_event maps the container's
        ``aborted`` event to ``{type:"error", message:"Agent run was
        cancelled"}`` — so the client tears down its live turn and drains the
        queue. Used whenever we cannot confirm the abort reached OpenClaw (no
        container, or the abort RPC failed). If the run is actually still alive
        (a transient RPC failure), the client re-adopts its continuing events
        via runId adoption, so this is safe either way. Best-effort."""
        try:
            mgmt = await get_management_api_client()
            frame: dict[str, str] = {
                "type": "error",
                "agent_id": agent_id,
                "message": "Agent run was cancelled",
            }
            if run_id:
                frame["runId"] = run_id
            mgmt.send_message(connection_id, frame)
        except Exception:
            pass

    try:
        # Built inside the try (mirrors _process_agent_chat_background) so an
        # invalid session_id surfaces the friendly aborted-terminal frame via
        # the except below instead of an unhandled BackgroundTasks exception.
        session_key = build_chat_session_key(agent_id, user_id, session_id)
        ecs_manager = get_ecs_manager()
        container, ip = await ecs_manager.resolve_running_container(owner_id)
        if not container or not ip:
            # No live container/IP means there's no run left in OpenClaw to
            # abort. Unblock the client rather than wedging it in "streaming".
            logger.info(
                "[%s] chat.abort no container/ip agent=%s — sending synthetic aborted terminal",
                user_id,
                agent_id,
            )
            await _notify_aborted_terminal()
            # The original chat.send may have opened the parked-vs-dead window
            # for this session. With no container left to emit a real terminal,
            # that _active_runs entry would never clear — leaving the connection
            # to PARK (treat itself as busy) on every later reader-loop exit.
            # We've just declared the run over to the client, so clear it here.
            # No-ops if no connection exists for the owner.
            get_gateway_pool().clear_active_run(owner_id, session_key)
            return

        params: dict[str, str] = {"sessionKey": session_key}
        if run_id:
            params["runId"] = run_id

        pool = get_gateway_pool()
        await pool.send_rpc(
            user_id=owner_id,
            req_id=str(uuid4()),
            method="chat.abort",
            params=params,
            ip=ip,
            token=container["gateway_token"],
        )
        logger.info("[%s] chat.abort SENT agent=%s runId=%s", user_id, agent_id, run_id or "-")
    except Exception as e:
        # The abort RPC failed AFTER a container was resolved (RPC timeout,
        # gateway reconnect failure, dead backend<->container WS). Don't leave
        # the client wedged: cancelMessage keeps isStreaming until a SERVER
        # terminal arrives, and if the WS to the container is gone, the run's
        # own terminal will never reach us. Synthesize the aborted terminal so
        # the client unblocks (runId adoption recovers it if the run is in fact
        # still alive after a transient failure).
        put_metric("chat.error", dimensions={"reason": "abort_failed", "reclassified": "false"})
        logger.warning(
            "[%s] chat.abort FAIL agent=%s: %s — sending synthetic aborted terminal",
            user_id,
            agent_id,
            e,
        )
        await _notify_aborted_terminal()


async def _process_agent_steer_background(
    connection_id: str,
    user_id: str,
    owner_id: str,
    agent_id: str,
    text: str,
    session_id: str | None = None,
) -> None:
    """Inject ``text`` into the in-flight run for ``agent:{agent_id}:{user_id}``.

    Upstream's steer (sendSteerChatMessage -> requestChatSend,
    ui/src/ui/controllers/chat.ts) is an ordinary ``chat.send`` with
    ``deliver:false`` and a freshly-generated ``idempotencyKey`` on the SAME
    sessionKey. The container's queue-mode "steer" merges the message into the
    active run rather than starting a new turn — so there is no separate steer
    RPC at the chat layer, and we do NOT open a new active-run window here (the
    original agent_chat send owns it).
    """
    logger.info(
        "[%s] chat.steer agent=%s owner=%s conn=%s text_len=%d",
        user_id,
        agent_id,
        owner_id,
        connection_id[:12],
        len(text),
    )
    management_api = await get_management_api_client()
    try:
        # Built inside the try (mirrors _process_agent_chat_background) so an
        # invalid session_id surfaces the friendly steer-failed error frame via
        # the except below instead of an unhandled BackgroundTasks exception.
        session_key = build_chat_session_key(agent_id, user_id, session_id)
        ecs_manager = get_ecs_manager()
        container, ip = await ecs_manager.resolve_running_container(owner_id)
        if not container:
            await management_api.send_message_async(
                connection_id,
                {
                    "type": "error",
                    "agent_id": agent_id,
                    "message": "No container provisioned.",
                },
            )
            return
        if not ip:
            await management_api.send_message_async(
                connection_id,
                {
                    "type": "error",
                    "agent_id": agent_id,
                    "message": "Your agent is starting up. Please try again in a moment.",
                },
            )
            return

        pool = get_gateway_pool()

        # Free-tier gate for the steer path. A steer is a raw chat.send that
        # costs real model tokens (queue-mode merges it into the in-flight run,
        # but it still costs), and it needs only a RUNNING container — not an
        # in-flight run — so without a gate here it is an ungated chat entrance:
        # the primary agent_chat send is gated by gate_chat, but this path
        # forwards its own chat.send.
        #
        # It checks the FULL trial predicate, not just the cap. Checking only
        # the cap left a trial owner whose window had expired — but whose
        # container was still up — able to keep steering (and spending) after
        # gate_chat had already started refusing their sends. Same predicate as
        # gate_chat Layer 2; the steer's actual cost is counted by the meter
        # Lambda at the AWS layer like every other invocation.
        from core.services import provision_gate, trial_cap

        steer_reason = await trial_cap.trial_block_reason(owner_id)
        if steer_reason is not None:
            gate = provision_gate.gate_for_trial_reason(steer_reason, owner_role="admin")
            put_metric("chat.error", dimensions={"reason": steer_reason, "reclassified": "false"})
            logger.info("[%s] chat.steer BLOCKED (%s) agent=%s", user_id, steer_reason, agent_id)
            await management_api.send_message_async(
                connection_id,
                {
                    "type": "error",
                    "code": gate.code,
                    "agent_id": agent_id,
                    "message": gate.message,
                },
            )
            return

        await pool.send_rpc(
            user_id=owner_id,
            req_id=str(uuid4()),
            method="chat.send",
            params={
                "sessionKey": session_key,
                "message": text,
                # deliver:false keeps the steer off external delivery routes
                # (webchat-only injection), matching upstream requestChatSend.
                "deliver": False,
                # Fresh key per upstream generateUUID(); the in-flight run is
                # targeted by sessionKey, not by this idempotencyKey.
                "idempotencyKey": str(uuid4()),
            },
            ip=ip,
            token=container["gateway_token"],
        )
        logger.info("[%s] chat.steer SENT agent=%s", user_id, agent_id)
    except Exception as e:
        put_metric("chat.error", dimensions={"reason": "steer_failed", "reclassified": "false"})
        logger.warning("[%s] chat.steer FAIL agent=%s: %s", user_id, agent_id, e)
        try:
            await management_api.send_message_async(
                connection_id,
                {
                    "type": "error",
                    # A steer is a mid-run injection into an ALREADY-active run —
                    # its failure is NOT terminal for that run (OpenClaw keeps
                    # working). The `steer_failed` code tells the frontend to
                    # surface a notice WITHOUT tearing down the live turn; a
                    # generic no-runId error would otherwise clear clientRunIdRef
                    # + isStreaming and wipe the still-streaming primary response.
                    "code": "steer_failed",
                    "agent_id": agent_id,
                    "message": f"Failed to steer: {e}",
                },
            )
        except Exception:
            pass
