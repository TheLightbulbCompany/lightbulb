"""Owner-only "start a Claude Code / Codex session on my Mac" over the selected-Mac relay.

This side proves WHO may ask (the caller is the member who paired that Mac, the
workspace is on the desktop-execution allowlist) and WHICH of their own ready Macs
answers. The Mac re-validates every field against its own approved folders and
installed CLIs, asks natively (exact approve-once, like shell), and launches only
its fixed binary. No path, flag or command text here is ever executed as given.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
import uuid

from core.services.desktop_execution import DesktopError, submissions_enabled

OPTIONS_COMMAND = "session.options.v1"
START_COMMAND = "session.start.v1"
COMMANDS = (OPTIONS_COMMAND, START_COMMAND)
MAC_CLIENT_ID = "openclaw-macos"
OPTIONS_MS = 8_000
# 120 s native prompt + launch; the web learns the outcome from a live event.
START_MS = 135_000
# The relay caps a node frame at 16 KiB; JSON-escaped non-ASCII can triple the bytes.
MAX_PROMPT_BYTES = 4_096
TOOLS = {
    "claude": {"modes": {"plan", "acceptEdits"}},
    # Codex's own list is {on-request, never}; "never" is not offered remotely.
    "codex": {"modes": {"on-request"}},
}
_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}\Z")
logger = logging.getLogger(__name__)
_background: set[asyncio.Task] = set()


def validate_start(body: dict) -> dict:
    fields = {"deviceId", "generation", "tool", "folder", "model", "mode", "prompt"}
    if not isinstance(body, dict) or set(body) - fields or {"deviceId", "generation", "tool", "folder"} - set(body):
        raise DesktopError("invalid_request")
    body = {"model": None, "mode": None, "prompt": "", **body}
    tool, folder, model, mode, prompt = (body[k] for k in ("tool", "folder", "model", "mode", "prompt"))
    if (
        any(not isinstance(body[k], str) or not _ID.fullmatch(body[k]) for k in ("deviceId", "generation"))
        or tool not in TOOLS
        or not isinstance(folder, str)
        or not folder.startswith("/")
        or len(folder) > 4096
        or "\x00" in folder
        or (model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)))
        or (mode is not None and mode not in TOOLS[tool]["modes"])
        or not isinstance(prompt, str)
        or "\x00" in prompt
        or len(prompt.encode("utf-8", "surrogatepass")) > MAX_PROMPT_BYTES
    ):
        raise DesktopError("invalid_request")
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
    from core.services.node_broker import NodeInvokeTimeout
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
    from core.repositories import container_repo
    from core.services.connection_service import ConnectionService

    connections = ConnectionService()
    paired = (await container_repo.get_by_owner_id(owner_id, consistent=True) or {}).get("paired_devices") or {}
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
        name = str((paired.get(device_id) or {}).get("displayName") or "Mac").removesuffix(" | Lightbulb Mac")
        devices.append({"deviceId": device_id, "generation": generation, "name": name[:120]})
    return {"enabled": True, "devices": devices}


async def options(owner_id: str, user_id: str, device_id: str, generation: str) -> dict:
    if not submissions_enabled(owner_id):
        raise DesktopError("desktop_disabled", 503)
    target = await _target(owner_id, user_id, device_id, generation, OPTIONS_COMMAND)
    result = await _invoke(target, OPTIONS_COMMAND, {}, OPTIONS_MS)
    payload = result.get("payload") if isinstance(result, dict) else None
    if not result.get("ok") or not isinstance(payload, dict):
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
                "outcome": outcome,
            },
            sort_keys=True,
        ),
    )


async def start(owner_id: str, user_id: str, raw: dict) -> dict:
    body = validate_start(raw)
    if not submissions_enabled(owner_id):
        raise DesktopError("desktop_disabled", 503)
    target = await _target(owner_id, user_id, body["deviceId"], body["generation"], START_COMMAND)
    start_id = str(uuid.uuid4())
    _audit(owner_id, user_id, start_id, body, "requested")
    task = asyncio.create_task(_finish(owner_id, user_id, start_id, target, body))
    _background.add(task)
    task.add_done_callback(_background.discard)
    return {"startId": start_id}


async def _finish(owner_id: str, user_id: str, start_id: str, target: dict, body: dict) -> None:
    params = {k: body[k] for k in ("tool", "folder", "model", "mode", "prompt")} | {"startId": start_id}
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
    except DesktopError as exc:
        ok, error = False, {"code": exc.code, "message": "Could not reach that Mac.", "retryable": True}
    except Exception:
        logger.exception("desktop session start failed")
        ok, error = False, {"code": "desktop_start_failed", "message": "Could not reach that Mac.", "retryable": True}
    _audit(owner_id, user_id, start_id, body, "started" if ok else (error or {}).get("code", "failed"))
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
