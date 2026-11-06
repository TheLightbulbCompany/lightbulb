"""Hermes-on-AgentCore bridge: authenticated ingress, control RPC, and native runs.

AgentCore speaks to this container on :8080 (GET /ping, POST /invocations). /invocations carries
the {path, method, headers, body} envelope the backend's runtime_transport already sends, and this
process answers the lightbulb-channel paths plus Paperclip's native run lifecycle:

  POST /plugins/lightbulb-channel/inbound/<agentId>   202 accepted, then the turn runs in a thread
  POST /plugins/lightbulb-channel/rpc                  health, models.*, skills.*, browser.*
  /internal/paperclip/v1/runs[/<id>[/stop]]            create, poll, and interrupt a real turn
  /internal/v1/runs[/<id>[/stop]]                      the same for a task's Worker turn (bridge/runs.py)

A turn = restore the agent's profile dir from S3 (once per process), run one Hermes AIAgent turn
under that profile's HERMES_HOME, post run.started/run.ended and the reply to the backend with the
hook token, save the profile dir back to S3. Nothing session-bound happens before the first
/invocations: AgentCore's warm pool runs this CMD on ~10 pooled instances per runtime version.

Spike scope. Not here: the local-session relay.
"""

from __future__ import annotations

import contextlib
import hmac
import json
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bridge
from bridge import GATEWAY_TOKEN, HOOK_TOKEN, OWNER, PORT, STARTED, now, task_mode
from bridge import sync, turn, browser_control, followups, mcp_compat, network, recycle, runs, subagents

logging.basicConfig(level=logging.INFO, format="[bridge] %(message)s", stream=sys.stdout)
log = logging.getLogger("bridge")


# ---- the two plugin paths ----------------------------------------------------------------------
def _authorized(headers: dict) -> bool:
    raw = headers.get("authorization") or headers.get("Authorization") or ""
    return bool(GATEWAY_TOKEN) and hmac.compare_digest(raw.strip().encode(), f"Bearer {GATEWAY_TOKEN}".encode())


def _header(headers: dict, name: str) -> str | None:
    name = name.lower()
    return next((v for k, v in headers.items() if k.lower() == name), None)


_RUN_CONTROL_RE = re.compile(r"^/plugins/lightbulb-channel/runs/([0-9a-f-]{36})/(abort|steer)$")
# Underscore included deliberately: it must not be NARROWER than the backend facade's own
# run-id charset (routers/messaging.py's _HERMES_RUN_ID_RE), or a run id the facade accepts
# 404s here -- and the facade reads a 404 as "the microVM lost this run", failing a run that
# was in fact never addressable.
_PAPERCLIP_RUN_RE = re.compile(r"^/internal/paperclip/v1/runs/([A-Za-z0-9_-]+)(/stop)?$")
_PAPERCLIP_SESSION_RE = re.compile(
    r"^paperclip:company:([A-Za-z0-9-]+):agent:([A-Za-z0-9-]+):(issue|run):([A-Za-z0-9-]+)$"
)


def _paperclip_envelope(body, *, path_run_id: str | None = None, require_request: bool = False):
    if not isinstance(body, dict) or not isinstance(body.get("identity"), dict):
        return None
    identity = body["identity"]
    required = ("runId", "companyId", "agentId", "sessionId", "paperclipApiUrl")
    if any(not isinstance(identity.get(key), str) or not identity[key] for key in required):
        return None
    if path_run_id is not None and identity["runId"] != path_run_id:
        return None
    match = _PAPERCLIP_SESSION_RE.fullmatch(identity["sessionId"])
    if not match or (match.group(1), match.group(2)) != (identity["companyId"], identity["agentId"]):
        return None
    scope_kind, scope_id = match.group(3), match.group(4)
    if scope_kind == "issue":
        if identity.get("issueId") != scope_id:
            return None
    elif scope_id != identity["runId"] or identity.get("issueId"):
        return None
    request = body.get("request")
    if require_request:
        if not isinstance(request, dict):
            return None
        if any(not isinstance(request.get(key), str) or not request[key] for key in ("input", "instructions")):
            return None
        if request.get("session_id") != identity["sessionId"]:
            return None
    return identity, request


