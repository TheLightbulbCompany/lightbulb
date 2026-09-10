"""Guarded browser runtime RPC policy, moved intact from the retired chat socket."""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from typing import Any

import httpx

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.containers import get_ecs_manager, get_gateway_pool
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services.messaging import (
    ApiProblem,
    RuntimeOperationAccepted,
    RuntimeRpcRequest,
    RuntimeRpcResponse,
    runtime_operation,
)
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
from core.services.paperclip_meta import read_from_agent_entry
from core.services.paperclip_openclaw_pairing import (
    ensure_operator_device_paired,
    is_seeded_agent,
    pair_with_openclaw,
    resolve_company_id_for_owner,
)

logger = logging.getLogger(__name__)

_RUN_DISPATCH_METHODS = frozenset({"chat.send", "sessions.send", "sessions.steer", "agent"})
_RUN_DISPATCH_USE = {
    "chat.send": "/api/v1/messaging/rooms/{roomId}/messages",
    "sessions.send": "/api/v1/messaging/rooms/{roomId}/messages",
    "sessions.steer": "/api/v1/messaging/runs/{runId}/steer",
    "agent": "/api/v1/messaging/rooms/{roomId}/messages",
}

# Default deny: only methods used by the control/status surfaces. Room commands,
# run control, approvals, and questions have dedicated product endpoints.
_PROXY_ALLOWED_METHODS = frozenset(
    {
        "agent.identity.get",
        "agents.create",
        "agents.delete",
        "agents.list",
        "agents.update",
        "browser.request",
        "channels.status",
        "chat.history",
        "chat.message.get",
        "commands.list",
        "config.get",
        "config.patch",
        "config.set",
        "doctor.memory.backfillDreamDiary",
        "doctor.memory.dedupeDreamDiary",
        "doctor.memory.dreamDiary",
        "doctor.memory.repairDreamingArtifacts",
        "doctor.memory.resetDreamDiary",
        "doctor.memory.resetGroundedShortTerm",
        "doctor.memory.status",
        "health",
        "models.authStatus",
        "models.list",
        "progressCard.get",
        "question.list",
        "sessions.branches.list",
        "sessions.branches.switch",
        "sessions.delete",
        "sessions.list",
        "sessions.patch",
        "sessions.rewind",
        "sessions.search",
        "skills.install",
        "skills.proposals.apply",
        "skills.proposals.inspect",
        "skills.proposals.list",
        "skills.proposals.reject",
        "skills.search",
        "skills.status",
        "skills.update",
        "tools.catalog",
    }
)

_SESSION_KEY_PARAM_BY_METHOD = {
    "sessions.patch": "key",
    "sessions.delete": "key",
    "chat.history": "sessionKey",
    "sessions.rewind": "sessionKey",
    "sessions.branches.list": "sessionKey",
    "sessions.branches.switch": "sessionKey",
    "chat.message.get": "sessionKey",
}
_SESSION_KEYS_LIST_PARAM_BY_METHOD = {"sessions.search": "sessionKeys"}
_FORBIDDEN_PARAMS_BY_METHOD: dict[str, frozenset[str]] = {
    "sessions.patch": frozenset({"permissionMode"}),
}
_MUTATING_METHODS = frozenset(
    {
        "agents.create",
        "agents.delete",
        "agents.update",
        "config.patch",
        "config.set",
        "sessions.branches.switch",
        "sessions.delete",
        "sessions.patch",
        "sessions.rewind",
        "skills.install",
        "skills.proposals.apply",
        "skills.proposals.reject",
        "skills.update",
    }
)
_PAPERCLIP_MIRROR_MAX_ATTEMPTS = 3


def _user_owns_session_key(key: Any, user_id: str) -> bool:
    if not isinstance(key, str):
        return False
    parts = key.split(":", 2)
    if len(parts) < 3 or parts[0] != "agent":
        return False
    rest = parts[2].lower()
    user = user_id.lower()
    return rest == user or rest.startswith(f"{user}:")


