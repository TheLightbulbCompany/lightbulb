"""Guarded browser runtime RPC policy, moved intact from the retired chat socket."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from typing import Any

import botocore.exceptions

from core.auth import AuthContext, require_org_admin, resolve_owner_id
from core.dynamodb import run_in_thread
from core.observability.metrics import put_metric
from core.repositories import container_repo, messaging_repo
from core.services import container_rpc, hermes_profiles
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
from core.services import agent_registry
from core.services.agent_lifecycle import (
    AgentLifecycleConflict,
    delete_agent_everywhere,
    ensure_root_ceo_survives,
)
from core.services.agent_meta import read_from_agent_entry, resolve_create_agent_fields

logger = logging.getLogger(__name__)

_RUN_DISPATCH_USE = {
    "chat.send": "/api/v1/messaging/rooms/{roomId}/messages",
    "sessions.send": "/api/v1/messaging/rooms/{roomId}/messages",
    "sessions.steer": "/api/v1/messaging/runs/{runId}/steer",
    "agent": "/api/v1/messaging/rooms/{roomId}/messages",
}
_RUN_DISPATCH_METHODS = frozenset(_RUN_DISPATCH_USE)

# Default deny. Exactly the methods something answers: the ones execute_sync serves itself
# (agents.*, config.*, and models.list for managed workspaces) plus the read-only ones the Hermes
# bridge registers in `bridge/server.py`'s RPC_METHODS (health, models.*, skills.status). The bridge answers anything else with
# UNAVAILABLE, so an OpenClaw-era method is refused here instead of costing a runtime invoke.
# Room commands, run control, approvals, and questions have dedicated product endpoints.
_PROXY_ALLOWED_METHODS = frozenset(
    {
        "agents.create",
        "agents.delete",
        "agents.list",
        "agents.update",
        "config.get",
        "config.patch",
        "health",
        "models.authStatus",
        "models.list",
        "skills.status",
    }
)

_MUTATING_METHODS = frozenset(
    {
        "agents.create",
        "agents.delete",
        "agents.update",
        "config.patch",
    }
)
# The profile config carries owner-level settings and secrets references (MCP servers, the
# auxiliary base_url), so reading or writing it is admin-only -- the same gate as the REST twin,
# routers/config.py. agents.* stay open to members, matching routers/teams/agents.py.
_ADMIN_METHODS = frozenset({"config.get", "config.patch"})
_RPC_TIMEOUT_S = 30.0
#: M4-R1 (Task 13a): served from the profile in S3 + the Paperclip roster, never forwarded to
#: the runtime.
_HERMES_PROFILE_METHODS = frozenset({"config.get", "config.patch", "agents.list"})
# A mutating method that lands here invalidates the named resource for every
# open frontend tab (the prior per-connection `*.changed` broadcast is gone).
_INVALIDATES = {
    "agents.create": "agents",
    "agents.update": "agents",
    "agents.delete": "agents",
    "config.patch": "config",
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
    no-``agentId`` ``config.get``.

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


async def _invalidate(owner_id: str, method: str) -> None:
    """Replaces the broadcasts the deleted gateway socket used to relay. Never raises."""
    resource = _INVALIDATES.get(method)
    if resource is None:
        return
    try:
        await publish_owner_users(owner_id, make_live_event("runtime.resource.invalidated", {"resource": resource}))
    except Exception:
        logger.warning("resource invalidation publish failed resource=%s", resource, exc_info=True)


def _guard_method(method: str) -> None:
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
    # `workspace` is never required or derived here: a Hermes/AgentCore agent has no EFS path. `clean` is built
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
    _guard_method(request.method)
    if request.method in _ADMIN_METHODS:
        require_org_admin(auth)
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
    _guard_method(method)
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
        except (botocore.exceptions.BotoCoreError, botocore.exceptions.ClientError) as exc:
            # Q9a row #3: the profile delete is an S3 call, and a transport fault on it must
            # surface as a clean retryable RPC error rather than an unhandled 500. It runs
            # BEFORE Paperclip removal on purpose (see delete_agent_everywhere), so the row
            # stays visible and a retry reconciles instead of stranding a half-deleted agent.
            raise ApiProblem(
                "RUNTIME_UNAVAILABLE", str(exc), retryable=True, retry_after=3, details={"method": method}
            ) from exc
        await _invalidate(owner_id, method)
        return result
    if method == "agents.create":
        from core.services.hermes_provisioning import HermesProfileError

        container = await container_rpc.running_container(owner_id)
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
        # BEFORE any mirror work: refuse the fields this intercept cannot persist rather than
        # answering {"ok": True} to a write that never happened (M6 task 10, row 12).
        _reject_unsupported_agentcore_update(params)
        try:
            await _mirror_agent_update(owner_id, params, agentcore=True, by=user_id)
        except AgentLifecycleConflict as exc:
            raise ApiProblem("AGENT_LIFECYCLE_CONFLICT", str(exc)) from exc
        # _mirror_agent_update already applied every profile-side patch (model-block
        # re-derivation, SOUL.md refresh) -- there is no bridge RPC for agents.update, so this
        # never falls through to container_rpc.call.
        await _invalidate(owner_id, "agents.update")
        return {"ok": True}
    if method == "models.list":
        # M6 task 10-C: the owner's REAL model list. Bedrock-managed workspaces are answered from
        # their own AIP set with no microVM wake; a BYO-key / ChatGPT-OAuth workspace falls
        # through to the bridge below, which holds Hermes's own provider catalog.
        listing = await _agentcore_models_list(owner_id)
        if listing is not None:
            return listing
    if method in _HERMES_PROFILE_METHODS:
        # M4-R1/M4-R43 (fix round 1 continuation): an AgentCore row's Hermes profile lives in
        # S3, not behind a live gateway socket — the backend is the ONLY writer of a profile
        # (spec §4.5), so these are served/rewritten here with no microVM wake for a settings
        # read. config.get NEVER returns .env content (it reads config.yaml alone).
        # config.patch bumps .revision on write, and the bridge re-pulls before the
        # agent's next turn (Task 13b).
        #
        # The wire shape below is what the REAL browser clients already call and read (the
        # bridge serves none of these methods): useConfigPatch.ts/PluginsTab.tsx never
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
            # M4-R48: config.patch with no agentId is BOTH real callers' actual shape
            # (useConfigPatch.ts, PluginsTab.tsx) — roster-wide fan-out, not a 400.
            patch = await _expand_agentcore_config_patch(owner_id, _config_patch_dict(params))
            result = await config_patch_roster(owner_id, patch, params.get("baseHash"))
            await _invalidate(owner_id, method)
            return result
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

    if method == "skills.status":
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
    inner = params.get("params") or {}
    meta = read_from_agent_entry({"params": inner}) if inner.get("paperclip") else None
    fields = resolve_create_agent_fields(role=params.get("role"), meta=meta)
    agent = await agent_registry.create(
        owner_id,
        name=params["name"],
        role=fields["role"],
        title=fields["title"],
        persona=fields["capabilities"],
        icon=fields["icon"],
    )
    # write_profile_for_agent IS this transport's pairing: config, credentials and SOUL. No profile,
    # no agent: the row is deleted again so the roster never shows one that cannot run.
    from core.services.hermes_provisioning import write_profile_for_agent

    try:
        await write_profile_for_agent(owner_id, agent)
    except Exception:
        await agent_registry.delete(owner_id, agent["id"])
        raise
    await _invalidate(owner_id, "agents.create")
    return {"agentId": agent["id"], "agent": agent}


#: The pseudo-key ``config.patch`` carries the owner-level overhead-model choice on (M6 task
#: 10-C, scan rows 4/23). It is NEVER stored: ``_expand_agentcore_config_patch`` replaces it with
#: the real per-task ``auxiliary`` block ``hermes_profiles.auxiliary_tasks_block`` builds, so the
#:18 task names and the provider are derived by the one backend writer rather than hand-assembled
#: by a browser that knows neither.
_AUX_MODEL_KEY = "model"


async def _owner_model_inputs(owner_id: str) -> tuple[str, str | None]:
    """``(provider_choice, byo_provider)`` -- the inputs ``model_block`` and the model list need, read directly rather than through ``resolve_profile_inputs`` (which also
    reads the BYO key out of Secrets Manager and builds every integration MCP server: work a
    settings READ has no use for)."""
    from core.services.container_provision import resolve_provider_choice

    return await resolve_provider_choice(owner_id)


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

    M6-R50: the picker lists the models the owner can ACTUALLY invoke. On a managed workspace
    that is the one server model -- built here, with no microVM wake for a settings read. On a
    BYO-key / ChatGPT-OAuth workspace the catalog is Hermes's own and only the runtime holds it,
    so ``None`` means "fall through to the bridge".
    """
    provider_choice, _byo_provider = await _owner_model_inputs(owner_id)
    if provider_choice == "openrouter_managed":
        model_id = hermes_profiles.settings.OPENROUTER_MANAGED_MODEL_ID.strip()
        models = [{"id": model_id, "name": model_id, "provider": "custom"}] if model_id else []
        return {"models": models, "defaultId": model_id}
    return None


