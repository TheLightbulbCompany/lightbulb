"""Owner-only "start a Claude Code / Codex session on my Mac" over the selected-Mac relay.

This side proves WHO may ask (the caller is the member who paired that Mac, the
workspace is on the desktop-execution allowlist) and WHICH of their own ready Macs
answers. The Mac re-validates every field against its own approved folders and
installed CLIs and launches only its fixed binary, visibly on the Mac. No path, flag
or command text here is ever executed as given.

There is no native approve-once prompt (unlike shell): the requester is always the
person the Mac is paired to, starting his own session. `_target` resolves only a ready
row AND a reciprocal pairing that both name the caller's Clerk user, and only that
pairing is granted `session.start.v1`, so no teammate, agent or other workspace member
can ever select someone else's Mac here.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import uuid

from core.auth import AuthContext, resolve_owner_id
from core.observability.product_analytics import capture_product_event
from core.services.desktop_execution import DesktopError, submissions_enabled

OPTIONS_COMMAND = "session.options.v1"
START_COMMAND = "session.start.v1"
COMMANDS = (OPTIONS_COMMAND, START_COMMAND)
MAC_CLIENT_ID = "openclaw-macos"
OPTIONS_MS = 8_000
# Desktop builds before the self-start fix still show a 115 s native prompt; the web
# learns the outcome from a live event either way.
START_MS = 135_000
# The relay caps a node frame at 16 KiB; JSON-escaped non-ASCII can triple the bytes.
MAX_PROMPT_BYTES = 4_096
TOOLS = {
    "claude": {
        "modes": {"auto", "manual", "acceptEdits", "plan"},
        "efforts": {"low", "medium", "high", "xhigh", "max"},
    },
    "codex": {"modes": {"auto-review", "on-request"}, "efforts": {"low", "medium", "high", "xhigh"}},
}
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_SUBCOMMAND = re.compile(r"[a-z][a-z-]*")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
logger = logging.getLogger(__name__)
_background: set[asyncio.Task] = set()
_ANALYTICS_NS = uuid.uuid5(uuid.NAMESPACE_URL, "lightbulb:remote-session-start")


def _capture_start(user_id: str, owner_id: str, start_id: str, tool: str, source: str, event: str) -> None:
    try:
        capture_product_event(
            distinct_id=user_id,
            event=event,
            properties={"workspace_id": owner_id, "start_id": start_id, "provider": tool, "source": source},
            event_uuid=str(uuid.uuid5(_ANALYTICS_NS, f"{owner_id}:{start_id}:{event}")),
        )
    except Exception:
        logger.warning("session start analytics capture failed (event=%s)", event, exc_info=True)


def validate_start(body: dict) -> dict:
    fields = {"startId", "deviceId", "generation", "tool", "folder", "model", "mode", "effort", "prompt"}
    if (
        not isinstance(body, dict)
        or set(body) - fields
        or {"startId", "deviceId", "generation", "tool", "folder"} - set(body)
    ):
        raise DesktopError("invalid_request")
    body = {"model": None, "mode": None, "effort": None, "prompt": "", **body}
    tool, folder, model, mode, prompt = (body[k] for k in ("tool", "folder", "model", "mode", "prompt"))
    if (
        any(not isinstance(body[k], str) or not _ID.fullmatch(body[k]) for k in ("deviceId", "generation"))
        or not isinstance(tool, str)
        or tool not in TOOLS
        or not isinstance(folder, str)
        or not folder.startswith("/")
        or len(folder) > 1024
        or "\x00" in folder
        or (model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)))
        or (mode is not None and (not isinstance(mode, str) or mode not in TOOLS[tool]["modes"]))
        or (
            body["effort"] is not None
            and (not isinstance(body["effort"], str) or body["effort"] not in TOOLS[tool]["efforts"])
        )
        or not isinstance(prompt, str)
        or "\x00" in prompt
        or len(prompt.encode("utf-8", "surrogatepass")) > MAX_PROMPT_BYTES
        # Claude's CLI dispatches a lone subcommand name (mcp, update, ...) even after `--`.
        or (tool == "claude" and _SUBCOMMAND.fullmatch(prompt.strip()))
    ):
        raise DesktopError("invalid_request")
    try:
        # Client-minted so the outcome event can never beat the 202 to the browser.
        if str(uuid.UUID(body["startId"])) != body["startId"]:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DesktopError("invalid_request") from None
    # Older web/phone clients omit mode. Their older Mac launcher would then
    # inherit global YOLO settings, so pass a safe mode it already understands.
    if mode is None:
        body["mode"] = "plan" if tool == "claude" else "on-request"
    return body


async def _target(owner_id: str, user_id: str, device_id: str, generation: str, command: str) -> dict:
    """The caller's OWN ready Mac only: resolution requires the ready row and the
    reciprocal pairing to name ``user_id``, so a teammate can never select it."""
    from core.services.connection_service import DesktopTargetError, resolve_desktop_target

    try:
        target = await resolve_desktop_target(owner_id, user_id, device_id, generation, command)
    except DesktopTargetError as exc:
        raise DesktopError(exc.code, 409) from None
    if target.get("clientId") != MAC_CLIENT_ID or target.get("memberId") != user_id:
        raise DesktopError("desktop_forbidden", 403)
    return target


async def _invoke(target: dict, command: str, params: dict, timeout_ms: int, *, forwarded: bool = False) -> dict:
    from core.services.task_address import own_address

    if target["taskAddress"] != own_address():
        if forwarded:
            raise DesktopError("desktop_forward_loop", 409)
        import httpx
        from core.config import settings

        # Address comes from the ready row, never from the caller.
        async with httpx.AsyncClient(timeout=timeout_ms / 1000 + 10, follow_redirects=False) as client:
            response = await client.post(
                target["taskAddress"] + "/api/v1/desktop-sessions/internal",
                headers={"x-lightbulb-ws-internal": settings.WS_INTERNAL_SECRET},
                json={
                    "ownerId": target["ownerId"],
                    "userId": target["memberId"],
                    "deviceId": target["deviceId"],
                    "generation": target["generation"],
                    "command": command,
                    "params": params,
                },
            )
        if response.status_code != 200:
            raise DesktopError("desktop_peer_unavailable", 503)
        return response.json()
    from core.services.node_broker import NodeCapacity, NodeInvokeTimeout
    from routers.gateway_relay import node_sessions

    session = node_sessions.get((target["ownerId"], target["deviceId"]))
    if session is None or session.generation != target["generation"]:
        raise DesktopError("desktop_generation_lost", 409)
    try:
        return await session.invoke(command, params, timeout_ms, result_grace_ms=5_000)
    except NodeInvokeTimeout:
        raise DesktopError("desktop_timeout", 504) from None
    except DesktopError:
        raise
    except NodeCapacity:
        raise DesktopError("desktop_busy", 429) from None
    except Exception:
        raise DesktopError("desktop_unavailable", 409) from None


async def handle_peer(body: dict) -> dict:
    """Backend-only hop to the task holding the Mac's socket. Authority is re-derived."""
    if (
        not isinstance(body, dict)
        or set(body) != {"ownerId", "userId", "deviceId", "generation", "command", "params"}
        or body["command"] not in COMMANDS
        or not isinstance(body["params"], dict)
        or any(
            not isinstance(body[k], str) or not _ID.fullmatch(body[k])
            for k in ("ownerId", "userId", "deviceId", "generation")
        )
    ):
        raise DesktopError("invalid_request")
    if not submissions_enabled(body["ownerId"]):
        raise DesktopError("desktop_disabled", 503)
    target = await _target(body["ownerId"], body["userId"], body["deviceId"], body["generation"], body["command"])
    timeout = START_MS if body["command"] == START_COMMAND else OPTIONS_MS
    return await _invoke(target, body["command"], body["params"], timeout, forwarded=True)