def _filter_sessions_for_user(result: Any, user_id: str) -> Any:
    if not isinstance(result, dict) or not isinstance(result.get("sessions"), list):
        return result
    return {
        **result,
        "sessions": [
            row
            for row in result["sessions"]
            if isinstance(row, dict) and _user_owns_session_key(row.get("key"), user_id)
        ],
    }


def _filter_search_hits_for_user(result: Any, user_id: str) -> Any:
    if not isinstance(result, dict) or not isinstance(result.get("results"), list):
        return result
    return {
        **result,
        "results": [
            row
            for row in result["results"]
            if isinstance(row, dict) and _user_owns_session_key(row.get("sessionKey"), user_id)
        ],
    }


def _guard_method(method: str, params: dict[str, Any], user_id: str) -> None:
    if method in _RUN_DISPATCH_METHODS:
        raise ApiProblem(
            "INVALID_REQUEST",
            f'"{method}" starts a run and is not available over the runtime RPC compatibility endpoint.',
            details={"method": method, "use": _RUN_DISPATCH_USE[method]},
        )
    if method not in _PROXY_ALLOWED_METHODS:
        put_metric("rpc.refused", dimensions={"method": method, "reason": "not_allowlisted"})
        raise ApiProblem(
            "INVALID_REQUEST",
            f'"{method}" is not available over the runtime RPC compatibility endpoint.',
            details={"method": method},
        )
    forbidden = sorted(_FORBIDDEN_PARAMS_BY_METHOD.get(method, frozenset()) & params.keys())
    if forbidden:
        put_metric("rpc.refused", dimensions={"method": method, "reason": "forbidden_param"})
        raise ApiProblem(
            "INVALID_REQUEST",
            "That session setting cannot be changed here.",
            details={"method": method, "fields": forbidden},
        )
    key_name = _SESSION_KEY_PARAM_BY_METHOD.get(method)
    if key_name is not None:
        key = params.get(key_name)
        if key is not None and (not isinstance(key, str) or not _user_owns_session_key(key, user_id)):
            raise ApiProblem("ROOM_FORBIDDEN", "You do not have access to that session.")
    list_name = _SESSION_KEYS_LIST_PARAM_BY_METHOD.get(method)
    if list_name is not None:
        keys = params.get(list_name)
        if keys is not None and (
            not isinstance(keys, list) or any(not _user_owns_session_key(key, user_id) for key in keys)
        ):
            raise ApiProblem("ROOM_FORBIDDEN", "You do not have access to that session.")


def _sanitize_agent_create(params: dict[str, Any]) -> dict[str, Any]:
    name = params.get("name")
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
        raise ApiProblem("INVALID_REQUEST", "Agent name must be 1..80 characters.")
    clean: dict[str, Any] = {"name": name.strip()}
    for key in ("role", "model"):
        value = params.get(key)
        if value is not None:
            if not isinstance(value, str) or len(value) > 256:
                raise ApiProblem("INVALID_REQUEST", f"Invalid agents.create {key}.")
            clean[key] = value
    inner = params.get("params")
    if inner is not None:
        if not isinstance(inner, dict) or set(inner) - {"paperclip"}:
            raise ApiProblem("INVALID_REQUEST", "Invalid agents.create params.")
        paperclip = inner.get("paperclip")
        if paperclip is not None:
            if not isinstance(paperclip, dict) or set(paperclip) - {"role", "title", "capabilities", "icon"}:
                raise ApiProblem("INVALID_REQUEST", "Invalid Paperclip agent metadata.")
            sanitized: dict[str, str] = {}
            for key, value in paperclip.items():
                if not isinstance(value, str) or len(value) > 4_000:
                    raise ApiProblem("INVALID_REQUEST", "Invalid Paperclip agent metadata.")
                sanitized[key] = value.strip()
            clean["params"] = {"paperclip": sanitized}
    return clean