def _handle_paperclip_run(path: str, method: str, headers: dict, body) -> tuple[int, dict] | None:
    if path == "/internal/paperclip/v1/runs" and method == "POST":
        parsed = _paperclip_envelope(body, require_request=True)
        if parsed is None:
            return 400, {"error": "invalid_paperclip_run"}
        if not turn.reserve_turn():
            return 503, {"error": "recycling"}
        identity, request = parsed
        registered = False
        try:
            registration = turn.register_paperclip_run(identity, request)
            if registration == "conflict":
                turn.release_turn()
                return 409, {"error": "idempotency_conflict"}
            registered = registration != "replayed"
            status = turn.paperclip_run_status(identity["runId"], identity)
            if registration == "replayed":
                turn.release_turn()
                return 200, status
            threading.Thread(
                target=turn.run_paperclip_turn,
                # The backend's local-session grant for this run (absent from an older backend),
                # as the room ingress takes it: it rides the identity copy into the turn context.
                args=(dict(identity, localSessionToken=_header(headers, "X-Lightbulb-Local-Session-Token")),
                      dict(request), True),
                daemon=True,
                name=f"paperclip-{identity['runId']}",
            ).start()
        except Exception:
            if registered:
                turn.discard_run(identity["runId"])
            turn.release_turn()
            raise
        return 202, status
    match = _PAPERCLIP_RUN_RE.fullmatch(path)
    if not match or method not in ("GET", "POST"):
        return None
    run_id, stop = match.group(1), bool(match.group(2))
    if (stop and method != "POST") or (not stop and method != "GET"):
        return None
    parsed = _paperclip_envelope(body, path_run_id=run_id)
    if parsed is None:
        return 400, {"error": "invalid_paperclip_identity"}
    identity, _request = parsed
    try:
        status = turn.paperclip_run_status(run_id, identity)
    except Exception:  # noqa: BLE001 -- the durable-record read failed (an S3 blip)
        # Never 404: the backend facade reads 404 as "the microVM lost this run" and fails it.
        # A 5xx makes the adapter log "status poll failed" and poll again.
        log.warning("paperclip run status lookup failed run=%s", run_id, exc_info=True)
        return 503, {"error": "run_status_unavailable"}
    if status is None:
        return 404, {"error": "run_not_found"}
    if not stop or status["status"] in ("completed", "failed", "cancelled"):
        return 200, status
    if not turn.abort_run(run_id):
        latest = turn.paperclip_run_status(run_id, identity)
        return (200, latest) if latest is not None else (404, {"error": "run_not_found"})
    return 202, {"run_id": run_id, "status": "stopping", "session_id": identity["sessionId"]}


_CALL_RUN_RE = re.compile(r"^/internal/calls/v1/runs/([A-Za-z0-9_-]+)/stop$")


MAX_VOICE_HISTORY = 50


def _call_envelope(body, *, path_run_id: str | None = None, require_request: bool = False):
    """A voice run (the only call envelope): identified by the agent's room session key, which
    serialises it with the room's turns, plus the call it belongs to, which names its transcript."""
    if not isinstance(body, dict) or not isinstance(body.get("identity"), dict):
        return None
    identity = body["identity"]
    if any(not isinstance(identity.get(k), str) or not identity[k] for k in ("runId", "companyId", "agentId", "roomId", "sessionId", "callId")):
        return None
    room_id = identity["roomId"]
    if identity.get("family") != "voice" or room_id != room_id.lower() or not turn._is_uuid(room_id):
        return None
    if not turn._is_uuid(identity["callId"]):
        return None
    if path_run_id is not None and identity["runId"] != path_run_id:
        return None
    if identity["sessionId"] != turn.lightbulb_session_key(identity["agentId"], room_id):
        return None
    request = body.get("request")
    if require_request:
        if not isinstance(request, dict) or request.get("session_id") != identity["sessionId"]:
            return None
        if any(not isinstance(request.get(k), str) or not request[k] for k in ("input", "instructions")):
            return None
        voice, frames = request.get("voice"), request.get("frames")
        if not isinstance(voice, dict) or voice.get("roomId") != room_id:
            return None
        # Frames go to the backend relay only (C2); there is no AppSync channel for a voice run.
        if not isinstance(frames, dict) or frames.get("sink") != "backend" or not isinstance(frames.get("endpoint"), str) or not frames["endpoint"]:
            return None
        history, participants = request.get("recentHistory", []), request.get("participants", [])
        if not isinstance(history, list) or len(history) > MAX_VOICE_HISTORY or not isinstance(participants, list):
            return None
        if not all(isinstance(m, dict) and isinstance(m.get("messageId"), str) for m in history):
            return None
        if not all(isinstance(p, dict) and isinstance(p.get("id"), str) for p in participants):
            return None
    return identity, request


