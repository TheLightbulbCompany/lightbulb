"""Background subagents: relay Hermes's `subagent.*` progress events to the backend, which
publishes them on the room's current live channel, and serve the subagent RPCs.

A child keeps its parent turn's `tool_progress_callback` for its whole life, so its events keep
arriving through FramePublisher.activity after that turn's run has closed. They cannot go out on
the run's frame channel: the room's deliveryKey may have rotated since. The backend looks up the
current key.
"""
from __future__ import annotations

import logging
import threading
import time

import bridge
from bridge import turn

log = logging.getLogger("bridge")

FLUSH_S = 1.0
MAX_BATCH = 25
# Exactly the types the backend's SubagentEventsRequest accepts: one unknown type would 422 its whole batch.
RELAYED = frozenset({"subagent.start", "subagent.thinking", "subagent.tool", "subagent.progress", "subagent.complete"})
# subagent id -> the room session key whose turn started it: the only authority the subagent RPCs
# accept. A child's events only ever arrive through its own parent turn's callback.
OWNER_OF: dict[str, str] = {}
_pending: dict[str, list[dict]] = {}
_lock = threading.Lock()
_seq = time.time_ns() // 1000
_started = False


def _not_none(v):
    return v is not None


def _str_list(v):
    return [str(x) for x in v] if isinstance(v, (list, tuple)) else None


