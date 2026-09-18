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


async def agentcore_agents_list(owner_id: str) -> dict[str, Any]:
    """``agents.list`` for an AgentCore owner: the Paperclip roster plus each profile's real
    model id and display name (M6 task 10-C, row 29), with no runtime invoke -- see
    ``_HERMES_PROFILE_METHODS``'s docstring above ``execute_sync``'s dispatch.

    Public (not ``_``-prefixed) because ``core.services.admin_service`` reuses it verbatim for
    ``list_user_agents``/``get_agent_detail`` (parity audit row 80): admin inspection of an
    AgentCore owner must never wake a parked microVM either.
    """
    roster = await load_agent_roster(owner_id)
    models = await _agentcore_roster_models(owner_id, roster)
    return {
        "defaultId": _default_agent_id(roster),
        "agents": [
            {**row, "model": model, "modelName": model_name} for row, (model, model_name) in zip(roster, models)
        ],
    }


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


#: Everything the AgentCore ``agents.update`` intercept actually WRITES: ``agentId`` addresses
#: the row, ``name`` + ``params.paperclip`` mirror to Paperclip (and refresh ``SOUL.md``), and
#: ``model`` re-derives ``config.yaml``'s model block. See ``_mirror_agent_update``.
_AGENTCORE_AGENT_UPDATE_FIELDS = frozenset({"agentId", "name", "model", "params"})


