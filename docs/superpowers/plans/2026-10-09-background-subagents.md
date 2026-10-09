# Background Subagents in Rooms: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show an agent's background `delegate_task` subagents in the room, and let any room member watch, steer, stop and read them. Copy the Hermes desktop app.

**Architecture:**
- **Bridge relay.** The bridge splits Hermes's `subagent.*` progress events off the turn's
  `tool_progress_callback`. A relay POSTs them to a new hook-token backend route, at most once a
  second per room session.
- **Backend publish.** The backend publishes them as `room.subagent.event` on the room's current
  AppSync channel.
- **Controls.** Snapshot, tail, steer and stop are Clerk product routes. Each makes one bridge RPC,
  and these are ports of upstream `tui_gateway/methods_subagents.py`.
- **Clients.** A shared reducer, ported from upstream `apps/desktop/src/store/subagents.ts`, feeds a
  strip above the composer on web and iPhone, and a card under the `delegate_task` tool row.

**Tech Stack:** Python 3 bridge (Hermes image), FastAPI + DynamoDB + AppSync Events backend,
TypeScript `@lightbulb/shared` (vitest), Next.js web (vitest), Expo iPhone (jest).

**Spec:** `docs/superpowers/specs/2026-10-09-background-subagents-design.md`

## Global Constraints

- **Upstream version.** Upstream source of truth is Hermes tag `v2026.9.14` (our pin). Call only
  symbols present there: `tools.delegate_tool_registry._active_subagents`, `_active_subagents_lock`,
  `steer_subagent(subagent_id, text)`, `interrupt_subagent(subagent_id)`, and the record field
  `agent._live_transcript_path`.
- **Who may act.** Any room member who can see the room may Stop or Steer
  (`service.require_room_member`). Steer and Stop also pass `chat_gate.gate_chat` and
  `container_repo.touch_last_interactive`. Snapshot and tail never call `touch_last_interactive`.
- **No session wake.** Snapshot and tail never invoke a cold session: `warm_until` from
  `container_repo.get_session_sync(owner_id)` ≤ now ⇒ answer empty without an RPC.
- **Polling.** The only polling is upstream's: snapshot every 5 s while the strip is visible and a
  row is live (stop after 3 failures, resume on focus), and tail every 2 s while its pane is open.
- **Relay limits.** At most 1 POST/s per session key, ≤ 25 events per POST. `subagent.text` is
  never sent. `subagent.thinking` coalesces per subagent per batch.
- **Clipping.** The backend clips strings: `goal` 500 chars, every other string field 220 chars.
- **Steer copy.** Steer success copy is "Steer queued", never "delivered".
- **Statuses.** Statuses follow upstream: `queued, running, completed, failed, interrupted`.
  `timeout`/`error` map to failed and `cancelled` to interrupted. An unknown status on
  `subagent.complete` maps to failed.
- **Styling.** Use colour tokens only. No colour literals (`brandTokens.test.ts`).
- **Branches.** Never base on another agent's branch. This worktree is
  `.claude/worktrees/bg-subagents`, branched from `origin/main`.
- **Tests.** Backend, shared, web and mobile tests run in CI on the PR. Bridge unit tests are not in
  CI: run them locally (Task 1, Step 2 gives the command).

## Review Focus

1. **A subagent started by one room's agent must never appear, or be steerable, from another room or
   by another agent's session key** (Task 2 test `test_list_only_returns_subagents_of_the_given_keys`;
   Task 4 test `test_tail_of_another_agents_subagent_is_404`).
2. **Events that arrive after the parent run ended still reach the room, and no longer go out as
   `agent_activity` frames on the closed run** (Task 1 test
   `test_subagent_events_after_finish_go_to_relay_not_frames`).
3. **A removed member gets 403 from every subagent route** (Task 4 test
   `test_non_member_is_refused_on_every_route`).
4. **A cold or parked session costs no invoke on snapshot or tail** (Task 4 test
   `test_cold_session_answers_empty_without_rpc`).
5. **A row whose `subagent.complete` carries an unknown status never keeps spinning** (Task 5 test
   `complete with unknown status fails closed`).

---

### Task 1: Bridge relay of `subagent.*` events

**Files:**
- Create: `apps/infra/hermes/bridge/subagents.py`
- Modify: `apps/infra/hermes/bridge/turn.py` (`FramePublisher.__init__` ~1597, `activity` ~1722, `finished` ~1728, the construction in `run_turn` ~3746)
- Test: `apps/infra/hermes/tests/test_subagents_relay.py`

**Interfaces:**
- Produces:
  - `subagents.relay(session_key: str | None, event_type: str, name, preview, kw: dict) -> None`
  - `subagents.flush_once() -> None`
  - `subagents.OWNER_OF: dict[str, str]` (subagent id → session key)
  - `subagents.payload(event_type, name, preview, kw) -> dict`
  - The backend POST body is `{"sessionKey": str, "events": [{"type": str, "seq": int, "payload": dict}]}`
    to `/api/v1/internal/messaging/subagents/{OWNER}/events`.
- Consumes:
  - `turn._post_backend(path: str, body: dict) -> int`. It never raises. The path is relative to
    `/api/v1/internal/messaging/`.
  - `bridge.OWNER`.

- [ ] **Step 1: Write the failing tests**

```python
"""Subagent event relay (spec 2026-10-09 background subagents, Bridge)."""
import pytest

import factories as f
from bridge import subagents, turn

KEY = turn.lightbulb_session_key("ceo", f.ROOM)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    subagents._pending.clear()
    subagents.OWNER_OF.clear()
    monkeypatch.setattr(subagents, "_ensure_started", lambda: None)
    yield
    subagents._pending.clear()
    subagents.OWNER_OF.clear()


def _kw(**over):
    return {"subagent_id": "sa-1", "goal": "Map Paperclip", "task_index": 0, "task_count": 3,
            "delegation_id": "d-1", "model": "gpt-5.6-sol", "tool_count": 2, **over}


def test_payload_matches_upstream_progress_subagent():
    p = subagents.payload("subagent.tool", "web_search", "paperclip artifacts", _kw())
    assert p["goal"] == "Map Paperclip" and p["task_count"] == 3 and p["task_index"] == 0
    assert p["subagent_id"] == "sa-1" and p["delegation_id"] == "d-1" and p["tool_count"] == 2
    assert p["tool_name"] == "web_search"
    assert p["tool_preview"] == "paperclip artifacts" and p["text"] == "paperclip artifacts"


def test_text_events_are_never_sent():
    subagents.relay(KEY, "subagent.text", None, "tok", _kw())
    assert subagents._pending == {}


def test_thinking_coalesces_per_subagent_but_tools_do_not():
    subagents.relay(KEY, "subagent.thinking", None, "a", _kw())
    subagents.relay(KEY, "subagent.tool", "read_file", "x", _kw())
    subagents.relay(KEY, "subagent.thinking", None, "b", _kw())
    subagents.relay(KEY, "subagent.tool", "read_file", "y", _kw())
    types = [(e["type"], e["payload"].get("text")) for e in subagents._pending[KEY]]
    assert types == [("subagent.tool", "x"), ("subagent.thinking", "b"), ("subagent.tool", "y")]


def test_relay_records_ownership_and_complete_releases_it():
    subagents.relay(KEY, "subagent.start", None, "Map Paperclip", _kw())
    assert subagents.OWNER_OF["sa-1"] == KEY
    subagents.relay(KEY, "subagent.complete", None, "done", _kw(status="completed"))
    assert "sa-1" not in subagents.OWNER_OF


def test_no_session_key_is_a_noop():
    subagents.relay(None, "subagent.start", None, "x", _kw())
    assert subagents._pending == {} and subagents.OWNER_OF == {}


def test_flush_posts_batches_of_25_in_order(monkeypatch):
    posts = []
    monkeypatch.setattr(turn, "_post_backend", lambda path, body: posts.append((path, body)) or 200)
    for i in range(30):
        subagents.relay(KEY, "subagent.tool", "t", str(i), _kw())
    subagents.flush_once()
    assert [p[0] for p in posts] == ["subagents/org_test/events"] * 2
    assert [len(p[1]["events"]) for p in posts] == [25, 5]
    seqs = [e["seq"] for p in posts for e in p[1]["events"]]
    assert seqs == sorted(seqs) and posts[0][1]["sessionKey"] == KEY
    assert subagents._pending == {}


def test_subagent_events_after_finish_go_to_relay_not_frames(monkeypatch):
    sent, relayed = [], []
    pub = turn.FramePublisher("https://appsync.invalid/event", "/ws/w/rooms/r/k", f.ROOM, "ceo", "run-1", None,
                              publish=lambda *a: sent.append(a) or 200, session_key=KEY)
    monkeypatch.setattr(subagents, "relay", lambda *a: relayed.append(a))
    pub.finished("completed", "", None)
    sent.clear()
    pub.activity("subagent.tool", "web_search", "q", None, **_kw())
    assert relayed and relayed[0][0] == KEY and relayed[0][1] == "subagent.tool"
    assert sent == []  # the closed run's channel gets nothing
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/infra/hermes && uv run --with pytest --with pytest-asyncio python -m pytest tests/test_subagents_relay.py -q`
Expected: FAIL with `ImportError: cannot import name 'subagents' from 'bridge'`.

- [ ] **Step 3: Write `bridge/subagents.py` (the relay half)**

```python
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


# Port of Hermes v2026.9.14 tui_gateway/tool_progress.py `_SUBAGENT_FIELDS`, in wire order.
_FIELDS = (
    ("subagent_id", bool, str), ("parent_id", bool, str), ("child_session_id", bool, str),
    ("delegation_id", bool, str), ("depth", _not_none, int), ("model", bool, str), ("tool_count", _not_none, int),
    ("toolsets", bool, _str_list), ("input_tokens", _not_none, _int_or_skip), ("output_tokens", _not_none, _int_or_skip),
    ("reasoning_tokens", _not_none, _int_or_skip), ("api_calls", _not_none, _int_or_skip),
    ("files_read", bool, _str_list), ("files_written", bool, _str_list), ("output_tail", bool, list),
    ("tool_name", bool, str), ("text", bool, str), ("status", bool, str), ("summary", bool, str),
    ("duration_seconds", _not_none, float),
)


def payload(event_type: str, name, preview, kw: dict) -> dict:
    """Port of upstream `_progress_subagent`'s payload build: the exact shape the desktop app reads."""
    out = {"goal": str(kw.get("goal") or ""), "task_count": int(kw.get("task_count") or 1),
           "task_index": int(kw.get("task_index") or 0)}
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
    p = payload(event_type, name, preview, kw)
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
```

- [ ] **Step 4: Wire `FramePublisher`**

In `turn.py`:
- Add `session_key=None` as the last keyword of `FramePublisher.__init__`. Store it as
  `self._session_key = session_key` and add `self._ended = False`.
- In `finished`, set `self._ended = True` as its last statement.
- Replace `activity` with:

```python
    def activity(self, event, tool_name=None, preview=None, args=None, **_kw) -> None:
        if isinstance(event, str) and event.startswith("subagent."):
            from bridge import subagents  # lazy: subagents imports this module

            subagents.relay(self._session_key, event, tool_name, preview, _kw)
            if self._ended:
                return  # a background child outlived its turn: the run's channel is closed
        if event in _TOOL_PROGRESS_EVENTS:
            return
        self.emit({"type": "agent_activity", "activity": event, "title": tool_name or "",
                   "detail": {"preview": preview}})
```

In `run_turn` (~3746), pass the key: `FramePublisher(run.get("frameEndpoint", ""), run.get("frameChannel", ""), room.get("id"), agent_id, run_id, thread_root, session_key=session_key)`.

- [ ] **Step 5: Run the tests and the existing frame tests**

Run: `cd apps/infra/hermes && uv run --with pytest --with pytest-asyncio python -m pytest tests/test_subagents_relay.py tests/test_followups.py -q`, then the whole bridge suite: `uv run --with pytest --with pytest-asyncio python -m pytest tests -q`.
Expected: PASS. Read the summary line and the exit code before going on.

- [ ] **Step 6: Commit**

```bash
git add apps/infra/hermes/bridge/subagents.py apps/infra/hermes/bridge/turn.py apps/infra/hermes/tests/test_subagents_relay.py
git commit -m "feat(bridge): relay background subagent events to the backend"
```

---

### Task 2: Bridge subagent RPCs

