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

Spike scope. Not here: steer/abort, heartbeats, usage, attachments, the local-session relay.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
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


def handle_plugin(path: str, method: str, headers: dict, body) -> tuple[int, dict]:
    if not _authorized(headers):
        return 401, {"error": "unauthorized"}
    if path.startswith("/plugins/lightbulb-channel/inbound/") and method == "POST":
        agent_id = path.rsplit("/", 1)[-1]
        try:
            turn.parse_ingress(body if isinstance(body, dict) else {})
            if body.get("targetAgentId") != agent_id:
                raise turn.IngressError("targetAgentId does not match the path")
        except turn.IngressError as exc:
            return 400, {"error": "invalid_ingress", "message": exc.message}
        run_id = body["run"]["runId"]
        if not turn.register_run(run_id):
            return 200, {"eventId": body.get("eventId"), "runId": run_id, "status": "replayed"}
        turn.RUNS[run_id]["started"] = True
        turn._post_backend(f"runs/{OWNER}/events", {
            "runId": run_id, "type": "run.started", "at": now(),
            "sessionKey": turn._resolve_session_key(agent_id, body),
        })
        threading.Thread(target=turn.run_turn, args=(agent_id, body), daemon=True, name=f"turn-{run_id}").start()
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
    "tools.catalog", "commands.list", "models.list", "chat.history", "agent.identity.get",
    "skills.install", "skills.search", "skills.status", "skills.update",
    "skills.proposals.apply", "skills.proposals.inspect", "skills.proposals.list",
    "skills.proposals.reject",
    "sessions.list", "sessions.patch", "sessions.delete", "sessions.rewind", "sessions.search",
    "sessions.branches.list", "sessions.branches.switch",
    "doctor.memory.status", "doctor.memory.dreamDiary", "doctor.memory.resetDreamDiary",
    "doctor.memory.backfillDreamDiary", "doctor.memory.dedupeDreamDiary",
    "doctor.memory.repairDreamingArtifacts", "doctor.memory.resetGroundedShortTerm",
    # browser.request is retired with the container browser (spec §5).
    "browser.request",
})


def _profiles() -> list[str]:
    return sorted(p.name for p in (sync.HOME / "profiles").glob("*") if p.is_dir())


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
    return {"ok": True, "payload": {
        "ready": True, "runtime": "hermes",
        "uptimeS": round(time.time() - STARTED, 1), "busy": turn._busy,
        "agents": len(_profiles()),
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


def _models_auth_status(_params: dict, **_kwargs) -> dict:
    ok = bool(os.environ.get("AWS_BEARER_TOKEN_BEDROCK"))
    return {"ok": True, "payload": {"providers": [
        {"provider": "bedrock", "status": "ok" if ok else "missing", "via": "bedrock-gate"}]}}


RPC_METHODS.update({"agents.list": _agents_list, "agents.update": _agents_update,
                    "agents.delete": _agents_delete, "channels.status": _channels_status,
                    "models.authStatus": _models_auth_status})


# ---- /ws door probe (throwaway: M2 replaces this echo with the node protocol) ----------------
_ws_seen: list[dict] = []


def _ws_accept(key: str) -> str:
    import base64, hashlib  # noqa: E401

    return base64.b64encode(hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()).decode()


def _ws_read_frame(rfile):
    import struct

    head = rfile.read(2)
    if len(head) < 2:
        return None, b""
    op, masked, ln = head[0] & 0x0F, head[1] & 0x80, head[1] & 0x7F
    if ln == 126:
        ln = struct.unpack(">H", rfile.read(2))[0]
    elif ln == 127:
        ln = struct.unpack(">Q", rfile.read(8))[0]
    mask = rfile.read(4) if masked else b""
    data = rfile.read(ln)
    if masked:
        data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
    return op, data


def _ws_send(wfile, data: bytes, op: int = 1) -> None:
    import struct

    n = len(data)
    head = bytes([0x80 | op]) + (bytes([n]) if n < 126 else bytes([126]) + struct.pack(">H", n) if n < 65536 else bytes([127]) + struct.pack(">Q", n))
    wfile.write(head + data)
    wfile.flush()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _send(self, code: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _ws_echo(self) -> None:
        self.send_response(101)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", _ws_accept(self.headers.get("Sec-WebSocket-Key") or ""))
        self.end_headers()
        seen = {k: v for k, v in self.headers.items() if k.lower().startswith(("x-amzn", "sec-websocket", "x-lightbulb"))}
        _ws_seen.append({"at": now(), "path": self.path, "headers": seen})
        log.info("ws open %s headers=%s", self.path, seen)
        while True:
            op, data = _ws_read_frame(self.rfile)
            if op is None or op == 8:
                break
            if op == 9:
                _ws_send(self.wfile, data, 10)  # ping -> pong
            elif op in (1, 2):
                _ws_send(self.wfile, b'{"echo":' + data + b"}" if op == 1 else data, op)
        log.info("ws closed %s", self.path)
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ping":
            return self._send(200, {"status": "HealthyBusy" if turn._busy else "Healthy", "time_of_last_update": int(time.time())})
        if self.path.split("?")[0] == "/ws" and (self.headers.get("Upgrade") or "").lower() == "websocket":
            return self._ws_echo()
        self._send(404, {"error": "not_found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/invocations":
            return self._send(404, {"error": "not_found"})
        raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            env = json.loads(raw or b"{}")
        except ValueError:
            return self._send(400, {"error": "body is not JSON"})
        if env.get("diagnostic"):  # bring-up escape hatch, deleted after the spike
            cmd = env.get("cmd")
            out = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60).stdout if cmd else ""
            return self._send(200, {"uptime_s": round(time.time() - STARTED, 1), "busy": turn._busy, "restored": sorted(sync._restored), "runs": {rid: r["state"] for rid, r in turn.RUNS.items()}, "last_turn_s": turn._last_turn, "ws_seen": _ws_seen[-3:], "cmd": out[-6000:]})
        code, obj = handle_plugin(env.get("path") or "", (env.get("method") or "POST").upper(), env.get("headers") or {}, env.get("body"))
        # Same shape the OpenClaw contract server returns: the gateway's status and body inside.
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