def _reject_unsupported_agentcore_update(params: dict[str, Any]) -> None:
    """M6 task 10, row 12 -- the worst failure class in the scan: ``agents.update`` answered
    ``{"ok": True}`` for an AgentCore row while ``_mirror_agent_update`` early-returned and
    wrote NOTHING, so the Tools tab's Save confirmed a change that never happened. Anything
    outside the writable vocabulary above is now refused.

    The error shape is the module's existing one -- ``ApiProblem`` + an ``upstreamCode`` in
    ``details`` -- which is exactly what ``_normalize_gateway_error`` produces for the bridge's
    own ``{"ok": false, "code": ...}`` refusals, so every stubbed-on-Hermes method already
    reaches the client this way. No second error shape is invented here.
    """
    unsupported = sorted(set(params) - _AGENTCORE_AGENT_UPDATE_FIELDS)
    inner = params.get("params")
    if isinstance(inner, dict):
        unsupported += [f"params.{key}" for key in sorted(set(inner) - {"paperclip"})]
    if not unsupported:
        return
    put_metric("rpc.refused", dimensions={"method": "agents.update", "reason": "unsupported_on_agentcore"})
    raise ApiProblem(
        "INVALID_REQUEST",
        "Per-tool allow/deny lists are not available on this runtime."
        if "tools" in unsupported
        else f"agents.update cannot change {', '.join(unsupported)} on this runtime.",
        details={
            "method": "agents.update",
            "upstreamCode": "UNSUPPORTED_ON_AGENTCORE",
            "fields": unsupported,
        },
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
        if is_agentcore_owner:
            # BEFORE any mirror work: refuse the fields this intercept cannot persist rather
            # than answering {"ok": True} to a write that never happened (M6 task 10, row 12).
            _reject_unsupported_agentcore_update(params)
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
    if method == "models.list" and await _is_agentcore(owner_id):
        # M6 task 10-C: the owner's REAL model list. Bedrock-managed workspaces are answered from
        # their own AIP set with no microVM wake; a BYO-key / ChatGPT-OAuth workspace falls
        # through to the bridge below, which holds Hermes's own provider catalog.
        listing = await _agentcore_models_list(owner_id)
        if listing is not None:
            return listing
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
            return await agentcore_agents_list(owner_id)
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
            patch = await _expand_agentcore_config_patch(owner_id, _config_patch_dict(params))
            result = await config_patch_roster(owner_id, patch, params.get("baseHash"))
            await _invalidate(owner_id, method)
            return result
        if method == "config.set":
            result = await run_in_thread(
                hermes_profiles.set_profile_config, owner_id, agent_id, params.get("config") or {}
            )
        else:
            patch = await _expand_agentcore_config_patch(owner_id, _config_patch_dict(params))
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
    elif method == "skills.status" and await _is_agentcore(owner_id):
        # The bridge answers the skill ROWS (it has to -- they come from Hermes's own loaders
        # inside the image). The disabled LIST is the half that gets written back, so it is
        # owned here: see agentcore_disabled_skill_names.
        result["disabledNames"] = await agentcore_disabled_skill_names(owner_id)
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


#: The owner's own Bedrock application inference profiles, by the key ``aip_arns`` stores each
#: under, mapped to the tier vocabulary ``hermes_profiles.model_block`` reasons in. A trial owner
#: holds only ``sonnet``. Display names come from ``spend_service.TIER_DISPLAY_NAMES`` -- the one
#: place the platform already names these (for the usage panel) -- never a second copy here.
_BEDROCK_AIP_TIERS: dict[str, str] = {"opus": "premium", "sonnet": "standard", "haiku": "cheap"}
_BEDROCK_PROVIDER_CHOICES = ("bedrock_claude", "bedrock_trial")
#: The pseudo-key ``config.patch`` carries the owner-level overhead-model choice on (M6 task
#: 10-C, scan rows 4/23). It is NEVER stored: ``_expand_agentcore_config_patch`` replaces it with
#: the real per-task ``auxiliary`` block ``hermes_profiles.auxiliary_tasks_block`` builds, so the
#:18 task names and the provider are derived by the one backend writer rather than hand-assembled
#: by a browser that knows neither.
_AUX_MODEL_KEY = "model"


async def _owner_model_inputs(owner_id: str) -> tuple[str, str | None, dict[str, str]]:
    """``(provider_choice, byo_provider, aip_arns)`` -- the three inputs ``model_block`` and the
    model list need, read directly rather than through ``resolve_profile_inputs`` (which also
    reads the BYO key out of Secrets Manager and builds every integration MCP server: work a
    settings READ has no use for)."""
    from core.services.container_provision import resolve_provider_choice

    provider_choice, byo_provider = await resolve_provider_choice(owner_id)
    container_row = await container_repo.get_by_owner_id(owner_id) or {}
    return provider_choice, byo_provider, container_row.get("bedrock_aip_arns") or {}


async def agentcore_disabled_skill_names(owner_id: str) -> list[str]:
    """The UNION of ``skills.disabled`` across every profile in an AgentCore owner's roster.

    The Skills panel is workspace-level and its patch fans out to every profile, replacing the
    list WHOLESALE -- so whatever this returns IS the next write for all of them, and a name
    missing from it is not merely unchanged, it is DELETED. One profile is not enough:
    ``agents.create`` -> ``hermes_profiles.write_agent_profile`` -> ``build_profile_config``
    emits a fresh ``skills`` block with no ``disabled`` key and never reads the owner's current
    list, so a newly created agent has an empty one and would re-enable every skill the owner
    had turned off, everywhere (M6-R57 / review N2).

    Served here rather than by the bridge because the backend is the ONLY writer of a profile's
    ``config.yaml`` (spec §4.5; the bridge has it in ``sync.EXCLUDE_GLOBS``, so a container copy
    never reaches S3) -- and because ``_s3_get`` already draws the line the bridge had to invent
    a two-valued "is this read authoritative" flag for: ABSENT is ``None`` (a profile
    mid-provision contributes nothing, which is correct and complete), and every other S3 error
    RE-RAISES. So a throttled read fails the RPC loudly instead of yielding a plausibly short
    list that the panel would write back over every profile.
    """
    names: set[str] = set()
    for row in await load_agent_roster(owner_id):
        config, _config_hash = await run_in_thread(hermes_profiles.config_snapshot, owner_id, row["id"])
        raw = (config.get("skills") or {}).get("disabled")
        if isinstance(raw, str):
            # Never our writer (we yaml.safe_dump a list), but `hermes config set` stores a list
            # as a quoted literal string. Reading that as one skill named `["pdf","shopify"]`
            # would show every skill enabled and let the next toggle wipe the real list.
            raw = json.loads(raw) if raw.strip().startswith("[") else [raw]
        names.update(value.strip() for value in raw or [] if isinstance(value, str) and value.strip())
    return sorted(names)


async def _agentcore_models_list(owner_id: str) -> dict[str, Any] | None:
    """``models.list`` for an AgentCore owner, or ``None`` when the bridge owns the answer.

    M6-R50: the picker lists the models the owner can ACTUALLY invoke. On a Bedrock-managed
    workspace the bedrock-gate admits only that owner's own application inference profiles, so
    the list is exactly those -- built here, with no microVM wake for a settings read. On a
    BYO-key / ChatGPT-OAuth workspace the catalog is Hermes's own and only the runtime holds it,
    so ``None`` means "fall through to the bridge".
    """
    provider_choice, byo_provider, aip_arns = await _owner_model_inputs(owner_id)
    if provider_choice not in _BEDROCK_PROVIDER_CHOICES:
        return None
    from core.services.spend_service import TIER_DISPLAY_NAMES

    models = [
        {"id": aip_arns[key], "name": TIER_DISPLAY_NAMES.get(key, key), "provider": "bedrock", "tier": tier}
        for key, tier in _BEDROCK_AIP_TIERS.items()
        if aip_arns.get(key)
    ]
    if not models:
        # No AIPs on the row yet (mid-provision). An empty list is the honest answer: the picker
        # then shows the agent's current model alone, and the next read fills it in.
        return {"models": [], "defaultId": ""}
    if not aip_arns.get("sonnet"):
        # `model_block` requires sonnet (it is the standard tier and every fresh profile's
        # default, so provisioning raises loudly without it -- see core.containers.config). A
        # READ must not 500 on the same fact: report the first model the owner does hold as the
        # default instead (fix round 1, M3).
        return {"models": models, "defaultId": models[0]["id"]}
    default, _aux_model, _aux_provider = hermes_profiles.model_block(
        provider_choice=provider_choice, byo_provider=byo_provider, aip_arns=aip_arns, is_ceo=False
    )
    return {"models": models, "defaultId": default["default"]}


async def _validated_model_id(owner_id: str, model: str, *, method: str) -> str:
    """``model`` iff it is one of the models THIS owner's ``models.list`` offers.

    The closed ``standard``/``premium`` vocabulary this replaces existed only because there was
    no list to check a free-form choice against (M6-R29). There is one now, and the check is the
    same fence: a Bedrock ref outside the owner's own AIP grant makes the gate refuse every turn.
    BYO/OAuth ids come from the bridge's copy of Hermes's catalog -- the same call the picker
    itself just made, so the microVM it needs is already warm.
    """
    listing = await _agentcore_models_list(owner_id)
    if listing is None:
        listing = await container_rpc.call(owner_id, "models.list", {}, timeout_s=_RPC_TIMEOUT_S)
    offered = [
        row["id"]
        for row in (listing or {}).get("models", [])
        if isinstance(row, dict) and isinstance(row.get("id"), str)
    ]
    if model in offered:
        return model
    put_metric("rpc.refused", dimensions={"method": method, "reason": "model_not_offered"})
    raise ApiProblem(
        "INVALID_REQUEST",
        "That model is not one this workspace can use.",
        details={"method": method, "upstreamCode": "MODEL_NOT_OFFERED", "accepted": offered},
    )


def _profile_model_id(config: Any) -> str:
    """An ``agents.list`` row's model: the profile's own ``config.yaml`` ``model.default``
    (M6 task 10-C, scan row 29) -- the real id ``models.list`` names and the picker sends back,
    not a tier word. ``""`` when the profile pins none, or when reading it failed."""
    block = config.get("model") if isinstance(config, dict) else None
    default = block.get("default") if isinstance(block, dict) else None
    return default if isinstance(default, str) else ""


async def _agentcore_roster_models(owner_id: str, roster: list[dict[str, Any]]) -> list[tuple[str, str]]:
    """Each roster row's ``(model id, display name)``, in roster order.

    ``return_exceptions`` is load-bearing (fix round 1, I3): ``_s3_get`` re-raises everything
    that is not a 404 -- a SlowDown, a throttle, a KMS blip -- and ``agents.list`` is the hottest
    control RPC on the platform (the chat roster plus every panel's ``useAgents``). One 503 on
    one of six profiles must blank one model LABEL, not empty the whole agent sidebar; a failed
    read falls through ``_profile_model_id``'s own "no model pinned" answer, the degraded state
    this shape already has.

    # ponytail: one default-executor thread per roster row per call, bounded only by roster
    # size (an 11-agent prod owner already exists). Batch or cache if a roster ever grows past
    # the low tens.
    """
    configs = await asyncio.gather(
        *(run_in_thread(hermes_profiles.read_profile_config, owner_id, row["id"]) for row in roster),
        return_exceptions=True,
    )
    for config in configs:
        if isinstance(config, BaseException):
            logger.warning("agents.list profile read failed owner=%s error=%s", owner_id, type(config).__name__)
    names = await _agentcore_model_names(owner_id)
    ids = [_profile_model_id(config) for config in configs]
    return [(model_id, names.get(model_id, "")) for model_id in ids]


async def _agentcore_model_names(owner_id: str) -> dict[str, str]:
    """``{model id: display name}`` for this owner, WITHOUT ever reaching the runtime.

    Ruling M6-R53 (fix round 1, I2): the chat sidebar must not dial ``models.list``, because on a
    BYO-key / ChatGPT-OAuth workspace that call goes to the bridge and cold-starts a microVM
    (~55 s) for a label nobody asked for. So ``agents.list`` -- which already reads every profile
    and already holds the tier names -- carries the name itself, and only the picker surfaces
    dial ``models.list``. A non-Bedrock owner gets ``{}`` here rather than a bridge call: the
    client then renders the raw id, and the real names arrive when the picker is opened."""
    listing = await _agentcore_models_list(owner_id)
    if listing is None:
        return {}
    return {row["id"]: row["name"] for row in listing["models"] if isinstance(row.get("name"), str)}


async def _expand_agentcore_config_patch(owner_id: str, patch: dict[str, Any]) -> dict[str, Any]:
    """Rewrite the overhead-model pseudo-key into the real ``auxiliary`` block (rows 4/23), and
    refuse a raw ``model`` block.

    ``{"auxiliary": {"model": "<id>"}}`` is what the Model Routing card sends. ``auxiliary.model``
    is not a Hermes key and is never stored: the id is validated against the owner's own
    ``models.list`` and expanded here, so the task names and the provider come from
    ``hermes_profiles`` -- the browser knows neither, and a hand-built block would drift from the
    one ``build_profile_config`` writes at provision.

    The ``model`` refusal (fix round 1, I4) closes the sibling door ``agents.update``'s fence left
    open: ``config.patch`` deep-merges whatever it is handed, so a raw
    ``{"model": {"default": "<an ARN outside this owner's grant>"}}`` -- fanned out to EVERY
    profile when no ``agentId`` is sent -- would make the bedrock-gate refuse every turn for the
    workspace until someone reprovisions. With this, ``model_block`` really is the one writer.
    """
    if "model" in patch:
        put_metric("rpc.refused", dimensions={"method": "config.patch", "reason": "raw_model_block"})
        raise ApiProblem(
            "INVALID_REQUEST",
            "The model is set per agent — use the agent's Model card.",
            details={
                "method": "config.patch",
                "upstreamCode": "UNSUPPORTED_ON_AGENTCORE",
                "fields": ["model"],
                "use": "agents.update",
            },
        )
    aux = patch.get("auxiliary")
    model = aux.get(_AUX_MODEL_KEY) if isinstance(aux, dict) else None
    if not isinstance(model, str) or not model:
        return patch
    model = await _validated_model_id(owner_id, model, method="config.patch")
    provider_choice, byo_provider, aip_arns = await _owner_model_inputs(owner_id)
    _model, _aux_model, aux_provider = hermes_profiles.model_block(
        provider_choice=provider_choice, byo_provider=byo_provider, aip_arns=aip_arns, is_ceo=False
    )
    expanded = {key: value for key, value in aux.items() if key != _AUX_MODEL_KEY}
    expanded.update(hermes_profiles.auxiliary_tasks_block(model, aux_provider))
    return {**patch, "auxiliary": expanded}


async def _apply_agentcore_model_patch(owner_id: str, agent_id: str, *, default_id: str) -> None:
    """Row #2 (Q9a fix round 1, M6-R28/M6-R29): write ``config.yaml``'s ``model`` key through the
    writer's own ``model_block`` -- never hand-merge a partial dict (a bare-string patch would
    let ``patch_profile_config``'s deep merge stomp the whole ``model`` dict, including
    ``provider``).

    ``default_id`` is required, and is an ALREADY-VALIDATED pick from the owner's own
    ``models.list`` (fix round 1, C1): an explicit choice is now the ONLY thing that rewrites
    this block, so there is no caller left that wants the role's tier default. ``is_ceo`` is
    therefore moot here -- it only ever chose ``default``, which ``default_id`` replaces -- and
    passing it would imply a policy this door no longer has."""
    from core.services.hermes_profiles import model_block
    from core.services.hermes_provisioning import resolve_profile_inputs

    container_row = await container_repo.get_by_owner_id(owner_id) or {}
    inputs = await resolve_profile_inputs(owner_id, container_row=container_row)
    model, _aux_model, _aux_provider = model_block(
        provider_choice=inputs.provider_choice,
        byo_provider=inputs.byo_provider,
        aip_arns=inputs.aip_arns,
        is_ceo=False,
        default_id=default_id,
    )
    await run_in_thread(hermes_profiles.patch_profile_config, owner_id, agent_id, {"model": model})


async def _mirror_agent_update(owner_id: str, params: dict[str, Any], *, agentcore: bool = False) -> None:
    inner = params.get("params") or {}
    paperclip = inner.get("paperclip") if isinstance(inner, dict) else None
    name = params.get("name")
    model = params.get("model")
    # M6-R29 / M6-R50: validated up front against the owner's own models.list -- a model the
    # workspace cannot invoke must never be silently ignored, nor silently stored.
    chosen_model: str | None = None
    if agentcore and "model" in params:
        if not isinstance(model, str) or not model:
            # `{"model": null}` is "clear the override, inherit the gateway default" on ECS.
            # There is no workspace-level default on Hermes -- each agent's own profile IS the
            # setting -- so this used to write nothing and still answer {"ok": True}: the same
            # silent-lie class row 12 was fixed for (fix round 1, M2).
            raise ApiProblem(
                "INVALID_REQUEST",
                "There is no workspace default to inherit on this runtime — pick a model instead.",
                details={"method": "agents.update", "upstreamCode": "UNSUPPORTED_ON_AGENTCORE", "fields": ["model"]},
            )
        chosen_model = await _validated_model_id(owner_id, model, method="agents.update")
    if not isinstance(name, str) and not isinstance(paperclip, dict):
        # A model-only update never touches Paperclip (it has no such field) -- but on an
        # AgentCore row it still needs the config.yaml re-derivation below.
        if agentcore and chosen_model is not None:
            agent_id = params.get("agentId")
            if isinstance(agent_id, str) and agent_id:
                await _apply_agentcore_model_patch(owner_id, agent_id, default_id=chosen_model)
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
        # the Paperclip row above; any identity-shaped change refreshes SOUL.md (I2) -- never a
        # full write_profile_for_agent, which would clobber the owner's own config.yaml/.env
        # edits.
        #
        # ONLY an explicit `model` rewrites the model block (fix round 1, C1). A role change
        # used to re-derive it from the role's tier (M6-R28), which was self-consistent while
        # the role WAS the only model choice. Now that the picker offers role-independent ids,
        # that made the Role & description card's Save -- which carries no `model` at all, and
        # sits directly below the picker in the same panel -- silently revert the owner's pick
        # (and re-promote a deliberately demoted CEO to opus, a billing-relevant field). A
        # fresh profile write still derives the tier default; nothing else does.
        if chosen_model is not None:
            await _apply_agentcore_model_patch(owner_id, agent_id, default_id=chosen_model)
        if body:
            # Shared with the Teams BFF's own PATCH door (M6-R59) -- one owner for the
            # generated-marker rule. See its docstring.
            from core.services.hermes_provisioning import refresh_agent_soul

            await refresh_agent_soul(owner_id, agent_id)
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