**Files:**
- Modify: `apps/infra/hermes/bridge/subagents.py` (append)
- Modify: `apps/infra/hermes/bridge/server.py` (after `RPC_METHODS: dict = {"health": _health}` ~394)
- Test: `apps/infra/hermes/tests/test_subagents_rpc.py`

**Interfaces:**
- Consumes: `OWNER_OF` from Task 1. From the Hermes module `tools.delegate_tool_registry`:
  `_active_subagents: dict[str, dict]`, `_active_subagents_lock`, `steer_subagent(id, text) -> bool`
  and `interrupt_subagent(id) -> bool`.
- Produces: RPC methods called by the backend in Task 4, each answering `{"ok": True, "payload": ...}`:
  - `subagent.list {sessionKeys: [str]}` → `{"subagents": [ {subagent_id, parent_id, depth, goal, delegation_id, model, started_at, status, tool_count, last_tool, accepting_steer, session_key} ]}`
  - `subagent.tail {sessionKey, subagentId}` → `{"subagent_id", "available": bool, "text": str, "truncated": bool}`
  - `subagent.steer {sessionKey, subagentId, text}` → `{"queued": bool}`
  - `subagent.interrupt {sessionKey, subagentId}` → `{"found": bool}`
  - A missing or bad param answers `{"ok": False, "error": {"code": "INVALID", "message": ...}}`.

- [ ] **Step 1: Write the failing tests**

```python
"""Subagent RPCs: ports of Hermes tui_gateway/methods_subagents.py, owned by room session key."""
import sys
import threading
import types

import pytest

import factories as f
from bridge import server, subagents, turn

KEY = turn.lightbulb_session_key("ceo", f.ROOM)
OTHER = turn.lightbulb_session_key("finance", f.ROOM)


class _Agent:
    def __init__(self, path=None):
        self._live_transcript_path = path
        self.steered, self.interrupted = [], False


@pytest.fixture
def registry(monkeypatch, tmp_path):
    transcript = tmp_path / "child.log"
    transcript.write_bytes(b"x" * 20000 + b"\nlast line")
    mine, theirs = _Agent(str(transcript)), _Agent()
    reg = types.ModuleType("tools.delegate_tool_registry")
    reg._active_subagents_lock = threading.Lock()
    reg._active_subagents = {
        "sa-1": {"subagent_id": "sa-1", "goal": "Map Paperclip", "status": "running", "depth": 0,
                 "started_at": 1760000000.0, "tool_count": 4, "last_tool": "web_search",
                 "accepting_steer": True, "agent": mine},
        "sa-2": {"subagent_id": "sa-2", "goal": "Other room", "status": "running", "agent": theirs},
    }
    reg.steer_subagent = lambda sid, text: reg._active_subagents[sid]["agent"].steered.append(text) or True
    reg.interrupt_subagent = lambda sid: setattr(reg._active_subagents[sid]["agent"], "interrupted", True) or True
    monkeypatch.setitem(sys.modules, "tools.delegate_tool_registry", reg)
    subagents.OWNER_OF.clear()
    subagents.OWNER_OF.update({"sa-1": KEY, "sa-2": OTHER})
    yield reg, mine, theirs
    subagents.OWNER_OF.clear()


def _call(method, params):
    return server.RPC_METHODS[method](params, timeout_ms=1000)


def test_list_only_returns_subagents_of_the_given_keys(registry):
    out = _call("subagent.list", {"sessionKeys": [KEY]})
    assert out["ok"] is True
    [row] = out["payload"]["subagents"]
    assert row["subagent_id"] == "sa-1" and row["session_key"] == KEY and row["last_tool"] == "web_search"
    assert "agent" not in row


def test_list_without_hermes_loaded_is_empty(monkeypatch):
    monkeypatch.delitem(sys.modules, "tools.delegate_tool_registry", raising=False)
    assert _call("subagent.list", {"sessionKeys": [KEY]}) == {"ok": True, "payload": {"subagents": []}}


def test_tail_reads_the_last_16k(registry):
    out = _call("subagent.tail", {"sessionKey": KEY, "subagentId": "sa-1"})["payload"]
    assert out["available"] is True and out["truncated"] is True
    assert len(out["text"].encode()) <= 16384 and out["text"].endswith("last line")


def test_controls_refuse_another_sessions_subagent(registry):
    _reg, _mine, theirs = registry
    assert _call("subagent.steer", {"sessionKey": KEY, "subagentId": "sa-2", "text": "go"})["payload"] == {"queued": False}
    assert _call("subagent.interrupt", {"sessionKey": KEY, "subagentId": "sa-2"})["payload"] == {"found": False}
    assert _call("subagent.tail", {"sessionKey": KEY, "subagentId": "sa-2"})["payload"]["available"] is False
    assert theirs.steered == [] and theirs.interrupted is False


def test_steer_and_interrupt_own_subagent(registry):
    _reg, mine, _theirs = registry
    assert _call("subagent.steer", {"sessionKey": KEY, "subagentId": "sa-1", "text": "focus on Library"})["payload"] == {"queued": True}
    assert mine.steered == ["focus on Library"]
    assert _call("subagent.interrupt", {"sessionKey": KEY, "subagentId": "sa-1"})["payload"] == {"found": True}
    assert mine.interrupted is True


@pytest.mark.parametrize("method,params", [
    ("subagent.list", {}), ("subagent.list", {"sessionKeys": "x"}),
    ("subagent.tail", {"sessionKey": KEY}), ("subagent.steer", {"sessionKey": KEY, "subagentId": "sa-1"}),
])
def test_bad_params_are_invalid(registry, method, params):
    assert _call(method, params)["error"]["code"] == "INVALID"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/infra/hermes && uv run --with pytest --with pytest-asyncio python -m pytest tests/test_subagents_rpc.py -q`
Expected: FAIL with `KeyError: 'subagent.list'`.

- [ ] **Step 3: Append the RPC half to `bridge/subagents.py`**

```python
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


def _owned(reg, session_key: str, subagent_id: str) -> dict | None:
    if reg is None or OWNER_OF.get(subagent_id) != session_key:
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
    rows = [{**{k: r.get(k) for k in SNAPSHOT_FIELDS}, "session_key": OWNER_OF[r["subagent_id"]]}
            for r in records if OWNER_OF.get(str(r.get("subagent_id"))) in wanted]
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
```

In `server.py`, after `RPC_METHODS: dict = {"health": _health}`:

```python
from bridge import subagents  # noqa: E402 -- registered with the rest of RPC_METHODS

RPC_METHODS.update(subagents.RPC_METHODS)
```

(If `server.py` already imports from `bridge` at the top, put `subagents` in that import instead and keep only the `update` line here.)

- [ ] **Step 4: Run the tests**

Run: `cd apps/infra/hermes && uv run --with pytest --with pytest-asyncio python -m pytest tests -q`
Expected: PASS, exit code 0.

- [ ] **Step 5: Commit**

```bash
git add apps/infra/hermes/bridge/subagents.py apps/infra/hermes/bridge/server.py apps/infra/hermes/tests/test_subagents_rpc.py
git commit -m "feat(bridge): subagent list/tail/steer/interrupt RPCs owned by room session"
```

---

### Task 3: Backend: receive relayed events and publish to the room

**Files:**
- Create: `apps/backend/core/services/subagents.py`
- Modify: `apps/backend/core/services/messaging.py` (`ROOM_LIVE_EVENT_TYPES` ~171 adds `"room.subagent.event"`; `API_ERROR_STATUS` ~135 adds `"SUBAGENT_NOT_FOUND": 404`)
- Modify: `apps/backend/routers/messaging.py` (new internal route next to `post_usage_report` ~1172)
- Test: `apps/backend/tests/unit/services/test_subagents_events.py`, `apps/backend/tests/unit/routers/test_messaging_subagent_events.py`

**Interfaces:**
- Consumes: the bridge body from Task 1.
- Produces:
  - `subagents.SubagentEventsRequest` (fields `session_key`, `events`).
  - `subagents.publish_events(owner_id: str, body: SubagentEventsRequest) -> dict` → `{"published": int}`.
  - The live event `room.subagent.event`, with payload `{"roomId", "agentId", "events": [{"type", "seq", "payload"}]}`.

- [ ] **Step 1: Write the failing service tests**

```python
from unittest.mock import AsyncMock, patch

import pytest

from core.services import subagents
from core.services.messaging import ApiProblem

ROOM = "11111111-1111-4111-8111-111111111111"
AGENT = "22222222-2222-4222-8222-222222222222"
KEY = f"agent:{AGENT}:lightbulb:channel:{ROOM}"
OWNER = "org_1"


def _body(events=None, key=KEY):
    return subagents.SubagentEventsRequest(sessionKey=key, events=events or [
        {"type": "subagent.tool", "seq": 1, "payload": {"subagent_id": "sa-1", "goal": "g" * 900, "text": "t" * 900}}])


def _patches(meta=None, member=True):
    return (
        patch("core.services.subagents.messaging_repo.get_room", AsyncMock(return_value=meta if meta is not None else {"ownerId": OWNER})),
        patch("core.services.subagents.messaging_repo.get_member", AsyncMock(return_value={"id": AGENT} if member else None)),
        patch("core.services.subagents._publish", AsyncMock(return_value=True)),
    )


@pytest.mark.asyncio
async def test_publishes_clipped_events_on_the_room():
    a, b, c = _patches()
    with a, b, c as publish:
        assert await subagents.publish_events(OWNER, _body()) == {"published": 1}
    room_id, event = publish.await_args.args
    assert room_id == ROOM and event["type"] == "room.subagent.event"
    [evt] = event["payload"]["events"]
    assert len(evt["payload"]["goal"]) == 500 and len(evt["payload"]["text"]) == 220
    assert event["payload"]["agentId"] == AGENT


@pytest.mark.asyncio
@pytest.mark.parametrize("meta,member", [({"ownerId": "org_other"}, True), ({"ownerId": OWNER}, False)])
async def test_foreign_room_or_non_member_agent_is_refused(meta, member):
    a, b, c = _patches(meta, member)
    with a, b, c as publish, pytest.raises(ApiProblem):
        await subagents.publish_events(OWNER, _body())
    publish.assert_not_awaited()


@pytest.mark.asyncio
async def test_non_room_session_key_is_invalid():
    with pytest.raises(ApiProblem):
        await subagents.publish_events(OWNER, _body(key="agent:x:main"))


def test_unknown_event_types_and_oversize_batches_are_rejected():
    with pytest.raises(ValueError):
        _body(events=[{"type": "tool.started", "seq": 1, "payload": {}}])
    with pytest.raises(ValueError):
        _body(events=[{"type": "subagent.tool", "seq": i, "payload": {}} for i in range(26)])
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_subagents_events.py -q`
Expected: FAIL with `ImportError: cannot import name 'subagents'`.

- [ ] **Step 3: Write `core/services/subagents.py` (events half)**

```python
"""Background subagents in rooms (docs/superpowers/specs/2026-10-09-background-subagents-design.md).

The bridge relays Hermes's `subagent.*` events here; this module publishes them on the room's
CURRENT live channel (a removed member's rotated key stays silent), and serves the product
routes' snapshot/tail/steer/stop through one bridge RPC each. Nothing is stored: AppSync is the
fanout and the runtime's own registry is the truth while children live.
"""
from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field

from core.repositories import messaging_repo
from core.services.messaging import ApiProblem, ContractModel, make_live_event
from core.services.session_keys import parse_lightbulb_session_key

logger = logging.getLogger(__name__)

GOAL_CAP = 500
STRING_CAP = 220
EventType = Literal["subagent.start", "subagent.progress", "subagent.tool", "subagent.thinking", "subagent.complete"]


class SubagentEvent(BaseModel):
    type: EventType
    seq: int
    payload: dict[str, Any]


class SubagentEventsRequest(ContractModel):
    session_key: str = Field(min_length=1, max_length=512)
    events: list[SubagentEvent] = Field(min_length=1, max_length=25)


def _clip(value: Any, cap: int) -> Any:
    if isinstance(value, str):
        return value[:cap]
    if isinstance(value, list):
        return [_clip(v, cap) for v in value[:50]]
    if isinstance(value, dict):
        return {k: _clip(v, cap) for k, v in list(value.items())[:50]}
    return value


def _clipped(payload: dict[str, Any]) -> dict[str, Any]:
    return {k: _clip(v, GOAL_CAP if k == "goal" else STRING_CAP) for k, v in payload.items()}


async def _publish(room_id: str, event: dict) -> bool:
    from core.services.live_events import get_live_event_publisher

    return await get_live_event_publisher().publish_room(room_id, event)


async def room_and_agent(owner_id: str, session_key: str) -> tuple[str, str]:
    """The room and agent a session key names, only if the room is this owner's and the agent is in it."""
    parsed = parse_lightbulb_session_key(session_key)
    if parsed is None:
        raise ApiProblem("INVALID_REQUEST", "sessionKey is not a room session.")
    room_id, agent_id = parsed["room_id"], parsed["agent_id"]
    meta = await messaging_repo.get_room(room_id)
    if not meta or meta.get("ownerId") != owner_id:
        raise ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    if await messaging_repo.get_member(room_id, "agent", agent_id) is None:
        raise ApiProblem("RUN_NOT_ACTIVE", "The agent is no longer in this room.")
    return room_id, agent_id


async def publish_events(owner_id: str, body: SubagentEventsRequest) -> dict:
    room_id, agent_id = await room_and_agent(owner_id, body.session_key)
    events = [{"type": e.type, "seq": e.seq, "payload": _clipped(e.payload)} for e in body.events]
    event = make_live_event("room.subagent.event", {"roomId": room_id, "agentId": agent_id, "events": events},
                            room_id=room_id)
    published = await _publish(room_id, event)
    return {"published": len(events) if published else 0}
```

