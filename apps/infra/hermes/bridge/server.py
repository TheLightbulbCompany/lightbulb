"""Hermes-on-AgentCore bridge (spike). One process, two routes, no gateway.

AgentCore speaks to this container on :8080 (GET /ping, POST /invocations). /invocations carries
the {path, method, headers, body} envelope the backend's runtime_transport already sends, and this
process answers the two paths the lightbulb-channel plugin answers today, so the backend cannot
tell a Hermes microVM from an OpenClaw one:

  POST /plugins/lightbulb-channel/inbound/<agentId>   202 accepted, then the turn runs in a thread
  POST /plugins/lightbulb-channel/rpc                  health, agents.list

A turn = restore the agent's profile dir from S3 (once per process), run one Hermes AIAgent turn
under that profile's HERMES_HOME, post run.started/run.ended and the reply to the backend with the
hook token, save the profile dir back to S3. Nothing session-bound happens before the first
/invocations: AgentCore's warm pool runs this CMD on ~10 pooled instances per runtime version.

Spike scope. Not here: the local-session relay.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import GATEWAY_TOKEN, HOOK_TOKEN, OWNER, PORT, STARTED, now
from bridge import sync, turn

logging.basicConfig(level=logging.INFO, format="[bridge] %(message)s", stream=sys.stdout)
log = logging.getLogger("bridge")


# ---- the two plugin paths ----------------------------------------------------------------------
def _authorized(headers: dict) -> bool:
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    return bool(GATEWAY_TOKEN) and raw.strip() == f"Bearer {GATEWAY_TOKEN}"


def _header(headers: dict, name: str) -> str | None:
    name = name.lower()
    return next((v for k, v in headers.items() if k.lower() == name), None)


_RUN_CONTROL_RE = re.compile(r"^/plugins/lightbulb-channel/runs/([0-9a-f-]{36})/(abort|steer)$")


def handle_plugin(path: str, method: str, headers: dict, body) -> tuple[int, dict]:
    if not _authorized(headers):
        return 401, {"error": "unauthorized"}
    control = _RUN_CONTROL_RE.match(path)
    if control and method == "POST" and control.group(2) == "abort":
        run_id = control.group(1)
        if turn.abort_run(run_id):
            return 202, {"runId": run_id, "state": "aborting"}
        return 404, {"runId": run_id, "state": "not_found"}
    if control and method == "POST" and control.group(2) == "steer":
        run_id = control.group(1)
        text = (body or {}).get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text.encode()) > turn.MAX_TEXT_BYTES:
            return 400, {"error": "invalid_steer", "message": "text is required"}
        if turn.steer_run(run_id, text.strip()):
            return 202, {"runId": run_id, "state": "accepted"}
        return 404, {"runId": run_id, "state": "not_found"}
    if path.startswith("/plugins/lightbulb-channel/inbound/") and method == "POST":
        agent_id = path.rsplit("/", 1)[-1]
        try:
            turn.parse_ingress(body if isinstance(body, dict) else {})
            if body.get("targetAgentId") != agent_id:
                raise turn.IngressError("targetAgentId does not match the path")
        except turn.IngressError as exc:
            return 400, {"error": "invalid_ingress", "message": exc.message}
        # agent_mail (Task 20) and paperclip_run (Task 21) carry no `run` block at all -- the
        # registry/replay/run.started steps below are for a room turn's run lifecycle, which
        # run-less ingresses have none of; run_turn's own has_run=False path handles the rest.
        run_id = (body.get("run") or {}).get("runId")
        event_id = body.get("eventId")
        if run_id:
            if not turn.register_run(run_id):
                return 200, {"eventId": event_id, "runId": run_id, "status": "replayed"}
            turn.mark_started(run_id)
            turn._post_backend(f"runs/{OWNER}/events", {
                "runId": run_id, "type": "run.started", "at": now(),
                "sessionKey": turn._resolve_session_key(agent_id, body),
            })
        elif event_id:
            # Fix round (Important 4): a run-less ingress has no `run.runId` to dedupe on, so
            # it never touched the RUNS registry at all -- a redelivered mail/paperclip event
            # (SES retries on "error"/"woken") ran a second full turn: tokens spent twice, a
            # second reply, real side effects. Register it in the SAME registry keyed by its
            # own eventId (namespaced so it can never collide with a real runId's UUID) and
            # answer the identical 200 replayed a real run's replay gets. There is no
            # run.started/mark_started for it -- there is no run lifecycle to start.
            event_key = f"event:{event_id}"
            if not turn.register_run(event_key):
                return 200, {"eventId": event_id, "runId": run_id, "status": "replayed"}
            # Nothing further to track once "seen" is recorded -- finish immediately so
            # prune_runs can reclaim it once the registry grows past its cap.
            turn.finish_run(event_key, "dispatched")
        local_session_token = _header(headers, "X-Lightbulb-Local-Session-Token")
        threading.Thread(target=turn.run_turn, args=(agent_id, body, local_session_token),
                         daemon=True, name=f"turn-{run_id or body.get('eventId')}").start()
        return 202, {"eventId": body.get("eventId"), "runId": run_id, "status": "accepted"}
    if path == "/plugins/lightbulb-channel/rpc" and method == "POST":
        return _rpc(body)
    return 404, {"error": "not_found", "path": path}


# ---- RPC (spec §5) ------------------------------------------------------------------------------
RPC_DEFAULT_TIMEOUT_MS = 30_000
RPC_MAX_TIMEOUT_MS = 120_000

# Control-panel surfaces only. Stubbing them keeps the panels rendering an explicit
# "unavailable" instead of spinning; each gets a real implementation when its Hermes home
# exists. The SHAPE is the contract — container_rpc unwraps {ok:false} into a RuntimeError
# carrying this exact JSON, so it must never wobble.
UI_ONLY_METHODS = frozenset({
    # `models.list` is served for real by task 10-C's `_models_list` below.
    "tools.catalog", "commands.list", "chat.history", "agent.identity.get",
    "skills.proposals.apply", "skills.proposals.inspect", "skills.proposals.reject",
    # Ruling M6-R56: hub search + install are cut. Both ran Hermes's hub machinery inside one
    # turn-free RPC, and that machinery does not fit here: `tools/skills_hub.py` resolves its
    # HUB_DIR from the LIVE hermes home, so the index cache (tens of MB) lands inside the
    # profile and syncs to S3; the GitHub adapter reads GITHUB_TOKEN through the secret scope,
    # which a settings RPC has no business installing; and a cold fetch + scan + upload cannot
    # fit the 90 s AgentCore ingress cap the brief set. The Skills panel is a READ on Hermes.
    "skills.install", "skills.search",
    # Ruling M6-R54: sessions stay stubbed. The backend's session guard is a PRIVACY rule
    # (a person sees only their own personal sessions) and Hermes has no personal session --
    # every one of ours is room- or event-scoped -- so there is no AgentCore shape for this
    # surface that does not widen that rule. The panel is hidden instead.
    "sessions.list", "sessions.patch", "sessions.delete",
    "sessions.rewind", "sessions.search",
    "sessions.branches.list", "sessions.branches.switch",
    "doctor.memory.status", "doctor.memory.dreamDiary", "doctor.memory.resetDreamDiary",
    "doctor.memory.backfillDreamDiary", "doctor.memory.dedupeDreamDiary",
    "doctor.memory.repairDreamingArtifacts", "doctor.memory.resetGroundedShortTerm",
    # browser.request is retired with the container browser (spec §5).
    "browser.request",
})


def _profiles() -> list[str]:
    return sorted(p.name for p in (sync.HOME / "profiles").glob("*") if p.is_dir())


#: (expires_at, ids) for _roster's S3 listing. A settings panel can land on a microVM that has
#: never run a turn, where `profiles/` is EMPTY -- answering "no agents" or "no skills" from
#: that would be an empty answer that reads as fact. One delimited LIST of the owner's prefix
#: is the cheap truth; cached because `health` polls every 60 s.
_ROSTER_TTL_S = 60.0
_roster_cache: tuple[float, tuple[str, ...]] = (0.0, ())


def _roster() -> list[str]:
    """Every agent id this owner has, local dirs UNION the S3 prefix. Falls back to the local
    dirs alone when S3 is unreachable -- a listing blip must not empty a panel."""
    global _roster_cache
    names = set(_profiles())
    if not sync.S3_BUCKET:
        return sorted(names)
    expires, cached = _roster_cache
    if time.time() >= expires:
        try:
            prefix = f"{sync.S3_PREFIX}/"
            pages = sync._s3().get_paginator("list_objects_v2").paginate(
                Bucket=sync.S3_BUCKET, Prefix=prefix, Delimiter="/")
            cached = tuple(sorted(
                p["Prefix"][len(prefix):].strip("/")
                for page in pages for p in page.get("CommonPrefixes", [])
            ))
            _roster_cache = (time.time() + _ROSTER_TTL_S, cached)
        except Exception:  # noqa: BLE001 -- keep serving the local view on an S3 blip
            log.warning("roster listing failed", exc_info=True)
    return sorted(names | set(cached))


def _boot_ready() -> bool:
    """Ingress cannot be accepted without the hook token (run callbacks) and the profiles
    dir. This is the whole readiness answer: cheap, local, and true only when a turn would
    actually work — readiness_state treats any exception here as 'not ready'."""
    return bool(HOOK_TOKEN) and (sync.HOME / "profiles").is_dir()


def _health(_params: dict, **_kwargs) -> dict:
    # container_rpc.call only raises when the gateway answers {ok:false} (or the transport
    # itself fails) -- it never inspects the payload -- and readiness_state's
    # _gateway_reachable treats "no exception" as ready. So a not-ready bridge MUST answer
    # ok:false here, or a booting container reads as ready fleet-wide.
    if not _boot_ready():
        return {"ok": False, "error": {"code": "NOT_READY", "message": "ingress cannot be accepted yet"}}
    # No session count (ruling M6-R54): it would have been one sqlite read per locally-restored
    # profile on the READINESS path, and with the Sessions panel hidden on AgentCore there is
    # nobody left to read it.
    return {"ok": True, "payload": {
        "ready": True, "runtime": "hermes",
        "uptimeS": round(time.time() - STARTED, 1), "busy": turn._busy,
        "agents": len(_roster()),
    }}


RPC_METHODS: dict = {"health": _health}


def _unavailable(method: str) -> dict:
    return {"ok": False, "error": {"code": "UNAVAILABLE",
                                   "message": f"{method} is not available on the Hermes runtime"}}


for _m in UI_ONLY_METHODS:
    RPC_METHODS.setdefault(_m, (lambda m: lambda _p, **_kwargs: _unavailable(m))(_m))


def _rpc(body) -> tuple[int, dict]:
    if not isinstance(body, dict):
        return 400, {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "body must be an object"}}
    method, params = body.get("method"), body.get("params", {})
    if not isinstance(method, str) or not method:
        return 400, {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "method is required"}}
    if not isinstance(params, dict):
        return 400, {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "params must be an object"}}
    timeout_ms = body.get("timeoutMs", RPC_DEFAULT_TIMEOUT_MS)
    if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, int) or timeout_ms <= 0:
        return 400, {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "timeoutMs must be a positive integer"}}
    timeout_ms = min(timeout_ms, RPC_MAX_TIMEOUT_MS)
    handler = RPC_METHODS.get(method)
    if handler is None:
        return 200, _unavailable(method)
    try:
        result = handler(params, timeout_ms=timeout_ms)
    except Exception:  # noqa: BLE001 — the exception text may carry params; never return it
        log.warning("rpc %s failed", method, exc_info=True)
        return 200, {"ok": False, "error": {"code": "UNAVAILABLE",
                                            "message": "gateway method failed before returning a response"}}
    if result.get("ok") is False and (result.get("error") or {}).get("code") == "AGENT_TIMEOUT":
        return 504, result
    return 200, result


def _require_profile(params: dict) -> str:
    agent_id = str(params.get("agentId") or "")
    if agent_id not in _profiles():
        raise LookupError(agent_id)
    return agent_id


def _config_get(params: dict, **_kwargs) -> dict:
    if params.get("agentId"):
        return {"ok": True, "payload": {"config": sync.read_profile_config(_require_profile(params))}}
    return {"ok": True, "payload": {"config": {name: sync.read_profile_config(name) for name in _profiles()}}}


def _config_patch(params: dict, **_kwargs) -> dict:
    agent_id = _require_profile(params)
    merged = sync.deep_merge(sync.read_profile_config(agent_id), params.get("patch") or {})
    sync.write_profile_config(agent_id, merged)
    return {"ok": True, "payload": {"config": merged}}


def _config_set(params: dict, **_kwargs) -> dict:
    agent_id = _require_profile(params)
    config = params.get("config") or {}
    sync.write_profile_config(agent_id, config)
    return {"ok": True, "payload": {"config": config}}


RPC_METHODS.update({"config.get": _config_get, "config.patch": _config_patch, "config.set": _config_set})


def _agent_id_param(params: dict) -> str:
    # Callers are split: admin.py sends agent_id, the Paperclip pairing path sends agentId.
    return str(params.get("agent_id") or params.get("agentId") or "")


def _agents_list(_params: dict, **_kwargs) -> dict:
    agents = []
    for name in _profiles():
        config = sync.read_profile_config(name)
        agents.append({"id": name, "name": (config.get("agent") or {}).get("name") or name,
                       "model": (config.get("model") or {}).get("default") or "", "enabled": True})
    return {"ok": True, "payload": {"agents": agents}}


def _agents_update(params: dict, **_kwargs) -> dict:
    agent_id = _require_profile({"agentId": _agent_id_param(params)})
    patch = {k: v for k, v in params.items() if k not in ("agent_id", "agentId")}
    merged = sync.deep_merge(sync.read_profile_config(agent_id), {"agent": patch} if patch else {})
    sync.write_profile_config(agent_id, merged)
    return {"ok": True, "payload": {"agent": {"id": agent_id, **(merged.get("agent") or {})}}}


def _agents_delete(params: dict, **_kwargs) -> dict:
    agent_id = _require_profile({"agentId": _agent_id_param(params)})
    shutil.rmtree(sync.HOME / "profiles" / agent_id, ignore_errors=True)
    sync.delete_profile_objects(agent_id)   # otherwise the next restore resurrects the profile
    return {"ok": True, "payload": {"deleted": True}}


def _channels_status(params: dict, **_kwargs) -> dict:
    # The BRIDGE owns channel ingress on Hermes, and Slack/Discord enter through the backend,
    # so there is no per-container channel account to report. Empty is the true answer.
    return {"ok": True, "payload": {"channels": [], "probe": bool(params.get("probe"))}}


def _provider_catalog(provider: str) -> list[str]:
    """Hermes's own curated model list for one provider (``hermes_cli.models``, the same source
    its `/model` picker uses). Empty outside the Hermes image (dev/tests) or when the catalog
    cannot be built -- the picker then shows the profile's current model alone, which is worse
    than a full list but never wrong."""
    try:
        from hermes_cli.models import curated_models_for_provider

        return [mid for mid, _desc in curated_models_for_provider(provider) if isinstance(mid, str) and mid]
    except Exception:  # noqa: BLE001 -- a missing/failed catalog is a degraded picker, not an error
        log.warning("model catalog unavailable for provider %s", provider, exc_info=True)
        return []


def _models_list(params: dict, **_kwargs) -> dict:
    """The models this workspace can pick from (M6 task 10-C).

    A BEDROCK profile answers an EMPTY list on purpose: the only models the bedrock-gate admits
    are the owner's own application inference profiles, which live in DynamoDB, not here -- the
    backend serves that list itself and never forwards. So this is the BYO-key / ChatGPT-OAuth
    answer, and it is Hermes's own catalog for whatever provider the profile pins. ``name`` is
    the id: Hermes's catalog carries descriptions ("recommended", "free"), not display names,
    and inventing one here would be a second naming authority."""
    names = _profiles()
    agent_id = str(params.get("agentId") or "")
    if agent_id not in names:
        # Every profile on a workspace shares its provider (the backend writes them together),
        # so any one answers the owner-level question a no-agentId call is asking.
        agent_id = names[0] if names else ""
    if not agent_id:
        return {"ok": True, "payload": {"models": [], "defaultId": ""}}
    model, provider = turn.profile_model(sync.read_profile_config(agent_id))
    if provider == "bedrock":
        return {"ok": True, "payload": {"models": [], "defaultId": model}}
    return {"ok": True, "payload": {
        "models": [{"id": mid, "name": mid, "provider": provider} for mid in _provider_catalog(provider)],
        "defaultId": model,
    }}


def _dotenv_has(agent_id: str, key: str) -> bool:
    """Is `key` set to a non-empty value in the profile's own `.env`? Presence only -- the value
    is a credential and is never read out of this function.

    Deliberately NOT `agent.secret_scope.load_env_file` (review M3), which exists and would
    parse the same grammar: that one MATERIALIZES every value into a dict, and a credential
    this function never needs cannot leak from a boolean. The grammar it has to match is our
    own writer's, `hermes_profiles.render_dotenv` -- `KEY=value` / `KEY="escaped"`, no
    `export`, no interpolation."""
    path = sync.HOME / "profiles" / agent_id / ".env"
    if not path.is_file():
        return False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        name, sep, value = line.partition("=")
        if sep and name.strip() == key:
            return bool(value.strip().strip("'\""))
    return False


#: Hermes provider name -> (where the credential lives, how to check it). The provider names are
#: the ones `hermes_profiles.model_block` writes into `config.yaml` -- `bedrock`, `anthropic`,
#: `openai-api`, `openai-codex` -- and they are what the frontend's `llm-auth-status.ts` already
#: matches on (exactly, or by its `openai-` prefix rule), so no id is translated here.
def _provider_credential(agent_id: str, provider: str) -> tuple[bool, str]:
    if provider == "bedrock":
        return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK")), "bedrock-gate"
    if provider == "anthropic":
        return _dotenv_has(agent_id, "ANTHROPIC_API_KEY"), "byo-key"
    if provider == "openai-api":
        return _dotenv_has(agent_id, "OPENAI_API_KEY"), "byo-key"
    if provider == "openai-codex":
        # The grant is the ROOT home's auth.json, staged by hermes_profiles.stage_codex_auth and
        # pulled per turn. A settings read can arrive before this process has run any turn, so
        # pull it here too -- restore_root_auth is a cheap revision check after the first call.
        with contextlib.suppress(Exception):
            sync.restore_root_auth()
        return (sync.HOME / "auth.json").is_file(), "chatgpt-oauth"
    return False, "unknown"


def _models_auth_status(params: dict, **_kwargs) -> dict:
    """The owner's provider and whether its credential is present. The old answer was hardcoded
    `bedrock` + the gate bearer, so a BYO-key or ChatGPT-OAuth owner got a permanently wrong dot
    (scan row 22).

    ONE profile, not the roster (review I2). The provider is an OWNER-level choice: every
    profile's `model.provider` is derived by `hermes_profiles.model_block` from
    `(provider_choice, byo_provider, …)`, which has no per-agent input -- only the model ID
    differs (the CEO's premium tier). So a second read cannot produce a second answer, and
    looping the roster cost one `ensure_profile` per agent, which on a cold microVM is NOT the
    "five backend-owned files" the old comment claimed: `ensure_profile` falls through to
    `_download_profile`, a full prefix LIST plus a recency pull of nearly every object.
    """
    roster = _roster()
    agent_id = params.get("agentId") or (roster[0] if roster else "")
    provider = ""
    if agent_id:
        with contextlib.suppress(Exception):
            # One profile's worth. Still not free on a cold microVM -- see the docstring.
            sync.ensure_profile(agent_id)
        provider = ((sync.read_profile_config(agent_id).get("model") or {}).get("provider") or "").strip()
    if not provider:  # no profile readable yet -- the gate bearer is still the runtime's own truth
        ok = bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK"))
        return {"ok": True, "payload": {"providers": [
            {"provider": "bedrock", "status": "ok" if ok else "missing", "via": "bedrock-gate"}]}}
    ok, via = _provider_credential(agent_id, provider)
    return {"ok": True, "payload": {"providers": [
        {"provider": provider, "status": "ok" if ok else "missing", "via": via}]}}


RPC_METHODS.update({"agents.list": _agents_list, "agents.update": _agents_update,
                    "agents.delete": _agents_delete, "channels.status": _channels_status,
                    "models.list": _models_list, "models.authStatus": _models_auth_status})


# ---- Hermes-native skills (M6 task 10-E, rulings M6-R50 / M6-R54) ------------------------------
# Every read below goes through HERMES'S OWN loaders -- `tools.skills_tool._find_all_skills`,
# `hermes_cli.skills_hub` -- never a parser of our own, because the format (SKILL.md front
# matter) is Hermes's to change. Those imports only resolve inside the real image, so each entry
# point degrades to UNAVAILABLE outside it; that is also what lets this suite run without Hermes
# installed. Sessions are deliberately NOT here -- see UI_ONLY_METHODS (M6-R54).


def _unsupported(message: str) -> dict:
    return {"ok": False, "error": {"code": "UNSUPPORTED", "message": message}}


def _profile_scope(agent_id: str):
    """Restore the profile (once per process) and hold Hermes's HERMES_HOME override on it.

    Hermes resolves the home at CALL time (`get_hermes_home()` is a ContextVar), so every loader
    below has to run inside this scope or it reads the root home instead of the agent's profile.

    # ponytail: a cold microVM pays a full `restore_profile` for a settings read -- the same
    # download a turn pays, and once per process. Restoring just `skills/` (or just `state.db`)
    # would need a second partial-restore path in sync.py; add it only if a cold settings read
    # is measured over the 90 s ingress budget.
    """
    sync.restore_profile(agent_id)
    sync.ensure_profile(agent_id)
    return turn._hermes_home_scope(agent_id)


def _disabled_names(config: dict) -> set[str]:
    """One profile's STORED `skills.disabled`, normalized by Hermes's own
    `parse_config_string_list` -- `hermes config set` writes a list as a quoted JSON/Python
    literal STRING, and reading that as one skill literally named `["pdf","shopify"]` would
    show every skill enabled and let the next toggle wipe the real list (review I6.1).
    That helper is pure (no home scope, no session context), so it is safe to call here.

    Deliberately NOT `agent.skill_utils.get_disabled_skill_names`, whose extra behaviours are
    wrong for a list we hand back to be re-written: it unions `skills.platform_disabled[…]`
    (promoting a platform-scoped disable to a global one on the next write) and subtracts
    `ESSENTIAL_SKILLS` (silently deleting a stored name). Both would corrupt the round trip.
    Recorded in the task report as review I6.2/I6.3.
    """
    from agent.skill_utils import parse_config_string_list

    raw = (config.get("skills") or {}).get("disabled")
    return {v.strip() for v in parse_config_string_list(raw) if v.strip()}


def _config_yaml_from_s3(agent_id: str) -> dict:
    """One profile's `config.yaml`, read by KEY. Not `ensure_profile`, which falls through to
    `_download_profile` -- a full prefix LIST plus a recency pull of nearly every object (the
    cost review I2 removed from `models.authStatus`). This is one small GetObject.

    Falls back to whatever is on local disk when S3 is unreachable or the object is absent.
    """
    if not sync.S3_BUCKET:
        return sync.read_profile_config(agent_id)
    try:
        import yaml

        key = f"{sync.S3_PREFIX}/{agent_id}/{sync.CONFIG_NAME}"
        body = sync._s3().get_object(Bucket=sync.S3_BUCKET, Key=key)["Body"].read()
        return yaml.safe_load(body.decode("utf-8")) or {}
    except Exception:  # noqa: BLE001 -- a missing/unreadable profile must not empty the union
        log.debug("config.yaml read failed for %s", agent_id, exc_info=True)
        return sync.read_profile_config(agent_id)


def _roster_disabled_names(scanned_agent_id: str) -> list[str]:
    """The UNION of `skills.disabled` across every profile in the roster (ruling M6-R57).

    The panel is workspace-level and its patch fans out to every profile, replacing the list
    wholesale -- so whatever this returns IS the next write for all of them, and a name missing
    from it is not merely unchanged, it is DELETED. Reading one profile was wrong because
    profiles genuinely diverge: `agents.create` -> `hermes_profiles.write_agent_profile` ->
    `build_profile_config` emits a FRESH `skills` block with no `disabled` key and never reads
    the owner's current list, so a newly created agent has an empty one. `_roster()` sorts UUIDs,
    so that agent can land first -- and the next toggle would then re-enable every skill the
    owner had turned off, everywhere (review N2).

    A union can only ever over-disable, never under-disable, which is the safe direction for an
    action whose whole purpose is turning something off.
    """
    names = _disabled_names(sync.read_profile_config(scanned_agent_id))
    for agent_id in _roster():
        if agent_id != scanned_agent_id:
            names |= _disabled_names(_config_yaml_from_s3(agent_id))
    return sorted(names)


def _skills_status(params: dict, **_kwargs) -> dict:
    """Hermes's installed skills, in the shape the Skills panel already reads.

    The skill ROWS are scanned from one profile when the caller names none: the skills a
    workspace has are the image's `/opt/hermes-skills` external dir plus what the migration
    copied into each profile, so restoring all six to list them would buy nothing.

    The DISABLED list is the roster-wide union instead (`_roster_disabled_names`), because
    unlike the rows it is written back.
    """
    from agent.skill_utils import ESSENTIAL_SKILLS
    from tools.skills_tool import _find_all_skills

    roster = _roster()
    agent_id = params.get("agentId") or (roster[0] if roster else "")
    if not agent_id:
        return {"ok": True, "payload": {"skills": [], "disabledNames": []}}
    with _profile_scope(agent_id):
        found = _find_all_skills(skip_disabled=True)
        skills_dir = str(sync.HOME / "profiles" / agent_id / "skills")
    disabled = set(_roster_disabled_names(agent_id))
    empty = {"bins": [], "env": [], "config": [], "os": []}
    return {"ok": True, "payload": {
        "managedSkillsDir": skills_dir,
        # Reviews I3 + N2: the STORED names, unioned across the roster -- never "which rendered
        # rows are off", and never one profile's list. A disabled name whose skill is not on the
        # scanned profile's disk appears in no row, and a profile written by `agents.create`
        # carries no `disabled` key at all; either omission would ride the panel's wholesale
        # list replace straight into a silent RE-ENABLE on every profile. The panel patches
        # this list, not its own rows.
        "disabledNames": sorted(disabled),
        "skills": [{
            "name": s["name"], "description": s.get("description") or "",
            "skillKey": s["name"], "source": s.get("category") or "hermes",
            # Hermes has no per-skill dependency manifest: a skill is a SKILL.md, and anything
            # it shells out to is the image's business. So nothing is ever "missing" or
            # "unsupported" here -- the panel's install/dependency machinery stays empty.
            "always": s["name"] in ESSENTIAL_SKILLS,
            "disabled": s["name"] in disabled,
            "blockedByAllowlist": False, "eligible": True,
            "requirements": dict(empty), "missing": dict(empty), "install": [],
        } for s in found],
    }}


def _skills_update(_params: dict, **_kwargs) -> dict:
    """Enabling/disabling a skill is a `config.yaml` write, and `config.yaml` is backend-owned
    (`sync.EXCLUDE_GLOBS`): a write from here never reaches S3 and is reverted by the next
    `.revision` re-pull or cold start. Refusing is the honest answer -- the durable door is the
    backend's own `config.patch {skills:{disabled:[...]}}`, which the Skills panel now uses."""
    return _unsupported("Skills are enabled through config.patch skills.disabled, not here.")


def _skills_proposals_list(_params: dict, **_kwargs) -> dict:
    """Hermes's nearest equivalent to skill proposals is the staged-write approval queue, and it
    only exists when `skills.write_approval` is on. Every profile pins it OFF
    (`hermes_profiles.build_profile_config`), so the queue is structurally empty -- say so
    rather than leaving the panel to guess."""
    return {"ok": True, "payload": {"proposals": [], "reason": "write_approval_disabled"}}


RPC_METHODS.update({
    "skills.status": _skills_status, "skills.update": _skills_update,
    "skills.proposals.list": _skills_proposals_list,
})


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ping":
            return self._send(200, {"status": "HealthyBusy" if turn._busy else "Healthy",
                                    "time_of_last_update": int(time.time())})
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/invocations":
            return self._send(404, {"error": "not_found"})
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            env = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        code, obj = handle_plugin(env.get("path") or "", (env.get("method") or "POST").upper(),
                                  env.get("headers") or {}, env.get("body"))
        # The shape the OpenClaw contract server returns: the gateway's status and body inside.
        self._send(200, {"status": code, "headers": {"content-type": "application/json"}, "body": obj})

    def log_message(self, fmt, *args) -> None:  # quiet the per-request access log
        pass


def main() -> None:
    sync.HOME.mkdir(parents=True, exist_ok=True)
    (sync.HOME / "profiles").mkdir(exist_ok=True)
    log.info("bridge on 0.0.0.0:%d home=%s owner=%s", PORT, sync.HOME, OWNER)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
