"""Background subagents: relay Hermes's `subagent.*` progress events to the backend, which
publishes them on the room's current live channel, and serve the subagent RPCs
(docs/superpowers/specs/2026-10-09-background-subagents-design.md).

A child keeps its parent turn's `tool_progress_callback` for its whole life, so its events keep
arriving through FramePublisher.activity after that turn's run has closed. They cannot go out on
the run's frame channel: the room's deliveryKey may have rotated since. The backend looks up the
current key.
"""
from __future__ import annotations

import logging
import threading
import time

from bridge import OWNER, turn

log = logging.getLogger("bridge")

FLUSH_S = 1.0
MAX_BATCH = 25
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
    # subagent.text is the child's per-token reply; upstream drops it on the parent too.
    if not session_key or event_type == "subagent.text":
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
            status = turn._post_backend(f"subagents/{OWNER}/events",
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