async def list_targets(owner_id: str, user_id: str) -> dict:
    """The caller's own ready Macs that can start sessions. No Mac round trip."""
    if not submissions_enabled(owner_id):
        return {"enabled": False, "devices": []}
    from core.services.connection_service import ConnectionService

    connections = ConnectionService()
    devices = []
    for connection_id, member, kind in await connections.query_by_owner(owner_id):
        if kind != "desktop-ready" or member != user_id:
            continue
        item = (
            await asyncio.to_thread(
                connections._client.get_item,
                TableName=connections.table_name,
                Key={"connectionId": {"S": connection_id}},
                ConsistentRead=True,
            )
        ).get("Item", {})
        device_id = item.get("deviceId", {}).get("S", "")
        generation = item.get("generation", {}).get("S", "")
        try:
            await _target(owner_id, user_id, device_id, generation, START_COMMAND)
        except DesktopError:
            continue  # stale rows, the CLI half, or a Mac without the grant
        # The pairing's displayName is "<clerk user id> | Lightbulb Mac": never show it. The
        # Mac reports its own name in its options.
        # ponytail: two Macs read the same until picked; send the name on the ready row if that bites.
        devices.append({"deviceId": device_id, "generation": generation, "name": "Your Mac"})
    return {"enabled": True, "devices": devices}