Confirm that `ROOM_NOT_FOUND`, `RUN_NOT_ACTIVE` and `INVALID_REQUEST` are keys in
`API_ERROR_STATUS`. Grep for them, and add any that are missing next to `RUN_NOT_FOUND`. Then:
- add `"SUBAGENT_NOT_FOUND": 404,` to `API_ERROR_STATUS`
- add `"room.subagent.event",` to `ROOM_LIVE_EVENT_TYPES`.

- [ ] **Step 4: Add the internal route and its test**

In `routers/messaging.py`, after `post_usage_report`:

```python
@router.post("/internal/messaging/subagents/{owner_id}/events")
async def post_subagent_events(owner_id: str, body: subagents.SubagentEventsRequest, request: Request):
    await _require_hook_token(owner_id, request)
    return await subagents.publish_events(owner_id, body)
```

Add `subagents` to the `from core.services import (...)` block at the top of the router.

`tests/unit/routers/test_messaging_subagent_events.py`:

```python
from unittest.mock import AsyncMock, patch

OWNER, TOKEN = "user_a", "hook-token-for-a"
KEY = "agent:22222222-2222-4222-8222-222222222222:lightbulb:channel:11111111-1111-4111-8111-111111111111"
BODY = {"sessionKey": KEY, "events": [{"type": "subagent.start", "seq": 1, "payload": {"subagent_id": "sa-1"}}]}


def _container():
    return patch("routers.messaging.container_repo.get_by_owner_id",
                 AsyncMock(return_value={"owner_id": OWNER, "hook_token": TOKEN}))


def test_valid_token_reaches_the_service(client):
    with _container(), patch("routers.messaging.subagents.publish_events",
                             AsyncMock(return_value={"published": 1})) as svc:
        r = client.post(f"/api/v1/internal/messaging/subagents/{OWNER}/events", json=BODY,
                        headers={"Authorization": f"Bearer {TOKEN}"})
    assert r.status_code == 200 and r.json() == {"published": 1}
    assert svc.await_args.args[0] == OWNER


def test_wrong_token_is_401(client):
    with _container(), patch("routers.messaging.subagents.publish_events", AsyncMock()) as svc:
        r = client.post(f"/api/v1/internal/messaging/subagents/{OWNER}/events", json=BODY,
                        headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401
    svc.assert_not_awaited()
```

- [ ] **Step 5: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_subagents_events.py tests/unit/routers/test_messaging_subagent_events.py -q`
Expected: PASS. (The full suite runs in CI.)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/subagents.py apps/backend/core/services/messaging.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_subagents_events.py apps/backend/tests/unit/routers/test_messaging_subagent_events.py
git commit -m "feat(messaging): publish relayed subagent events on the room's live channel"
```

---

### Task 4: Backend product routes: snapshot, tail, steer, stop

**Files:**
- Modify: `apps/backend/core/services/subagents.py` (append)
- Modify: `apps/backend/routers/messaging.py` (four routes next to `steer_run` ~1077)
- Test: `apps/backend/tests/unit/services/test_subagents_controls.py`, `apps/backend/tests/unit/routers/test_messaging_subagent_routes.py`

**Interfaces:**
- Consumes:
  - the bridge RPCs from Task 2
  - `container_rpc.call(owner_id, method, params, *, timeout_s)`, which raises `RuntimeError` on
    `{ok:false}` and `asyncio.TimeoutError` on a 504
  - `container_repo.get_session_sync(owner_id) -> dict` (sync)
  - `messaging_repo.list_members(room_id) -> list[dict]`
  - `service.require_room_member(owner_id, user_id, room_id) -> (meta, member)`
- Produces (HTTP; the JSON envelopes are camelCase, and each subagent entry is upstream's snake_case
  record plus `agent_id`):
  - `GET /api/v1/messaging/rooms/{roomId}/subagents` → `{"subagents": [...]}`
  - `GET /api/v1/messaging/rooms/{roomId}/agents/{agentId}/subagents/{subagentId}/tail` → `{"subagentId", "available", "text", "truncated"}`
  - `POST …/{subagentId}/steer {text}` → `{"state": "queued"}`, or 404 `SUBAGENT_NOT_FOUND`
  - `POST …/{subagentId}/stop` → `{"state": "stopping"}`, or 404 `SUBAGENT_NOT_FOUND`
  - service functions `list_for_room`, `tail`, `steer`, `stop` (signatures below).

- [ ] **Step 1: Write the failing service tests**

```python
import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

from core.services import subagents
from core.services.messaging import ApiProblem

OWNER, ROOM = "org_1", "11111111-1111-4111-8111-111111111111"
CEO, FIN = "22222222-2222-4222-8222-222222222222", "33333333-3333-4333-8333-333333333333"


def _warm(warm=True):
    until = int(time.time()) + (600 if warm else -10)
    return patch("core.services.subagents.container_repo.get_session_sync", return_value={"warm_until": until})


def _members():
    return patch("core.services.subagents.messaging_repo.list_members", AsyncMock(return_value=[
        {"kind": "agent", "id": CEO}, {"kind": "agent", "id": FIN}, {"kind": "human", "id": "user_1"}]))


@pytest.mark.asyncio
async def test_cold_session_answers_empty_without_rpc():
    with _warm(False), _members(), patch("core.services.subagents.container_rpc.call", AsyncMock()) as rpc:
        assert await subagents.list_for_room(OWNER, ROOM) == {"subagents": []}
        assert (await subagents.tail(OWNER, ROOM, CEO, "sa-1"))["available"] is False
        with pytest.raises(ApiProblem) as exc:
            await subagents.steer(OWNER, ROOM, CEO, "sa-1", "go")
        assert exc.value.error.code == "SUBAGENT_NOT_FOUND"
    rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_asks_once_for_every_agent_member_and_names_the_agent():
    key = f"agent:{CEO}:lightbulb:channel:{ROOM}"
    rpc = AsyncMock(return_value={"subagents": [{"subagent_id": "sa-1", "goal": "g", "session_key": key}]})
    with _warm(), _members(), patch("core.services.subagents.container_rpc.call", rpc):
        out = await subagents.list_for_room(OWNER, ROOM)
    assert out == {"subagents": [{"subagent_id": "sa-1", "goal": "g", "agent_id": CEO}]}
    method, params = rpc.await_args.args[1], rpc.await_args.args[2]
    assert method == "subagent.list" and sorted(params["sessionKeys"]) == sorted(
        [key, f"agent:{FIN}:lightbulb:channel:{ROOM}"])


@pytest.mark.asyncio
@pytest.mark.parametrize("fn,args,payload", [
    (subagents.steer, ("sa-1", "go"), {"queued": False}),
    (subagents.stop, ("sa-1",), {"found": False}),
])
async def test_unowned_or_finished_subagent_is_404(fn, args, payload):
    with _warm(), patch("core.services.subagents.container_rpc.call", AsyncMock(return_value=payload)):
        with pytest.raises(ApiProblem) as exc:
            await fn(OWNER, ROOM, CEO, *args)
    assert exc.value.error.code == "SUBAGENT_NOT_FOUND"


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [RuntimeError('{"code":"UNAVAILABLE"}'), asyncio.TimeoutError()])
async def test_runtime_errors_are_retryable_unavailable(error):
    with _warm(), _members(), patch("core.services.subagents.container_rpc.call", AsyncMock(side_effect=error)):
        with pytest.raises(ApiProblem) as exc:
            await subagents.list_for_room(OWNER, ROOM)
    assert exc.value.error.code == "RUNTIME_UNAVAILABLE"
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_subagents_controls.py -q`
Expected: FAIL with `AttributeError: module 'core.services.subagents' has no attribute 'list_for_room'`.

- [ ] **Step 3: Append the controls half to `core/services/subagents.py`**

Add `import asyncio` and `import time` to the module's imports, `container_repo` to the repositories import, and `from core.services import container_rpc` and `from core.dynamodb import run_in_thread`. Then:

```python
RPC_TIMEOUT_S = 10.0


def session_key(room_id: str, agent_id: str) -> str:
    return f"agent:{agent_id}:lightbulb:channel:{room_id}"


async def _warm(owner_id: str) -> bool:
    """Read-only: the single-flight estimate runtime_transport keeps. A cold session has no live
    children, and a read must never be what wakes it."""
    row = await run_in_thread(container_repo.get_session_sync, owner_id)
    return int(row.get("warm_until") or 0) > int(time.time())


async def _rpc(owner_id: str, method: str, params: dict) -> dict:
    try:
        return await container_rpc.call(owner_id, method, params, timeout_s=RPC_TIMEOUT_S) or {}
    except (RuntimeError, asyncio.TimeoutError) as exc:
        raise ApiProblem("RUNTIME_UNAVAILABLE", "The runtime is unavailable.", retryable=True, retry_after=3) from exc


async def list_for_room(owner_id: str, room_id: str) -> dict:
    if not await _warm(owner_id):
        return {"subagents": []}
    agents = [m["id"] for m in await messaging_repo.list_members(room_id) if m.get("kind") == "agent"]
    if not agents:
        return {"subagents": []}
    by_key = {session_key(room_id, a): a for a in agents}
    payload = await _rpc(owner_id, "subagent.list", {"sessionKeys": list(by_key)})
    rows = []
    for row in payload.get("subagents") or []:
        agent_id = by_key.get(row.pop("session_key", None))
        if agent_id:
            rows.append({**row, "agent_id": agent_id})
    return {"subagents": rows}


async def tail(owner_id: str, room_id: str, agent_id: str, subagent_id: str) -> dict:
    empty = {"subagentId": subagent_id, "available": False, "text": "", "truncated": False}
    if not await _warm(owner_id):
        return empty
    p = await _rpc(owner_id, "subagent.tail", {"sessionKey": session_key(room_id, agent_id), "subagentId": subagent_id})
    return {"subagentId": subagent_id, "available": bool(p.get("available")), "text": str(p.get("text") or ""),
            "truncated": bool(p.get("truncated"))}


def _gone() -> ApiProblem:
    return ApiProblem("SUBAGENT_NOT_FOUND", "That subagent has already finished.")


async def steer(owner_id: str, room_id: str, agent_id: str, subagent_id: str, text: str) -> dict:
    if not await _warm(owner_id):
        raise _gone()
    p = await _rpc(owner_id, "subagent.steer",
                   {"sessionKey": session_key(room_id, agent_id), "subagentId": subagent_id, "text": text})
    if not p.get("queued"):
        raise _gone()
    return {"state": "queued"}


async def stop(owner_id: str, room_id: str, agent_id: str, subagent_id: str) -> dict:
    if not await _warm(owner_id):
        raise _gone()
    p = await _rpc(owner_id, "subagent.interrupt", {"sessionKey": session_key(room_id, agent_id), "subagentId": subagent_id})
    if not p.get("found"):
        raise _gone()
    return {"state": "stopping"}
```

- [ ] **Step 4: Add the routes and the router tests**

In `routers/messaging.py`, near `steer_run`:

