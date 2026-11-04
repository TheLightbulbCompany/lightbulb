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

#: The requestIds this process's agents answered with a reply (newest kept, for an assignment's turn).
_replied: deque[str] = deque(maxlen=256)

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

#: Extra event subscribers (the sweep and hold notices are called directly). Each gets the event dict.
on_event: list[Callable[[dict], None]] = []


class NetworkOff(RuntimeError):
    """The sidecar is not running in this process."""


class OpError(RuntimeError):
    """The engine answered ``ok:false``. Carries only its error code and reason, never a body."""

    def __init__(self, op: str, code: str, reason: str | None = None) -> None:
        super().__init__(f"{op}: {code}")
        self.code, self.reason = code, reason


_lock = threading.Lock()        # start/stop
_save_lock = threading.Lock()   # one snapshot+put at a time
_proc: subprocess.Popen | None = None
_token = ""
_etag: str | None = None        # the state.db ETag we hold; None = absent in S3
_sha: str | None = None
_next_try = 0.0
_off = False                    # a 412: off for the life of this process
_stopping = False
_ready = False                  # the socket is open and subscribed: callers may connect
_sessions: dict = {}
_save_timer: threading.Timer | None = None
_superseded: deque[float] = deque()
_skip_logged = False            # network.save_skipped_reconnect is logged once per process


def _key(name: str) -> str:
    return f"{bridge.OWNER}/network/{name}"


def _code(exc: Exception) -> str:
    return str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", ""))


def _spawn(target, name: str, *args) -> None:
    threading.Thread(target=target, args=args, name=name, daemon=True).start()


def _running() -> bool:
    proc = _proc  # one read: a concurrent _kill() may clear it
    # Not before its socket opens: a background start spawns the process first, and an RPC that
    # slipped in then got FileNotFoundError from connect (dev, 2026-10-24 13:26Z).
    return _ready and proc is not None and proc.poll() is None and not _stopping


def running() -> bool:
    """True while this process's sidecar runs."""
    return _running()


# ---- start -------------------------------------------------------------------------------------
def ensure_started(*, background: bool) -> bool:
    """True when the sidecar runs. A no-op without the member key or without sessions.json.
    One lock, never a second sidecar; a failed start waits RETRY_S before the next try."""
    if not bridge.NETWORK_KEY or _off:
        return False
    if _running():
        return True
    if background:
        _spawn(_ensure, "network-start")
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
        else:
            _spawn(sweep, "network-sweep")
        return started


def _get(key: str) -> dict | None:
    try:
        return sync._s3().get_object(Bucket=bridge.S3_BUCKET, Key=key)
    except Exception as exc:  # noqa: BLE001
        if sync._is_absent(exc):
            return None
        raise


def _fetch_sessions() -> tuple[bytes, dict] | None:
    got = _get(_key("sessions.json"))
    if got is None:
        return None
    raw = got["Body"].read()
    return raw, json.loads(raw)


def _enable_workers() -> None:
    """Assignments need ``network_workers = true`` in the member's flat config.toml. Only when the
    runtime sets LIGHTBULB_NETWORK_WORKERS=1; otherwise the file is left untouched."""
    if os.environ.get("LIGHTBULB_NETWORK_WORKERS") != "1":
        return
    path = NET_HOME / "config.toml"
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    cut = next((i for i, ln in enumerate(lines) if ln.strip().startswith("[")), len(lines))
    top = [ln for ln in lines[:cut] if ln.split("=")[0].strip() != "network_workers"]
    path.write_text("\n".join([*top, "network_workers = true", *lines[cut:]]) + "\n", encoding="utf-8")


def _start() -> bool:
    global _proc, _token, _etag, _sha, _sessions, _stopping, _ready
    _a2a_declared.clear()  # a fresh engine starts with only the file's sessions
    fetched = _fetch_sessions()
    if fetched is None:
        return False  # not connected yet (no log: the normal state of most workspaces)
    raw_sessions, _sessions = fetched
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
        _etag, _sha = state["ETag"], sync._sha(body)

    key_path = NET_HOME / "identity.key"
    fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(base64.b64decode(bridge.NETWORK_KEY))
    os.chmod(key_path, 0o600)

    _enable_workers()
    SOCKET.unlink(missing_ok=True)
    _token = secrets.token_urlsafe(32)
    _stopping = False
    _ready = False
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
    _spawn(_subscriber, "network-events", sub)
    _ready = True
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
    global _proc, _ready
    _ready = False
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
    # The line id is always ours, an integer (the engine's request id is a u64); op bodies never
    # carry an `id` (an ack names its record as `record`).
    return (json.dumps({**(body or {}), "token": _token, "id": secrets.randbits(53), "op": op}) + "\n").encode()


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


