"""Guarded browser runtime RPC policy, moved intact from the retired chat socket."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any

import httpx

from core.auth import AuthContext, resolve_owner_id
from core.config import settings
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services import container_rpc, hermes_profiles, runtime_transport
from core.services.live_events import publish_owner_users
from core.services.messaging import (
    ApiProblem,
    RuntimeOperationAccepted,
    RuntimeRpcRequest,
    RuntimeRpcResponse,
    load_agent_roster,
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
#: M4-R1 (Task 13a): served from the profile in S3 + the Paperclip roster for an AgentCore row,
#: never forwarded to a live gateway socket. ECS rows are untouched — see _is_agentcore below.
_HERMES_PROFILE_METHODS = frozenset({"config.get", "config.patch", "config.set", "agents.list"})
# A mutating method that lands here invalidates the named resource for every
# open frontend tab (the prior per-connection `*.changed` broadcast is gone).
_INVALIDATES = {
    # "agents.create" is only ever passed to _invalidate from _create_agent's AgentCore branch
    # (Q9a row #1) -- the ECS branch stays byte-identical and never invalidates here.
    "agents.create": "agents",
    "agents.update": "agents",
    "agents.delete": "agents",
    "config.set": "config",
    "config.patch": "config",
    "skills.install": "skills",
    "skills.update": "skills",
    "skills.proposals.apply": "skills",
    "skills.proposals.reject": "skills",
}


def _default_agent_id(roster: list[dict[str, Any]]) -> str | None:
    """The CEO row, or the first roster row when nobody is CEO — packages/shared's
    ``useAgents.ts`` reads this as ``data.defaultId`` (M4-R43, fix round 1 continuation)."""
    for row in roster:
        if row.get("role") == "ceo":
            return row.get("id")
    return roster[0].get("id") if roster else None


async def _config_get_roster_map(owner_id: str) -> dict[str, Any]:
    """``config.get`` with no ``agentId`` — the shape the real browser client actually sends
    (``useConfigPatch.ts``/``PluginsTab.tsx`` never pass one). ``hash`` is the sha256 of the
    per-profile hashes concatenated in roster order (M4-R43): stable for an unchanged roster,
    and changes if any one profile's config.yaml does."""
    roster = await load_agent_roster(owner_id)
    config_map: dict[str, Any] = {}
    hashes: list[str] = []
    for row in roster:
        config, config_hash = await run_in_thread(hermes_profiles.config_snapshot, owner_id, row["id"])
        config_map[row["id"]] = config
        hashes.append(config_hash)
    return {"config": config_map, "hash": hashlib.sha256("".join(hashes).encode()).hexdigest()}


async def config_patch_roster(owner_id: str, patch: dict[str, Any], base_hash: str | None) -> dict[str, Any]:
    """``config.patch`` with no ``agentId`` (M4-R48, fix round 2 continuation) — BOTH real
    callers (``useConfigPatch.ts``, ``PluginsTab.tsx``) never send one, so there is no single
    profile to CAS against: the patch fans out to EVERY roster profile, gated on the SAME map
    hash ``config.get`` (no ``agentId``) returns. A single-profile hash could never match what
    these clients actually hold as their ``baseHash`` — they only ever read it from the
    no-``agentId`` ``config.get``. ``config.set`` has no such fan-out (see the caller): zero real
    callers, and a whole-document replace across N profiles would clobber per-profile
    differences.

    # ponytail: not transactional — one profile's PUT failing mid-loop leaves a partial write.
    Unhandled here per the controller's own ruling (parked): unlike ``refresh_integrations``,
    this isn't a reconcile invoked on a timer, but the SAME patch dict re-sent is itself
    idempotent per profile, and the next ``config.get`` map hash reflects exactly what landed —
    a client can always detect and retry a partial apply.
    """
    current = await _config_get_roster_map(owner_id)
    if base_hash is not None and base_hash != current["hash"]:
        raise ApiProblem(
            "INVALID_REQUEST",
            "The config changed since you last loaded it. Refresh and try again.",
            details={"method": "config.patch", "upstreamCode": "CONFIG_STALE"},
        )
    if not patch:
        return current
    for agent_id in current["config"]:
        await run_in_thread(hermes_profiles.patch_profile_config, owner_id, agent_id, patch)
    return await _config_get_roster_map(owner_id)


def _config_patch_dict(params: dict[str, Any]) -> dict[str, Any]:
    """Accepts the RFC7386 JSON-merge-patch ``raw`` STRING the only real web caller sends
    (``useConfigPatch.ts:41-43``, ``PluginsTab.tsx:90-96``) or a plain ``patch`` dict (other/
    internal callers, e.g. the pre-existing test suite)."""
    raw = params.get("raw")
    if isinstance(raw, str):
        try:
            decoded = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ApiProblem("INVALID_REQUEST", "config.patch raw must be valid JSON.") from exc
        return decoded if isinstance(decoded, dict) else {}
    patch = params.get("patch")
    return patch if isinstance(patch, dict) else {}