```python
async def _subagent_scope(auth: AuthContext, room_id: str, agent_id: str | None = None) -> str:
    owner_id = resolve_owner_id(auth)
    await service.require_room_member(owner_id, auth.user_id, room_id)
    if agent_id is not None:
        agent_id = service.validate_uuid(agent_id, label="agent ID")
        if await messaging_repo.get_member(room_id, "agent", agent_id) is None:
            raise ApiProblem("SUBAGENT_NOT_FOUND", "That agent is not in this room.")
    return owner_id


async def _subagent_control_gate(auth: AuthContext, owner_id: str, room_id: str, agent_id: str) -> None:
    gate = await chat_gate.gate_chat(user_id=auth.user_id, owner_id=owner_id,
                                     session_key=subagents.session_key(room_id, agent_id))
    if gate.get("blocked"):
        raise ApiProblem("RUN_NOT_ACTIVE", gate.get("message", "This workspace cannot act on agents right now."))
    await container_repo.touch_last_interactive(owner_id)


@router.get("/messaging/rooms/{room_id}/subagents")
async def list_room_subagents(room_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = await _subagent_scope(auth, room_id)
    return await subagents.list_for_room(owner_id, room_id)


@router.get("/messaging/rooms/{room_id}/agents/{agent_id}/subagents/{subagent_id}/tail")
async def tail_subagent(room_id: str, agent_id: str, subagent_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = await _subagent_scope(auth, room_id, agent_id)
    return await subagents.tail(owner_id, room_id, agent_id, subagent_id)


@router.post("/messaging/rooms/{room_id}/agents/{agent_id}/subagents/{subagent_id}/steer")
async def steer_subagent(room_id: str, agent_id: str, subagent_id: str, request: SteerRunRequest,
                         auth: AuthContext = Depends(get_current_user)):
    owner_id = await _subagent_scope(auth, room_id, agent_id)
    await _subagent_control_gate(auth, owner_id, room_id, agent_id)
    return await subagents.steer(owner_id, room_id, agent_id, subagent_id, request.text)


@router.post("/messaging/rooms/{room_id}/agents/{agent_id}/subagents/{subagent_id}/stop")
async def stop_subagent(room_id: str, agent_id: str, subagent_id: str, auth: AuthContext = Depends(get_current_user)):
    owner_id = await _subagent_scope(auth, room_id, agent_id)
    await _subagent_control_gate(auth, owner_id, room_id, agent_id)
    return await subagents.stop(owner_id, room_id, agent_id, subagent_id)
```

`SteerRunRequest` is already imported by the router (`from core.services.messaging import (...)`).
Check that `validate_uuid` is reachable as `service.validate_uuid`; it is defined in
`core/services/messaging.py:980`.

`tests/unit/routers/test_messaging_subagent_routes.py` calls the handlers directly, in the style of `test_messaging_run_controls.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.services.messaging import ApiProblem, SteerRunRequest
from routers.messaging import list_room_subagents, steer_subagent, stop_subagent, tail_subagent

ROOM, CEO = "11111111-1111-4111-8111-111111111111", "22222222-2222-4222-8222-222222222222"


def _auth():
    auth = MagicMock()
    auth.org_id, auth.user_id = "org_1", "user_1"
    return auth


ROUTES = [
    lambda: list_room_subagents(ROOM, auth=_auth()),
    lambda: tail_subagent(ROOM, CEO, "sa-1", auth=_auth()),
    lambda: steer_subagent(ROOM, CEO, "sa-1", SteerRunRequest(text="go"), auth=_auth()),
    lambda: stop_subagent(ROOM, CEO, "sa-1", auth=_auth()),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", ROUTES)
async def test_non_member_is_refused_on_every_route(call):
    denied = ApiProblem("ROOM_NOT_FOUND", "Room not found.")
    with patch("routers.messaging.service.require_room_member", AsyncMock(side_effect=denied)), \
         patch("routers.messaging.subagents.container_rpc.call", AsyncMock()) as rpc:
        with pytest.raises(ApiProblem):
            await call()
    rpc.assert_not_awaited()


@pytest.mark.asyncio
async def test_tail_of_another_agents_subagent_is_404():
    with patch("routers.messaging.service.require_room_member", AsyncMock(return_value=({}, {}))), \
         patch("routers.messaging.messaging_repo.get_member", AsyncMock(return_value=None)):
        with pytest.raises(ApiProblem) as exc:
            await tail_subagent(ROOM, CEO, "sa-1", auth=_auth())
    assert exc.value.error.code == "SUBAGENT_NOT_FOUND"


@pytest.mark.asyncio
async def test_steer_passes_the_chat_gate_and_marks_interactive():
    with patch("routers.messaging.service.require_room_member", AsyncMock(return_value=({}, {}))), \
         patch("routers.messaging.messaging_repo.get_member", AsyncMock(return_value={"id": CEO})), \
         patch("routers.messaging.chat_gate.gate_chat", AsyncMock(return_value={"blocked": False})) as gate, \
         patch("routers.messaging.container_repo.touch_last_interactive", AsyncMock()) as touch, \
         patch("routers.messaging.subagents.steer", AsyncMock(return_value={"state": "queued"})):
        assert await steer_subagent(ROOM, CEO, "sa-1", SteerRunRequest(text="go"), auth=_auth()) == {"state": "queued"}
    gate.assert_awaited_once()
    touch.assert_awaited_once()


@pytest.mark.asyncio
async def test_blocked_workspace_cannot_stop():
    with patch("routers.messaging.service.require_room_member", AsyncMock(return_value=({}, {}))), \
         patch("routers.messaging.messaging_repo.get_member", AsyncMock(return_value={"id": CEO})), \
         patch("routers.messaging.chat_gate.gate_chat", AsyncMock(return_value={"blocked": True, "message": "Trial ended"})), \
         patch("routers.messaging.subagents.stop", AsyncMock()) as stop:
        with pytest.raises(ApiProblem):
            await stop_subagent(ROOM, CEO, "sa-1", auth=_auth())
    stop.assert_not_awaited()
```

- [ ] **Step 5: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/services/test_subagents_controls.py tests/unit/routers/test_messaging_subagent_routes.py -q; echo exit=$?`
Expected: PASS and `exit=0`. Do not pipe through `tail`.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/services/subagents.py apps/backend/routers/messaging.py apps/backend/tests/unit/services/test_subagents_controls.py apps/backend/tests/unit/routers/test_messaging_subagent_routes.py
git commit -m "feat(messaging): subagent snapshot, tail, steer and stop routes for room members"
```

---

### Task 5: Shared: live event type and the ported subagent reducer