def ask(op: str, body: dict | None = None, timeout_s: float = 10.0) -> dict:
    """``call`` that returns the answer's ``result`` (``{id, ok, result}``) and raises OpError
    from the top-level ``error``/``reason`` on ``ok:false``."""
    answer = call(op, body, timeout_s)
    if not answer.get("ok"):
        raise OpError(op, str(answer.get("error") or "internal"), answer.get("reason"))
    if op == "send" and (body or {}).get("reply_to"):
        _replied.append(body["reply_to"])
    return answer.get("result") or {}


#: People whose ``a2a`` session this engine run was told about (cleared when a set replaces them).
_a2a_declared: set[str] = set()


def declare_a2a_session(person: str) -> None:
    """Declare ``{app: "a2a", person}`` to the engine once (the engine refuses ``app.send`` for an
    undeclared session). Same ``members``/``revoked_keys`` handling as ``reload_sessions``."""
    if person in _a2a_declared:
        return
    sessions = list(_sessions.get("sessions") or [])
    if not any(s.get("app") == "a2a" and s.get("person") == person for s in sessions):
        sessions.append({"app": "a2a", "person": person})
    params = {"sessions": sessions, "revoked_keys": _sessions.get("revoked_keys") or []}
    if "members" in _sessions:
        params["members"] = _sessions["members"]
    ask("cloud.sessions.set", params)
    _a2a_declared.add(person)


def reload_sessions() -> int:
    """Re-read ``sessions.json`` from S3 and declare it to the running sidecar (the backend's
    ``network.sessions`` RPC, after it rewrote the file). Returns the session count."""
    global _sessions
    fetched = _fetch_sessions()
    if fetched is None:
        raise NetworkOff("sessions.json is gone")
    raw, data = fetched
    params = {"sessions": data.get("sessions") or []}
    # The engine keeps its gate when `members` is absent, so pass it only when written: it
    # answers only computers whose sign-in proof names one of these people.
    if "members" in data:
        params["members"] = data["members"]
    # The engine replaces its revoked set on every set (absent is none), so a sign-out
    # (``network_login.revoke``) reaches a warm engine only if the reload carries it.
    params["revoked_keys"] = data.get("revoked_keys") or []
    ask("cloud.sessions.set", params)
    _sessions = data
    _a2a_declared.clear()  # the set replaced the engine's sessions, the a2a ones included
    SESSIONS_FILE.write_bytes(raw)
    return len(data.get("sessions") or [])


#: How long a join waits for the inviting admin's Allow (the engine's own wait is 600 s).
JOIN_WAIT_S = 660.0


#: Each once link this process joined with (its SHA-256, never the link): ``joining`` while the
#: join waits for Allow, ``joined`` after. A retried ``network.join`` (an API Gateway 504, a lost
#: answer) with the same link is that join, not a second one; a failed join can be tried again.
_joins: dict[str, str] = {}


def _link_id(ticket: str) -> str:
    return hashlib.sha256(ticket.encode()).hexdigest()


def join_known(ticket: str) -> bool:
    return _joins.get(_link_id(ticket)) in ("joining", "joined")