async def _is_agentcore(owner_id: str) -> bool:
    """Whether this owner's runtime is AgentCore. Reuses ``runtime_transport``'s own
    owner-id-only helper (built for exactly this call shape — see its docstring) instead of a
    second copy of the ``container_repo`` fetch + ``host_for`` check."""
    return await runtime_transport.is_agentcore_owner(owner_id)


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
    # `workspace` is never required or derived here: an ECS agent's EFS path is
    # assigned downstream from the Paperclip agent's own id (see
    # paperclip_openclaw_pairing's `workspace=f"/home/node/.openclaw/workspace-{pc_uuid}"`),
    # and a Hermes/AgentCore agent has no EFS path at all. `clean` is built
    # from an explicit allowlist above, so a client-supplied `workspace` (or
    # any other unrecognized key) is already dropped, not merely ignored —
    # this is never trusted or forwarded.
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
        from core.services.config_patcher import ConfigPatchError

        agent_id = params.get("agentId")
        if not isinstance(agent_id, str) or not agent_id:
            raise ApiProblem("INVALID_REQUEST", "Agent ID is required.")
        try:
            result = await delete_agent_everywhere(owner_id=owner_id, agent_id=agent_id)
        except AgentLifecycleConflict as exc:
            raise ApiProblem("AGENT_LIFECYCLE_CONFLICT", str(exc)) from exc
        except ConfigPatchError as exc:
            # Row #3 (Q9a): an ECS EFS failure (e.g. a stale lock) must be a clean RPC error,
            # not a 500 -- the AgentCore branch never reaches this (it deletes the S3 profile
            # prefix instead, see delete_agent_everywhere), so this only ever fires for ECS.
            raise ApiProblem(
                "RUNTIME_UNAVAILABLE",
                "The runtime configuration could not be updated.",
                retryable=True,
                retry_after=3,
                details={"method": method},
            ) from exc
        await _invalidate(owner_id, method)
        return result
    if method == "agents.create":
        from core.services.hermes_provisioning import HermesProfileError

        container, _ip = await container_rpc.running_container(owner_id)
        try:
            return await _create_agent(owner_id, user_id, _sanitize_agent_create(params), container)
        except HermesProfileError as exc:
            # Row #1 (Q9a): a missing BYO key / failed Paperclip key mint on an AgentCore
            # create must be a clean RPC error, not a 500.
            raise ApiProblem(
                "RUNTIME_UNAVAILABLE",
                str(exc),
                retryable=True,
                retry_after=3,
                details={"method": method},
            ) from exc
    if method == "agents.update":
        is_agentcore_owner = await _is_agentcore(owner_id)
        try:
            await _mirror_agent_update(owner_id, params, agentcore=is_agentcore_owner)
        except AgentLifecycleConflict as exc:
            raise ApiProblem("AGENT_LIFECYCLE_CONFLICT", str(exc)) from exc
        if is_agentcore_owner:
            # Q9a fix round 1: _mirror_agent_update above already applied every AgentCore-side
            # patch (model-block re-derivation, SOUL.md refresh) -- there is no AgentCore
            # bridge RPC for agents.update, so this never falls through to container_rpc.call.
            await _invalidate(owner_id, "agents.update")
            return {"ok": True}
    if method in _HERMES_PROFILE_METHODS and await _is_agentcore(owner_id):
        # M4-R1/M4-R43 (fix round 1 continuation): an AgentCore row's Hermes profile lives in
        # S3, not behind a live gateway socket — the backend is the ONLY writer of a profile
        # (spec §4.5), so these are served/rewritten here with no microVM wake for a settings
        # read. config.get NEVER returns .env content (it reads config.yaml alone).
        # config.set/config.patch bump .revision on write, and the bridge re-pulls before the
        # agent's next turn (Task 13b). ECS rows fall straight through to container_rpc.call.
        #
        # The wire shape below is NOT bridge/server.py's own config.get/config.patch/agents.list
        # (that spike stand-in requires an agentId and returns no hash at all) — it is what the
        # REAL browser clients already call and read: useConfigPatch.ts/PluginsTab.tsx never
        # send an agentId and read snapshot.config/snapshot.hash, and packages/shared's
        # useAgents.ts reads data.agents/data.defaultId.
        if method == "agents.list":
            roster = await load_agent_roster(owner_id)
            return {"defaultId": _default_agent_id(roster), "agents": roster}
        agent_id = params.get("agentId")
        has_agent_id = isinstance(agent_id, str) and bool(agent_id)
        # hermes_profiles.{config_snapshot,patch,set}_profile_config are plain sync functions
        # that do blocking S3 I/O directly (see their own module docstring) — run_in_thread here
        # is what keeps that off the event loop; this repo already took a prod backend freeze
        # from blocking I/O on the loop (EFS tree walk, 2026-09-12).
        if method == "config.get":
            if not has_agent_id:  # C1: never 400 — the real client never sends one.
                return await _config_get_roster_map(owner_id)
            config, config_hash = await run_in_thread(hermes_profiles.config_snapshot, owner_id, agent_id)
            return {"config": config, "hash": config_hash}
        if not has_agent_id:
            if method == "config.set":
                # Zero real callers send config.set with no agentId, and a whole-document
                # replace fanned out over N profiles would clobber per-profile differences.
                raise ApiProblem("INVALID_REQUEST", "Agent ID is required.")
            # M4-R48: config.patch with no agentId is BOTH real callers' actual shape
            # (useConfigPatch.ts, PluginsTab.tsx) — roster-wide fan-out, not a 400.
            result = await config_patch_roster(owner_id, _config_patch_dict(params), params.get("baseHash"))
            await _invalidate(owner_id, method)
            return result
        if method == "config.set":
            result = await run_in_thread(
                hermes_profiles.set_profile_config, owner_id, agent_id, params.get("config") or {}
            )
        else:
            patch = _config_patch_dict(params)
            base_hash = params.get("baseHash")
            if base_hash is not None:
                _current, current_hash = await run_in_thread(hermes_profiles.config_snapshot, owner_id, agent_id)
                if base_hash != current_hash:
                    # C2: same stale-config shape the ECS path answers via
                    # _normalize_gateway_error — an upstreamCode plus a message, INVALID_REQUEST.
                    raise ApiProblem(
                        "INVALID_REQUEST",
                        "The config changed since you last loaded it. Refresh and try again.",
                        details={"method": method, "upstreamCode": "CONFIG_STALE"},
                    )
            if not patch:
                # Nothing to write — no upload, no .revision bump (M4-R43).
                return {"ok": True}
            result = await run_in_thread(hermes_profiles.patch_profile_config, owner_id, agent_id, patch)
        await _invalidate(owner_id, method)
        return result
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


