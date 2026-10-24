"""The workspace's network member: start, restore, save and stop the ``lightbulb member`` sidecar.

The sidecar is the engine (``/usr/local/bin/lightbulb``) running as this workspace's member. It
speaks one JSON object per line on a Unix socket; every line carries a per-boot control token that
lives only in this module's memory (never ``os.environ``, never a file), so an agent's shell
cannot drive the socket.

Start trigger: only ``server.handle_plugin``, on a real ``/invocations``. A warm-pool microVM gets
none until it is claimed, and one microVM per owner is claimed, so two never run one key.

Persistence: only ``<owner>/network/state.db`` goes to S3 -- a ``snapshot`` (``VACUUM INTO``)
copy, written 1 s after any event or op, skipped when its SHA-256 is unchanged, and always
conditional (``IfMatch`` the ETag we hold, or ``IfNoneMatch: *`` when there was none). A 412 means
another writer: the sidecar stops and the network stays off for this process. ``identity.key``
(the member key from the boot secret) is written locally, mode 0600, and never uploaded.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import socket
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import bridge
from bridge import sync

log = logging.getLogger("bridge")

LIGHTBULB = "/usr/local/bin/lightbulb"
NET_HOME = Path("/opt/data/network")
SNAPSHOT = Path("/opt/data/network-out/state.db")
SOCKET = Path("/opt/data/network.sock")
SESSIONS_FILE = Path("/opt/data/network-out/sessions.json")

RETRY_S = 60.0
SAVE_DEBOUNCE_S = 1.0
READY_S = 10.0
CONTESTED_WINDOW_S = 600.0
CONTESTED_COUNT = 4  # "more than 3 superseded in 10 min" (spec §9)

#: Event subscribers, filled by Task 6 (sweep, holds). Each gets the event dict.
on_event: list[Callable[[dict], None]] = []


class NetworkOff(RuntimeError):
    """The sidecar is not running in this process."""


_lock = threading.Lock()        # start/stop
_save_lock = threading.Lock()   # one snapshot+put at a time
_proc: subprocess.Popen | None = None
_token = ""
_etag: str | None = None        # the state.db ETag we hold; None = absent in S3
_sha: str | None = None
_next_try = 0.0
_off = False                    # a 412: off for the life of this process
_stopping = False
_sessions: dict = {}
_save_timer: threading.Timer | None = None
_superseded: deque[float] = deque()


def _key(name: str) -> str:
    return f"{bridge.OWNER}/network/{name}"


def _code(exc: Exception) -> str:
    return str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", ""))


def _running() -> bool:
    proc = _proc  # one read: a concurrent _kill() may clear it
    return proc is not None and proc.poll() is None and not _stopping


# ---- start -------------------------------------------------------------------------------------
def ensure_started(*, background: bool) -> bool:
    """True when the sidecar runs. A no-op without the member key or without sessions.json.
    One lock, never a second sidecar; a failed start waits RETRY_S before the next try."""
    if not bridge.NETWORK_KEY or _off:
        return False
    if _running():
        return True
    if background:
        threading.Thread(target=_ensure, name="network-start", daemon=True).start()
        return False
    return _ensure()


def _ensure() -> bool:
    global _next_try
    with _lock:
        if _off or not bridge.NETWORK_KEY:
            return False
        if _running():
            return True
        if time.monotonic() < _next_try:
            return False
        try:
            started = _start()
        except Exception:  # noqa: BLE001 -- the network must never cost the invoke
            log.warning("network.start_failed", exc_info=True)
            _kill()
            started = False
        if not started:
            _next_try = time.monotonic() + RETRY_S
        return started


def _get(key: str) -> dict | None:
    try:
        return sync._s3().get_object(Bucket=bridge.S3_BUCKET, Key=key)
    except Exception as exc:  # noqa: BLE001
        if _code(exc) in ("NoSuchKey", "404"):
            return None
        raise


def _start() -> bool:
    global _proc, _token, _etag, _sha, _sessions, _stopping
    got = _get(_key("sessions.json"))
    if got is None:
        return False  # not connected yet (no log: the normal state of most workspaces)
    raw_sessions = got["Body"].read()
    _sessions = json.loads(raw_sessions)
    for path in (NET_HOME, SNAPSHOT.parent, SESSIONS_FILE.parent):
        path.mkdir(parents=True, exist_ok=True)
    SESSIONS_FILE.write_bytes(raw_sessions)

    # A crashed sidecar can leave a hot journal; SQLite would roll it onto the restored file.
    for leftover in ("state.db-journal", "state.db-wal", "state.db-shm"):
        (NET_HOME / leftover).unlink(missing_ok=True)
    state = _get(_key("state.db"))
    if state is None:
        _etag, _sha = None, None
        (NET_HOME / "state.db").unlink(missing_ok=True)
    else:
        body = state["Body"].read()
        (NET_HOME / "state.db").write_bytes(body)
        _etag, _sha = state["ETag"], hashlib.sha256(body).hexdigest()

    key_path = NET_HOME / "identity.key"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64decode(bridge.NETWORK_KEY))
    os.chmod(key_path, 0o600)

    SOCKET.unlink(missing_ok=True)
    _token = secrets.token_urlsafe(32)
    _stopping = False
    _proc = subprocess.Popen(
        [LIGHTBULB, "member", "--home", str(NET_HOME), "--snapshot", str(SNAPSHOT),
         "--socket", str(SOCKET), "--name", str(_sessions.get("workspace") or ""),
         "--sessions", str(SESSIONS_FILE)],
        stdin=subprocess.PIPE,
    )
    _proc.stdin.write((_token + "\n").encode())
    _proc.stdin.close()

    deadline = time.monotonic() + READY_S
    while not _connectable():
        if _proc.poll() is not None:
            raise RuntimeError(f"lightbulb member exited {_proc.returncode} before its socket opened")
        if time.monotonic() > deadline:
            raise RuntimeError("lightbulb member socket did not open")
        time.sleep(0.05)
    sub = _connect(None)
    sub.sendall(_line("subscribe", None))
    threading.Thread(target=_subscriber, args=(sub,), name="network-events", daemon=True).start()
    log.info("network: member started (%d sessions)", len(_sessions.get("sessions") or []))
    return True


def _connectable() -> bool:
    if not SOCKET.exists():
        return False
    try:
        _connect(1.0).close()
        return True
    except OSError:
        return False


def _kill() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.kill()
        _proc.wait()
    _proc = None


# ---- the socket --------------------------------------------------------------------------------
def _connect(timeout_s: float | None) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout_s)
    sock.connect(str(SOCKET))
    return sock


def _line(op: str, body: dict | None) -> bytes:
    return (json.dumps({**(body or {}), "token": _token, "id": secrets.token_hex(8), "op": op}) + "\n").encode()


def _call(op: str, body: dict | None, timeout_s: float) -> dict:
    with _connect(timeout_s) as sock:
        sock.sendall(_line(op, body))
        with sock.makefile("rb") as fh:
            raw = fh.readline()
    if not raw:
        raise NetworkOff(f"{op}: the sidecar closed the socket")
    return json.loads(raw)


def call(op: str, body: dict | None = None, timeout_s: float = 10.0) -> dict:
    """One socket line with the token, one answer. Raises NetworkOff when the sidecar is not up."""
    if not _running():
        raise NetworkOff(op)
    answer = _call(op, body, timeout_s)
    if op != "snapshot":
        # ponytail: every op schedules a save; a read-only op costs one snapshot, no put (same sha).
        _schedule_save()
    return answer


def sessions() -> list[dict]:
    """The sessions file as read at start."""
    return list(_sessions.get("sessions") or [])


def session_for_agent(agent_id: str) -> str | None:
    return next((s.get("name") for s in sessions() if s.get("agentId") == agent_id), None)


def agent_for_session(name: str) -> str | None:
    return next((s.get("agentId") for s in sessions() if s.get("name") == name), None)


def _subscriber(sock: socket.socket) -> None:
    with sock, sock.makefile("rb") as fh:
        for raw in fh:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            _handle_event(event)
    if not _stopping:
        log.warning("network: event stream ended (sidecar exit %s)", _proc and _proc.poll())


def _handle_event(event: dict) -> None:
    if event.get("event") == "superseded":
        now = time.monotonic()
        _superseded.append(now)
        while _superseded and now - _superseded[0] > CONTESTED_WINDOW_S:
            _superseded.popleft()
        if len(_superseded) >= CONTESTED_COUNT:
            _superseded.clear()
            log.warning("network: member key contested (%d superseded in %.0fs)",
                        CONTESTED_COUNT, CONTESTED_WINDOW_S)
            sync.emit_metrics({"network.member_key_contested": (1, "Count")})
    _schedule_save()
    for fn in list(on_event):
        try:
            fn(event)
        except Exception:  # noqa: BLE001 -- one bad subscriber must not stop the stream
            log.warning("network: on_event subscriber failed", exc_info=True)


# ---- save --------------------------------------------------------------------------------------
def _schedule_save() -> None:
    global _save_timer
    with _save_lock:
        if _save_timer is not None or not _running():
            return
        _save_timer = threading.Timer(SAVE_DEBOUNCE_S, _debounced_save)
        _save_timer.daemon = True
        _save_timer.start()


def _debounced_save() -> None:
    global _save_timer
    with _save_lock:
        _save_timer = None
    if not _running():
        return
    try:
        _call("snapshot", {"path": str(SNAPSHOT)}, 10.0)
        _put_snapshot()
    except Exception:  # noqa: BLE001 -- the next event saves again
        log.warning("network: save failed", exc_info=True)


def _put_snapshot() -> None:
    """Upload SNAPSHOT if it changed, conditionally. A 412 turns the network off."""
    global _etag, _sha, _off
    with _save_lock:
        if _off:
            return
        data = SNAPSHOT.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest == _sha:
            return
        cond = {"IfMatch": _etag} if _etag else {"IfNoneMatch": "*"}
        try:
            resp = sync._s3().put_object(Bucket=bridge.S3_BUCKET, Key=_key("state.db"), Body=data, **cond)
        except Exception as exc:  # noqa: BLE001
            if _code(exc) not in ("PreconditionFailed", "412"):
                raise
            _off = True
            log.warning("network: state.db changed under us (412); the network is off for this process")
            sync.emit_metrics({"network.state_conflict": (1, "Count")})
        else:
            _etag, _sha = resp["ETag"], digest
            return
    threading.Thread(target=_halt, name="network-conflict-stop", daemon=True).start()


# ---- stop --------------------------------------------------------------------------------------
def _halt() -> None:
    """Stop the sidecar without saving (the 412 path)."""
    global _stopping
    with _lock:
        _stopping = True
        _kill()


def stop(deadline_s: float = 6.0) -> None:
    """Ask the sidecar to stop, wait <= deadline_s for its final snapshot, then put it. On a timeout
    the sidecar is killed and the last debounced save stands."""
    global _stopping, _save_timer
    end = time.monotonic() + deadline_s
    if not _lock.acquire(timeout=deadline_s):
        return
    try:
        if _proc is None or _proc.poll() is not None:
            return
        _stopping = True
        with _save_lock:
            if _save_timer is not None:
                _save_timer.cancel()
                _save_timer = None
        try:
            _call("stop", None, max(0.1, end - time.monotonic()))
        except Exception:  # noqa: BLE001 -- a dead socket: the sidecar treats SIGTERM the same
            _proc.terminate()
        try:
            code = _proc.wait(max(0.0, end - time.monotonic()))
        except subprocess.TimeoutExpired:
            log.warning("network: sidecar did not stop in %.1fs; killed", deadline_s)
            _kill()
            return
        if code == 0 and SNAPSHOT.is_file():
            _put_snapshot()
    except Exception:  # noqa: BLE001 -- the stop runs inside SIGTERM's budget
        log.warning("network: stop failed", exc_info=True)
    finally:
        _lock.release()