def join_in_background(ticket: str) -> None:
    """``team.join`` with an admin computer's once link, off the RPC thread: the join waits for
    that admin's Allow. Logs the outcome's state and reason, never the link."""
    link = _link_id(ticket)
    if _joins.get(link) in ("joining", "joined"):
        return
    _joins[link] = "joining"

    def _join() -> None:
        try:
            r = ask("team.join", {"ticket": ticket, "name": str(_sessions.get("workspace") or "")},
                    timeout_s=JOIN_WAIT_S)
            log.info("network.join state=%s reason=%s", r.get("state"), r.get("reason"))
            _joins[link] = "joined" if r.get("state") == "admitted" else "failed"
        except Exception as exc:  # noqa: BLE001 -- the admin's computer shows nothing; the log says why
            _joins[link] = "failed"
            log.warning("network.join failed (%s)", type(exc).__name__)

    _spawn(_join, "network-join")


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
    kind = event.get("event")
    if kind == "superseded":
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
    if kind in ("message", "lag"):  # off the reader thread: a peek must not stall the stream
        _spawn(sweep, "network-sweep")
    elif kind in ("held", "hold_closed"):
        _spawn(_hold_notice, "network-hold", event)
    for fn in list(on_event):
        try:
            fn(event)
        except Exception:  # noqa: BLE001 -- one bad subscriber must not stop the stream
            log.warning("network: on_event subscriber failed", exc_info=True)


# ---- inbound: network messages as local run-less turns ----------------------------------------
def sweep() -> None:
    """One ``inbox.peek``; each record not yet run in this process becomes one run-less turn.
    Runs at start, on ``message`` and on ``lag``. The message text goes only into the turn."""
    from bridge import turn  # turn is heavy; network is imported by server before it needs it
    try:
        peek = ask("inbox.peek", {})
    except Exception:  # noqa: BLE001 -- the next event or start sweeps again
        log.warning("network: inbox.peek failed", exc_info=True)
        return
    for group in peek.get("sessions") or []:
        session = group.get("session")
        agent_id = agent_for_session(session)
        for record in group.get("records") or []:
            if not agent_id or not record.get("id"):
                log.warning("network: a record for session %s has no agent or id; left unacked", session)
                continue
            if record.get("kind") == "cancel":
                _spawn(_cancel_record, f"network-cancel-{record['id']}", session, record)
                continue
            if not turn.reserve_turn():
                return  # a recycle hold: the rest stay unacked, and the next sweep runs them
            key = f"event:{record['id']}"
            if not turn.register_run(key):
                turn.release_turn()  # already run in this process
                continue
            _spawn(_run_record, f"network-turn-{record['id']}", session, agent_id, record, key)


def _ack(session: str, record: dict, stage: str) -> None:
    ask("inbox.ack", {"session": session, "record": record["id"], "stage": stage})


def _cancel_record(session: str, record: dict) -> None:
    """A cancel never takes a model turn: on the same thread it would queue behind the very turn it
    has to stop. It interrupts that turn and answers for it. A turn already over has its result,
    so nothing more is sent."""
    from bridge import turn
    target = record.get("reply_to")
    try:
        if target and turn.abort_run(f"event:{target}"):
            ask("send", {"from": session, "reply_to": target, "request_id": f"cancelled-{record['id']}",
                         "text": "Stopped: the assignment was cancelled.", "status": "cancelled"})
        _ack(session, record, "delivered")
        _ack(session, record, "read")
    except Exception:  # noqa: BLE001 -- stays unacked, so the next sweep cancels again
        log.warning("network: cancel %s failed", record["id"], exc_info=True)


def _send_assignment_result(session: str, record: dict, text: str, reason: str = "no_result",
                            failed_text: str = "The agent finished without a result.") -> None:
    """A model can end an assignment's turn in plain text, without the reply tool. The asker gets
    that text as the result anyway, rather than waiting out the deadline: this is not left to the
    model. A turn with no text at all reports ``failed``. Stays best-effort: the deadline still
    closes the assignment if this send fails."""
    request_id = record.get("request_id")
    if not request_id or request_id in _replied or record["id"] in _replied:
        return
    body = {"from": session, "reply_to": request_id, "request_id": f"result-{record['id']}",
            "text": text.strip() or failed_text,
            "status": "done" if text.strip() else "failed"}
    if not text.strip():
        body["reason"] = reason
    try:
        ask("send", body)
    except Exception:  # noqa: BLE001 -- the class only, never the text
        log.warning("network: result for %s not sent", record["id"], exc_info=True)