async def _create_agent(owner_id: str, user_id: str, params: dict[str, Any], container: dict) -> dict:
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
    if runtime_transport.host_for(container) == "agentcore":
        # Row #1 (Q9a fix round 1, M6-R27): no OpenClaw gateway exists on this transport for
        # pair_with_openclaw to reach -- its agents.create/agents.update RPCs have no bridge
        # handler (bridge/server.py has no such method) and would 500 loudly before
        # write_profile_for_agent ever ran. Skip it AND ensure_operator_device_paired (a
        # device-pairing concept this transport has no equivalent of) entirely --
        # write_profile_for_agent below IS this transport's pairing: it gives the agent its
        # config, its Paperclip key, and its SOUL (the identity-staging gap scan row #16
        # flagged), and there is no openclaw.json for apply_paperclip_meta to write into.
        from core.services.hermes_provisioning import write_profile_for_agent

        await write_profile_for_agent(owner_id, paperclip_agent, container_row=container)
        await _invalidate(owner_id, "agents.create")
    else:
        await pair_with_openclaw(owner_id=owner_id, paperclip_agent=paperclip_agent)
        # pair_with_openclaw above also registers the agent's Lightbulb channel
        # account + binding — see its docstring.
        await ensure_operator_device_paired(
            owner_id=owner_id,
            paperclip_agent=paperclip_agent,
            gateway_token=container["gateway_token"],
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


#: The only two model tiers hermes_profiles.model_block understands (the same is_ceo switch
#: premium_model_ref/cheap_model_ref key off) -- Q9a fix round 1, M6-R29. An AgentCore
#: agents.update never stores a client-supplied model ref verbatim: a raw ref could point
#: outside the owner's own AIP grant (the gate then refuses every turn) or off the writer's
#: BYO/OAuth vocabulary entirely, and the bridge answers models.list with UNAVAILABLE so there
#: is no picker to validate a free-form choice against in the first place.
_AGENTCORE_MODEL_TIERS: dict[str, bool] = {"standard": False, "premium": True}


def _agentcore_model_tier_is_ceo(model: str) -> bool:
    """The ``is_ceo`` bool ``hermes_profiles.model_block`` wants, from the closed tier
    vocabulary -- raises ``INVALID_REQUEST`` naming the accepted values for anything else."""
    if model not in _AGENTCORE_MODEL_TIERS:
        raise ApiProblem(
            "INVALID_REQUEST",
            f"model must be one of: {', '.join(sorted(_AGENTCORE_MODEL_TIERS))}.",
            details={"method": "agents.update", "accepted": sorted(_AGENTCORE_MODEL_TIERS)},
        )
    return _AGENTCORE_MODEL_TIERS[model]


async def _apply_agentcore_model_patch(owner_id: str, agent_id: str, *, is_ceo: bool) -> None:
    """Row #2 (Q9a fix round 1, M6-R28/M6-R29): re-derive ``config.yaml``'s ``model`` key
    through the writer's own ``model_block`` -- never trust a client-supplied ref, and never
    hand-merge a partial dict (a bare-string patch would let ``patch_profile_config``'s deep
    merge stomp the whole ``model`` dict, including ``provider``)."""
    from core.services.hermes_profiles import model_block
    from core.services.hermes_provisioning import resolve_profile_inputs

    container_row = await container_repo.get_by_owner_id(owner_id) or {}
    inputs = await resolve_profile_inputs(owner_id, container_row=container_row)
    model, _aux_model, _aux_provider = model_block(
        provider_choice=inputs.provider_choice,
        byo_provider=inputs.byo_provider,
        aip_arns=inputs.aip_arns,
        is_ceo=is_ceo,
    )
    await run_in_thread(hermes_profiles.patch_profile_config, owner_id, agent_id, {"model": model})


async def _refresh_agentcore_soul(owner_id: str, agent_id: str) -> None:
    """Row #2 (Q9a fix round 1, I2): name/title/role/capabilities reach the agent ONLY through
    ``SOUL.md`` (``hermes_profiles.build_soul_md``) -- rebuild it from the live (post-mirror)
    Paperclip roster row and upload ONLY that file. Never ``write_profile_for_agent``/
    ``upload_profile``, which would also overwrite the owner's own ``config.yaml``/``.env``.

    M6-R48: regenerate ONLY while the stored file is still the backend-generated one -- its first
    line is ``hermes_profiles.SOUL_GENERATED_MARKER``. The Instructions tab strips that marker when
    an owner saves ``SOUL.md``, and a migrated/marketplace profile carries text that was never
    generated here at all, so in both cases the file is the owner's and a rename must leave it
    alone. A profile with no ``SOUL.md`` yet is still written: there is nothing to preserve."""
    current = await run_in_thread(hermes_profiles.read_soul_md, owner_id, agent_id)
    if current is not None and not hermes_profiles.is_generated_soul(current):
        return
    roster = await load_agent_roster(owner_id)
    agent_row = next((row for row in roster if isinstance(row, dict) and row.get("id") == agent_id), None)
    if agent_row is None:
        return
    soul_md = hermes_profiles.build_soul_md(agent_row)
    await run_in_thread(hermes_profiles.upload_soul_md, owner_id, agent_id, soul_md)


async def _mirror_agent_update(owner_id: str, params: dict[str, Any], *, agentcore: bool = False) -> None:
    inner = params.get("params") or {}
    paperclip = inner.get("paperclip") if isinstance(inner, dict) else None
    name = params.get("name")
    model = params.get("model")
    # M6-R29: validated up front, even when a role change is also present (below) and wins the
    # actual re-derivation -- a garbage `model` value must never be silently ignored.
    is_ceo_from_model: bool | None = None
    if agentcore and isinstance(model, str) and model:
        is_ceo_from_model = _agentcore_model_tier_is_ceo(model)
    if not isinstance(name, str) and not isinstance(paperclip, dict):
        # A model-only update never touches Paperclip (it has no such field) -- but on an
        # AgentCore row it still needs the config.yaml re-derivation below.
        if agentcore and is_ceo_from_model is not None:
            agent_id = params.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                await _apply_agentcore_model_patch(owner_id, agent_id, is_ceo=is_ceo_from_model)
        return
    agent_id = params.get("agentId")
    if not isinstance(agent_id, str) or not agent_id:
        return
    body: dict[str, str] = {}
    if isinstance(name, str):
        body["name"] = name
    meta = None
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
    if agentcore:
        # Row #2 (Q9a fix round 1): an AgentCore row has no openclaw.json for
        # apply_paperclip_meta to write. name/role/title/capabilities/icon already landed on
        # the Paperclip row above; a role change re-derives config.yaml's model block
        # (M6-R28) and any identity-shaped change refreshes SOUL.md (I2) -- never a full
        # write_profile_for_agent, which would clobber the owner's own config.yaml/.env edits.
        if meta is not None:
            await _apply_agentcore_model_patch(owner_id, agent_id, is_ceo=(meta.role == "ceo"))
        elif is_ceo_from_model is not None:
            await _apply_agentcore_model_patch(owner_id, agent_id, is_ceo=is_ceo_from_model)
        if body:
            await _refresh_agentcore_soul(owner_id, agent_id)
        return
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
