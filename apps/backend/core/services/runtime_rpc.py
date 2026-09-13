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
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services import container_rpc
from core.services.live_events import publish_owner_users
from core.services.messaging import (
    ApiProblem,
    RuntimeOperationAccepted,
    RuntimeRpcRequest,
    RuntimeRpcResponse,
    make_live_event,
    runtime_operation,
)
from core.services.paperclip_admin_client import (
    PaperclipAdminClient,
    PaperclipApiError,
)
from core.services.paperclip_admin_session import get_admin_session_cookie
from core.services.paperclip_agent_lifecycle import (
    AgentLifecycleConflict,
    create_paperclip_for_chat,
    delete_agent_everywhere,
    ensure_root_ceo_survives,
)
from core.services.paperclip_meta import read_from_agent_entry
from core.services.paperclip_openclaw_pairing import (
    ensure_operator_device_paired,
    pair_with_openclaw,
    resolve_company_id_for_owner,
)

logger = logging.getLogger(__name__)

_RUN_DISPATCH_USE = {
    "chat.send": "/api/v1/messaging/rooms/{roomId}/messages",
    "sessions.send": "/api/v1/messaging/rooms/{roomId}/messages",
    "sessions.steer": "/api/v1/messaging/runs/{runId}/steer",
    "agent": "/api/v1/messaging/rooms/{roomId}/messages",
}
_RUN_DISPATCH_METHODS = frozenset(_RUN_DISPATCH_USE)

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
_RPC_TIMEOUT_S = 30.0
# A mutating method that lands here invalidates the named resource for every
# open frontend tab (the prior per-connection `*.changed` broadcast is gone).
_INVALIDATES = {
    "agents.update": "agents",
    "agents.delete": "agents",
    "config.set": "config",
    "config.patch": "config",
    "skills.install": "skills",
    "skills.update": "skills",
    "skills.proposals.apply": "skills",
    "skills.proposals.reject": "skills",
}


async def _invalidate(owner_id: str, method: str) -> None:
    """Replaces the broadcasts the deleted gateway socket used to relay. Never raises."""
    resource = _INVALIDATES.get(method)
    if resource is None:
        return
    try:
        await publish_owner_users(owner_id, make_live_event("runtime.resource.invalidated", {"resource": resource}))
    except Exception:
        logger.warning("resource invalidation publish failed resource=%s", resource, exc_info=True)


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


def _sanitize_agent_update(params: dict[str, Any]) -> dict[str, Any]:
    clean = dict(params)
    if "name" in clean:
        name = clean["name"]
        if not isinstance(name, str):
            raise ApiProblem("INVALID_REQUEST", "Agent name must be 1..80 characters.")
        name = name.strip()
        if not name or len(name) > 80:
            raise ApiProblem("INVALID_REQUEST", "Agent name must be 1..80 characters.")
        clean["name"] = name
    return clean


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
    if method == "agents.update":
        params = _sanitize_agent_update(params)
    if method == "agents.delete":
        agent_id = params.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            raise ApiProblem("INVALID_REQUEST", "Agent ID is required.")
        try:
            result = await delete_agent_everywhere(owner_id=owner_id, agent_id=agent_id)
        except AgentLifecycleConflict as exc:
            raise ApiProblem("AGENT_LIFECYCLE_CONFLICT", str(exc)) from exc
        await _invalidate(owner_id, method)
        return result
    if method == "agents.create":
        container, ip = await container_rpc.running_container(owner_id)
        return await _create_agent(owner_id, user_id, _sanitize_agent_create(params), ip, container["gateway_token"])
    if method == "agents.update":
        try:
            await _mirror_agent_update(owner_id, params)
        except AgentLifecycleConflict as exc:
            raise ApiProblem("AGENT_LIFECYCLE_CONFLICT", str(exc)) from exc
    try:
        result = await container_rpc.call(owner_id, method, params, timeout_s=_RPC_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except ApiProblem:
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

    await _invalidate(owner_id, method)
    if method == "sessions.list":
        result = _filter_sessions_for_user(result, user_id)
    elif method == "sessions.search":
        result = _filter_search_hits_for_user(result, user_id)
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
    paperclip_agent = await create_paperclip_for_chat(
        creator_user_id=user_id,
        owner_id=owner_id,
        name=name,
        role=role,
        model=model,
        meta=meta,
    )
    await pair_with_openclaw(owner_id=owner_id, paperclip_agent=paperclip_agent)
    # pair_with_openclaw above also registers the agent's Lightbulb channel
    # account + binding — see its docstring.
    await ensure_operator_device_paired(
        owner_id=owner_id,
        paperclip_agent=paperclip_agent,
        gateway_token=token,
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


async def _mirror_paperclip_update_with_retry(
    *,
    admin: PaperclipAdminClient,
    cookie: str,
    company_id: str,
    agent_id: str,
    body: dict[str, str],
) -> None:
    target = await admin.get_agent(session_cookie=cookie, agent_id=agent_id)
    if target.get("companyId") != company_id:
        return
    if body.get("role") not in (None, "ceo") and target.get("role") == "ceo" and not target.get("reportsTo"):
        payload = await admin.list_agents(company_id=company_id, session_cookie=cookie)
        agents = payload if isinstance(payload, list) else payload.get("agents", [])
        agents = [agent for agent in agents if isinstance(agent, dict)]
        ensure_root_ceo_survives(target=target, agents=agents)
    for attempt in range(_PAPERCLIP_MIRROR_MAX_ATTEMPTS):
        try:
            await admin.patch_agent(
                session_cookie=cookie,
                agent_id=agent_id,
                body=body,
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
    name = params.get("name")
    if not isinstance(name, str) and not isinstance(paperclip, dict):
        return
    agent_id = params.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return
    body: dict[str, str] = {}
    if isinstance(name, str):
        body["name"] = name
    if isinstance(paperclip, dict):
        meta = read_from_agent_entry({"params": inner})
        body["role"] = meta.role
        for key in ("title", "capabilities", "icon"):
            value = paperclip.get(key)
            if isinstance(value, str):
                body[key] = value
    try:
        from core.repositories.paperclip_repo import PaperclipRepo

        company_id = await resolve_company_id_for_owner(
            owner_id=owner_id,
            repo=PaperclipRepo(table_name="paperclip-companies"),
        )
        if company_id:
            async with httpx.AsyncClient(base_url=settings.PAPERCLIP_INTERNAL_URL, timeout=15.0) as client:
                await _mirror_paperclip_update_with_retry(
                    admin=PaperclipAdminClient(http_client=client),
                    cookie=await get_admin_session_cookie(client),
                    company_id=company_id,
                    agent_id=agent_id,
                    body=body,
                )
    except AgentLifecycleConflict:
        raise
    except (PaperclipApiError, httpx.HTTPError, RuntimeError):
        logger.warning("Paperclip agent metadata mirror failed owner=%s agent=%s", owner_id, agent_id, exc_info=True)
    if not isinstance(paperclip, dict):
        return
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