async def _validated_model_id(owner_id: str, model: str, *, method: str) -> str:
    """``model`` iff it is one of the models THIS owner's ``models.list`` offers.

    The closed ``standard``/``premium`` vocabulary this replaces existed only because there was
    no list to check a free-form choice against (M6-R29). There is one now, and the check is the
    same fence: a model outside the owner's own offer is refused before it reaches a profile.
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
    provider_choice, byo_provider = await _owner_model_inputs(owner_id)
    model_block, _aux_model, aux_provider = hermes_profiles.model_block(
        provider_choice=provider_choice, byo_provider=byo_provider, is_ceo=False
    )
    expanded = {key: value for key, value in aux.items() if key != _AUX_MODEL_KEY}
    expanded.update(
        hermes_profiles.auxiliary_tasks_block(
            model, aux_provider, base_url=model_block.get("base_url", ""), api_key=model_block.get("api_key", "")
        )
    )
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

    inputs = await resolve_profile_inputs(owner_id)
    model, _aux_model, _aux_provider = model_block(
        provider_choice=inputs.provider_choice,
        byo_provider=inputs.byo_provider,
        is_ceo=False,
        default_id=default_id,
    )
    await run_in_thread(hermes_profiles.patch_profile_config, owner_id, agent_id, {"model": model})


async def _mirror_agent_update(
    owner_id: str, params: dict[str, Any], *, agentcore: bool = False, by: str | None = None
) -> None:
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
    fields = {"persona" if k == "capabilities" else k: v for k, v in body.items()}
    try:
        roster = await agent_registry.roster(owner_id)
        target = next((a for a in roster if a["id"] == agent_id), None)
        if target is None:
            return
        if body.get("role") not in (None, "ceo"):
            ensure_root_ceo_survives(target=target, agents=roster)
        await agent_registry.update(owner_id, agent_id, fields)
    except agent_registry.RegistryUnavailable as exc:
        logger.warning("agent metadata write failed owner=%s agent=%s", owner_id, agent_id, exc_info=True)
        raise ApiProblem(
            "RUNTIME_UNAVAILABLE",
            "Couldn't save the agent's details.",
            retryable=True,
            retry_after=3,
            details={"method": "agents.update"},
        ) from exc
    if agentcore:
        # Row #2 (Q9a fix round 1): an AgentCore row has no openclaw.json for
        # a config write. name/role/title/capabilities/icon already landed on
        # the registry row above; any identity-shaped change refreshes SOUL.md (I2) -- never a
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

            await refresh_agent_soul(owner_id, agent_id, by=by)
        return
    # No metadata mirror: the role/title/capabilities/icon block used to be written into the
    # agent's `openclaw.json` entry so the runtime could read it back. The Paperclip row IS
    # the record on this transport, and `refresh_agent_soul` above is what carries an identity
    # change into the profile the agent actually reads.
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