def _run_record(session: str, agent_id: str, record: dict, key: str) -> None:
    """Holds one reserved turn. Acks ``delivered`` at start and ``read`` only when the turn
    completed: a failed turn (run_turn reports it, it does not raise) stays unread, so the engine
    keeps its text and the next process runs it again."""
    from bridge import turn
    try:
        _ack(session, record, "delivered")
    except Exception:  # noqa: BLE001 -- not taken: let a later sweep try it again
        log.warning("network: delivered ack failed for %s", record["id"], exc_info=True)
        turn.discard_run(key)
        turn.release_turn()
        return
    message = {"text": record.get("framed") or "", "label": record.get("label")}
    message.update({k: record[k] for k in ("kind", "reply_to", "status", "done_when", "deadline_at") if k in record})
    body = {"version": 1, "eventKind": "network_message", "eventId": record["id"],
            "targetAgentId": agent_id,
            "sessionKey": record.get("thread") or record.get("from") or record["id"],
            "abortKey": key,
            "message": message}
    outcome = None
    try:
        outcome = turn.run_turn(agent_id, body, None, True)  # releases the reserved turn itself
    except Exception:  # noqa: BLE001 -- stays unread; the class only, never the text
        log.warning("network: turn for %s failed", record["id"], exc_info=True)
    finally:
        cancelled = turn.run_aborted(key)  # _cancel_record answered for it; it must not run again
        turn.finish_run(key, outcome or "error")
        if outcome == "completed" or cancelled:
            if record.get("kind") == "assign" and not cancelled:
                _send_assignment_result(session, record, body.get("finalReply") or "")
            try:
                _ack(session, record, "read")
            except Exception:  # noqa: BLE001
                log.warning("network: read ack failed for %s", record["id"], exc_info=True)
        elif record.get("kind") == "assign":
            # The asker is waiting and an assignment has a deadline: say so now, and do not run it again later.
            log.warning("network: assignment %s ended %s; reported failed", record["id"], outcome)
            _send_assignment_result(session, record, "", reason="turn_failed", failed_text="The agent could not run this assignment.")
            try:
                _ack(session, record, "read")
            except Exception:  # noqa: BLE001
                log.warning("network: read ack failed for %s", record["id"], exc_info=True)
        else:
            log.warning("network: turn for %s ended %s; left unread", record["id"], outcome)


def _hold_notice(event: dict) -> None:
    """Tell the backend a hold opened or closed: id, state, label and target only. The held
    message's ``body`` (and any preview) never leaves the bridge."""
    from bridge import turn
    try:
        hold = ask("hold.get", {"hold": event.get("id")})
    except Exception:  # noqa: BLE001
        log.warning("network: hold.get failed for %s", event.get("id"), exc_info=True)
        return
    turn._post_backend(f"network/holds/{bridge.OWNER}", {
        "holdId": hold.get("id") or event.get("id"),
        "state": "open" if event.get("event") == "held" else "closed",
        "label": hold.get("label"),
        "target": hold.get("target") or hold.get("target_session"),
    })


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


def _sessions_file_exists() -> bool:
    try:
        sync._s3().head_object(Bucket=bridge.S3_BUCKET, Key=_key("sessions.json"))
        return True
    except Exception as exc:  # noqa: BLE001
        if sync._is_absent(exc):
            return False
        raise


def _put_snapshot() -> None:
    """Upload SNAPSHOT if it changed, conditionally. A 412 turns the network off."""
    global _etag, _sha, _off, _skip_logged
    with _save_lock:
        if _off:
            return
        data = SNAPSHOT.read_bytes()
        digest = sync._sha(data)
        if digest == _sha:
            return
        if not _sessions_file_exists():
            # "Reconnect workspace" deletes <owner>/network/: a save racing it (a SIGTERM's) must
            # not resurrect the old key's state.db.
            if not _skip_logged:
                _skip_logged = True
                log.warning("network.save_skipped_reconnect")
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
            if _sessions_file_exists():
                _etag, _sha = resp["ETag"], digest
                return
            # Reconnect deleted the prefix between the check above and the put: take the write
            # back, so the old key's state.db is never restored under the new connection.
            sync._s3().delete_object(Bucket=bridge.S3_BUCKET, Key=_key("state.db"))
            _off = True
            log.warning("network.save_skipped_reconnect")
    _spawn(_halt, "network-conflict-stop")


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