**Files:**
- Modify: `packages/shared/src/live/types.ts` (the `LiveEventType` union ~120, `LIVE_EVENT_TYPES` ~150, the envelope union ~303, the validator switch ~440)
- Create: `packages/shared/src/messaging/subagents.ts`
- Modify: `packages/shared/src/index.ts` (export the new module's public names)
- Test: `packages/shared/src/messaging/subagents.test.ts`, plus one case in `packages/shared/src/messaging/roomActivity.test.ts` (or the existing `parseLiveEvent` test file)

**Interfaces:**
- Produces:
  - `type SubagentStatus = "completed" | "failed" | "interrupted" | "queued" | "running"`
  - `interface SubagentProgress { id; agentId; parentId: string | null; goal; delegationId?; model?; status; taskCount; taskIndex; startedAt; updatedAt; durationSeconds?; toolCount?; stream: SubagentStreamEntry[]; summary?; currentTool? }`
  - `interface RoomSubagents { items: SubagentProgress[]; retired: string[] }`
  - `EMPTY_ROOM_SUBAGENTS: RoomSubagents`
  - `applySubagentEvent(state: RoomSubagents, agentId: string, type: string, payload: Record<string, unknown>, now: number): RoomSubagents`
  - `reconcileSubagentSnapshot(state: RoomSubagents, children: Record<string, unknown>[], now: number): RoomSubagents`
    (each child carries `agent_id`)
  - `pruneFinishedSubagents(state: RoomSubagents, agentId: string): RoomSubagents`
  - `liveSubagents(state: RoomSubagents, agentId?: string): SubagentProgress[]`
  - `subagentActivity(item: SubagentProgress): string` (the last stream line, "queued", or
    "waiting for activity")
  - the live event `"room.subagent.event"`, with payload
    `RoomSubagentEvent = { roomId: string; agentId: string; events: { type: string; seq: number; payload: Record<string, unknown> }[] }`

- [ ] **Step 1: Write the failing reducer tests**

```ts
import { describe, expect, it } from "vitest";
import { EMPTY_ROOM_SUBAGENTS, applySubagentEvent, liveSubagents, pruneFinishedSubagents, reconcileSubagentSnapshot, subagentActivity } from "./subagents";

const A = "agent-ceo";
const base = { subagent_id: "sa-1", goal: "Map Paperclip", task_index: 0, task_count: 3, delegation_id: "d-1" };

describe("subagent reducer (port of Hermes desktop store/subagents.ts)", () => {
  it("start then tool shows a running row with the tool as activity", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.start", base, 1000);
    s = applySubagentEvent(s, A, "subagent.tool", { ...base, tool_name: "web_search", tool_preview: "paperclip docs" }, 2000);
    const [row] = liveSubagents(s, A);
    expect(row.status).toBe("running");
    expect(row.startedAt).toBe(1000);
    expect(subagentActivity(row)).toBe('Web Search("paperclip docs")');
  });

  it("complete with unknown status fails closed", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.start", base, 1000);
    s = applySubagentEvent(s, A, "subagent.complete", { ...base, status: "running" }, 2000);
    expect(s.items[0].status).toBe("failed");
    expect(liveSubagents(s, A)).toEqual([]);
  });

  it("maps timeout to failed with a synthesized summary and cancelled to interrupted", () => {
    const t = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.complete", { ...base, status: "timeout", duration_seconds: 612 }, 1);
    expect(t.items[0].status).toBe("failed");
    expect(t.items[0].summary).toBe("Timed out after 612s");
    const c = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.complete", { ...base, status: "cancelled" }, 1);
    expect(c.items[0].status).toBe("interrupted");
  });

  it("a terminal row ignores later events, and a pruned id never comes back", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.complete", { ...base, status: "completed" }, 1);
    s = applySubagentEvent(s, A, "subagent.tool", { ...base, tool_name: "x" }, 2);
    expect(s.items[0].status).toBe("completed");
    s = pruneFinishedSubagents(s, A);
    expect(s.items).toEqual([]);
    s = applySubagentEvent(s, A, "subagent.tool", { ...base, tool_name: "x" }, 3);
    expect(s.items).toEqual([]);
  });

  it("prune keeps running background rows and other agents' rows", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.start", base, 1);
    s = applySubagentEvent(s, A, "subagent.complete", { ...base, subagent_id: "sa-2", status: "completed" }, 2);
    s = applySubagentEvent(s, "agent-fin", "subagent.complete", { ...base, subagent_id: "sa-3", status: "completed" }, 2);
    s = pruneFinishedSubagents(s, A);
    expect(s.items.map((i) => i.id).sort()).toEqual(["sa-1", "sa-3"]);
  });

  it("snapshot seeds cold rows, drops live rows it no longer lists, and never overwrites newer live text", () => {
    let s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.tool", { ...base, tool_name: "read_file", tool_preview: "a.md" }, 5000);
    s = applySubagentEvent(s, A, "subagent.start", { ...base, subagent_id: "gone" }, 5000);
    s = reconcileSubagentSnapshot(s, [
      { ...base, agent_id: A, status: "running", last_tool: "web_search", started_at: 4 },
      { subagent_id: "sa-9", goal: "New", agent_id: A, status: "queued", last_tool: "terminal", started_at: 6 },
    ], 7000);
    const ids = s.items.map((i) => i.id).sort();
    expect(ids).toEqual(["sa-1", "sa-9"]);
    const sa1 = s.items.find((i) => i.id === "sa-1")!;
    expect(subagentActivity(sa1)).toBe('Read File("a.md")');
    expect(sa1.startedAt).toBe(4000);
    expect(subagentActivity(s.items.find((i) => i.id === "sa-9")!)).toBe("Terminal");
  });

  it("a queued row with no activity says queued", () => {
    const s = applySubagentEvent(EMPTY_ROOM_SUBAGENTS, A, "subagent.start", { ...base, status: "queued" }, 1);
    expect(subagentActivity(s.items[0])).toBe("queued");
  });
});
```

Add to the live-event parser test: a `room.subagent.event` with `{roomId, agentId, events:[{type:"subagent.start", seq:1, payload:{}}]}` parses, and one with `events: "x"` is rejected.

- [ ] **Step 2: Run them to verify they fail**

Run: `cd packages/shared && npx vitest run src/messaging/subagents.test.ts`
Expected: FAIL, `Cannot find module './subagents'`.

- [ ] **Step 3: Write `messaging/subagents.ts`**

```ts
/**
 * Background subagents in a room: a port of Hermes's desktop store (v2026.9.14
 * apps/desktop/src/store/subagents.ts) as pure functions, so web and iPhone share one reducer.
 * Upstream rules kept: an unknown status on subagent.complete fails closed; a terminal row ignores
 * later events; a snapshot never appends over newer live text; prune keeps running background rows.
 * Added: retired ids (a pruned id never comes back), and rows keyed per agent.
 */
export type SubagentStatus = "completed" | "failed" | "interrupted" | "queued" | "running";
export type SubagentStreamKind = "progress" | "summary" | "thinking" | "tool";
export interface SubagentStreamEntry { at: number; isError?: boolean; kind: SubagentStreamKind; text: string }
export interface SubagentProgress {
  id: string; agentId: string; parentId: string | null; goal: string; delegationId?: string; model?: string;
  status: SubagentStatus; taskCount: number; taskIndex: number; startedAt: number; updatedAt: number;
  durationSeconds?: number; toolCount?: number; stream: SubagentStreamEntry[]; summary?: string; currentTool?: string;
}
export interface RoomSubagents { items: SubagentProgress[]; retired: string[] }
export const EMPTY_ROOM_SUBAGENTS: RoomSubagents = { items: [], retired: [] };

type Payload = Record<string, unknown>;
const TERMINAL: ReadonlySet<SubagentStatus> = new Set(["completed", "failed", "interrupted"]);
const MAX_STREAM = 24;
const PREVIEW_MAX = 220;
const TOOL_PREVIEW_MAX = 96;
const MAX_RETIRED = 200;

const isStr = (v: unknown): v is string => typeof v === "string";
const str = (v: unknown) => (isStr(v) ? v : "");
const num = (v: unknown) => (typeof v === "number" && Number.isFinite(v) ? v : undefined);
const capitalize = (s: string) => (s ? s[0].toUpperCase() + s.slice(1) : s);

function asStatus(v: unknown, terminalEvent: boolean): SubagentStatus {
  if (v === "completed" || v === "failed" || v === "interrupted") return v;
  if (v === "timeout" || v === "error") return "failed";
  if (v === "cancelled" || v === "canceled") return "interrupted";
  if (terminalEvent) return "failed";
  return v === "queued" ? "queued" : "running";
}

function compact(text: string, max = PREVIEW_MAX): string {
  const line = text.replace(/\s+/g, " ").trim();
  return line.length > max ? `${line.slice(0, max - 1)}…` : line;
}

const toolLabel = (name: string) => name.split("_").filter(Boolean).map(capitalize).join(" ") || name;
const formatTool = (name: string, preview = "") => {
  const snippet = compact(preview, TOOL_PREVIEW_MAX);
  return snippet ? `${toolLabel(name)}("${snippet}")` : toolLabel(name);
};

function appendStream(stream: SubagentStreamEntry[], entry: SubagentStreamEntry): SubagentStreamEntry[] {
  const last = stream.at(-1);
  if (last?.kind === entry.kind && last.text === entry.text && last.isError === entry.isError) return stream;
  return [...stream, entry].slice(-MAX_STREAM);
}

const timeoutSummary = (p: Payload) => (str(p.status) === "timeout" ? `Timed out after ${num(p.duration_seconds) ?? "?"}s` : "");

function streamFromPayload(p: Payload, status: SubagentStatus, eventType: string, at: number): SubagentStreamEntry[] {
  const out: SubagentStreamEntry[] = [];
  const tool = str(p.tool_name);
  const preview = str(p.tool_preview) || str(p.text);
  const text = compact(str(p.text) || preview);
  if (tool) out.push({ at, isError: !!p.error, kind: "tool", text: formatTool(tool, preview) });
  if (eventType === "subagent.progress" && text) out.push({ at, isError: !!p.error, kind: "progress", text });
  if (eventType === "subagent.thinking" && text) out.push({ at, kind: "thinking", text });
  const summary = compact(str(p.summary) || str(p.text) || timeoutSummary(p));
  if (TERMINAL.has(status) && summary) out.push({ at, isError: status === "failed", kind: "summary", text: summary });
  return out;
}

const idOf = (p: Payload) => str(p.subagent_id) || `${str(p.parent_id) || "root"}:${num(p.task_index) ?? 0}:${str(p.goal)}`;

function toProgress(p: Payload, agentId: string, prev: SubagentProgress | undefined, eventType: string, at: number): SubagentProgress {
  const status = asStatus(p.status, eventType === "subagent.complete");
  return {
    id: prev?.id ?? idOf(p),
    agentId,
    parentId: str(p.parent_id) || prev?.parentId || null,
    goal: str(p.goal) || prev?.goal || "Subagent",
    delegationId: str(p.delegation_id) || prev?.delegationId,
    model: str(p.model) || prev?.model,
    status,
    taskCount: num(p.task_count) ?? prev?.taskCount ?? 1,
    taskIndex: num(p.task_index) ?? prev?.taskIndex ?? 0,
    startedAt: prev?.startedAt ?? at,
    updatedAt: at,
    durationSeconds: num(p.duration_seconds) ?? prev?.durationSeconds,
    toolCount: num(p.tool_count) ?? prev?.toolCount,
    stream: streamFromPayload(p, status, eventType, at).reduce(appendStream, prev?.stream ?? []),
    summary: str(p.summary) || timeoutSummary(p) || prev?.summary || undefined,
    currentTool: TERMINAL.has(status) ? undefined : str(p.tool_name) || prev?.currentTool,
  };
}

export function applySubagentEvent(state: RoomSubagents, agentId: string, type: string, payload: Payload, now: number): RoomSubagents {
  const id = idOf(payload);
  if (state.retired.includes(id)) return state;
  const index = state.items.findIndex((i) => i.id === id);
  const prev = index >= 0 ? state.items[index] : undefined;
  if (prev && TERMINAL.has(prev.status)) return state;
  const next = toProgress(payload, agentId, prev, type, now);
  const items = index >= 0 ? state.items.map((i, k) => (k === index ? next : i)) : [...state.items, next];
  return { ...state, items };
}

export function reconcileSubagentSnapshot(state: RoomSubagents, children: Payload[], now: number): RoomSubagents {
  const ids = new Set(children.map((c) => str(c.subagent_id)).filter(Boolean));
  const items = state.items.filter((i) => TERMINAL.has(i.status) || ids.has(i.id));
  for (const child of children) {
    const id = str(child.subagent_id);
    const agentId = str(child.agent_id);
    if (!id || !agentId || state.retired.includes(id)) continue;
    const index = items.findIndex((i) => i.id === id);
    const prev = items[index];
    if (prev && TERMINAL.has(prev.status)) continue;
    const projected = toProgress(child, agentId, prev, "", now);
    projected.startedAt = (num(child.started_at) ?? 0) * 1000 || prev?.startedAt || projected.startedAt;
    projected.updatedAt = prev?.updatedAt ?? projected.startedAt;
    if (prev) projected.stream = prev.stream;
    if (!projected.stream.length && str(child.last_tool)) {
      projected.stream = [{ at: projected.updatedAt, kind: "tool", text: formatTool(str(child.last_tool)) }];
    }
    if (index < 0) items.push(projected);
    else items[index] = JSON.stringify(prev) === JSON.stringify(projected) ? prev : projected;
  }
  const same = items.length === state.items.length && items.every((i, k) => i === state.items[k]);
  return same ? state : { ...state, items };
}

export function pruneFinishedSubagents(state: RoomSubagents, agentId: string): RoomSubagents {
  const gone = state.items.filter((i) => i.agentId === agentId && TERMINAL.has(i.status)).map((i) => i.id);
  if (!gone.length) return state;
  return {
    items: state.items.filter((i) => !gone.includes(i.id)),
    retired: [...state.retired, ...gone].slice(-MAX_RETIRED),
  };
}

export function liveSubagents(state: RoomSubagents, agentId?: string): SubagentProgress[] {
  return state.items.filter((i) => (i.status === "running" || i.status === "queued") && (!agentId || i.agentId === agentId));
}

export function subagentActivity(item: SubagentProgress): string {
  const last = item.stream.at(-1)?.text;
  if (last) return last;
  return item.status === "queued" ? "queued" : "waiting for activity";
}
```

Note one deliberate difference from upstream's `reconcileSubagentSnapshot`: a row the snapshot
already has keeps its existing stream (`if (prev) projected.stream = prev.stream`), because
`toProgress` from a snapshot record would otherwise append its `last_tool`. This is upstream's
stated rule ("a repeated snapshot must not append over newer live text"). Upstream enforces it by
seeding only cold rows. The test pins it.

- [ ] **Step 4: Add the live event type**

In `live/types.ts`, make four edits next to `room.browser.frame`:
1. `| "room.subagent.event"` in `LiveEventType`.
2. `"room.subagent.event",` in `LIVE_EVENT_TYPES`.
3. The payload type and the envelope union entry:

```ts
export type RoomSubagentEvent = {
  roomId: string;
  agentId: string;
  events: { type: string; seq: number; payload: Record<string, unknown> }[];
};
```
and `| LiveEventEnvelope<"room.subagent.event", RoomSubagentEvent>`.

4. The validator case:

```ts
case "room.subagent.event":
  return str(payload.roomId) && str(payload.agentId) && Array.isArray(payload.events)
    && payload.events.every((e: unknown) => isObj(e) && str(e.type) && typeof e.seq === "number" && isObj(e.payload));
```

Export the new names from `packages/shared/src/index.ts`, next to the `subagentStrip` export block:
`EMPTY_ROOM_SUBAGENTS, applySubagentEvent, liveSubagents, pruneFinishedSubagents, reconcileSubagentSnapshot, subagentActivity`
and the types `RoomSubagents, SubagentProgress, SubagentStatus`.

- [ ] **Step 5: Run the tests**

Run: `cd packages/shared && npx vitest run src/messaging/subagents.test.ts src/messaging/roomActivity.test.ts && npx tsc --noEmit -p .`
Expected: PASS, and no type errors.

- [ ] **Step 6: Commit**

```bash
git add packages/shared/src/messaging/subagents.ts packages/shared/src/messaging/subagents.test.ts packages/shared/src/live/types.ts packages/shared/src/index.ts packages/shared/src/messaging/roomActivity.test.ts
git commit -m "feat(shared): room.subagent.event and the Hermes subagent reducer"
```

---

### Task 6: Shared: client methods, `useRooms` wiring, snapshot and tail hooks

**Files:**
- Modify: `packages/shared/src/messaging/client.ts` (the interface ~162 and its implementation ~307)
- Modify: `packages/shared/src/messaging/useRooms.ts` (the live handler at ~807, the return value at ~1390)
- Create: `packages/shared/src/messaging/useSubagentTail.ts`
- Modify: every `MessagingClient` mock (listed below)
- Test: `packages/shared/src/messaging/client.test.ts`, `packages/shared/src/messaging/useRooms.test.tsx`, `packages/shared/src/messaging/useSubagentTail.test.tsx`

**Interfaces:**
- Consumes: Task 4 routes; Task 5 reducer and event type.
- Produces:
  - `MessagingClient` gains four methods:
    - `listSubagents(roomId: string): Promise<{ subagents: Record<string, unknown>[] }>`
    - `subagentTail(roomId: string, agentId: string, subagentId: string): Promise<SubagentTail>`
    - `steerSubagent(roomId: string, agentId: string, subagentId: string, text: string): Promise<{ state: "queued" }>`
    - `stopSubagent(roomId: string, agentId: string, subagentId: string): Promise<{ state: "stopping" }>`
  - `type SubagentTail = { subagentId: string; available: boolean; text: string; truncated: boolean }`
  - `useRooms` returns:
    - `subagents: RoomSubagents` (active room only)
    - `refreshSubagents(): Promise<void>`
    - `steerSubagent(agentId, subagentId, text)`
    - `stopSubagent(agentId, subagentId)`
    - `subagentsPollVisible(visible: boolean): void` (the strip says whether it is on screen)
  - `useSubagentTail(client: MessagingClient, target: { roomId; agentId; subagentId } | null): { tail: SubagentTail | null }`,
    which polls every 2 s while `target` is non-null.

- [ ] **Step 1: Write the failing tests**

In `client.test.ts`, following the existing `steerRun` case:

```ts
it("calls the subagent routes", async () => {
  await client.listSubagents("r1");
  await client.subagentTail("r1", "a1", "sa-1");
  await client.steerSubagent("r1", "a1", "sa-1", "focus");
  await client.stopSubagent("r1", "a1", "sa-1");
  expect(api.get).toHaveBeenCalledWith("/messaging/rooms/r1/subagents");
  expect(api.get).toHaveBeenCalledWith("/messaging/rooms/r1/agents/a1/subagents/sa-1/tail");
  expect(api.post).toHaveBeenCalledWith("/messaging/rooms/r1/agents/a1/subagents/sa-1/steer", { text: "focus" });
  expect(api.post).toHaveBeenCalledWith("/messaging/rooms/r1/agents/a1/subagents/sa-1/stop", {});
});
```

In `useRooms.test.tsx`, using that file's existing harness for emitting live events and its
`flushPromises`/fake-timer helpers (read the top of the file first, and reuse its names):

```ts
it("room.subagent.event for the active room fills subagents; run_active prunes that agent's finished rows", async () => {
  // render with active room R, emit:
  emit({ type: "room.subagent.event", payload: { roomId: R, agentId: "a1", events: [
    { type: "subagent.start", seq: 1, payload: { subagent_id: "sa-1", goal: "Map" } },
    { type: "subagent.complete", seq: 2, payload: { subagent_id: "sa-2", goal: "Done", status: "completed" } },
  ] } });
  expect(result.current.subagents.items.map((i) => i.id)).toEqual(["sa-1", "sa-2"]);
  emit(runFrame({ roomId: R, agentId: "a1", frame: { type: "run_active", active: true } }));
  expect(result.current.subagents.items.map((i) => i.id)).toEqual(["sa-1"]);
});

it("snapshots on room open, polls every 5 s only while visible with a live row, and stops after 3 failures", async () => {
  vi.useFakeTimers();
  client.listSubagents.mockResolvedValue({ subagents: [{ subagent_id: "sa-1", agent_id: "a1", goal: "g", status: "running" }] });
  // render with active room R
  await vi.advanceTimersByTimeAsync(0);
  expect(client.listSubagents).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(10_000);
  expect(client.listSubagents).toHaveBeenCalledTimes(1); // not visible yet
  act(() => result.current.subagentsPollVisible(true));
  await vi.advanceTimersByTimeAsync(5_000);
  expect(client.listSubagents).toHaveBeenCalledTimes(2);
  client.listSubagents.mockRejectedValue(new Error("503"));
  await vi.advanceTimersByTimeAsync(20_000);
  expect(client.listSubagents).toHaveBeenCalledTimes(5); // 3 failures, then it stops
});
```

`useSubagentTail.test.tsx`:

```tsx
import { renderHook, act } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { useSubagentTail } from "./useSubagentTail";

describe("useSubagentTail", () => {
  it("polls every 2 s while a target is set and stops when cleared", async () => {
    vi.useFakeTimers();
    const client = { subagentTail: vi.fn().mockResolvedValue({ subagentId: "sa-1", available: true, text: "hi", truncated: false }) } as never;
    const { result, rerender } = renderHook(({ t }) => useSubagentTail(client, t), {
      initialProps: { t: { roomId: "r", agentId: "a", subagentId: "sa-1" } as { roomId: string; agentId: string; subagentId: string } | null },
    });
    await act(() => vi.advanceTimersByTimeAsync(0));
    expect(result.current.tail?.text).toBe("hi");
    await act(() => vi.advanceTimersByTimeAsync(4_000));
    expect((client as { subagentTail: ReturnType<typeof vi.fn> }).subagentTail).toHaveBeenCalledTimes(3);
    rerender({ t: null });
    await act(() => vi.advanceTimersByTimeAsync(10_000));
    expect((client as { subagentTail: ReturnType<typeof vi.fn> }).subagentTail).toHaveBeenCalledTimes(3);
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd packages/shared && npx vitest run src/messaging/client.test.ts src/messaging/useRooms.test.tsx src/messaging/useSubagentTail.test.tsx`
Expected: FAIL, because the methods and the hook are missing.

- [ ] **Step 3: Implement the client methods**

`client.ts`: add the four signatures and the `SubagentTail` type to the interface. The implementation goes next to `steerRun`:

```ts
listSubagents: (roomId) => api.get(`/messaging/rooms/${encodeURIComponent(roomId)}/subagents`) as Promise<{ subagents: Record<string, unknown>[] }>,
subagentTail: (roomId, agentId, subagentId) => api.get(`/messaging/rooms/${encodeURIComponent(roomId)}/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentId)}/tail`) as Promise<SubagentTail>,
steerSubagent: (roomId, agentId, subagentId, text) => api.post(`/messaging/rooms/${encodeURIComponent(roomId)}/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentId)}/steer`, { text }) as Promise<{ state: "queued" }>,
stopSubagent: (roomId, agentId, subagentId) => api.post(`/messaging/rooms/${encodeURIComponent(roomId)}/agents/${encodeURIComponent(agentId)}/subagents/${encodeURIComponent(subagentId)}/stop`, {}) as Promise<{ state: "stopping" }>,
```