def _handle_call_run(path: str, method: str, headers: dict, body) -> tuple[int, dict] | None:
    if path == "/internal/calls/v1/runs" and method == "POST":
        parsed = _call_envelope(body, require_request=True)
        if parsed is None:
            return 400, {"error": "invalid_call_run"}
        identity, request = parsed
        if not turn.reserve_turn():
            return 503, {"error": "recycling"}
        # The backend's local-session grant for this voice run, as the room ingress takes it; kept as
        # the run's current grant too (turn.verified_call_room_key reads it there, a steer lowers it).
        grant = _header(headers, "X-Lightbulb-Local-Session-Token")
        try:
            if not turn.register_call_run(identity, grant):
                turn.release_turn()
                return 200, {"run_id": identity["runId"], "status": "replayed"}
            threading.Thread(target=turn.run_call_turn,
                             args=(dict(identity, localSessionToken=grant), dict(request), True),
                             daemon=True, name=f"call-{identity['runId']}").start()
        except Exception:
            turn.discard_run(identity["runId"])
            turn.release_turn()
            raise
        return 202, {"run_id": identity["runId"], "status": "queued"}
    match = _CALL_RUN_RE.fullmatch(path)
    if not match or method != "POST":
        return None
    parsed = _call_envelope(body, path_run_id=match.group(1))
    if parsed is None:
        return 400, {"error": "invalid_call_identity"}
    if not turn.abort_run(match.group(1)):
        return 404, {"error": "run_not_found"}
    return 202, {"run_id": match.group(1), "status": "stopping"}


def handle_plugin(path: str, method: str, headers: dict, body) -> tuple[int, dict]:
    if not _authorized(headers):
        return 401, {"error": "unauthorized"}
    # A task VM (spec 02 §3.3) runs its task's runs and answers RPC reads, nothing else: no room
    # turn, no Paperclip run, no call -- each would restore and write the specialist profile.
    task_run = path.startswith("/internal/v1/runs")
    if task_mode.active() and not task_run and path != "/plugins/lightbulb-channel/rpc":
        return task_mode.Refused.reply
    # A real, authorized invoke: this microVM is claimed, so the network member may start here
    # (never on a warm-pool instance). No-op without a member key or sessions.json. A task
    # session never starts one: the workspace member belongs to the workspace VM.
    identity = body.get("identity") if isinstance(body, dict) else None
    session = identity.get("session") if isinstance(identity, dict) else None
    wants_task = task_run and isinstance(session, dict) and session.get("kind") == "task"
    if not (task_mode.active() or wants_task):
        network.ensure_started(background=True)
    # The website-login vault token rides every backend invoke (runtime_transport._vault_header);
    # memory only. See bridge/__init__.py's VAULT_TOKEN.
    vault = _header(headers, "X-Lightbulb-Vault")
    if vault:
        bridge.VAULT_TOKEN = vault
    call = _handle_call_run(path, method, headers, body)
    if call is not None:
        return call
    paperclip = _handle_paperclip_run(path, method, headers, body)
    if paperclip is not None:
        return paperclip
    if path == "/internal/v1/curate" and method == "POST":  # lane V5b; a task VM was refused above
        agent_id = body.get("agentId") if isinstance(body, dict) else None
        if not isinstance(agent_id, str) or agent_id not in _roster():  # as profile.warm
            return 400, {"error": "invalid_request"}
        return turn.curate_agent(agent_id)
    task_run = runs.handle(path, method, headers, body)
    if task_run is not None:
        return task_run
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
        # A voice steer by another speaker carries a lowered local-session grant for the call turn.
        if turn.steer_run(run_id, text.strip(), grant=_header(headers, "X-Lightbulb-Local-Session-Token")):
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
        # An old backend may still send X-Lightbulb-Provider-Generation; the header is ignored --
        # the generation-stale/switch-frozen gates were retired (Task 11, the backend no longer
        # tracks a provider-switch generation at all). Only a pending session stop refuses.
        if not turn.reserve_turn():
            return 503, {"error": "recycling"}
        # agent_mail (Task 20) and paperclip_run (Task 21) carry no `run` block at all -- the
        # registry/replay/run.started steps below are for a room turn's run lifecycle, which
        # run-less ingresses have none of; run_turn's own has_run=False path handles the rest.
        registered_key = None
        try:
            run_id = (body.get("run") or {}).get("runId")
            event_id = body.get("eventId")
            if run_id:
                if not turn.register_run(run_id):
                    turn.release_turn()
                    return 200, {"eventId": event_id, "runId": run_id, "status": "replayed"}
                registered_key = run_id
                turn.mark_started(run_id)
                turn._post_backend(f"runs/{OWNER}/events", {
                    "runId": run_id, "type": "run.started", "at": now(),
                    "sessionKey": turn._resolve_session_key(agent_id, body),
                })
            elif event_id:
                # Run-less ingresses dedupe by eventId in the same registry.
                event_key = f"event:{event_id}"
                if not turn.register_run(event_key):
                    turn.release_turn()
                    return 200, {"eventId": event_id, "runId": run_id, "status": "replayed"}
                registered_key = event_key
                turn.finish_run(event_key, "dispatched")
            local_session_token = _header(headers, "X-Lightbulb-Local-Session-Token")
            threading.Thread(target=turn.run_turn, args=(agent_id, body, local_session_token, True),
                             daemon=True, name=f"turn-{run_id or body.get('eventId')}").start()
        except Exception:
            if registered_key:
                turn.discard_run(registered_key)
            turn.release_turn()
            raise
        return 202, {"eventId": body.get("eventId"), "runId": run_id, "status": "accepted"}
    if path == "/plugins/lightbulb-channel/rpc" and method == "POST":
        return _rpc(body)
    return 404, {"error": "not_found", "path": path}