async def _running_container(owner_id: str) -> tuple[dict, str]:
    manager = get_ecs_manager()
    container, ip = await manager.resolve_running_container(owner_id)
    if container and ip:
        token = container.get("gateway_token")
        if isinstance(token, str) and token:
            return container, ip
    row = container or await container_repo.get_by_owner_id(owner_id, consistent=True)
    if row and row.get("status") == "stopped":
        from core.services.container_wake import request_wake_silent

        await request_wake_silent(owner_id)
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE",
            "The runtime is waking. Try again shortly.",
            retryable=True,
            retry_after=3,
        )
    raise ApiProblem(
        "RUNTIME_UNAVAILABLE",
        "The runtime is temporarily unavailable.",
        retryable=True,
        retry_after=3,
    )


async def execute(
    auth: AuthContext, request: RuntimeRpcRequest
) -> tuple[int, RuntimeRpcResponse | RuntimeOperationAccepted]:
    owner_id = resolve_owner_id(auth)
    params = dict(request.params)
    _guard_method(request.method, params, auth.user_id)
    if request.method in _MUTATING_METHODS:
        # Clear a racing idle-stop claim before any durable or external effect.
        await container_repo.touch_last_interactive(owner_id)
    if request.method == "agents.create":
        operation_id = str(uuid.uuid4())
        await messaging_repo.create_operation(
            owner_id=owner_id,
            operation_id=operation_id,
            requester_user_id=auth.user_id,
            method=request.method,
            params=_sanitize_agent_create(params),
        )
        return 202, RuntimeOperationAccepted(operation_id=operation_id)

    result = await execute_sync(
        owner_id=owner_id,
        user_id=auth.user_id,
        request_id=str(request.request_id),
        method=request.method,
        params=params,
    )
    return 200, RuntimeRpcResponse(payload=result)


async def execute_sync(
    *,
    owner_id: str,
    user_id: str,
    request_id: str,
    method: str,
    params: dict[str, Any],
) -> Any:
    """Execute one already-admitted synchronous RPC with all side-effect guards."""
    _guard_method(method, params, user_id)
    container, ip = await _running_container(owner_id)
    token = container["gateway_token"]
    if method == "agents.create":
        return await _create_agent(owner_id, user_id, _sanitize_agent_create(params), ip, token)
    if method == "agents.update":
        params = dict(params)
        await _mirror_agent_update(owner_id, params)
    if method == "agents.delete":
        agent_id = params.get("agentId")
        if isinstance(agent_id, str) and agent_id:
            if await is_seeded_agent(owner_id=owner_id, agent_id=agent_id):
                raise ApiProblem(
                    "INVALID_REQUEST",
                    "Friday and Atlas are managed by Lightbulb and cannot be deleted.",
                )
            await prepare_paperclip_agent_delete(owner_id=owner_id, agent_id=agent_id)
    try:
        pool = get_gateway_pool()
        agent_id = params.get("agentId") if method == "agents.delete" else None
        if isinstance(agent_id, str) and agent_id:
            result = await delete_runtime_agent_with_lock_retry(
                gateway=pool,
                owner_id=owner_id,
                agent_id=agent_id,
                ip=ip,
                token=token,
            )
        else:
            result = await pool.send_rpc(
                user_id=owner_id,
                req_id=request_id,
                method=method,
                params=params,
                ip=ip,
                token=token,
            )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError as exc:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE",
            "The runtime request timed out.",
            retryable=True,
            retry_after=3,
            details={"method": method},
        ) from exc
    except RuntimeError as exc:
        raise _normalize_gateway_error(method, exc) from exc
    except Exception as exc:
        logger.warning("runtime RPC failed method=%s owner=%s error=%s", method, owner_id, type(exc).__name__)
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE",
            "The runtime request failed.",
            retryable=True,
            retry_after=3,
            details={"method": method},
        ) from exc

    if method == "sessions.list":
        result = _filter_sessions_for_user(result, user_id)
    elif method == "sessions.search":
        result = _filter_search_hits_for_user(result, user_id)
    if method == "agents.delete" and isinstance(params.get("agentId"), str):
        await _cleanup_deleted_agent(owner_id, params["agentId"])
    return result