- [ ] **Step 4: Wire `useRooms`**

- **State.** Add `const [subagents, setSubagents] = useState<RoomSubagents>(EMPTY_ROOM_SUBAGENTS);`
  and reset it to `EMPTY_ROOM_SUBAGENTS` wherever `activeRoomId` changes (in the same effect that
  resets `runs` for a new room).
- **Live events.** In the live handler effect at ~807, add:

```ts
if (event.type === "room.subagent.event" && event.payload.roomId === activeRoomId) {
  const { agentId, events } = event.payload;
  const now = Date.now();
  setSubagents((s) => [...events].sort((a, b) => a.seq - b.seq)
    .reduce((acc, e) => applySubagentEvent(acc, agentId, e.type, e.payload, now), s));
}
```
- **Turn start.** Inside the existing `room.run.frame` block, add this before `setRuns`:
  `if (payload.frame.type === "run_active" && payload.frame.active) setSubagents((s) => pruneFinishedSubagents(s, payload.agentId));`
  This is upstream's `message.start` boundary. Read the block first to find the exact field path of
  the frame type, since `applyRunFrame` reads it.
- **Snapshot polling.** Add these with `useRef`s:

```ts
const subagentPoll = useRef({ visible: false, failures: 0 });
const refreshSubagents = useCallback(async () => {
  if (!activeRoomId) return;
  const roomId = activeRoomId;
  try {
    const { subagents: children } = await messaging.listSubagents(roomId);
    subagentPoll.current.failures = 0;
    if (roomId === activeRoomIdRef.current) setSubagents((s) => reconcileSubagentSnapshot(s, children, Date.now()));
  } catch {
    subagentPoll.current.failures += 1;
  }
}, [activeRoomId, messaging]);

useEffect(() => { void refreshSubagents(); }, [refreshSubagents]); // hydrate once per room

useEffect(() => {
  const id = setInterval(() => {
    const p = subagentPoll.current;
    if (!p.visible || p.failures >= 3 || !liveSubagents(subagentsRef.current).length) return;
    void refreshSubagents();
  }, 5_000);
  const onFocus = () => { subagentPoll.current.failures = 0; void refreshSubagents(); };
  globalThis.addEventListener?.("focus", onFocus);
  return () => { clearInterval(id); globalThis.removeEventListener?.("focus", onFocus); };
}, [refreshSubagents]);

const subagentsPollVisible = useCallback((visible: boolean) => { subagentPoll.current.visible = visible; }, []);
const steerSubagent = useCallback((agentId: string, subagentId: string, text: string) =>
  activeRoomId ? messaging.steerSubagent(activeRoomId, agentId, subagentId, text) : Promise.reject(new Error("no room")), [activeRoomId, messaging]);
const stopSubagent = useCallback((agentId: string, subagentId: string) =>
  activeRoomId ? messaging.stopSubagent(activeRoomId, agentId, subagentId) : Promise.reject(new Error("no room")), [activeRoomId, messaging]);
```

  - `subagentsRef` mirrors `subagents` (`useRef` updated in an effect).
  - `activeRoomIdRef` is the existing ref if the hook has one. Grep for it. If it is missing, add
    the same mirror.
  - On mobile, `globalThis.addEventListener` is undefined, so the `?.` guards it. Mobile resumes via
    its own AppState handler in Task 8.
- **Return value.** Add `subagents, refreshSubagents, steerSubagent, stopSubagent, subagentsPollVisible`
  to the returned object and to its type.

- [ ] **Step 5: Write `useSubagentTail.ts`**

```ts
import { useEffect, useState } from "react";
import type { MessagingClient, SubagentTail } from "./client";

/** Upstream's transcript pane: the last 16 KB of one subagent, every 2 s while it is open. */
export function useSubagentTail(client: MessagingClient, target: { roomId: string; agentId: string; subagentId: string } | null) {
  const [tail, setTail] = useState<SubagentTail | null>(null);
  const key = target ? `${target.roomId}/${target.agentId}/${target.subagentId}` : "";
  useEffect(() => {
    if (!target) { setTail(null); return; }
    let live = true;
    const read = () => client.subagentTail(target.roomId, target.agentId, target.subagentId)
      .then((t) => { if (live) setTail(t); }).catch(() => {});
    void read();
    const id = setInterval(read, 2_000);
    return () => { live = false; clearInterval(id); };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `key` is the target's identity
  }, [client, key]);
  return { tail };
}
```

Export `useSubagentTail` and `SubagentTail` from `index.ts`.

- [ ] **Step 6: Update every `MessagingClient` mock**

Add `listSubagents: vi.fn().mockResolvedValue({ subagents: [] }), subagentTail: vi.fn(), steerSubagent: vi.fn(), stopSubagent: vi.fn()` (on mobile, `jest.fn()` with the same resolved value) to:
- `packages/shared/src/messaging/useRooms.test.tsx:42`
- `packages/shared/src/messaging/useRooms.reliability.test.tsx:44`
- `apps/frontend/src/hooks/__tests__/useRooms.test.tsx:58`
- `apps/frontend/src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx:81`
- `apps/mobile/src/app/(app)/room/__tests__/conversation.test.tsx:171`
- `apps/mobile/src/app/(app)/room/__tests__/thread.test.tsx:106`
- `apps/mobile/src/app/(app)/__tests__/session.test.tsx:106`

Then run `grep -rn "steerRun: vi.fn\|steerRun: jest.fn" apps packages` and update any mock the list missed.

- [ ] **Step 7: Run the tests**

Run: `cd packages/shared && npx vitest run && npx tsc --noEmit -p .; echo exit=$?`
Expected: all PASS, `exit=0`.

- [ ] **Step 8: Commit**

```bash
git add packages/shared apps/frontend/src/hooks/__tests__/useRooms.test.tsx apps/frontend/src/components/messaging/__tests__/MessagingWorkspaceContext.test.tsx apps/mobile/src/app
git commit -m "feat(shared): subagent client routes, live room state, snapshot and tail polling"
```

---

### Task 7: Web/desktop: strip above the composer and the card on the delegate row

**Files:**
- Create: `apps/frontend/src/components/messaging/RoomSubagentStrip.tsx`
- Create: `apps/frontend/src/components/messaging/DelegateCard.tsx`
- Modify: `apps/frontend/src/components/messaging/RoomPanel.tsx` (render the strip right above `<RoomComposer {...composer}` at ~806; render `DelegateCard` in the live-run article at ~712 and the ended run at ~695)
- Modify: `apps/frontend/src/components/messaging/MessagingWorkspaceContext.tsx` (~895: pass `subagents`, `steerSubagent`, `stopSubagent`, `subagentsPollVisible`, `messaging` client through to `RoomPanel` the way `onAbortRun` is passed)
- Modify: `apps/frontend/src/components/messaging/messaging.css` (strip styles next to `.native-session-panel` ~617)
- Test: `apps/frontend/src/components/messaging/__tests__/RoomSubagentStrip.test.tsx`, `apps/frontend/src/components/messaging/__tests__/DelegateCard.test.tsx`

**Interfaces:**
- Consumes: `RoomSubagents`, `liveSubagents`, `subagentActivity` and `sessionClock(ms, precise)`
  from `@lightbulb/shared`, and `useSubagentTail` (Task 6).
