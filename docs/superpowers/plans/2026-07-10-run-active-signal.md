# Run-Active Signal (server-truth busy indicator) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the `/chat` "is it working?" indicator reflect OpenClaw's authoritative run-state, so the busy signal stays lit for the whole run — including the silent windows (long tool execution, the beat between a tool result and the next tokens, the tool-approval pause) — instead of collapsing whenever the token stream goes quiet.

**Architecture:** OpenClaw's gateway already computes an authoritative per-session run-state from its live abort-controller registry and broadcasts it as a `sessions.changed` event whose payload **embeds** `hasActiveRun` (+ a `status` enum). Today Isol8 never subscribes to or consumes that signal — our frontend derives "busy" purely from chat-stream events, so it goes silent in every quiet window. This plan (1) subscribes the pooled gateway connection to session events, (2) forwards a deduped, webchat-scoped `run_active` frame to the initiating member on every `sessions.changed` transition, and (3) has the frontend treat `busy = isStreaming || containerRunActive`. The existing `run_ended` terminal frame remains the authoritative close, so a dropped `sessions.changed` (it is sent `dropIfSlow`) still self-heals at run end.

**Tech Stack:** Python 3 / FastAPI (backend `core/gateway/connection_pool.py`), pytest. React 19 / TypeScript (frontend `hooks/useAgentChat.ts`, `components/chat/StreamingStatusBar.tsx`), vitest + @testing-library/react.

## Global Constraints

- **Scope to webchat only.** `sessions.changed` fires for every session on the container (cron, channels, Paperclip-adapter runs). Forward `run_active` ONLY for `source == "webchat"` session keys, demuxed to the initiating member via the existing `target_member` path. Reuse the existing gating flags in `_handle_message` (`is_channel_session`, `is_cron_session`, `is_isolated_session`). This is a security/correctness boundary: a member-less non-webchat key would broadcast to the whole org.
- **`_run_state` MUST be a separate map from `_active_runs`.** `_active_runs` drives the parked-vs-dead reconnect decision (`is_alive` / `has_active_runs`) and MUST keep its existing lifecycle (mark on `chat.send`, clear on true terminal). Do not read or mutate `_active_runs` from the `sessions.changed` path, and do not gate `_active_runs` on `sessions.changed`.
- **No `sessions.list` RPC from inside `_handle_message`.** `_handle_message` runs inside the reader loop; awaiting an RPC there deadlocks (the same loop resolves the response). The `sessions.changed` payload already carries `hasActiveRun`, so read it directly — never issue a follow-up RPC on the event path.
- **Forward on transitions only.** Dedup against the last-forwarded value per session key so a burst of `sessions.changed` (message/transcript events also fire it) doesn't spam identical frames.
- **`sessions.subscribe` on every connect AND reconnect.** OpenClaw only broadcasts to subscribed connIds and drops the subscription on disconnect. Placing the subscribe inside `connect()` covers reconnect for free because `reopen()` calls `connect()`.
- Backend tests: `cd apps/backend && uv run pytest <path> -v`. Frontend tests are RAM-heavy — run SINGLE FILES only: `cd apps/frontend && node_modules/.bin/vitest run <path>`. After adding/altering frontend tests, run `node_modules/.bin/tsc --noEmit`.
- Frontend CI is Lint & Build only (no vitest in CI) — run the touched test files + tsc locally before pushing.

---

## Wire reference (verified against fork `2026.7.1-beta.3` + current `main`)

**`sessions.subscribe`** — gateway RPC, params `{}`, response `{subscribed: boolean}`. Mandatory to receive `sessions.changed` (fork `src/gateway/server-methods/sessions.ts:906`).

**`sessions.changed`** — protocol-v4 frame `{type:"event", event:"sessions.changed", payload:{…}}`. Payload embeds flat per-session fields including `sessionKey`, `status` (`"running"|"done"|"failed"|"killed"|"timeout"`), and `hasActiveRun` (bool, present in practice from live abort-controller state) (fork `src/gateway/session-event-payload.ts:16-77`). It is NOT a bare re-read signal. A webchat `chat.send` run reports `hasActiveRun:true` from run start, stays true across the tool-approval pause, and flips false at the true terminal (fork `src/gateway/session-active-runs.ts`, `chat-abort.ts:226`).