def _normalize_gateway_error(method: str, exc: RuntimeError) -> ApiProblem:
    try:
        raw = json.loads(str(exc))
    except (TypeError, ValueError):
        raw = {}
    message = raw.get("message") if isinstance(raw, dict) else None
    upstream_code = raw.get("code") if isinstance(raw, dict) else None
    details: dict[str, Any] = {"method": method}
    if isinstance(upstream_code, str) and upstream_code:
        details["upstreamCode"] = upstream_code[:128]
    return ApiProblem(
        "INVALID_REQUEST",
        str(message)[:500] if isinstance(message, str) and message else "The runtime rejected the request.",
        details=details,
    )


async def _create_agent(owner_id: str, user_id: str, params: dict[str, Any], ip: str, token: str) -> dict:
    name = params["name"]
    role = params.get("role")
    model = params.get("model")
    inner = params.get("params") or {}
    meta = read_from_agent_entry({"params": inner}) if inner.get("paperclip") else None
    from core.services.agent_budget import AGENT_BUDGET_EXCEEDED_DETAIL, is_at_agent_budget

    try:
        paperclip_agent = await create_paperclip_for_chat(
            creator_user_id=user_id,
            owner_id=owner_id,
            name=name,
            role=role,
            model=model,
            meta=meta,
            idempotency_replay_only=await is_at_agent_budget(owner_id),
        )
    except PaperclipAgentCreateReplayMiss as exc:
        raise ApiProblem("MEMBER_LIMIT", AGENT_BUDGET_EXCEEDED_DETAIL) from exc
    pool = get_gateway_pool()
    await pair_with_openclaw(
        owner_id=owner_id,
        paperclip_agent=paperclip_agent,
        gateway_pool=pool,
        ip=ip,
        token=token,
    )
    # The OpenClaw agent entry now exists (pair_with_openclaw above), but
    # build_openclaw_config_dict's channels.lightbulb.accounts/bindings only
    # get derived when the whole config is rewritten — never for an agent
    # hired after provision. Without this, the new agent has no inbound route
    # on the lightbulb channel and every room message to it 404s forever.
    from core.services.config_patcher import register_lightbulb_agent_account

    await register_lightbulb_agent_account(owner_id, paperclip_agent["id"])
    await ensure_operator_device_paired(
        owner_id=owner_id,
        paperclip_agent=paperclip_agent,
        gateway_token=token,
        ip=ip,
    )
    if meta is not None:
        from core.services.config_patcher import apply_paperclip_meta

        block = {"role": meta.role}
        for key in ("title", "capabilities", "icon"):
            value = getattr(meta, key)
            if value:
                block[key] = value
        await apply_paperclip_meta(owner_id, agent_id=paperclip_agent["id"], meta=block)
    from routers.teams.agents import _redact_agent

    return {"agentId": paperclip_agent["id"], "agent": _redact_agent(paperclip_agent)}


async def _mirror_paperclip_meta_with_retry(
    *,
    admin: PaperclipAdminClient,
    cookie: str,
    company_id: str,
    owner_id: str,
    agent_id: str,
    role: str,
    title: str | None,
    capabilities: str | None,
    icon: str | None,
) -> None:
    target = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
    if target.get("companyId") != company_id:
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
        except (PaperclipApiError, httpx.HTTPError) as exc:
            if (
                not (isinstance(exc, httpx.HTTPError) or getattr(exc, "retryable", False))
                or attempt + 1 == _PAPERCLIP_MIRROR_MAX_ATTEMPTS
            ):
                raise
            await asyncio.sleep(0.2 * 2**attempt)