def _int_or_skip(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _float_or_skip(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# Port of Hermes v2026.9.14 tui_gateway/tool_progress.py `_SUBAGENT_FIELDS`, in wire order.
_FIELDS = (
    ("subagent_id", bool, str), ("parent_id", bool, str), ("child_session_id", bool, str),
    ("delegation_id", bool, str), ("depth", _not_none, _int_or_skip), ("model", bool, str), ("tool_count", _not_none, int),
    ("toolsets", bool, _str_list), ("input_tokens", _not_none, _int_or_skip), ("output_tokens", _not_none, _int_or_skip),
    ("reasoning_tokens", _not_none, _int_or_skip), ("api_calls", _not_none, _int_or_skip),
    ("files_read", bool, _str_list), ("files_written", bool, _str_list), ("output_tail", bool, list),
    ("tool_name", bool, str), ("text", bool, str), ("status", bool, str), ("summary", bool, str),
    ("duration_seconds", _not_none, _float_or_skip),
)


def payload(event_type: str, name, preview, kw: dict) -> dict:
    """Port of upstream `_progress_subagent`'s payload build: the exact shape the desktop app reads."""
    count, index = _int_or_skip(kw.get("task_count") or 1), _int_or_skip(kw.get("task_index") or 0)
    out = {"goal": str(kw.get("goal") or ""), "task_count": 1 if count is None else count,
           "task_index": 0 if index is None else index}
    source = {**kw, "tool_name": name, "text": preview}
    for key, present, coerce in _FIELDS:
        if present(source.get(key)):
            val = coerce(source[key])
            if val is not None:
                out[key] = val
    if preview and event_type == "subagent.tool":
        out["tool_preview"] = str(preview)
        out["text"] = str(preview)
    return out


def relay(session_key: str | None, event_type: str, name, preview, kw: dict) -> None:
    # subagent.text (the child's per-token reply; upstream drops it too) and anything unknown is dropped.
    if not session_key or event_type not in RELAYED:
        return
    try:
        p = payload(event_type, name, preview, kw)
    except Exception:  # noqa: BLE001 -- the relay must never raise into the agent's callback
        log.warning("subagent event dropped: bad payload", exc_info=True)
        return
    sid = p.get("subagent_id")
    global _seq
    with _lock:
        _seq += 1
        if sid:
            if event_type == "subagent.complete":
                OWNER_OF.pop(sid, None)
            else:
                OWNER_OF[sid] = session_key
        queue = _pending.setdefault(session_key, [])
        if event_type == "subagent.thinking" and sid:
            queue[:] = [e for e in queue
                        if not (e["type"] == event_type and e["payload"].get("subagent_id") == sid)]
        queue.append({"type": event_type, "seq": _seq, "payload": p})
    _ensure_started()


def flush_once() -> None:
    with _lock:
        batches = list(_pending.items())
        _pending.clear()
    for session_key, events in batches:
        for i in range(0, len(events), MAX_BATCH):
            status = turn._post_backend(f"subagents/{bridge.OWNER}/events",
                                        {"sessionKey": session_key, "events": events[i:i + MAX_BATCH]})
            if status != 200:
                # ponytail: dropped, like a frame; the 5 s snapshot heals the client.
                log.warning("subagent events POST answered %s; dropped %d", status, len(events[i:i + MAX_BATCH]))


def _loop() -> None:
    while True:
        time.sleep(FLUSH_S)
        try:
            flush_once()
        except Exception:  # noqa: BLE001 -- the relay must outlive any one bad pass
            log.warning("subagent relay pass failed", exc_info=True)


def _ensure_started() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
    threading.Thread(target=_loop, name="subagent-relay", daemon=True).start()


# ---- RPCs: ports of Hermes v2026.9.14 tui_gateway/methods_subagents.py ------------------------
TAIL_BYTES = 16384
SNAPSHOT_FIELDS = ("subagent_id", "parent_id", "depth", "goal", "delegation_id", "model",
                   "started_at", "status", "tool_count", "last_tool", "accepting_steer")


def _invalid(message: str) -> dict:
    return {"ok": False, "error": {"code": "INVALID", "message": message}}


def _registry():
    """Hermes's live-child registry, only if a turn already imported it: an RPC must not import
    Hermes (and open a state.db) on a warm-pool microVM. No turn in this process means no children."""
    import sys

    return sys.modules.get("tools.delegate_tool_registry")


def _owner(subagent_id: str) -> str | None:
    with _lock:
        return OWNER_OF.get(subagent_id)


def _owned(reg, session_key: str, subagent_id: str) -> dict | None:
    if reg is None or _owner(subagent_id) != session_key:
        return None
    with reg._active_subagents_lock:
        record = reg._active_subagents.get(subagent_id)
        return dict(record) if record else None


def _ids(params: dict) -> tuple[str, str] | None:
    key, sid = params.get("sessionKey"), params.get("subagentId")
    return (key, sid) if isinstance(key, str) and key and isinstance(sid, str) and sid else None


def rpc_list(params: dict, **_kw) -> dict:
    keys = params.get("sessionKeys")
    if not isinstance(keys, list) or not keys or not all(isinstance(k, str) for k in keys):
        return _invalid("sessionKeys must be a non-empty list of strings")
    reg, wanted = _registry(), set(keys)
    if reg is None:
        return {"ok": True, "payload": {"subagents": []}}
    with reg._active_subagents_lock:
        records = [dict(r) for r in reg._active_subagents.values()]
    with _lock:
        owners = dict(OWNER_OF)
    rows = [{**{k: r.get(k) for k in SNAPSHOT_FIELDS}, "session_key": owners[str(r["subagent_id"])]}
            for r in records if owners.get(str(r.get("subagent_id"))) in wanted]
    return {"ok": True, "payload": {"subagents": rows}}


def rpc_tail(params: dict, **_kw) -> dict:
    ids = _ids(params)
    if ids is None:
        return _invalid("sessionKey and subagentId are required")
    result = {"subagent_id": ids[1], "available": False, "text": "", "truncated": False}
    record = _owned(_registry(), *ids)
    path = getattr(record.get("agent"), "_live_transcript_path", None) if record else None
    if not path:
        return {"ok": True, "payload": result}
    try:
        with open(path, "rb") as stream:
            size = stream.seek(0, 2)
            stream.seek(max(0, size - TAIL_BYTES))
            text = stream.read(TAIL_BYTES).decode("utf-8", errors="ignore")
    except OSError:
        return {"ok": True, "payload": result}  # creation/cleanup races are normal
    return {"ok": True, "payload": {**result, "available": True, "text": text, "truncated": size > TAIL_BYTES}}


def rpc_steer(params: dict, **_kw) -> dict:
    ids, text = _ids(params), params.get("text")
    if ids is None or not isinstance(text, str) or not text.strip():
        return _invalid("sessionKey, subagentId and text are required")
    reg = _registry()
    queued = bool(_owned(reg, *ids)) and bool(reg.steer_subagent(ids[1], text))
    return {"ok": True, "payload": {"queued": queued}}


def rpc_interrupt(params: dict, **_kw) -> dict:
    ids = _ids(params)
    if ids is None:
        return _invalid("sessionKey and subagentId are required")
    reg = _registry()
    found = bool(_owned(reg, *ids)) and bool(reg.interrupt_subagent(ids[1]))
    return {"ok": True, "payload": {"found": found}}


RPC_METHODS = {"subagent.list": rpc_list, "subagent.tail": rpc_tail,
               "subagent.steer": rpc_steer, "subagent.interrupt": rpc_interrupt}