**Isol8 session key** for webchat: `agent:{agentId}:{userId}` (3-part "General") or `agent:{agentId}:{userId}:{sessionId}` (4-part). Parsed by the existing `_parse_session_key` → `{source, agent_id, member_id}`; `source == "webchat"` for these. The 4-part trailing `sessionId` is extracted exactly as the existing code does at `connection_pool.py:1128`.

**Existing frontend terminal frames** already consumed: `run_ended` (true-terminal close), `done`, `error`. The frontend busy-gate release already keys on these (`useAgentChat.ts:829`).

---

## Task 1: Backend — subscribe the pooled connection to session events

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (add `_subscribe_session_events`; call it in `connect()` after the reader loop starts, ~line 483)
- Test: `apps/backend/tests/unit/gateway/test_connection_pool_run_active.py` (new)

**Interfaces:**
- Produces: `GatewayConnection._subscribe_session_events(self) -> None` — sends a fire-and-forget `sessions.subscribe` req frame on `self._ws`. Called once per successful `connect()` (and therefore once per `reopen()`).

- [ ] **Step 1: Write the failing test**

Create `apps/backend/tests/unit/gateway/test_connection_pool_run_active.py`:

```python
import json
import pytest
from core.gateway.connection_pool import GatewayConnection


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.mark.asyncio
async def test_subscribe_session_events_sends_sessions_subscribe():
    conn = GatewayConnection.__new__(GatewayConnection)  # skip __init__ networking
    conn._ws = FakeWS()
    await conn._subscribe_session_events()
    methods = [m.get("method") for m in conn._ws.sent]
    assert "sessions.subscribe" in methods
    frame = next(m for m in conn._ws.sent if m.get("method") == "sessions.subscribe")
    assert frame["type"] == "req"
    assert frame["params"] == {}
    assert frame.get("id")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_connection_pool_run_active.py::test_subscribe_session_events_sends_sessions_subscribe -v`
Expected: FAIL with `AttributeError: 'GatewayConnection' object has no attribute '_subscribe_session_events'`

- [ ] **Step 3: Write minimal implementation**

In `connection_pool.py`, add the method near `_verify_health` (after ~line 563):

```python
    async def _subscribe_session_events(self) -> None:
        """Subscribe this connection to OpenClaw `sessions.changed` broadcasts.

        OpenClaw only broadcasts session events to connections that have called
        `sessions.subscribe` (fork src/gateway/server-methods/sessions.ts:906),
        and drops the subscription on disconnect. Sent on every connect; because
        `reopen()` calls `connect()`, this re-subscribes on reconnect too.
        Fire-and-forget: the `{subscribed:true}` ack is not needed to proceed —
        if it never lands we simply fall back to stream-derived busy state, so
        we don't block connect on it. The ack `res` is harmlessly ignored by
        `_handle_message` (no matching entry in `_pending_rpcs`).
        """
        req_id = str(uuid.uuid4())
        frame = {"type": "req", "id": req_id, "method": "sessions.subscribe", "params": {}}
        try:
            await self._ws.send(json.dumps(frame))
        except Exception:
            logger.warning("sessions.subscribe send failed user=%s", self.user_id, exc_info=True)
```

Then call it in `connect()` immediately after the reader loop starts. Change (around line 483-484):

```python
        self._reader_task = asyncio.create_task(self._reader_loop())
        await self._subscribe_session_events()
        put_metric("gateway.connection", dimensions={"event": "connect"})
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_connection_pool_run_active.py::test_subscribe_session_events_sends_sessions_subscribe -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_connection_pool_run_active.py
git commit -m "feat(gateway): subscribe pooled connection to sessions.changed"
```

---

## Task 2: Backend — forward `run_active` on `sessions.changed` (webchat-scoped, deduped)

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (init `_run_state` map; add a `sessions.changed` branch in `_handle_message`; reset dedup in `_clear_active_run`)
- Test: `apps/backend/tests/unit/gateway/test_connection_pool_run_active.py` (extend)