# ---- RPC (spec §5) ------------------------------------------------------------------------------
RPC_DEFAULT_TIMEOUT_MS = 30_000
RPC_MAX_TIMEOUT_MS = 120_000

# Control-panel surfaces this runtime does not serve (models/skills panels, sessions, doctor
# memory, browser.request) are simply NOT REGISTERED in RPC_METHODS: `_rpc`'s `handler is None`
# branch answers them with `_unavailable(method)`, the same {ok:false, code:"UNAVAILABLE"} shape
# container_rpc unwraps into a RuntimeError. The SHAPE is the contract; it must never wobble.


def _profiles() -> list[str]:
    return sorted(p.name for p in (sync.HOME / "profiles").glob("*") if p.is_dir())


#: (expires_at, ids) for _roster's S3 listing. A settings panel can land on a microVM that has
#: never run a turn, where `profiles/` is EMPTY -- answering "no agents" from that would be an
#: empty answer that reads as fact. One delimited LIST of the owner's prefix is the cheap truth;
#: cached because `health` polls every 60 s.
_ROSTER_TTL_S = 60.0
_roster_cache: tuple[float, tuple[str, ...]] = (0.0, ())


def _roster() -> list[str]:
    """Every agent id this owner has: the local profile dirs UNION the S3 prefix.

    A listing blip must not empty a panel, so the fallback is the local view. Nothing served
    from here is ever written BACK -- the one read that is (`skills.disabled`) is answered by
    the backend, which owns every profile's config.yaml -- so a short answer degrades a display
    and cannot corrupt a write. That is why there is no "is this AUTHORITATIVE" flag any more.
    """
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


def _health(params: dict, **_kwargs) -> dict:
    # container_rpc.call only raises when the gateway answers {ok:false} (or the transport
    # itself fails) -- it never inspects the payload -- and readiness_state's
    # _gateway_reachable treats "no exception" as ready. So a not-ready bridge MUST answer
    # ok:false here, or a booting container reads as ready fleet-wide.
    if not _boot_ready():
        return {"ok": False, "error": {"code": "NOT_READY", "message": "ingress cannot be accepted yet"}}
    # No session count (ruling M6-R54): it would have been one sqlite read per locally-restored
    # profile on the READINESS path, and with the Sessions panel hidden on AgentCore there is
    # nobody left to read it.
    busy = turn._busy
    if (params or {}).get("drain"):
        # A roll about to stop this session: idle -> hold new turns (TTL'd) and answer busy 0, so
        # no turn can start between this answer and the stop; busy -> no hold, busy >= 1.
        busy = 0 if turn.hold_turns() else max(busy, 1)
    outer_mb, inner_mb = sync.disk_free_mb()  # outer = what actually runs out (sync.disk_free_mb)
    return {"ok": True, "payload": {
        "diskFreeMb": outer_mb, "diskFreeInnerMb": inner_mb,
        "ready": True, "runtime": "hermes",
        "uptimeS": round(time.time() - STARTED, 1), "busy": busy,
        "agents": len(_roster()),
        "browserProtocol": 2 if os.environ.get("AGENTCORE_PROFILE_ID") else 1,
        "memoryProtocol": 1 if os.environ.get("AGENTCORE_MEMORY_ID") else 0,
        # The cloud browser save merges three-way against its own base (browser_state), so a
        # paired Mac's sign-ins may be written back without this runtime overwriting them.
        "browserStateMerge": 1,
    }}


RPC_METHODS: dict = {"health": _health}
RPC_METHODS.update(subagents.RPC_METHODS)


