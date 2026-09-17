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
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from bridge import GATEWAY_TOKEN, HOME, OWNER, PORT, STARTED, now
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
        if not isinstance(body, dict) or body.get("targetAgentId") != agent_id:
            return 400, {"error": "invalid_ingress", "message": "targetAgentId does not match the path"}
        run_id = (body.get("run") or {}).get("runId")
        if run_id in turn._runs:
            return 200, {"eventId": body.get("eventId"), "runId": run_id, "status": "replayed"}
        turn._runs[run_id] = "running"
        threading.Thread(target=turn.run_turn, args=(agent_id, body), daemon=True, name=f"turn-{run_id}").start()
        return 202, {"eventId": body.get("eventId"), "runId": run_id, "status": "accepted"}
    if path == "/plugins/lightbulb-channel/rpc" and method == "POST":
        m = (body or {}).get("method") if isinstance(body, dict) else None
        if m == "health":
            return 200, {"ok": True, "payload": {"ready": True, "runtime": "hermes", "uptimeS": round(time.time() - STARTED, 1), "busy": turn._busy}}
        if m == "agents.list":
            return 200, {"ok": True, "payload": {"agents": [{"id": p.name} for p in (HOME / "profiles").glob("*") if p.is_dir()]}}
        return 200, {"ok": False, "error": {"code": "UNAVAILABLE", "message": f"method not implemented in the spike bridge: {m}"}}
    return 404, {"error": "not_found", "path": path}


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
            return self._send(200, {"uptime_s": round(time.time() - STARTED, 1), "busy": turn._busy, "restored": sorted(sync._restored), "runs": turn._runs, "last_turn_s": turn._last_turn, "ws_seen": _ws_seen[-3:], "cmd": out[-6000:]})
        code, obj = handle_plugin(env.get("path") or "", (env.get("method") or "POST").upper(), env.get("headers") or {}, env.get("body"))
        # Same shape the OpenClaw contract server returns: the gateway's status and body inside.
        self._send(200, {"status": code, "headers": {"content-type": "application/json"}, "body": obj})

    def log_message(self, fmt, *args) -> None:  # quiet the per-request access log
        pass


def main() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "profiles").mkdir(exist_ok=True)
    log.info("bridge on 0.0.0.0:%d home=%s owner=%s", PORT, HOME, OWNER)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