async def options(owner_id: str, user_id: str, device_id: str, generation: str) -> dict:
    if not submissions_enabled(owner_id):
        raise DesktopError("desktop_disabled", 503)
    target = await _target(owner_id, user_id, device_id, generation, OPTIONS_COMMAND)
    result = await _invoke(target, OPTIONS_COMMAND, {}, OPTIONS_MS)
    payload = result.get("payload") if isinstance(result, dict) else None
    if not isinstance(result, dict) or not result.get("ok") or not isinstance(payload, dict):
        raise DesktopError("desktop_options_unavailable", 409)
    return payload


def _audit(owner_id: str, user_id: str, start_id: str, body: dict, outcome: str) -> None:
    # The start audit record: who, which Mac, which fixed tool, which approved folder.
    # The prompt is the owner's own text and stays out of logs.
    logger.info(
        "desktop.session.start %s",
        json.dumps(
            {
                "startId": start_id,
                "ownerId": owner_id,
                "userId": user_id,
                "deviceId": body["deviceId"],
                "tool": body["tool"],
                "folder": body["folder"],
                "model": body["model"],
                "mode": body["mode"],
                "effort": body["effort"],
                "outcome": outcome,
            },
            sort_keys=True,
        ),
    )


async def start(owner_id: str, user_id: str, raw: dict, *, source: str = "remote") -> dict:
    body = validate_start(raw)
    if not submissions_enabled(owner_id):
        raise DesktopError("desktop_disabled", 503)
    target = await _target(owner_id, user_id, body["deviceId"], body["generation"], START_COMMAND)
    start_id = body["startId"]
    _audit(owner_id, user_id, start_id, body, "requested")
    params = {k: body[k] for k in ("tool", "folder", "model", "mode", "prompt")} | {"startId": start_id}
    if body["effort"] is not None:
        params["effort"] = body["effort"]
    return _launch(owner_id, user_id, start_id, target, body, source, params)