def _browser_state(params: dict, **_kwargs) -> dict:
    """Checkpoint through the isolated state helper; never return its URL or exception text. A
    ``doorUrl`` is the backend's own Mac sync for a shared session (browser_control.mac_checkpoint)."""
    try:
        if (params or {}).get("doorUrl"):
            return {"ok": True, "payload": browser_control.mac_checkpoint(params)}
        from bridge import browser_state
        return {"ok": True, "payload": browser_state.checkpoint(params)}
    except Exception:
        return {"ok": False, "error": {"code": "UNAVAILABLE", "message": "browser checkpoint failed"}}


RPC_METHODS["browser.state"] = _browser_state


def _browser_frames(params: dict, **_kwargs) -> dict:
    """A live-frame watcher's heartbeat (backend, while an agent browses); with a session's `runId`,
    that session's Browser tab only."""
    params = params or {}
    ttl = float(browser_control.FRAMES_TTL_S if params.get("ttl") is None else params["ttl"])  # 0 stops a cast
    if params.get("runId"):
        return {"ok": True, "payload": {"runs": browser_control.session_frames(str(params["runId"]), ttl)}}
    return {"ok": True, "payload": {"runs": browser_control.frames_wanted(ttl)}}


def _browser_input(params: dict, **_kwargs) -> dict:
    """A watcher's click or keys on a run's live frames."""
    try:
        browser_control.dispatch_input(params or {})
        return {"ok": True, "payload": {"ok": True}}
    except LookupError:
        return {"ok": False, "error": {"code": "NOT_CASTING", "message": "that tab is not being shown"}}
    except Exception as exc:  # noqa: BLE001 -- the class only: CDP errors can echo page content
        return {"ok": False, "error": {"code": "UNAVAILABLE", "message": f"browser input failed ({type(exc).__name__})"}}


def _browser_attach(params: dict, **_kwargs) -> dict:
    """A shared session's CDP onto the cloud browser (browser_control.attach_session)."""
    try:
        return {"ok": True, "payload": browser_control.attach_session(params or {})}
    except ValueError:
        return {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "invalid browser attach"}}


RPC_METHODS["browser.frames"] = _browser_frames
RPC_METHODS["browser.input"] = _browser_input
RPC_METHODS["browser.attach"] = _browser_attach


def _unavailable(method: str) -> dict:
    return {"ok": False, "error": {"code": "UNAVAILABLE",
                                   "message": f"{method} is not available on the Hermes runtime"}}


# ---- network.* (the backend's calls onto the workspace's network member) ----------------------
# Each one starts the sidecar in the foreground first: handle_plugin only starts it in the
# background, so the first RPC of a cold microVM would otherwise find it off.
def _net(method: str, op: str, body: dict | None = None, run=None) -> dict:
    """``run`` (no arguments) replaces the one engine op when a method does more than ask."""
    if task_mode.active():  # the workspace member belongs to the workspace VM: never started here
        return {"ok": False, "error": {"code": "task_mode", "message": f"{method} is not available on a task VM"}}
    if not network.ensure_started(background=False):
        return _unavailable(method)
    try:
        return {"ok": True, "payload": run() if run else network.ask(op, body)}
    except network.NetworkOff:
        return _unavailable(method)
    except network.OpError as exc:
        error = {"code": exc.code, "message": f"{method} was refused ({exc.code})"}
        if exc.reason:
            error["reason"] = exc.reason
        return {"ok": False, "error": error}


def _pick(params: dict, *keys: str) -> dict:
    return {k: params[k] for k in keys if params.get(k) is not None}


def _network_join(params: dict, **_kwargs) -> dict:
    """An admin ran ``lightbulb invite --agents``: join the network their computer's once ask link
    is for. Answers at once (``{state: "joining"}``); the join itself runs in the background (an
    invite is the approval: nobody is asked). A workspace already on a network is ``already_in_team`` (the first
    network wins). The link is a credential: nothing here logs it."""
    ticket = params.get("ticket")
    if not isinstance(ticket, str) or not ticket:
        return {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "ticket is required"}}

    def run() -> dict:
        if network.join_known(ticket):  # a retry of the join already under way (or done)
            return {"state": "joining"}
        if len(network.ask("team.get", {}).get("members") or []) > 1:
            raise network.OpError("team.get", "already_in_team")
        network.join_in_background(ticket)
        return {"state": "joining"}

    return _net("network.join", "", run=run)


def _network_invite(params: dict, **_kwargs) -> dict:
    """A once link to this workspace's network for a person's computer (the backend's
    ``/network/join-line``). The ticket is a bearer credential: it is returned, never logged, and
    ``_net`` puts only the op's code in an error."""
    def run() -> dict:
        team = network.ask("team.get", {})  # first: a failed read must not burn a link
        got = network.ask("team.invite", {"mode": "once", "code": False})
        return {"ticket": got.get("ticket"), "team_id": team.get("team_id"), "expires_at": got.get("expires_at")}

    return _net("network.invite", "", run=run)