**Interfaces:**
- Consumes: the existing `_handle_message` locals `event_name`, `payload`, `session_key`, `parsed_key`, `target_member`, `session_source`, `is_channel_session`, `is_cron_session`, `is_isolated_session`, `session_id`, and the helper `_forward_to_frontends(message, target_member)`.
- Produces: forwarded frame shape `{"type": "run_active", "active": bool, "agent_id"?: str, "session_id"?: str}`, demuxed to `target_member`.

- [ ] **Step 1: Write the failing test**

Add to `apps/backend/tests/unit/gateway/test_connection_pool_run_active.py`:

```python
@pytest.mark.asyncio
async def test_sessions_changed_forwards_run_active_for_webchat(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "user_1"
    conn.owner_id = "user_1"
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    forwarded: list[tuple[dict, str | None]] = []

    async def fake_forward(message, target_member_id=None):
        forwarded.append((message, target_member_id))

    monkeypatch.setattr(conn, "_forward_to_frontends", fake_forward)

    evt = {
        "type": "event",
        "event": "sessions.changed",
        "payload": {"sessionKey": "agent:agentA:user_1", "hasActiveRun": True, "status": "running"},
    }
    await conn._handle_message(evt)

    run_active = [m for (m, _t) in forwarded if m.get("type") == "run_active"]
    assert run_active == [{"type": "run_active", "active": True, "agent_id": "agentA"}]


@pytest.mark.asyncio
async def test_sessions_changed_dedupes_and_scopes(monkeypatch):
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "user_1"
    conn.owner_id = "user_1"
    conn._active_runs = {}
    conn._run_state = {}
    conn._side_run_ids = {}
    forwarded: list[dict] = []

    async def fake_forward(message, target_member_id=None):
        forwarded.append(message)

    monkeypatch.setattr(conn, "_forward_to_frontends", fake_forward)

    def changed(key, active):
        return {"type": "event", "event": "sessions.changed",
                "payload": {"sessionKey": key, "hasActiveRun": active}}

    # webchat: active -> forwarded once; duplicate active -> not re-forwarded
    await conn._handle_message(changed("agent:agentA:user_1", True))
    await conn._handle_message(changed("agent:agentA:user_1", True))
    # a cron session must NEVER forward run_active
    await conn._handle_message(changed("cron:job1", True))
    # transition to inactive -> forwarded
    await conn._handle_message(changed("agent:agentA:user_1", False))

    run_active = [m for m in forwarded if m.get("type") == "run_active"]
    assert run_active == [
        {"type": "run_active", "active": True, "agent_id": "agentA"},
        {"type": "run_active", "active": False, "agent_id": "agentA"},
    ]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_connection_pool_run_active.py -v`
Expected: FAIL (`AttributeError: _run_state` / no `run_active` frames forwarded)

- [ ] **Step 3: Write minimal implementation**

3a. Initialize the dedup map where `_active_runs` is initialized (near line 166). Add:

```python
        # Last-forwarded run-active value per webchat session_key. Deduplicates
        # `sessions.changed` bursts so only transitions produce a `run_active`
        # frame. SEPARATE from `_active_runs` (which is the parked-vs-dead
        # signal) — never conflate the two.
        self._run_state: dict[str, bool] = {}
```

3b. In `_handle_message`, add a branch BEFORE the final `else` at line 1452 (the `# Forward other events as-is` fallthrough), inside the `if msg_type == "event":` block, as a sibling to the `elif event_name == "chat.side_result":` branch:

```python
            elif event_name == "sessions.changed":
                # OpenClaw's authoritative per-session run-state (from its live
                # abort-controller registry), broadcast on every run
                # start/end/transition. The payload embeds `hasActiveRun`, so we
                # read it directly — NO follow-up RPC (that would deadlock inside
                # the reader loop). Webchat-only + member-demuxed: a non-webchat
                # or member-less key must never reach the web UI (would leak
                # another surface or broadcast to the whole org). Deduped against
                # the last-forwarded value so message/transcript-driven
                # `sessions.changed` bursts don't spam identical frames.
                if not session_key or is_channel_session or is_cron_session or is_isolated_session:
                    return
                active = bool(payload.get("hasActiveRun"))
                if self._run_state.get(session_key) == active:
                    return
                self._run_state[session_key] = active
                fwd_ra: dict = {"type": "run_active", "active": active}
                event_agent_id = parsed_key.get("agent_id")
                if event_agent_id:
                    fwd_ra["agent_id"] = event_agent_id
                if session_id is not None:
                    fwd_ra["session_id"] = session_id
                await self._forward_to_frontends(fwd_ra, target_member)
```

3c. Reset the dedup entry at the true terminal so the NEXT run's first `hasActiveRun:true` is treated as a transition. In `_clear_active_run` (line 305):

```python
    def _clear_active_run(self, session_key: str) -> None:
        """Pop the run keyed by *session_key* (chat.final / error / aborted)."""
        self._active_runs.pop(session_key, None)
        # Reset run-active dedup so the next run's first `hasActiveRun:true`
        # sessions.changed is forwarded as a fresh transition. (The frontend
        # busy-gate is already closed by the run_ended/done/error frame; this
        # only clears our transition bookkeeping.)
        self._run_state.pop(session_key, None)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_connection_pool_run_active.py -v`
Expected: PASS (3 tests)

- [ ] **Step 5: Run the existing gateway suite for regressions**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/ -q`
Expected: PASS (no regressions in busy-gate / demux / reconnect tests)

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_connection_pool_run_active.py
git commit -m "feat(gateway): forward run_active on sessions.changed (webchat-scoped, deduped)"
```

---

## Task 3: Frontend — consume `run_active`, derive `busy`, clear on terminal/reconnect

**Files:**
- Modify: `apps/frontend/src/hooks/useGateway.tsx` (add `run_active` to the `ChatIncomingMessage` union)
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (`containerRunActive` state; handle `run_active`; clear on terminal + reconnect; expose `busy`)
- Test: `apps/frontend/src/hooks/__tests__/useAgentChat.runActive.test.tsx` (new)

**Interfaces:**
- Consumes: incoming frame `{type:"run_active", active:boolean, agent_id?:string, session_id?:string}` from Task 2.
- Produces: `useAgentChat(...)` return gains `busy: boolean` (`= isStreaming || containerRunActive`). Existing `isStreaming` is unchanged and still returned.

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/hooks/__tests__/useAgentChat.runActive.test.tsx` (mirror the mock harness in `useAgentChat.busyGate.test.tsx` — same `vi.mock("@/hooks/useGateway", …)` shape exposing `onChatMessage`). Core assertions:

```tsx
// (imports + mocks identical to useAgentChat.busyGate.test.tsx; chatHandlers captured from onChatMessage)

it("run_active:true makes busy true with no stream events, and run_ended clears it", async () => {
  const { useAgentChat } = await import("@/hooks/useAgentChat");
  const { result } = renderHook(() => useAgentChat("agentA", "user-1"));

  act(() => {
    chatHandlers.forEach((h) => h({ type: "run_active", active: true, agent_id: "agentA" }));
  });
  expect(result.current.busy).toBe(true);      // busy even though no chunk/tool arrived
  expect(result.current.isStreaming).toBe(false);

  act(() => {
    chatHandlers.forEach((h) => h({ type: "run_ended", runId: "r1", outcome: "done", agent_id: "agentA" }));
  });
  expect(result.current.busy).toBe(false);
});