async def _mirror_agent_update(owner_id: str, params: dict[str, Any]) -> None:
    inner = params.get("params") or {}
    paperclip = inner.get("paperclip") if isinstance(inner, dict) else None
    if not isinstance(paperclip, dict):
        return
    meta = read_from_agent_entry({"params": inner})
    agent_id = params.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return
    try:
        from core.repositories.paperclip_repo import PaperclipRepo

        company_id = await resolve_company_id_for_owner(
            owner_id=owner_id,
            repo=PaperclipRepo(table_name="paperclip-companies"),
        )
        if company_id:
            async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as client:
                await _mirror_paperclip_meta_with_retry(
                    admin=PaperclipAdminClient(http_client=client),
                    cookie=await get_admin_session_cookie(client),
                    company_id=company_id,
                    owner_id=owner_id,
                    agent_id=agent_id,
                    role=meta.role,
                    title=paperclip.get("title") if isinstance(paperclip.get("title"), str) else None,
                    capabilities=(
                        paperclip.get("capabilities") if isinstance(paperclip.get("capabilities"), str) else None
                    ),
                    icon=paperclip.get("icon") if isinstance(paperclip.get("icon"), str) else None,
                )
    except (PaperclipApiError, httpx.HTTPError, RuntimeError):
        logger.warning("Paperclip agent metadata mirror failed owner=%s agent=%s", owner_id, agent_id, exc_info=True)
    try:
        from core.services.config_patcher import apply_paperclip_meta

        block: dict[str, str] = {"role": meta.role}
        for key in ("title", "capabilities", "icon"):
            value = paperclip.get(key)
            if isinstance(value, str):
                block[key] = value.strip()
        await apply_paperclip_meta(owner_id, agent_id=agent_id, meta=block)
    except Exception:
        logger.warning(
            "EFS Paperclip agent metadata mirror failed owner=%s agent=%s", owner_id, agent_id, exc_info=True
        )
    params.pop("params", None)


async def _cleanup_deleted_agent(owner_id: str, agent_id: str) -> None:
    try:
        from core.containers.workspace import cleanup_deleted_agent

        await cleanup_deleted_agent(owner_id, agent_id)
    except Exception:
        logger.warning("post-delete EFS cleanup failed owner=%s agent=%s", owner_id, agent_id, exc_info=True)
    try:
        await delete_paperclip_for_chat(owner_id=owner_id, agent_id=agent_id)
    except Exception:
        logger.warning("post-delete Paperclip cleanup failed owner=%s agent=%s", owner_id, agent_id, exc_info=True)
    try:
        from core.services.config_patcher import deregister_lightbulb_agent_account

        await deregister_lightbulb_agent_account(owner_id, agent_id)
    except Exception:
        logger.warning(
            "post-delete lightbulb channel account cleanup failed owner=%s agent=%s", owner_id, agent_id, exc_info=True
        )


async def execute_control(
    auth: AuthContext,
    *,
    method: str,
    params: dict[str, Any],
) -> Any:
    """Call one dedicated, body-whitelisted approval/question method."""
    if method not in {"exec.approval.resolve", "plugin.approval.resolve", "question.resolve"}:
        raise ValueError("unsupported runtime control method")
    owner_id = resolve_owner_id(auth)
    container, ip = await _running_container(owner_id)
    try:
        return await get_gateway_pool().send_rpc(
            user_id=owner_id,
            req_id=str(uuid.uuid4()),
            method=method,
            params=params,
            ip=ip,
            token=container["gateway_token"],
        )
    except asyncio.TimeoutError as exc:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE",
            "The runtime request timed out.",
            retryable=True,
            retry_after=3,
        ) from exc
    except RuntimeError as exc:
        raise _normalize_gateway_error(method, exc) from exc
    except Exception as exc:
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE",
            "The runtime request failed.",
            retryable=True,
            retry_after=3,
        ) from exc


async def get_operation(auth: AuthContext, operation_id: str):
    owner_id = resolve_owner_id(auth)
    try:
        operation_id = str(uuid.UUID(operation_id))
    except ValueError as exc:
        raise ApiProblem("INVALID_REQUEST", "Invalid operation ID.") from exc
    row = await messaging_repo.get_operation(owner_id, operation_id)
    if row is None or row.get("requesterUserId") != auth.user_id:
        raise ApiProblem("OPERATION_NOT_FOUND", "Runtime operation not found.")
    return runtime_operation(row)