def _network_team(params: dict, **_kwargs) -> dict:
    """Which network the member is in and how many are on it (the backend's team-id backfill)."""
    def run() -> dict:
        team = network.ask("team.get", {})
        return {"team_id": team.get("team_id"), "member_count": len(team.get("members") or [])}

    return _net("network.team", "", run=run)


def _network_hold(params: dict, **_kwargs) -> dict:
    return _net("network.hold", "hold.answer", _pick(params, "hold", "verdict", "scope", "decided_by"))


def _network_leave(params: dict, **_kwargs) -> dict:
    """An admin takes the workspace off its network (``team.leave``): back to its own solo team,
    so ``invite --agents`` can add it to another."""
    return _net("network.leave", "team.leave", {})


def _network_sessions(params: dict, **_kwargs) -> dict:
    return _net("network.sessions", "cloud.sessions.set", run=lambda: {"count": network.reload_sessions()})


def _network_a2a_send(params: dict, timeout_ms=RPC_DEFAULT_TIMEOUT_MS, **_kwargs) -> dict:
    """A hosted A2A send: declare the person's ``a2a`` session, then ``app.send`` with the A2A
    message. ``a2a`` carries a JWS in its metadata: nothing here logs it."""
    body = {k: params.get(k) for k in ("to", "text", "request_id", "person")}
    a2a = params.get("a2a")
    if not all(isinstance(v, str) and v for v in body.values()) or not isinstance(a2a, dict):
        return {"ok": False, "error": {"code": "INVALID_REQUEST",
                                       "message": "to, text, request_id, person and a2a are required"}}

    def run() -> dict:
        network.declare_a2a_session(body["person"])
        return network.ask("app.send", {"app": "a2a", "person": body["person"], "to": body["to"],
                                        "text": body["text"], "request_id": body["request_id"],
                                        "a2a": a2a, "wait": True}, timeout_ms / 1000)

    return _net("network.a2a_send", "", run=run)


def _network_wake(params: dict, **_kwargs) -> dict:
    """A peer rang the doorbell: start the member, nothing else (spec job 3)."""
    return _net("network.wake", "", run=dict)


RPC_METHODS.update({"network.join": _network_join, "network.sessions": _network_sessions,
                    "network.hold": _network_hold,
                    "network.invite": _network_invite, "network.team": _network_team,
                    "network.leave": _network_leave,
                    "network.wake": _network_wake, "network.a2a_send": _network_a2a_send})


# The backend's memory sync, remember and wipes (core/services/memory_*.py) onto the cloud member:
# each one is the engine op of the same name, its params passed through unchanged (they carry fact
# text: like every params here, never logged).
_MEMORY_OPS = ("memory.import", "memory.list", "memory.remember", "memory.forget", "memory.forget_author")


def _memory_op(method: str):
    # The backend's own timeout (memory.import asks 60 s), not the 10 s engine default.
    return lambda params, timeout_ms=RPC_DEFAULT_TIMEOUT_MS, **_kwargs: _net(
        method, "", run=lambda: network.ask(method, params, timeout_ms / 1000))


RPC_METHODS.update({m: _memory_op(m) for m in _MEMORY_OPS})


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

    Hermes's own catalog for whatever provider the profile pins. ``name`` is
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
#: the ones `hermes_profiles.model_block` writes into `config.yaml` -- `custom` (the managed
#: gate), `anthropic`, `openai-api`, `openai-codex` -- and they are what the frontend's `llm-auth-status.ts` already
#: matches on (exactly, or by its `openai-` prefix rule), so no id is translated here.
def _provider_credential(agent_id: str, provider: str) -> tuple[bool, str]:
    if provider == "custom" and sync.is_managed_route(sync.read_profile_config(agent_id)):
        return bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK")), "lightbulb-gate"
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
        try:
            blob = json.loads((sync.HOME / "auth.json").read_text(encoding="utf-8"))
            state = (blob.get("providers") or {}).get("openai-codex") or {}
            tokens = state.get("tokens") or {}
            present = isinstance(tokens, dict) and any(tokens.get(k) for k in ("access_token", "refresh_token"))
            if not present and state.get("custody") == "backend":
                # The backend holds the grant (spec 13): a vend fills the local file, and its
                # `model` is the proof the grant is live.
                from bridge import credentials

                vended = credentials.vend(agent_id)
                present = vended is not None and vended.model is not None
        except (OSError, ValueError, AttributeError):
            present = False
        return present, "chatgpt-oauth"
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
    if not provider:  # no profile readable yet: no provider to report (the panel shows neutral)
        return {"ok": True, "payload": {"providers": []}}
    ok, via = _provider_credential(agent_id, provider)
    return {"ok": True, "payload": {"providers": [
        {"provider": provider, "status": "ok" if ok else "missing", "via": via}]}}