async def open_in_terminal(auth: AuthContext, session_id: str, raw: dict) -> dict:
    """Open in a terminal: the owner's own session that runs inside an app (Claude desktop, the
    Codex app, an IDE) opens as a held fork of its conversation on the Mac it runs on. The owner
    only, never a teammate or an agent (the route takes a human Clerk session). The Mac picks the
    folder from the conversation itself and refuses one outside its approved folders; the app's
    own session keeps running."""
    from core.services import local_sessions as sessions

    if not isinstance(raw, dict) or set(raw) != {"startId"}:
        raise DesktopError("invalid_request")
    start_id = raw["startId"]
    try:
        if str(uuid.UUID(start_id)) != start_id:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise DesktopError("invalid_request") from None
    owner_id, user_id = resolve_owner_id(auth), auth.user_id
    if not submissions_enabled(owner_id):
        raise DesktopError("desktop_disabled", 503)
    row, _ = await sessions.authorize_session(auth, None, session_id)
    if row["humanUserId"] != user_id:
        raise DesktopError("desktop_forbidden", 403)
    conversation = row.get("nativeConversationId")
    try:
        valid = isinstance(conversation, str) and str(uuid.UUID(conversation)) == conversation
    except ValueError:
        valid = False
    if (
        not sessions.conversation_only(row)
        or row.get("endedAt") is not None
        or row["provider"] not in TOOLS
        or not valid
    ):
        raise DesktopError("session_not_openable", 409)
    devices = (await list_targets(owner_id, user_id))["devices"]
    device = next((d for d in devices if d["deviceId"] == row["deviceId"]), None)
    if device is None:
        raise DesktopError("desktop_offline", 409)
    target = await _target(owner_id, user_id, device["deviceId"], device["generation"], START_COMMAND)
    body = {
        "deviceId": device["deviceId"],
        "tool": row["provider"],
        "folder": None,
        "model": None,
        "mode": None,
        "effort": None,
    }
    _audit(owner_id, user_id, start_id, body, "requested")
    params = {"tool": row["provider"], "conversationId": conversation, "startId": start_id}
    return _launch(owner_id, user_id, start_id, target, body, "open", params)


def _launch(owner_id: str, user_id: str, start_id: str, target: dict, body: dict, source: str, params: dict) -> dict:
    task = asyncio.create_task(_finish(owner_id, user_id, start_id, target, body, source, params))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"startId": start_id}


async def _finish(
    owner_id: str, user_id: str, start_id: str, target: dict, body: dict, source: str, params: dict
) -> None:
    _capture_start(user_id, owner_id, start_id, body["tool"], source, "local_session_start_requested")
    try:
        result = await _invoke(target, START_COMMAND, params, START_MS)
        ok = isinstance(result, dict) and result.get("ok") is True
        error = None
        if not ok:
            node_error = (result or {}).get("error") or {}
            error = {
                "code": str(node_error.get("code") or "desktop_start_failed")[:64],
                "message": str(node_error.get("message") or "The Mac did not start the session.")[:300],
                "retryable": False,
            }
            # A Mac on a build from before Open in a terminal rejects the conversation-only payload.
            if source == "open" and error["code"] == "INVALID_REQUEST" and error["message"] == "Malformed request.":
                error["message"] = "Update the Lightbulb app on this Mac, then try again."
    except DesktopError as exc:
        ok, error = False, {"code": exc.code, "message": "Could not reach that Mac.", "retryable": True}
    except Exception:
        logger.exception("desktop session start failed")
        ok, error = False, {"code": "desktop_start_failed", "message": "Could not reach that Mac.", "retryable": True}
    _audit(owner_id, user_id, start_id, body, "started" if ok else (error or {}).get("code", "failed"))
    _capture_start(
        user_id,
        owner_id,
        start_id,
        body["tool"],
        source,
        "local_session_terminal_opened" if ok else "local_session_start_failed",
    )
    from core.services.live_events import get_live_event_publisher
    from core.services.messaging import make_live_event

    event = make_live_event(
        "runtime.operation.changed",
        {
            "operationId": start_id,
            "state": "succeeded" if ok else "failed",
            "updatedAt": datetime.datetime.now(datetime.UTC).isoformat(),
            "result": {"tool": body["tool"]} if ok else None,
            "error": error,
        },
        event_id=start_id,
    )
    try:
        await get_live_event_publisher().publish_user(owner_id, user_id, event)
    except Exception:
        logger.warning("desktop session start outcome publish failed")