- Produces:
  - `RoomSubagentStrip({ state, agentName, client, roomId, onSteer, onStop, onVisible })`
  - `DelegateCard({ state, agentId, delegationId, agentLive })`

- [ ] **Step 1: Write the failing tests**

```tsx
// RoomSubagentStrip.test.tsx
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { applySubagentEvent, EMPTY_ROOM_SUBAGENTS } from "@lightbulb/shared";
import { RoomSubagentStrip } from "../RoomSubagentStrip";

const mk = (n: number) => Array.from({ length: n }).reduce<typeof EMPTY_ROOM_SUBAGENTS>((s, _, i) =>
  applySubagentEvent(s, "a1", "subagent.tool", { subagent_id: `sa-${i}`, goal: `Track ${i}`, tool_name: "web_search", tool_preview: "q" }, Date.now()),
  EMPTY_ROOM_SUBAGENTS);
const props = (state = mk(3)) => ({
  state, roomId: "r1", agentName: () => "Forge", onVisible: vi.fn(),
  onSteer: vi.fn().mockResolvedValue({ state: "queued" }), onStop: vi.fn().mockResolvedValue({ state: "stopping" }),
  client: { subagentTail: vi.fn().mockResolvedValue({ subagentId: "sa-0", available: true, text: "line 1\nline 2", truncated: false }) } as never,
});

describe("RoomSubagentStrip", () => {
  it("renders nothing without live subagents", () => {
    const { container } = render(<RoomSubagentStrip {...props(EMPTY_ROOM_SUBAGENTS)} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("shows the count, three rows and +N more", () => {
    render(<RoomSubagentStrip {...props(mk(5))} />);
    expect(screen.getByRole("region", { name: "Running subagents" })).toBeInTheDocument();
    expect(screen.getByText("Subagents (5)")).toBeInTheDocument();
    expect(screen.getAllByText(/Track \d/)).toHaveLength(3);
    expect(screen.getByRole("button", { name: "+2 more" })).toBeInTheDocument();
  });

  it("a row opens steer, stop and the transcript; steer says queued", async () => {
    const p = props();
    render(<RoomSubagentStrip {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /Track 0/ }));
    await waitFor(() => expect(screen.getByText(/line 2/)).toBeInTheDocument());
    fireEvent.change(screen.getByRole("textbox", { name: "Steer Track 0" }), { target: { value: "focus on Library" } });
    fireEvent.click(screen.getByRole("button", { name: "Steer" }));
    await waitFor(() => expect(screen.getByText("Steer queued")).toBeInTheDocument());
    expect(p.onSteer).toHaveBeenCalledWith("a1", "sa-0", "focus on Library");
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(p.onStop).toHaveBeenCalledWith("a1", "sa-0");
  });

  it("a finished subagent's steer shows the 404 copy", async () => {
    const p = { ...props(), onSteer: vi.fn().mockRejectedValue(Object.assign(new Error("gone"), { code: "SUBAGENT_NOT_FOUND" })) };
    render(<RoomSubagentStrip {...p} />);
    fireEvent.click(screen.getByRole("button", { name: /Track 0/ }));
    fireEvent.change(screen.getByRole("textbox", { name: "Steer Track 0" }), { target: { value: "x" } });
    fireEvent.click(screen.getByRole("button", { name: "Steer" }));
    await waitFor(() => expect(screen.getByText("That subagent has already finished.")).toBeInTheDocument());
  });
});
```

```tsx
// DelegateCard.test.tsx
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { applySubagentEvent, EMPTY_ROOM_SUBAGENTS } from "@lightbulb/shared";
import { DelegateCard } from "../DelegateCard";

describe("DelegateCard", () => {
  const s = [0, 1].reduce((acc, i) => applySubagentEvent(acc, "a1", i ? "subagent.complete" : "subagent.start",
    { subagent_id: `sa-${i}`, goal: `Track ${i}`, delegation_id: "d-1", model: "gpt-5.6-sol", status: i ? "completed" : undefined, duration_seconds: 12 }, 1000), EMPTY_ROOM_SUBAGENTS);

  it("one row per child of this delegation with status and meta", () => {
    render(<DelegateCard state={s} agentId="a1" delegationId="d-1" agentLive />);
    expect(screen.getByText("Track 0")).toBeInTheDocument();
    expect(screen.getByLabelText("Running")).toBeInTheDocument();
    expect(screen.getByLabelText("Completed")).toBeInTheDocument();
    expect(screen.getByText(/gpt-5.6-sol · 12s/)).toBeInTheDocument();
  });

  it("with no live view, a child that is neither terminal nor live shows the static dispatched dot, never a spinner", () => {
    render(<DelegateCard state={EMPTY_ROOM_SUBAGENTS} agentId="a1" delegationId="d-1" agentLive={false} />);
    expect(screen.getByLabelText("Dispatched")).toBeInTheDocument();
    expect(screen.queryByLabelText("Running")).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run them to verify they fail**

Run: `cd apps/frontend && npx vitest run src/components/messaging/__tests__/RoomSubagentStrip.test.tsx src/components/messaging/__tests__/DelegateCard.test.tsx`
Expected: FAIL, because the modules are missing.

- [ ] **Step 3: Write `RoomSubagentStrip.tsx`**

```tsx
"use client";
import { useEffect, useState } from "react";
import { liveSubagents, sessionClock, subagentActivity, useSubagentTail, type MessagingClient, type RoomSubagents, type SubagentProgress } from "@lightbulb/shared";

const PREVIEW = 3; // upstream Desktop previews three workers
const GONE = "That subagent has already finished.";

function useNow(on: boolean) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const id = setInterval(() => setNow(Date.now()), 1_000);
    return () => clearInterval(id);
  }, [on]);
  return now;
}

function Detail({ item, roomId, client, onSteer, onStop }: {
  item: SubagentProgress; roomId: string; client: MessagingClient;
  onSteer: (agentId: string, id: string, text: string) => Promise<unknown>;
  onStop: (agentId: string, id: string) => Promise<unknown>;
}) {
  const { tail } = useSubagentTail(client, { roomId, agentId: item.agentId, subagentId: item.id });
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const fail = (e: unknown) => setNote((e as { code?: string })?.code === "SUBAGENT_NOT_FOUND" ? GONE : "Could not reach the agent. Try again.");
  return <div className="room-subagent-detail">
    <form className="room-subagent-steer" onSubmit={(e) => {
      e.preventDefault();
      const value = text.trim();
      if (!value) return;
      onSteer(item.agentId, item.id, value).then(() => { setText(""); setNote("Steer queued"); }, fail);
    }}>
      <input aria-label={`Steer ${item.goal}`} value={text} onChange={(e) => setText(e.target.value)} placeholder="Steer this subagent" />
      <button type="submit" disabled={!text.trim()}>Steer</button>
      <button type="button" onClick={() => onStop(item.agentId, item.id).then(() => setNote("Stopping…"), fail)}>Stop</button>
    </form>
    {note && <p className="room-subagent-note" role="status">{note}</p>}
    <pre className="room-subagent-tail">{tail?.available ? tail.text : "No transcript yet."}</pre>
  </div>;
}

export function RoomSubagentStrip({ state, roomId, agentName, client, onSteer, onStop, onVisible }: {
  state: RoomSubagents; roomId: string; agentName: (agentId: string) => string; client: MessagingClient;
  onSteer: (agentId: string, id: string, text: string) => Promise<unknown>;
  onStop: (agentId: string, id: string) => Promise<unknown>;
  onVisible: (visible: boolean) => void;
}) {
  const live = liveSubagents(state);
  const [open, setOpen] = useState(true);
  const [all, setAll] = useState(false);
  const [picked, setPicked] = useState<string | null>(null);
  const now = useNow(live.length > 0);
  const visible = live.length > 0 && open;
  useEffect(() => { onVisible(visible); return () => onVisible(false); }, [visible, onVisible]);
  if (!live.length) return null;
  const rows = all ? live : live.slice(0, PREVIEW);
  const pickedItem = live.find((i) => i.id === picked);
  return <div className="room-subagent-strip" role="region" aria-label="Running subagents">
    <button type="button" className="room-subagent-heading" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
      Subagents ({live.length})
    </button>
    {open && <>
      {rows.map((item) => <button key={item.id} type="button" className="room-subagent-row" aria-expanded={picked === item.id}
        onClick={() => setPicked((p) => (p === item.id ? null : item.id))}>
        <span className="room-subagent-spinner" aria-hidden="true" />
        <span className="room-subagent-goal">{agentName(item.agentId)} · {item.goal}</span>
        <span className="room-subagent-activity">{subagentActivity(item)}</span>
        <span className="room-subagent-meta">{sessionClock(now - item.startedAt, true)}</span>
      </button>)}
      {!all && live.length > PREVIEW && <button type="button" className="room-subagent-row room-subagent-more" onClick={() => setAll(true)}>+{live.length - PREVIEW} more</button>}
      {pickedItem && <Detail item={pickedItem} roomId={roomId} client={client} onSteer={onSteer} onStop={onStop} />}
    </>}
  </div>;
}
```

- [ ] **Step 4: Write `DelegateCard.tsx`**

```tsx
"use client";
import type { RoomSubagents, SubagentStatus } from "@lightbulb/shared";
import { subagentActivity } from "@lightbulb/shared";

const ICON: Record<SubagentStatus | "dispatched", { glyph: string; label: string; cls: string }> = {
  running: { glyph: "", label: "Running", cls: "is-running" },
  queued: { glyph: "○", label: "Queued", cls: "is-queued" },
  completed: { glyph: "✓", label: "Completed", cls: "is-ok" },
  failed: { glyph: "✗", label: "Failed", cls: "is-error" },
  interrupted: { glyph: "■", label: "Stopped", cls: "is-warn" },
  dispatched: { glyph: "•", label: "Dispatched", cls: "is-muted" },
};

/** Upstream delegate.tsx: one row per child of this fan-out. A child nothing is watching shows a
 *  static dot and never spins. */
export function DelegateCard({ state, agentId, delegationId, agentLive }: {
  state: RoomSubagents; agentId: string; delegationId: string; agentLive: boolean;
}) {
  const children = state.items.filter((i) => i.agentId === agentId && i.delegationId === delegationId)
    .sort((a, b) => a.taskIndex - b.taskIndex);
  if (!children.length) {
    return agentLive ? null : <div className="room-delegate-card"><p className="room-delegate-row">
      <span className={`room-delegate-icon ${ICON.dispatched.cls}`} aria-label={ICON.dispatched.label}>{ICON.dispatched.glyph}</span>
      <span className="room-delegate-goal">Background subagents dispatched</span></p></div>;
  }
  return <div className="room-delegate-card">{children.map((c) => {
    const icon = ICON[c.status];
    const meta = [c.model, c.durationSeconds != null ? `${Math.round(c.durationSeconds)}s` : null].filter(Boolean).join(" · ");
    return <div key={c.id} className="room-delegate-row">
      <span className={`room-delegate-icon ${icon.cls}`} aria-label={icon.label}>{icon.glyph}</span>
      <span className="room-delegate-goal">{c.goal}</span>
      {meta && <span className="room-delegate-meta">{meta}</span>}
      <span className="room-delegate-activity">{subagentActivity(c)}</span>
    </div>;
  })}</div>;
}
```

- [ ] **Step 5: Mount them and add CSS**

- **`MessagingWorkspaceContext.tsx` (~895).** Pass these alongside `onAbortRun`:
  - `subagents: rooms.subagents`
  - `onSteerSubagent: rooms.steerSubagent`
  - `onStopSubagent: rooms.stopSubagent`
  - `onSubagentsVisible: rooms.subagentsPollVisible`
  - `messagingClient` (the client instance the context already holds; grep for `createMessagingClient`/`messaging` in that file)

  Add the matching optional props to `RoomPanel`'s props type.
- **`RoomPanel.tsx`, the strip.** Right above `<RoomComposer {...composer}` (~806):

```tsx
{subagents && messagingClient && <RoomSubagentStrip state={subagents} roomId={room.id} agentName={agentName}
  client={messagingClient} onSteer={onSteerSubagent!} onStop={onStopSubagent!} onVisible={onSubagentsVisible!} />}