RPC_METHODS.update({"models.list": _models_list, "models.authStatus": _models_auth_status})


# ---- Hermes-native skills (M6 task 10-E, rulings M6-R50 / M6-R54) ------------------------------
# Every read below goes through HERMES'S OWN loaders -- `tools.skills_tool._find_all_skills`,
# `hermes_cli.skills_hub` -- never a parser of our own, because the format (SKILL.md front
# matter) is Hermes's to change. Those imports only resolve inside the real image, so each entry
# point degrades to UNAVAILABLE outside it; that is also what lets this suite run without Hermes
# installed. Sessions are deliberately NOT here: nothing registers them, so `_rpc` answers them
# UNAVAILABLE on its own (M6-R54).


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


def _stored_disabled_names(config: dict) -> set[str]:
    """One profile's STORED `skills.disabled`, for the per-row `disabled` flag only.

    Normalized by Hermes's own `parse_config_string_list` -- `hermes config set` writes a list
    as a quoted literal STRING, and reading that as one skill literally named
    `["pdf","shopify"]` would render every row enabled. That helper is pure (no home scope, no
    session context), so it is safe to call here.

    Deliberately NOT `agent.skill_utils.get_disabled_skill_names`: it unions
    `skills.platform_disabled[...]` and subtracts `ESSENTIAL_SKILLS`, which are display
    behaviours we do not want mixed into a stored-state read.
    """
    from agent.skill_utils import parse_config_string_list

    raw = (config.get("skills") or {}).get("disabled")
    return {v.strip() for v in parse_config_string_list(raw) if v.strip()}


def _skills_status(params: dict, **_kwargs) -> dict:
    """Hermes's installed skills, in the shape the Skills panel already reads.

    ROWS ONLY. The skills a workspace has are the image's `/opt/hermes-skills` external dir
    plus what the migration copied into each profile, so they are scanned from one profile --
    restoring all six to list them would buy nothing.

    `disabledNames` is NOT answered here. That list is written straight back (the panel's patch
    replaces `skills.disabled` on every profile wholesale, so a name missing from it is
    DELETED), and answering it needed the roster-wide union of a file the backend OWNS --
    every profile's config.yaml, which never leaves the backend for S3 (`sync.EXCLUDE_GLOBS`).
    The backend serves it in `runtime_rpc.agentcore_disabled_skill_names`, where a throttled
    read raises instead of yielding a plausibly short list. Each row still carries its own
    `disabled` flag, which is a display fact and safe from here.
    """
    from agent.skill_utils import ESSENTIAL_SKILLS
    from tools.skills_tool import _find_all_skills

    roster = _roster()
    agent_id = params.get("agentId") or (roster[0] if roster else "")
    if not agent_id:
        return {"ok": True, "payload": {"skills": []}}
    with _profile_scope(agent_id):
        found = _find_all_skills(skip_disabled=True)
        skills_dir = str(sync.HOME / "profiles" / agent_id / "skills")
    disabled = _stored_disabled_names(sync.read_profile_config(agent_id))
    empty = {"bins": [], "env": [], "config": [], "os": []}
    payload = {
        "managedSkillsDir": skills_dir,
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
    }
    return {"ok": True, "payload": payload}


RPC_METHODS["skills.status"] = _skills_status


def _profile_warm(params: dict, **_kwargs) -> dict:
    """Pull one agent's profile from S3 now, off the request thread, so the turn that follows
    (a Live summon, typically) finds it restored and the agent's first build done (turn.warm_agent).
    The backend fires this for every agent in the room when someone goes Live; a second call for a
    restored agent is a no-op.

    With a ``roomId`` the agent's session for that room is then compacted if it is near Hermes's
    threshold (turn.compact_session): a spoken turn never compacts, so this is where it happens
    before the first question. An image that predates the parameter ignores it."""
    agent_id = params.get("agentId")
    if not isinstance(agent_id, str) or agent_id not in _roster():
        return {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "agentId must name an agent"}}
    room_id = params.get("roomId")

    def warm() -> None:
        try:
            sync.restore_profile(agent_id)
            sync.ensure_profile(agent_id)
        except Exception:  # noqa: BLE001 -- the next turn restores it the ordinary way
            log.warning("profile.warm failed for %s", agent_id, exc_info=True)
            return
        # Counted as a turn: a roll or the recycle waits for it, and none starts under their hold.
        if turn.reserve_turn():
            try:
                turn.RESTORED.add(agent_id)  # the SIGTERM save covers what the compaction wrote
                # The agent's first build (plugins, MCP), so the first spoken turn starts thinking at once.
                turn.warm_agent(agent_id)
                if isinstance(room_id, str):
                    turn.compact_session(agent_id, turn.lightbulb_session_key(agent_id, room_id))
            finally:
                turn.release_turn()

    threading.Thread(target=warm, name=f"profile-warm-{agent_id[:8]}", daemon=True).start()
    return {"ok": True, "payload": {"warming": True}}


