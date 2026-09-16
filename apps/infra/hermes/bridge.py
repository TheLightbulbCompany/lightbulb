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
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="[bridge] %(message)s", stream=sys.stdout)
log = logging.getLogger("bridge")

PORT = int(os.environ.get("AGENTCORE_PORT", "8080"))
HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
OWNER = os.environ.get("ISOL8_OWNER_ID", "")
API = os.environ.get("ISOL8_API_URL", "").rstrip("/")
HOOK_TOKEN = os.environ.get("OPENCLAW_HOOK_TOKEN", "")
GATEWAY_TOKEN = os.environ.get("GATEWAY_TOKEN", "")  # what the backend sends as Bearer on ingress/rpc
MODEL = os.environ.get("ANTHROPIC_MODEL", "")
S3_BUCKET = os.environ.get("S3_BUCKET", "")
S3_PREFIX = os.environ.get("S3_PREFIX", f"hermes/{OWNER}").strip("/")

STARTED = time.time()
_busy = 0
_busy_lock = threading.Lock()
_restored: set[str] = set()
_runs: dict[str, str] = {}  # runId -> state, for replay detection within this process
_last_turn: dict[str, float] = {}


def _busy_add(n: int) -> None:
    global _busy
    with _busy_lock:
        _busy += n


# ---- S3 profile sync (the AWS reference's workspace_sync, cut down) -------------------------
def _s3():
    import boto3

    return boto3.client("s3")


def _profile_dir(agent_id: str) -> Path:
    return HOME / "profiles" / agent_id


def restore_profile(agent_id: str) -> float:
    """Download the agent's profile dir from S3 once per process. Returns seconds taken."""
    if agent_id in _restored or not S3_BUCKET:
        _profile_dir(agent_id).mkdir(parents=True, exist_ok=True)
        _restored.add(agent_id)
        return 0.0
    t0 = time.time()
    dest = _profile_dir(agent_id)
    dest.mkdir(parents=True, exist_ok=True)
    prefix = f"{S3_PREFIX}/{agent_id}/"
    s3 = _s3()
    n = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            rel = obj["Key"][len(prefix):]
            if not rel:
                continue
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            s3.download_file(S3_BUCKET, obj["Key"], str(target))
            n += 1
    _restored.add(agent_id)
    took = time.time() - t0
    log.info("restored profile %s: %d objects in %.1fs", agent_id, n, took)
    return took


def save_profile(agent_id: str) -> float:
    """Upload the agent's profile dir. SQLite files go through the backup API; WAL/SHM are skipped."""
    if not S3_BUCKET:
        return 0.0
    t0 = time.time()
    src = _profile_dir(agent_id)
    prefix = f"{S3_PREFIX}/{agent_id}/"
    s3 = _s3()
    n = 0
    for path in src.rglob("*"):
        if not path.is_file() or path.suffix in (".db-wal", ".db-shm") or path.name.endswith((".sqlite-wal", ".sqlite-shm")):
            continue
        rel = path.relative_to(src).as_posix()
        upload = path
        if path.suffix in (".db", ".sqlite"):
            snap = path.with_name(path.name + ".s3snap")
            with sqlite3.connect(path) as conn, sqlite3.connect(snap) as out:
                conn.backup(out)
            upload = snap
        s3.upload_file(str(upload), S3_BUCKET, prefix + rel)
        if upload is not path:
            upload.unlink(missing_ok=True)
        n += 1
    took = time.time() - t0
    log.info("saved profile %s: %d objects in %.1fs", agent_id, n, took)
    return took