```

  Use the panel's existing agent-name lookup (grep `displayName` in `RoomPanel.tsx` for the helper
  `renderRun` uses).
- **`RoomPanel.tsx`, the card.** Under `<ToolActivityTrace calls={run.tools} />` at both sites (~695, ~712):

```tsx
{subagents && run.tools.filter((t) => t.name === "delegate_task").map((t) => {
  const delegationId = delegationIdOf(t.result);
  return delegationId ? <DelegateCard key={t.id} state={subagents} agentId={run.agentId} delegationId={delegationId} agentLive={run.active} /> : null;
})}
```

  `delegationIdOf` goes in `DelegateCard.tsx` and is exported from there:

```ts
export function delegationIdOf(result: unknown): string | null {
  const parse = (v: unknown): unknown => { if (typeof v !== "string") return v; try { return JSON.parse(v); } catch { return null; } };
  const r = parse(result) as { delegation_id?: unknown } | null;
  return r && typeof r.delegation_id === "string" ? r.delegation_id : null;
}
```

  Read `RunToolCall.result` in `timeline.ts` to confirm whether the result arrives as a string or an
  object. `delegationIdOf` handles both. Also check the field name of the "run is live" flag on the
  run view (`run.active` or similar) and use the real one.
- **Tests.** Add a unit test for `delegationIdOf` to `DelegateCard.test.tsx`:
  `delegationIdOf('{"status":"dispatched","delegation_id":"d-1"}') === "d-1"`, and `delegationIdOf("oops") === null`.
- **`messaging.css`**, next to `.native-session-panel`:

```css
.room-subagent-strip { padding: 4px 16px; border-top: 1px solid var(--border); font-size: .8125rem; }
.room-subagent-heading { background: none; border: 0; color: var(--fg-soft); font: inherit; font-weight: 600; padding: 2px 0; cursor: pointer; }
.room-subagent-row { display: flex; width: 100%; gap: 8px; align-items: baseline; padding: 2px 0; background: none; border: 0; color: inherit; text-align: left; font: inherit; cursor: pointer; }
.room-subagent-spinner { width: 8px; height: 8px; border-radius: 50%; border: 1.5px solid var(--live); border-top-color: transparent; animation: room-subagent-spin 1s linear infinite; flex: none; align-self: center; }
@keyframes room-subagent-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .room-subagent-spinner { animation: none; border-top-color: var(--live); } }
.room-subagent-goal { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 40%; }
.room-subagent-activity { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--fg-mute); font-family: var(--font-mono), ui-monospace, monospace; }
.room-subagent-meta { white-space: nowrap; color: var(--fg-mute); font-variant-numeric: tabular-nums; }
.room-subagent-more { color: var(--fg-mute); }
.room-subagent-detail { padding: 6px 0 6px 16px; }
.room-subagent-steer { display: flex; gap: 6px; }
.room-subagent-steer input { flex: 1; min-width: 0; }
.room-subagent-note { color: var(--fg-soft); margin: 4px 0; }
.room-subagent-tail { max-height: 16rem; overflow: auto; white-space: pre-wrap; font-size: .75rem; background: var(--plane-raised); padding: 8px; border-radius: 6px; }
.room-delegate-card { margin: 4px 0; padding: 6px 8px; border: 1px solid var(--border); border-radius: 6px; }
.room-delegate-row { display: flex; gap: 8px; align-items: baseline; font-size: .8125rem; }
.room-delegate-goal { font-weight: 600; }
.room-delegate-meta, .room-delegate-activity { color: var(--fg-mute); }
.room-delegate-activity { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.room-delegate-icon.is-ok { color: var(--live); }
.room-delegate-icon.is-error { color: var(--alert); }
.room-delegate-icon.is-warn, .room-delegate-icon.is-muted, .room-delegate-icon.is-queued { color: var(--fg-mute); }
.room-delegate-icon.is-running { width: 8px; height: 8px; border-radius: 50%; border: 1.5px solid var(--live); border-top-color: transparent; animation: room-subagent-spin 1s linear infinite; }
```

Check each token (`--border`, `--fg-soft`, `--fg-mute`, `--live`, `--alert`, `--plane-raised`) in
`src/app/globals.css`, and swap any missing one for its nearest existing token. Never use a literal.

- [ ] **Step 6: Run the tests**

Run: `cd apps/frontend && npx vitest run src/components/messaging src/__tests__/brandTokens.test.ts && npx tsc --noEmit; echo exit=$?`
Expected: PASS, `exit=0`.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/messaging
git commit -m "feat(web): background subagent strip above the composer and delegate card"
```

---

### Task 8: iPhone: strip, transcript sheet, delegate card

**Files:**
- Create: `apps/mobile/src/components/room/RoomSubagentStrip.tsx`
- Create: `apps/mobile/src/components/room/DelegateCard.tsx`
- Modify: `apps/mobile/src/app/(app)/room/[roomId]/index.tsx` (strip right above `<RoomComposer` ~677; pass `subagents` to `RunBlock` ~517/~621)
- Modify: `apps/mobile/src/components/room/RunBlock.tsx` (render `DelegateCard` for `delegate_task` tools, in the block that renders `run.tools` ~141-166)
- Test: `apps/mobile/src/components/room/__tests__/RoomSubagentStrip.test.tsx`

**Interfaces:**
- Consumes:
  - `rooms.subagents`, `rooms.steerSubagent`, `rooms.stopSubagent` and `rooms.subagentsPollVisible` (Task 6)
  - `useSubagentTail`, `liveSubagents`, `subagentActivity` and `sessionClock`
  - `delegationIdOf`, which this task moves into `@lightbulb/shared`: put it in
    `packages/shared/src/messaging/subagents.ts`, export it, and import it on web too, deleting the
    web copy from Task 7.
- Produces:
  - `RoomSubagentStrip({ state, roomId, agentName, client, onSteer, onStop, onVisible })`, the same
    contract as web
  - `DelegateCard({ state, agentId, delegationId, agentLive })`

- [ ] **Step 1: Write the failing test**

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { applySubagentEvent, EMPTY_ROOM_SUBAGENTS } from "@lightbulb/shared";
import { RoomSubagentStrip } from "../RoomSubagentStrip";

const state = [0, 1, 2, 3].reduce((s, i) => applySubagentEvent(s, "a1", "subagent.tool",
  { subagent_id: `sa-${i}`, goal: `Track ${i}`, tool_name: "web_search", tool_preview: "q" }, Date.now()), EMPTY_ROOM_SUBAGENTS);

it("shows three rows and +1 more; a row opens a sheet with steer, stop and transcript", async () => {
  const onSteer = jest.fn().mockResolvedValue({ state: "queued" });
  const onStop = jest.fn().mockResolvedValue({ state: "stopping" });
  const client = { subagentTail: jest.fn().mockResolvedValue({ subagentId: "sa-0", available: true, text: "line 2", truncated: false }) } as never;
  render(<RoomSubagentStrip state={state} roomId="r1" agentName={() => "Forge"} client={client} onSteer={onSteer} onStop={onStop} onVisible={jest.fn()} />);
  expect(screen.getByText("Subagents (4)")).toBeTruthy();
  expect(screen.getByText("+1 more")).toBeTruthy();
  fireEvent.press(screen.getByLabelText(/Track 0/));
  await waitFor(() => expect(screen.getByText(/line 2/)).toBeTruthy());
  fireEvent.changeText(screen.getByLabelText("Steer Track 0"), "focus");
  fireEvent.press(screen.getByLabelText("Steer"));
  await waitFor(() => expect(screen.getByText("Steer queued")).toBeTruthy());
  fireEvent.press(screen.getByLabelText("Stop"));
  expect(onStop).toHaveBeenCalledWith("a1", "sa-0");
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/mobile && npx jest src/components/room/__tests__/RoomSubagentStrip.test.tsx`
Expected: FAIL, because the module is missing.

- [ ] **Step 3: Implement**

The strip has the same behaviour as web (Task 7, Step 3), built from React Native primitives:
- **Rows.** Each row is a `Pressable` with `accessibilityLabel={`${agentName(item.agentId)} · ${item.goal}`}`.
  It shows an `ActivityIndicator size="small"` (static `○` when `AccessibilityInfo.isReduceMotionEnabled`),
  the goal, the activity line (`numberOfLines={1}`), and `sessionClock(now - item.startedAt, true)`
  on a 1 s `useNow`.
- **Detail.** The detail opens in the native sheet pattern `RunBlock`/mobile already uses (see the
  `mobile-native-sheets` memory: grep `apps/mobile/src/components` for the existing sheet component
  and reuse it). It holds a `TextInput` with `accessibilityLabel={`Steer ${item.goal}`}`, `Pressable`s
  labelled "Steer" and "Stop", a status line, and a `ScrollView` with the tail text in the
  monospace style `RunBlock` uses for tool output. Steer and Stop errors use the same copy as web
  ("That subagent has already finished." on `SUBAGENT_NOT_FOUND`).
- **Visibility.** Call `onVisible(true)` while mounted with live rows and `onVisible(false)` on
  unmount. Also resume polling on app foreground: in the room screen, call `rooms.refreshSubagents()`
  from the existing `AppState` "active" handler (grep `AppState` in the room screen, and add one if
  there is none).
- **Colours.** Use `chatPalette` from `apps/mobile/src/config/theme.ts`. No literals.

`DelegateCard` uses the same logic as web, with `Text` rows and icons from the same `ICON` map. It
lives in `RunBlock`, under the tools list, for every `run.tools` entry whose `name === "delegate_task"`
and whose `delegationIdOf(t.result)` is non-null.

In the room screen, render the strip right above `<RoomComposer` with
`state={rooms.subagents} roomId={roomId} agentName={agentName} client={messagingClient}` and the
rooms callbacks. Find the screen's client instance: grep `createMessagingClient` or `useMessaging`
in `apps/mobile/src`. Pass `subagents={rooms.subagents}` to both `RunBlock` sites, and add the
optional prop to `RunBlock`.

- [ ] **Step 4: Run the tests**

Run: `cd apps/mobile && npx jest src/components/room src/app/\(app\)/room && npx tsc --noEmit; echo exit=$?`
Expected: PASS, `exit=0`.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile packages/shared apps/frontend/src/components/messaging
git commit -m "feat(mobile): background subagent strip, sheet and delegate card"
```

---

### Task 9: Ship: PRs, image, dev acceptance, prod, cleanup

**Files:**
- Delete: `docs/superpowers/specs/2026-10-09-background-subagents-design.md`, `docs/superpowers/plans/2026-10-09-background-subagents.md` (in the final PR, once the feature ships)
- Modify: `hermes-version.json` (`dev.tag`, then `prod.tag`) in separate PRs

- [ ] **Step 1: Open the PR.** Push the branch and open one PR to `main`. Its body states the
  rollout order and links the spec. Wait for Codex. Check all four surfaces (reviews, inline
  comments, issue comments, reactions), then merge per the merge memory.
- [ ] **Step 2: Image.** The merge triggers `build-hermes-image.yml`. Find the `h-<date>-<sha7>` tag
  it pushes, then open a PR flipping `hermes-version.json`'s `dev.tag` to it and merge it.
- [ ] **Step 3: Roll dev runtimes:** `apps/backend/scripts/roll_hermes_runtimes.py` against dev.
  Verify with `aws bedrock-agentcore-control get-agent-runtime` that the `containerUri` carries the
  new tag.
- [ ] **Step 4: Dev acceptance**, with Chrome driving dev.thelightbulb.company as a dev test account
  signed in with a Clerk ticket:
  - Ask the CEO in a DM: "Research three things in parallel with delegate_task: A, B, C."
  - Screenshot the strip with three rows, activity ticking and elapsed time counting.
  - Open one row: the transcript fills.
  - Steer one: "Steer queued".
  - Stop one: the row leaves the strip, and the follow-up reply notes it was interrupted.
  - The follow-up reply lands, and the strip is empty.
  - Reload during the run: the strip comes back from the snapshot, and the delegate card shows
    either rows or the static dot, never a spinner without a live row.
  - Repeat on the iPhone simulator (`mobile-simulator-driving-recipe` memory).
  - Post the screenshots on the PR.
- [ ] **Step 5: Prod.** Check that `deploy.yml` reads 0 in-flight runs, then dispatch it. Flip
  `prod.tag` in a PR. Roll prod runtimes, rolling the founder runtime only while
  `subagent.list` / `followups.busy()` show no live delegations.
- [ ] **Step 6: Clean up.** Delete the spec and this plan in a follow-up PR, then remove the
  worktree and the remote branch.
```