RPC_METHODS["profile.warm"] = _profile_warm


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
            # No `time_of_last_update`: AWS's long-running-agent guide says a timestamp that
            # advances on every ping reads as a continuous status change, so the idle timeout
            # never fires and every session ran to maxLifetime (prod 2026-09-23..25). Omitted,
            # the platform tracks Healthy/HealthyBusy transitions itself.
            return self._send(200, {"status": _ping_status()})
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path == "/browser/invoke":  # the screen driver (cua_driver.py): one InvokeBrowser action
            if self.client_address[0] not in ("127.0.0.1", "::1"):
                return self._send(403, {"error": "loopback_only"})
            try:
                action = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                return self._send(200, browser_control.invoke(action))
            except Exception as exc:  # noqa: BLE001 -- the driver reports it to the model as a tool error
                return self._send(503, {"error": f"{type(exc).__name__}: {exc}"})
        if self.path != "/invocations":
            return self._send(404, {"error": "not_found"})
        recycle.note_invoke()
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


def _ping_status() -> str:
    """Busy while a turn (including its post-turn wait and save) runs, a delegation is live, or a
    delegation's completion is still owed to the backend -- AgentCore parks only an idle session."""
    return "HealthyBusy" if turn._busy or followups.busy() else "Healthy"


#: AWS gives a stopped session at most 15 s between SIGTERM and SIGKILL (AgentCore runtime
#: lifecycle docs), so a 20 s budget was never real: the kill landed mid-upload. 10 s leaves the
#: save a margin to finish and os._exit cleanly inside AWS's bound.
SIGTERM_SAVE_S = 10.0


def _save_on_sigterm(signum, _frame) -> None:
    """AgentCore stops a session with SIGTERM. Push every profile this process restored (and the
    root auth.json) before dying, bounded, so a stop between turns -- or mid-turn -- keeps what
    the agent wrote. Inert on a warm-pool microVM: nothing restored, nothing to save."""
    def _save() -> None:
        for agent_id in sorted(turn.RESTORED):
            try:
                sync.save_profile(agent_id)
                sync.reseal_attempt(agent_id)  # a task VM stopped after its seal: the save above may have moved a sealed file
            except Exception:  # noqa: BLE001 -- one bad profile must not cost the others
                log.warning("sigterm: save_profile %s failed", agent_id, exc_info=True)
        try:
            sync.save_root_auth()
        except Exception:  # noqa: BLE001
            log.warning("sigterm: save_root_auth failed", exc_info=True)

    log.info("sigterm: saving %d profile(s)", len(turn.RESTORED))
    deadline = time.monotonic() + SIGTERM_SAVE_S
    worker = threading.Thread(target=_save, name="sigterm-save", daemon=True)
    # Beside the profile saves, never after them: a slow save must not eat the network's stop.
    net = threading.Thread(target=network.stop, name="sigterm-network", daemon=True)
    worker.start()
    net.start()
    worker.join(SIGTERM_SAVE_S)
    net.join(max(0.0, deadline - time.monotonic()))
    os._exit(128 + signum)


def main() -> None:
    # Installed here, not on the first turn: Python only lets the MAIN thread install a signal
    # handler, and turns run on worker threads. The handler itself is session-inert until a
    # profile is restored, so the warm-pool rule still holds.
    signal.signal(signal.SIGTERM, _save_on_sigterm)
    sync.HOME.mkdir(parents=True, exist_ok=True)
    (sync.HOME / "profiles").mkdir(exist_ok=True)
    # No turn is running yet: whatever is here is a crashed turn's leftovers or the old
    # shared per-room cache (media/messaging/attachments/) -- neither may outlive its turn.
    shutil.rmtree(sync.turn_media_root(), ignore_errors=True)
    mcp_compat.install()
    turn.install_router_tool_search()
    browser_control.install()
    log.info("bridge on 0.0.0.0:%d home=%s owner=%s", PORT, sync.HOME, OWNER)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