# ---- backend callbacks (hook token) -----------------------------------------------------------
def _post_backend(path: str, body: dict) -> int:
    req = urllib.request.Request(
        f"{API}/api/v1/internal/messaging/{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {HOOK_TOKEN}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            return res.status
    except urllib.error.HTTPError as exc:  # type: ignore[attr-defined]
        log.warning("backend %s -> %s %s", path, exc.code, exc.read()[:200])
        return exc.code
    except Exception as exc:  # noqa: BLE001
        log.warning("backend %s failed: %s", path, exc)
        return 0


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---- the turn --------------------------------------------------------------------------------
def _history(body: dict, agent_id: str) -> list[dict]:
    """The room's recent messages as Hermes conversation history: this agent's own messages are
    the assistant, everything else is a user turn prefixed with the author's name."""
    names = {p["id"]: (p.get("displayName") or p.get("name") or p["id"]) for p in body.get("participants", [])}
    out = []
    for m in body.get("recentHistory", []):
        author = m.get("author") or {}
        text = (m.get("text") or "").strip()
        if not text:
            continue
        if author.get("kind") == "agent" and author.get("id") == agent_id:
            out.append({"role": "assistant", "content": text})
        else:
            out.append({"role": "user", "content": f"{names.get(author.get('id'), author.get('id', '?'))}: {text}"})
    return out


def run_turn(agent_id: str, body: dict) -> None:
    run = body.get("run") or {}
    run_id = run.get("runId")
    room = body.get("room") or {}
    msg = body.get("message") or {}
    session_key = f"agent:{agent_id}:lightbulb:{room.get('kind', 'channel')}:{room.get('id')}"
    _busy_add(1)
    outcome, error_kind, reply = "completed", None, ""
    usage = {"model": MODEL, "inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheWriteTokens": 0}
    t0 = time.time()
    done = threading.Event()

    def _heartbeat() -> None:  # the backend sweeper closes a run silent for 90 s
        while not done.wait(30):
            _post_backend(f"runs/{OWNER}/events", {"runId": run_id, "type": "run.heartbeat", "at": _now(), "sessionKey": session_key})

    try:
        restore_profile(agent_id)
        _post_backend(f"runs/{OWNER}/events", {"runId": run_id, "type": "run.started", "at": _now(), "sessionKey": session_key})
        threading.Thread(target=_heartbeat, name=f"hb-{run_id}", daemon=True).start()
        from hermes_constants import reset_hermes_home_override, set_hermes_home_override

        token = set_hermes_home_override(_profile_dir(agent_id))
        try:
            from run_agent import AIAgent

            sender = body.get("sender") or {}
            agent = AIAgent(
                model=MODEL,
                provider="bedrock",
                quiet_mode=True,
                session_id=f"{room.get('id')}:{agent_id}",
                platform="lightbulb",
                user_id=str(sender.get("id", "")),
                chat_id=str(room.get("id", "")),
                chat_type="group" if room.get("kind") != "dm" else "dm",
            )
            me = next((p for p in body.get("participants", []) if p.get("id") == agent_id), {})
            system = (
                f"You are {me.get('displayName') or me.get('name') or 'an agent'} in the Lightbulb room "
                f"#{room.get('name') or room.get('id')}. Reply as yourself; other participants are humans and agents."
            )
            result = agent.run_conversation(
                user_message=f"{(body.get('sender') or {}).get('id', '?')}: {msg.get('text', '')}",
                system_message=system,
                conversation_history=_history(body, agent_id) or None,
            )
            reply = (result or {}).get("final_response") or ""
            usage.update(
                inputTokens=int(getattr(agent, "session_input_tokens", 0) or 0),
                outputTokens=int(getattr(agent, "session_output_tokens", 0) or 0),
                cacheReadTokens=int(getattr(agent, "session_cache_read_tokens", 0) or 0),
                cacheWriteTokens=int(getattr(agent, "session_cache_write_tokens", 0) or 0),
            )
            if not (result or {}).get("api_calls"):  # Hermes answers a dead model with apologetic prose; never post it
                outcome, error_kind, reply = "error", "model_unavailable", ""
            elif not reply:
                outcome, error_kind = "error", "empty_response"
        finally:
            reset_hermes_home_override(token)
    except Exception as exc:  # noqa: BLE001
        outcome, error_kind = "error", type(exc).__name__
        log.error("turn %s failed: %s\n%s", run_id, exc, traceback.format_exc())
    done.set()
    took = time.time() - t0
    if reply:
        _post_backend(
            f"agent-messages/{OWNER}",
            {
                "eventId": f"{body.get('eventId')}:reply",
                "roomId": room.get("id"),
                "agentId": agent_id,
                "text": reply,
                "replyToMessageId": None,
                "threadRootMessageId": None,
                "runId": run_id,
            },
        )
    _post_backend(
        f"runs/{OWNER}/events",
        {"runId": run_id, "type": "run.ended", "at": _now(), "sessionKey": session_key, "outcome": outcome, "errorKind": error_kind, "usage": usage},
    )
    try:
        save_profile(agent_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("save_profile failed: %s", exc)
    _runs[run_id] = outcome
    _last_turn[agent_id] = took
    _busy_add(-1)
    log.info("turn %s agent=%s outcome=%s %.1fs reply=%r", run_id, agent_id, outcome, took, reply[:80])


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
        if run_id in _runs:
            return 200, {"eventId": body.get("eventId"), "runId": run_id, "status": "replayed"}
        _runs[run_id] = "running"
        threading.Thread(target=run_turn, args=(agent_id, body), daemon=True, name=f"turn-{run_id}").start()
        return 202, {"eventId": body.get("eventId"), "runId": run_id, "status": "accepted"}
    if path == "/plugins/lightbulb-channel/rpc" and method == "POST":
        m = (body or {}).get("method") if isinstance(body, dict) else None
        if m == "health":
            return 200, {"ok": True, "payload": {"ready": True, "runtime": "hermes", "uptimeS": round(time.time() - STARTED, 1), "busy": _busy}}
        if m == "agents.list":
            return 200, {"ok": True, "payload": {"agents": [{"id": p.name} for p in (HOME / "profiles").glob("*") if p.is_dir()]}}
        return 200, {"ok": False, "error": {"code": "UNAVAILABLE", "message": f"method not implemented in the spike bridge: {m}"}}
    return 404, {"error": "not_found", "path": path}


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
            return self._send(200, {"status": "HealthyBusy" if _busy else "Healthy", "time_of_last_update": int(time.time())})
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
            return self._send(200, {"uptime_s": round(time.time() - STARTED, 1), "busy": _busy, "restored": sorted(_restored), "runs": _runs, "last_turn_s": _last_turn, "cmd": out[-6000:]})
        code, obj = handle_plugin(env.get("path") or "", (env.get("method") or "POST").upper(), env.get("headers") or {}, env.get("body"))
        # Same shape the OpenClaw contract server returns: the gateway's status and body inside.
        self._send(200, {"status": code, "headers": {"content-type": "application/json"}, "body": obj})

    def log_message(self, fmt, *args) -> None:  # quiet the per-request access log
        pass


def main() -> None:
    HOME.mkdir(parents=True, exist_ok=True)
    (HOME / "profiles").mkdir(exist_ok=True)
    log.info("bridge on 0.0.0.0:%d home=%s owner=%s model=%s s3=%s/%s", PORT, HOME, OWNER, MODEL, S3_BUCKET, S3_PREFIX)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