it("run_active for a DIFFERENT agent does not flip this hook's busy", async () => {
  const { useAgentChat } = await import("@/hooks/useAgentChat");
  const { result } = renderHook(() => useAgentChat("agentA", "user-1"));
  act(() => {
    chatHandlers.forEach((h) => h({ type: "run_active", active: true, agent_id: "agentB" }));
  });
  expect(result.current.busy).toBe(false);
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && node_modules/.bin/vitest run src/hooks/__tests__/useAgentChat.runActive.test.tsx`
Expected: FAIL (`result.current.busy` is undefined; `run_active` unhandled)

- [ ] **Step 3: Write minimal implementation**

3a. In `useGateway.tsx`, extend the `ChatIncomingMessage` union with the new frame (place beside the other `{type: "run_ended"; …}` member):

```ts
  | { type: "run_active"; active: boolean; agent_id?: string; session_id?: string }
```

3b. In `useAgentChat.ts`, add state next to `isStreaming` (after line 460):

```ts
  const [containerRunActive, setContainerRunActive] = useState(false);
```

3c. Handle the frame in the `onChatMessage` handler. The handler already drops other-agent frames at the top (`msg.agent_id !== agentIdRef.current` → return, line ~802), so an agent-tagged `run_active` for a different agent never reaches here. Add near the terminal handling (after the block at line 829 that treats `done`/`error`/`run_ended`):

```ts
      if (msg.type === "run_active") {
        setContainerRunActive(!!msg.active);
        return;
      }
```

3d. Close on the authoritative terminal. In the existing terminal branch (`msg.type === "done" || msg.type === "error" || msg.type === "run_ended"`, line 829), also clear container-run-active so a lost `run_active:false` can't strand the spinner:

```ts
      if (msg.type === "done" || msg.type === "error" || msg.type === "run_ended") {
        setContainerRunActive(false);
        // …existing busy-gate/queue-drain logic unchanged…
```

3e. Clear on WS reconnect. Find the effect that resets live state on `isConnected` transitions (the reconnect effect) and add `setContainerRunActive(false)` to it, so a stale true from before a drop doesn't persist. If no such effect exists, add:

```ts
  useEffect(() => {
    if (!isConnected) setContainerRunActive(false);
  }, [isConnected]);
```

3f. Expose `busy` in the return object (next to `isStreaming`, line ~771):

```ts
    isStreaming,
    busy: isStreaming || containerRunActive,
```

And add `busy: boolean;` to the hook's return type interface next to `isStreaming: boolean;` (line 154).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && node_modules/.bin/vitest run src/hooks/__tests__/useAgentChat.runActive.test.tsx`
Expected: PASS

- [ ] **Step 5: Guard against regressions + typecheck**

Run: `cd apps/frontend && node_modules/.bin/vitest run src/hooks/__tests__/useAgentChat.busyGate.test.tsx src/hooks/__tests__/useAgentChat.reconnect.test.tsx`
Run: `cd apps/frontend && node_modules/.bin/tsc --noEmit`
Expected: PASS; no type errors.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/hooks/useGateway.tsx apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/hooks/__tests__/useAgentChat.runActive.test.tsx
git commit -m "feat(chat): consume run_active signal for server-truth busy state"
```

---

## Task 4: Frontend — keep the status indicator lit across silent windows

**Files:**
- Modify: `apps/frontend/src/components/chat/AgentChatWindow.tsx` (pass `busy` to the status bar / spinner)
- Modify: `apps/frontend/src/components/chat/StreamingStatusBar.tsx` (render while busy; show a plain "Working…" label when active-but-not-streaming)
- Test: `apps/frontend/src/components/chat/__tests__/StreamingStatusBar.test.tsx` (extend or create)

**Interfaces:**
- Consumes: `busy` from `useAgentChat` (Task 3).
- Produces: the status bar is visible whenever `busy` is true, not only while `isStreaming`.

- [ ] **Step 1: Write the failing test**

Add to `apps/frontend/src/components/chat/__tests__/StreamingStatusBar.test.tsx` (create if absent; render `StreamingStatusBar` with the props it takes today plus the new `active`/`busy` prop):

```tsx
it("renders a working indicator when the run is active but no tool is streaming", () => {
  render(<StreamingStatusBar active={true} toolBlocks={[]} /* + any existing required props */ />);
  expect(screen.getByText(/working/i)).toBeInTheDocument();
});

it("renders nothing when not active and nothing is streaming", () => {
  const { container } = render(<StreamingStatusBar active={false} toolBlocks={[]} />);
  expect(container).toBeEmptyDOMElement();
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd apps/frontend && node_modules/.bin/vitest run src/components/chat/__tests__/StreamingStatusBar.test.tsx`
Expected: FAIL (no `active` prop / no "Working" branch)

- [ ] **Step 3: Write minimal implementation**

3a. In `StreamingStatusBar.tsx`, accept an `active?: boolean` prop. The component currently returns `null` when there is nothing to show (line ~41). Change the render gate so it shows while `active` is true even with no tool block, and label it plainly when active-but-idle:

```tsx
// props: add `active?: boolean;`
// existing derivation produces a `label` from tool blocks via deriveStreamingLabel.
const label = deriveStreamingLabel(toolBlocks) ?? (active ? "Working…" : null);
if (!label) return null;
// …render the pill with `label` as today…
```

3b. In `AgentChatWindow.tsx`, destructure `busy` from `useAgentChat` and pass it as `active` to the status bar (find the `<StreamingStatusBar … />` render and the `useAgentChat` destructure):

```tsx
const { /* …, */ isStreaming, busy, /* … */ } = useAgentChat(agentId, sessionName);
// …
<StreamingStatusBar active={busy} /* …existing props… */ />
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd apps/frontend && node_modules/.bin/vitest run src/components/chat/__tests__/StreamingStatusBar.test.tsx`
Expected: PASS

- [ ] **Step 5: Regression + typecheck**

Run: `cd apps/frontend && node_modules/.bin/vitest run src/components/chat/__tests__/AgentChatWindow.historyShell.test.tsx src/components/chat/__tests__/AgentChatWindow.modelPill.test.tsx`
Run: `cd apps/frontend && node_modules/.bin/tsc --noEmit`
Expected: PASS; no type errors.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/chat/AgentChatWindow.tsx apps/frontend/src/components/chat/StreamingStatusBar.tsx apps/frontend/src/components/chat/__tests__/StreamingStatusBar.test.tsx
git commit -m "feat(chat): keep status indicator lit across silent active windows"
```

---

## Out of scope (deliberate — file as follow-ups if wanted)

- **True "stuck/hung" detection.** OpenClaw's `status` enum is `running/done/failed/killed/timeout`; a genuinely hung run still reads `running` with `hasActiveRun:true`. Distinguishing "running but making no progress" needs a client-side staleness heuristic (time since last stream event) — a small separate enhancement, not required to fix the silence.
- **Background / overnight (Paperclip-adapter) agents.** These register `controlUiVisible:false` and are excluded from `sessions.changed`'s active-run projection by design (fork `session-active-runs.ts`). Run-state visibility for those belongs on the Teams/Paperclip surface — a distinct issue, not this plan.
- **Typed `status` passthrough to the UI** (e.g. rendering a `failed`/`timeout` terminal differently). The existing `error`/`errorKind` frames already carry terminal error typing; adding `status` to `run_active` is optional polish.

## Self-Review

- **Spec coverage:** Subscribe (Task 1) ✓; consume + forward server-truth active signal, webchat-scoped + deduped (Task 2) ✓; frontend busy = isStreaming || containerRunActive with terminal/reconnect clear (Task 3) ✓; indicator stays lit across silent windows (Task 4) ✓. The "parity with OpenClaw Control UI" goal is met for interactive `/chat`; background-agent parity is explicitly out of scope with rationale.
- **`_active_runs` separation:** `_run_state` is a distinct map; the only touch of `_active_runs`-adjacent code is adding a `_run_state.pop` inside `_clear_active_run` (dedup reset at the same lifecycle point), which does not change `_active_runs` behavior. ✓
- **Deadlock:** no RPC issued on the event path; `hasActiveRun` read from the embedded payload. ✓
- **Type consistency:** frame `{type:"run_active", active, agent_id?, session_id?}` is identical across backend forward (Task 2), the `ChatIncomingMessage` union (Task 3), and the handler (Task 3). `busy` is defined once in the return and consumed in Task 4. ✓
- **Reconnect:** subscribe re-sent via `connect()` (called by `reopen()`); frontend clears on `isConnected` false. ✓
