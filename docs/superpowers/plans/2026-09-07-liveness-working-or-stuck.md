# Liveness: Tell the Owner Whether It's Working or Stuck — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the word "Working…" with a ticking clock and a named phase, so an owner
watching a silent run can tell the difference between progress and a hang.

**Architecture:** OpenClaw 8.1 emits a typed startup-phase stream and compaction notices that
we currently drop on the floor in `_transform_agent_event`. We forward both through the
existing `agent_activity` envelope (no new frame type, no protocol change), have `useAgentChat`
record the phase and the run's timing, and turn our status bar from a gap-filler into
persistent run telemetry that carries a clock, a phase, and a post-run recap. Separately we
enable the gateway's session observer, which publishes a headline at **no model cost** at our
subscriber tier.

**Tech Stack:** Python 3 / FastAPI backend (`uv`, `pytest`), React 19 / Next.js 16 frontend
(`vitest`), OpenClaw gateway WebSocket protocol.

**Spec:** `docs/superpowers/plans/2026-09-06-openclaw-81-audit-findings.md` — findings
UI0, D3, D9, A2, UI1. Read "THE PLAN → OUTCOME 2" plus each finding body before starting.

## Global Constraints

- **Image tag we run:** `v2026.8.1-isol8.3`. Verify OpenClaw behaviour with
  `git show v2026.8.1-isol8.3:<path>` in `~/dev/agent-fork`. **Its working tree is a stale
  2026.7.2 — never read it.** Never `git checkout`/`switch` there.
- **`aria-live` on anything that ticks is a defect.** Upstream sets `aria-live="off"` on six
  in-flight surfaces and writes the reason down twice. Any element in this plan that updates
  once per second gets `aria-live="off"`, with announcements carried by a separate,
  non-ticking element.
- **A2 TRIPWIRE — do not add `sessions.messages.subscribe` anywhere in this plan.** The
  observer is free at our `broad` audience tier and billed at `direct`; that RPC is what flips
  it. See finding A2 for the adjudication.
- **Colour literals fail the brand-token scan** outside `landing.css`, `components/landing/**`,
  `lib/clerkAppearance.ts`, `*Brand.ts` and tests. Use `var(--…)` tokens from `globals.css`.
- **Backend tests:** `cd apps/backend && uv run pytest tests/ -v`. **Frontend:**
  `cd apps/frontend && pnpm test`. Run targeted tests locally; let CI run the full suite.
- **Frontend vitest is RAM-heavy** — run targeted files, not the whole suite, while iterating.
- **`_transform_agent_event` is a `@staticmethod` today (`connection_pool.py:1069`) and the
  transcript-integrity plan converts it to an instance method.** Whichever plan lands first
  writes the instance form; the other rebases. This plan's new test file must call it through an
  instance either way — `GatewayConnection.__new__(GatewayConnection)._transform_agent_event(...)`
  — which works under both signatures. The production call site (`:1953`) is already
  `self._transform_agent_event(payload)` and needs no change.
- **`useAgentChat.ts` is shared with the transcript plan** (its Task 4 rewrites the
  `chat.history` handler; this plan's Task 5 rewrites the `agent_activity` handler and the
  terminal paths). One worktree, one agent, transcript's hydration first.
- No PR or issue references in code comments.

---

## File Structure

| file | responsibility | change |
|---|---|---|
| `apps/frontend/src/components/chat/ProvisioningStepper.tsx` | provisioning UI | Task 1: drop `aria-live` from the clock |
| `apps/backend/core/gateway/connection_pool.py` | gateway event translation | Tasks 2–4: two new stream branches, one new subscribe RPC |
| `apps/backend/tests/unit/gateway/test_transform_agent_event.py` | **new** — locks the transform's branch table | Tasks 2–3 |
| `apps/backend/tests/unit/gateway/test_session_subscribe.py` | **new** — locks the observer subscribe | Task 4 |
| `apps/frontend/src/hooks/useAgentChat.ts` | chat state machine | Task 5: track `runPhase` and `runStartedAt` |
| `apps/frontend/src/components/chat/StreamingStatusBar.tsx` | the silent-gap indicator | Task 6: elapsed clock + phase text |
| `apps/frontend/src/components/chat/__tests__/StreamingStatusBar.test.tsx` | **new** | Task 6 |

**Why the indicator must persist — a deliberate reversal.** `StreamingStatusBar.tsx:30-33`
currently renders only when `!isStreaming && active`, and its header comment argues that while
tokens are arriving the transcript *is* the status, so a second indicator is noise. **That
reasoning is wrong and this plan reverses it.** Upstream considered the same question and
decided the opposite, in `ui/src/pages/chat/chat-thread-build.ts:620-632`:

> *"The active claw is **telemetry, not a placeholder**: it stays through visible assistant
> text and tool cards until the run settles."*

Their condition is `showWorkingIndicator = props.runWorking === true && !initialHistoryLoad`
— on for the whole run. The argument defeats a duplicate status *label*; it does not defeat a
*clock* and a *token count*, which are orthogonal to the transcript rather than a vaguer
restatement of it. The transcript says WHAT; the indicator says HOW LONG.

Three things our gap-only version forecloses, all of which this plan wants:
1. The clock disappears exactly on the longest runs — the ones that stream steadily and never
   go quiet are precisely where "how long has this been going?" is asked.
2. A **turn recap** ("Done in 4m 12s") is structurally impossible: the bar has unmounted by
   the time the run settles.
3. A **waiting-for-approval** state cannot render while text is streaming.

**Placement stays ours.** Upstream's indicator is a `reading-indicator` item pushed into the
transcript's `items` array, so it scrolls with the work; ours is pinned below the transcript
(`AgentChatWindow.tsx:562`, after `<MessageList>`). For a persistent telemetry strip, pinned
is defensible and arguably better — it cannot scroll out of view — and keeping it avoids a
`MessageList` refactor for no behavioural gain. We are copying their *behaviour*, not their
layout.

---

### Task 1: Stop announcing the provisioning clock once per second

`ProvisioningStepper.tsx:708` runs `setInterval(tick, 1000)` and `:1780` renders the result
inside `aria-live="polite"`. Provisioning routinely runs five minutes or more, so a screen
reader reads the elapsed time once per second for the whole of first-run, drowning out the
step transitions in the same region.

**Files:**
- Modify: `apps/frontend/src/components/chat/ProvisioningStepper.tsx:1780`

**Interfaces:**
- Consumes: nothing. Produces: nothing. Fully independent — ship it on its own.

- [ ] **Step 1: Confirm the tick rate and the live region**

```bash
sed -n '705,712p' apps/frontend/src/components/chat/ProvisioningStepper.tsx
sed -n '1778,1784p' apps/frontend/src/components/chat/ProvisioningStepper.tsx
```

Expected: a 1000ms interval, and a `<div className="provision-elapsed" aria-live="polite">`
wrapping `formatElapsed(elapsedMs)`.

- [ ] **Step 2: Make the ticking element silent**

Replace the opening tag at `:1780`:

```tsx
              <div className="provision-elapsed" aria-live="polite">
```

with:

```tsx
              {/* aria-live="off" is deliberate: this value ticks once per second,
                  and a live region containing a 1 Hz counter floods assistive tech
                  for the whole provisioning window (minutes) and buries the step
                  transitions announced elsewhere. The step text keeps its own
                  live region; this is the number, not the news. */}
              <div className="provision-elapsed" aria-live="off">
```

- [ ] **Step 3: Verify the step text still announces**

```bash
grep -n 'aria-live' apps/frontend/src/components/chat/ProvisioningStepper.tsx
```

Expected: the remaining `aria-live="polite"` regions are the ones carrying *step* and *error*
text — notably the `isRetryingError` block at ~`:1790` — not the elapsed value. If the step
label has no live region of its own, add `role="status"` to it in this task; a screen-reader
user must still learn that provisioning moved on.

- [ ] **Step 4: Run the stepper's tests**

Run: `cd apps/frontend && pnpm test -- ProvisioningStepper`

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/chat/ProvisioningStepper.tsx
git commit -m "fix(a11y): stop announcing the provisioning clock once per second"
```

---

### Task 2: Forward the run startup phases

8.1 added `ChatStatusEventSchema` (`packages/gateway-protocol/src/schema/logs-chat.ts:245-262`)
with a closed phase enum — `preparing_workspace`, `naming_worktree`, `creating_worktree`,
`running_setup`, `provisioning_environment`, `preparing_context`, `starting_model` — mirrored
on the agent stream as `stream: "run_status"` (`src/infra/agent-run-status-events.ts:11-17`).
We drop **both** copies: `_transform_agent_event` has no `run_status` branch, and
`connection_pool.py:1981` returns early on any chat state that isn't `final`/`error`/`aborted`.

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_transform_agent_event`, the
  `elif stream in ("plan", "patch", "item", "acp")` branch at ~1170)
- Create: `apps/backend/tests/unit/gateway/test_transform_agent_event.py`

**Interfaces:**
- Consumes: nothing.
- Produces: a frame `{"type": "agent_activity", "activity": "run_status", "title": <phase>,
  "detail": {...}, "runId": str}` — consumed by Task 5.

- [ ] **Step 1: Confirm the event shape at the tag**

```bash
cd ~/dev/agent-fork && git show v2026.8.1-isol8.3:src/infra/agent-run-status-events.ts | head -30
```

Expected: a `run_status` stream carrying a `phase` field from the closed enum above.

- [ ] **Step 2: Write the failing test**

Create `apps/backend/tests/unit/gateway/test_transform_agent_event.py`:

```python
"""The agent-event transform's branch table.

Anything _transform_agent_event does not recognise returns None and is silently
dropped, so a stream we forget to handle disappears with no error anywhere. These
tests pin the branches we rely on.
"""

from core.gateway.connection_pool import GatewayConnection


def _payload(stream: str, data: dict, run_id: str = "run_1") -> dict:
    return {"stream": stream, "data": data, "runId": run_id}


def _transform(payload: dict):
    """Call through an instance.

    _transform_agent_event is a @staticmethod today, and the transcript-integrity
    plan converts it to an instance method so it can reach self._sender. Calling
    through an unbound instance works under BOTH signatures, so these tests do not
    break whichever plan lands second.
    """
    return GatewayConnection.__new__(GatewayConnection)._transform_agent_event(payload)


def test_run_status_becomes_an_agent_activity_frame():
    out = _transform(_payload("run_status", {"phase": "starting_model"}))
    assert out is not None
    assert out["type"] == "agent_activity"
    assert out["activity"] == "run_status"
    assert out["title"] == "starting_model"
    assert out["detail"] == {"phase": "starting_model"}
    assert out["runId"] == "run_1"


def test_run_status_without_a_phase_is_dropped():
    """A phase-less status event has nothing to show; don't render an empty pill."""
    assert _transform(_payload("run_status", {})) is None


def test_unknown_stream_is_still_dropped():
    """Guards against a future edit that makes the transform permissive."""
    assert _transform(_payload("nonsense", {"x": 1})) is None
```

- [ ] **Step 3: Run to confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_transform_agent_event.py -v`

Expected: FAIL — `test_run_status_becomes_an_agent_activity_frame` gets `None`.

- [ ] **Step 4: Add the branch**

In `_transform_agent_event`, immediately before the
`elif stream in ("plan", "patch", "item", "acp"):` branch, insert:

```python
        elif stream == "run_status":
            # 8.1's typed startup phases (agent-run-status-events.ts): the window
            # between "send" and the first token, which is otherwise dead air
            # under a generic spinner. The phase is a closed enum
            # (preparing_workspace | naming_worktree | creating_worktree |
            # running_setup | provisioning_environment | preparing_context |
            # starting_model), so the frontend maps it to copy rather than
            # printing it raw. Rides the existing agent_activity envelope — no
            # new frame type and no protocol change.
            phase = data.get("phase", "")
            if phase:
                transformed = {
                    "type": "agent_activity",
                    "activity": "run_status",
                    "title": phase,
                    "detail": data,
                }
```

- [ ] **Step 5: Run to verify it passes**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_transform_agent_event.py -v`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_transform_agent_event.py
git commit -m "feat(gateway): forward run startup phases to the chat surface"
```

---

### Task 3: Forward compaction notices

`src/agents/embedded-agent-subscribe.handlers.compaction.ts:37-56` emits
`{phase: "start"}` / `{phase: "end", completed, willRetry, outcome, reason}`. A long session
stalls for tens of seconds while history is compacted with no explanation, and a *failed*
compaction (`completed: false`) shows as an unexplained pause followed by a possibly
context-lost reply. This is a standing gap, present at 7.2 too — not an 8.1 regression.

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_transform_agent_event`)
- Modify: `apps/backend/tests/unit/gateway/test_transform_agent_event.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `{"type": "agent_activity", "activity": "compaction", "title": <phase>,
  "detail": {...}, "runId": str}`.

- [ ] **Step 1: Write the failing tests**

Append to `apps/backend/tests/unit/gateway/test_transform_agent_event.py`:

```python
def test_compaction_start_becomes_an_activity_frame():
    out = _transform(_payload("compaction", {"phase": "start"}))
    assert out is not None
    assert out["activity"] == "compaction"
    assert out["title"] == "start"


def test_compaction_end_carries_the_outcome_fields():
    """A failed compaction must be distinguishable from a successful one."""
    out = _transform(_payload("compaction", {"phase": "end", "completed": False, "willRetry": True}))
    assert out is not None
    assert out["detail"]["completed"] is False
    assert out["detail"]["willRetry"] is True
```

- [ ] **Step 2: Run to confirm they fail**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_transform_agent_event.py -v -k compaction`

Expected: FAIL — both get `None`.

- [ ] **Step 3: Extend the branch**

Change the `run_status` branch added in Task 2 to cover both streams:

```python
        elif stream in ("run_status", "compaction"):
            # run_status: 8.1's typed startup phases (agent-run-status-events.ts) —
            # the otherwise-dead window between send and the first token.
            # compaction: history compaction start/end. A long session stalls for
            # tens of seconds here with no explanation, and a failed compaction
            # (completed: false) otherwise reads as an unexplained pause followed
            # by a context-lost reply. Both ride the existing agent_activity
            # envelope; `detail` carries the full payload so the frontend can
            # distinguish a completed compaction from a failed one.
            phase = data.get("phase", "")
            if phase:
                transformed = {
                    "type": "agent_activity",
                    "activity": stream,
                    "title": phase,
                    "detail": data,
                }
```

- [ ] **Step 4: Run all the transform tests**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_transform_agent_event.py -v`

Expected: all PASS, including Task 2's.

- [ ] **Step 5: Commit**

```bash
git add apps/backend/core/gateway/connection_pool.py apps/backend/tests/unit/gateway/test_transform_agent_event.py
git commit -m "feat(gateway): forward context-compaction notices"
```

---

### Task 4: Turn on the session observer's free tier

The gateway publishes `session.observer` digests carrying a `headline`, persisted onto the row
as `observerDigest` and delivered on `sessions.changed`. It is gated on
`gateway.controlUi.sessionObserver !== false` (`src/gateway/session-observer.ts:251, 459`) —
**not** on `controlUi.enabled`, which is the one we set false. We already `sessions.subscribe`
on connect, but `visibleConnections` starts empty and only `sessions.observer.visibility`
populates it (`src/gateway/session-observer-rpc.ts:43`), so `classify()` returns `"none"` and
the observer suspends immediately.

**READ THE TRIPWIRE IN GLOBAL CONSTRAINTS BEFORE STARTING.** At our tier
(`sessions.subscribe` only) `classify()` returns `"broad"`,
`utilityModelRef` resolves to `undefined` (`session-observer.ts:669-672`), and the preamble
publisher runs with **no model call**. Adding `sessions.messages.subscribe` would flip us to
`"direct"` and start billing the utility model per digest cycle.

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_subscribe_session_events`, ~953-970)
- Create: `apps/backend/tests/unit/gateway/test_session_subscribe.py`

**Interfaces:**
- Consumes: nothing.
- Produces: an additional `sessions.observer.visibility` RPC on every connect, after which
  `observerDigest.headline` appears on `sessions.changed` payloads.

> **⚠️ NOTHING CONSUMES THIS YET.** The rail plan defers `sessions.changed` consumption (A9),
> so as scheduled this ships an extra RPC on every connect whose payload no client reads. That
> is not harmful, but it buys nothing and it makes the cost tripwire below look like it guards
> something in use. **Land this task only alongside a consumer** — either move it into the rail
> plan's A9 work, or add the `sessions.changed` merge here. If you ship it bare, say so in the
> PR so the next person does not mistake it for a working feature.

- [ ] **Step 1: Read our current subscribe**

```bash
sed -n '950,975p' apps/backend/core/gateway/connection_pool.py
```

- [ ] **Step 2: Write the failing test**

Create `apps/backend/tests/unit/gateway/test_session_subscribe.py`:

```python
"""On connect we must declare visibility, not just subscribe.

The gateway's session observer publishes headlines only to connections marked
visible. sessions.subscribe alone leaves visibleConnections empty, classify()
returns "none", and the observer suspends — so we receive nothing.

Do NOT add sessions.messages.subscribe here. That flips the observer's audience
from "broad" (preamble publisher, no model call) to "direct" (utility model per
digest cycle), which bills against the owner's provider and the trial cap.
"""

import pytest


@pytest.mark.asyncio
async def test_connect_declares_observer_visibility(gateway_connection_with_stub_rpc):
    conn, raw = gateway_connection_with_stub_rpc
    await conn._subscribe_session_events()
    assert "sessions.subscribe" in _methods(raw)
    assert "sessions.observer.visibility" in _methods(raw)
    vis = next(json.loads(r) for r in raw if json.loads(r)["method"] == "sessions.observer.visibility")
    assert vis["params"] == {"visible": True}


@pytest.mark.asyncio
async def test_connect_does_not_subscribe_to_messages(gateway_connection_with_stub_rpc):
    """Guards the cost tripwire — see the module docstring."""
    conn, raw = gateway_connection_with_stub_rpc
    await conn._subscribe_session_events()
    assert "sessions.messages.subscribe" not in _methods(raw)
```

**There is no `_send_rpc_raw`.** `_subscribe_session_events` (`connection_pool.py:953-970`)
builds a raw dict and calls `await self._ws.send(json.dumps(frame))`. Stub the socket and parse
what it was handed:

```python
import json
import pytest
from unittest.mock import AsyncMock
from core.gateway.connection_pool import GatewayConnection


@pytest.fixture
def gateway_connection_with_stub_rpc():
    """A GatewayConnection whose websocket records the frames sent to it.

    _subscribe_session_events writes raw JSON to self._ws rather than going
    through an RPC helper, so this stubs the socket and decodes the frames.
    """
    conn = GatewayConnection.__new__(GatewayConnection)
    conn.user_id = "u_1"
    raw: list[str] = []
    conn._ws = AsyncMock()
    conn._ws.send = AsyncMock(side_effect=lambda payload: raw.append(payload))
    return conn, raw


def _methods(raw: list[str]) -> list[str]:
    return [json.loads(r).get("method") for r in raw]
```

The tests then read `_methods(raw)` rather than `call["method"]`, and pull params with
`next(json.loads(r) for r in raw if json.loads(r)["method"] == "sessions.observer.visibility")`.

- [ ] **Step 3: Run to confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/test_session_subscribe.py -v`

Expected: FAIL — only `sessions.subscribe` was sent.

- [ ] **Step 4: Add the visibility call**

In `_subscribe_session_events`, immediately after the existing `sessions.subscribe`:

```python
        # The observer publishes session headlines only to connections it
        # considers VISIBLE; sessions.subscribe alone leaves visibleConnections
        # empty, classify() returns "none", and the observer suspends. This is
        # the whole opt-in.
        #
        # COST: at this tier — a visible sessions.subscribe subscriber — the
        # audience classifies as "broad", utilityModelRef resolves to undefined,
        # and only the preamble publisher runs. No model call, no spend. Adding
        # sessions.messages.subscribe anywhere would flip the audience to
        # "direct" and start billing the utility model per digest cycle, against
        # the owner's provider and the $3/day trial cap. Do not add it.
        #
        # Best-effort: an older image without the RPC answers with an error we
        # must not let break the connect path.
        frame = {
            "type": "req",
            "id": str(uuid.uuid4()),
            "method": "sessions.observer.visibility",
            "params": {"visible": True},
        }
        try:
            await self._ws.send(json.dumps(frame))
        except Exception:  # noqa: BLE001 — headlines are a nicety, not a dependency
            logger.warning(
                "sessions.observer.visibility send failed user=%s", self.user_id, exc_info=True
            )
```

This mirrors the `sessions.subscribe` block directly above it — same raw-frame shape, same
fire-and-forget posture, same log level.

- [ ] **Step 5: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/ -v`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/gateway/ apps/backend/tests/unit/gateway/
git commit -m "feat(gateway): declare observer visibility so session headlines arrive"
```

---

### Task 5: Track the run's phase, timing and recap in chat state

`useAgentChat` already receives `agent_activity` frames and turns them into tool-stream pills
(`:1883`). A pill is the wrong surface for a startup phase — a phase is the run's *current
state*, not an event to log. Record it separately, along with the timing the indicator and the
recap need.

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (the `agent_activity` handler at ~1883,
  the run-termination handling, and the hook's return value)
- Test: `apps/frontend/src/hooks/__tests__/useAgentChat.test.ts` (extend; create if absent)

**Interfaces:**
- Consumes: the `agent_activity` frame with `activity: "run_status"` from Task 2.
- Produces, on the hook's return value:
  - `runPhase: string | null` — the raw phase enum value while a run is active, else null.
  - `runStartedAt: number | null` — epoch ms of when this client first saw the run start.
  - `lastTurnRecap: { runtimeMs: number } | null` — set when a run settles **successfully**,
    cleared when the next run starts. Null after a failed or aborted run.

- [ ] **Step 1: Write the failing tests**

```ts
it("records the latest run_status phase and clears it when the run ends", () => {
  const { result } = renderChatHook();
  act(() => {
    deliverFrame(result, {
      type: "agent_activity", activity: "run_status",
      title: "preparing_context", detail: { phase: "preparing_context" }, runId: "r1",
    });
  });
  expect(result.current.runPhase).toBe("preparing_context");

  act(() => {
    deliverFrame(result, {
      type: "agent_activity", activity: "run_status",
      title: "starting_model", detail: { phase: "starting_model" }, runId: "r1",
    });
  });
  expect(result.current.runPhase).toBe("starting_model");

  act(() => { deliverFrame(result, { type: "done", runId: "r1" }); });
  expect(result.current.runPhase).toBeNull();
});

it("starts the clock on the first frame of a run and does not restart it", () => {
  const { result } = renderChatHook();
  act(() => {
    deliverFrame(result, {
      type: "agent_activity", activity: "run_status",
      title: "preparing_context", detail: { phase: "preparing_context" }, runId: "r1",
    });
  });
  const first = result.current.runStartedAt;
  expect(first).not.toBeNull();
  act(() => {
    deliverFrame(result, {
      type: "agent_activity", activity: "run_status",
      title: "starting_model", detail: { phase: "starting_model" }, runId: "r1",
    });
  });
  expect(result.current.runStartedAt).toBe(first);
});

it("produces a turn recap when a run completes successfully", () => {
  const { result } = renderChatHook();
  const start = Date.now() - 30_000;
  act(() => {
    deliverFrame(result, {
      type: "agent_activity", activity: "run_status", title: "starting_model",
      detail: { phase: "starting_model", startedAt: start }, runId: "r1",
    });
  });
  act(() => { deliverFrame(result, { type: "done", runId: "r1" }); });
  expect(result.current.lastTurnRecap?.runtimeMs).toBeGreaterThanOrEqual(30_000);
});

it("produces NO recap for a failed run", () => {
  // Upstream's rule: "the error surfaces own those". A red error card plus a
  // cheerful "Done in 12s" underneath is a contradiction.
  const { result } = renderChatHook();
  act(() => {
    deliverFrame(result, {
      type: "agent_activity", activity: "run_status", title: "starting_model",
      detail: { phase: "starting_model", startedAt: Date.now() - 1000 }, runId: "r1",
    });
  });
  act(() => { deliverFrame(result, { type: "error", message: "boom", runId: "r1" }); });
  expect(result.current.lastTurnRecap).toBeNull();
});

it("clears the previous recap when a new run starts", () => {
  const { result } = renderChatHook();
  act(() => {
    deliverFrame(result, {
      type: "agent_activity", activity: "run_status", title: "starting_model",
      detail: { phase: "starting_model", startedAt: Date.now() - 1000 }, runId: "r1",
    });
    deliverFrame(result, { type: "done", runId: "r1" });
  });
  expect(result.current.lastTurnRecap).not.toBeNull();
  act(() => {
    deliverFrame(result, {
      type: "agent_activity", activity: "run_status", title: "preparing_context",
      detail: { phase: "preparing_context" }, runId: "r2",
    });
  });
  expect(result.current.lastTurnRecap).toBeNull();
});
```

`renderChatHook` and `deliverFrame` are helpers — if the existing test file has equivalents
under other names, use those. If there is no `useAgentChat` test file, model the harness on
`apps/frontend/src/components/chat/__tests__/AgentChatWindow.modelPill.test.tsx`, which
already mocks the gateway.

> **KNOWN LIMITATION, state it in the PR: this clock does not survive a reconnect.** No
> `run_status` is re-emitted mid-run, so a client that reconnects into a live run has no
> `runStartedAt` and Task 6 renders no clock — falling back to the plain label. The only source
> of a true mid-run start time is `chat.history`'s `inFlightRun`, which the
> **transcript-integrity plan's Task 4** fetches. Wire this to that once both have landed; do
> not claim reconnect coverage before then.

- [ ] **Step 2: Run to confirm they fail**

Run: `cd apps/frontend && pnpm test -- useAgentChat`

Expected: FAIL — `runPhase` is undefined.

- [ ] **Step 3: Add the state**

Near the other run-scoped state in `useAgentChat`:

```ts
  // Run telemetry for the working indicator. Deliberately NOT tool-stream pills:
  // a phase is the run's current state, not an event worth logging in the
  // transcript, and the recap is a post-run summary rather than an activity.
  const [runPhase, setRunPhase] = useState<string | null>(null);
  const [runStartedAt, setRunStartedAt] = useState<number | null>(null);
  const [lastTurnRecap, setLastTurnRecap] = useState<{ runtimeMs: number } | null>(null);
```

In the `agent_activity` handler at `:1883`, **before** the existing pill logic:

```ts
        if (msg.activity === "run_status") {
          const phase = typeof msg.detail?.phase === "string" ? msg.detail.phase : null;
          if (phase) setRunPhase(phase);
          // `emitAgentRunStatusEvent` emits `data: { phase }` and NOTHING ELSE
          // (src/infra/agent-run-status-events.ts) — there is no startedAt on the
          // wire. So this is when THIS CLIENT first saw the run, not when the run
          // began. Set once and leave it: re-setting on each phase would reset the
          // clock three times during startup.
          setRunStartedAt((prev) => prev ?? Date.now());
          // A new run supersedes the previous run's recap.
          setLastTurnRecap(null);
          return; // a phase is state, not a transcript pill
        }
```

At the **successful** terminal path (`done`, and the synthesized `run_ended`):

```ts
      setRunPhase(null);
      setLastTurnRecap(
        runStartedAt !== null ? { runtimeMs: Date.now() - runStartedAt } : null,
      );
      setRunStartedAt(null);
```

At the **failure** terminal paths (`error`, and the aborted case) — no recap:

```ts
      setRunPhase(null);
      setRunStartedAt(null);
      setLastTurnRecap(null);
```

Read `runStartedAt` through a ref if the terminal handler is inside a callback that would
close over a stale value; the existing hook already uses refs for exactly this reason.

Add `runPhase`, `runStartedAt` and `lastTurnRecap` to the hook's returned object.

- [ ] **Step 4: Run to verify**

Run: `cd apps/frontend && pnpm test -- useAgentChat`

Expected: PASS.

- [ ] **Step 5: Typecheck — CI typechecks test files too**

Run: `cd apps/frontend && pnpm exec tsc --noEmit`

Expected: no errors.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/hooks/__tests__/
git commit -m "feat(chat): track run phase, timing and turn recap"
```

---

### Task 6: Make the indicator persistent telemetry

The behavioural reversal. The bar stops being a gap-filler and stays up for the whole run,
carrying a clock and a phase — then hands over to the recap.

**Files:**
- Modify: `apps/frontend/src/components/chat/StreamingStatusBar.tsx`
- Modify: `apps/frontend/src/components/chat/AgentChatWindow.tsx:562`
- Create: `apps/frontend/src/components/chat/__tests__/StreamingStatusBar.test.tsx`

**Interfaces:**
- Consumes: `runPhase`, `runStartedAt`, `lastTurnRecap` from Task 5.
- Produces: no new exports.

- [ ] **Step 1: Write the failing tests**

```tsx
import { render, screen } from "@testing-library/react";
import { StreamingStatusBar } from "../StreamingStatusBar";

const base = { active: true, runPhase: null, runStartedAt: null, lastTurnRecap: null };

describe("StreamingStatusBar", () => {
  it("STAYS VISIBLE while tokens stream — it is telemetry, not a placeholder", () => {
    render(<StreamingStatusBar {...base} runStartedAt={Date.now() - 3000} />);
    expect(screen.getByTestId("streaming-status-bar")).toBeInTheDocument();
  });

  it("renders nothing when no run is active and there is no recap", () => {
    render(<StreamingStatusBar {...base} active={false} />);
    expect(screen.queryByTestId("streaming-status-bar")).toBeNull();
  });

  it("shows a phase label in plain language, not the raw enum", () => {
    render(<StreamingStatusBar {...base} runPhase="starting_model" runStartedAt={Date.now()} />);
    expect(screen.getByText(/Starting the model/i)).toBeInTheDocument();
    expect(screen.queryByText("starting_model")).toBeNull();
  });

  it("falls back to Working… when there is no phase", () => {
    render(<StreamingStatusBar {...base} runStartedAt={Date.now()} />);
    expect(screen.getByText(/Working/)).toBeInTheDocument();
  });

  it("shows elapsed time and does not announce it", () => {
    render(<StreamingStatusBar {...base} runStartedAt={Date.now() - 65_000} />);
    const elapsed = screen.getByTestId("status-elapsed");
    expect(elapsed).toHaveTextContent("1m 5s");
    expect(elapsed).toHaveAttribute("aria-live", "off");
  });

  it("shows no clock when the run start is unknown", () => {
    render(<StreamingStatusBar {...base} runStartedAt={null} />);
    expect(screen.queryByTestId("status-elapsed")).toBeNull();
  });

  it("shows the recap after the run settles, with no run active", () => {
    render(<StreamingStatusBar {...base} active={false} lastTurnRecap={{ runtimeMs: 252_000 }} />);
    expect(screen.getByTestId("streaming-status-bar")).toBeInTheDocument();
    expect(screen.getByText(/Done in 4m 12s/i)).toBeInTheDocument();
  });

  it("prefers the live run over a stale recap", () => {
    render(
      <StreamingStatusBar {...base} runStartedAt={Date.now()} lastTurnRecap={{ runtimeMs: 1000 }} />,
    );
    expect(screen.queryByText(/Done in/i)).toBeNull();
  });
});
```

- [ ] **Step 2: Run to confirm they fail**

Run: `cd apps/frontend && pnpm test -- StreamingStatusBar`

Expected: FAIL — the component renders null while streaming and takes no such props.

- [ ] **Step 3: Rewrite the component**

Replace `StreamingStatusBar.tsx` entirely:

```tsx
import * as React from "react";
import { AgentGlyph } from "./MessageList";

/**
 * Run telemetry, shown beneath the transcript.
 *
 * This used to render ONLY in silent gaps (`!isStreaming && active`), on the
 * reasoning that while tokens are arriving the transcript is its own status.
 * That argument defeats a duplicate status LABEL; it does not defeat a clock.
 * Elapsed time is orthogonal to the transcript — the transcript says what is
 * happening, this says how long it has been happening — and hiding it during
 * streaming removed it from exactly the long runs where it is asked for.
 * Upstream reached the same conclusion: "The active claw is telemetry, not a
 * placeholder: it stays through visible assistant text and tool cards until the
 * run settles" (chat-thread-build.ts).
 *
 * It deliberately does NOT narrate the current step. ToolActivityTrace does that
 * in place, in plain language, where the work appears.
 */

/** The gateway's closed startup-phase enum, in the owner's language. Anything
 *  unlisted falls back to "Working…" rather than printing a raw enum value at
 *  someone who does not know what a worktree is.
 *
 *  Only three of these fire on our fleet — preparing_workspace, preparing_context
 *  and starting_model, via resolveRunStartupPhase. The worktree/setup/environment
 *  phases come from project sessions and worker environments, neither of which we
 *  run. They are kept because the enum is closed and cheap, not because we expect
 *  them. */
const PHASE_LABEL: Record<string, string> = {
  preparing_workspace: "Preparing the workspace…",
  naming_worktree: "Preparing the workspace…",
  creating_worktree: "Preparing the workspace…",
  running_setup: "Running setup…",
  provisioning_environment: "Provisioning the environment…",
  preparing_context: "Reading the conversation…",
  starting_model: "Starting the model…",
};

function formatElapsed(ms: number): string {
  const total = Math.floor(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

interface StreamingStatusBarProps {
  // NOTE: `isStreaming` is GONE. The old component needed it to hide itself
  // during streaming; persistent telemetry does not care whether tokens are
  // arriving, only whether a run is live. Remove it from the call site too.
  /** Server-truth "a run is active" (useAgentChat's `busy`). */
  active?: boolean;
  runPhase?: string | null;
  runStartedAt?: number | null;
  /** Set once a run settles successfully; sticky until the next run starts. */
  lastTurnRecap?: { runtimeMs: number } | null;
}

export function StreamingStatusBar({
  active,
  runPhase = null,
  runStartedAt = null,
  lastTurnRecap = null,
}: StreamingStatusBarProps) {
  const running = Boolean(active);
  const [now, setNow] = React.useState(() => Date.now());

  // Tick only while a run is live AND we know when it started. An interval
  // behind a settled recap is pure wakeups.
  React.useEffect(() => {
    if (!running || runStartedAt === null) return;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [running, runStartedAt]);

  if (!running && !lastTurnRecap) return null;

  // A live run always outranks a previous run's recap.
  const label = running
    ? (runPhase && PHASE_LABEL[runPhase]) || "Working…"
    : `Done in ${formatElapsed(lastTurnRecap!.runtimeMs)}`;

  return (
    <div
      data-testid="streaming-status-bar"
      className="flex justify-center pointer-events-none px-4 pt-2 pb-1"
      role="status"
      // The label changes rarely and is worth announcing. The clock inside is
      // not — see the elapsed span's aria-live="off".
      aria-live="polite"
    >
      <div className="flex items-center gap-2 text-xs text-[var(--midnight-muted)] bg-[var(--cream-deep)]/80 backdrop-blur-sm rounded-full px-3 py-1.5 shadow-sm">
        <AgentGlyph state={running ? "thinking" : "idle"} size={16} />
        <span className="font-medium">{label}</span>
        {running && runStartedAt !== null && (
          <span
            data-testid="status-elapsed"
            // REQUIRED: this ticks once per second, and a live region containing
            // it floods assistive tech. The label above carries the announcement.
            aria-live="off"
            className="tabular-nums opacity-70"
          >
            {formatElapsed(Math.max(0, now - runStartedAt))}
          </span>
        )}
      </div>
    </div>
  );
}
```

Check `AgentGlyph`'s accepted `state` values before using `"idle"` — if it has no such state,
use the nearest settled one rather than inventing a prop value.

- [ ] **Step 4: Wire the props through**

`AgentChatWindow.tsx:562`:

```tsx
      <StreamingStatusBar
        active={busy}
        runPhase={runPhase}
        runStartedAt={runStartedAt}
        lastTurnRecap={lastTurnRecap}
      />
```

Destructure the three new values from the `useAgentChat(...)` call at `:245-247`.

- [ ] **Step 5: Run the tests**

Run: `cd apps/frontend && pnpm test -- StreamingStatusBar AgentChatWindow`

Expected: PASS. If an existing `AgentChatWindow` test asserted the bar is absent during
streaming, **update it** — that assertion encoded the behaviour this task reverses.

- [ ] **Step 6: Typecheck and brand-token scan**

```bash
cd apps/frontend && pnpm exec tsc --noEmit && node scripts/brand-tokens-scan.mjs
```

Expected: no errors, no new offenders.

- [ ] **Step 7: Commit**

```bash
git add apps/frontend/src/components/chat/
git commit -m "feat(chat): persistent run telemetry with elapsed time, phase and turn recap"
```

---

## Verification — the whole feature, on dev

- [ ] **Step 1: Ship to dev.** Merge; dev deploys on merge. Watch it:
  `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status`

- [ ] **Step 2: Confirm the observer subscribe is landing.** Search backend logs for a
  `sessions.observer.visibility` RPC on connect, and confirm **no**
  `sessions.messages.subscribe` appears anywhere.

- [ ] **Step 3: Behavioural check — Claude-in-Chrome on dev, not the journey test.**
  Open dev.flickmeon.ai/chat and send Friday something that takes a while, e.g.
  *"Read the last 20 messages in this conversation and summarise the themes."*

  **PASS:** a named phase and a clock appear before the first token; the clock **keeps ticking
  while the reply streams**; when it settles the strip changes to "Done in Xm Ys" and stays
  until the next message.
  **FAIL:** the strip vanishes when text starts arriving (the old behaviour), a raw enum like
  `starting_model` is shown, or a recap appears after an error.

- [ ] **Step 4: Failure path.** Trigger an error — easiest is a trial owner at their daily cap.
  **PASS:** the error card renders and there is **no** "Done in…" strip beneath it.

- [ ] **Step 5: Accessibility check.** With VoiceOver on (⌘F5), run a fresh provisioning flow
  and a chat run. **PASS:** the elapsed numbers are not read every second; phase and step
  changes are. **FAIL:** a stream of numbers.

- [ ] **Step 6: Promote to prod** only after 3–5 pass — `gh workflow run deploy.yml --ref main`.
  Merging alone ships nothing.

---

## Expected UX

**Before:** you send a message. Nothing for anywhere between two seconds and two minutes, then
text arrives. A pill reading "Working…" appears only in the silences, identical at second 3 and
second 300. You cannot tell a long run from a hung one, so you either wait too long or reload
and lose the run. When it finishes, nothing tells you it took four minutes.

**After:** "Reading the conversation… 4s" → "Starting the model… 11s" → the reply streams
**with the clock still running** → it settles into "Done in 4m 12s" and stays there until you
send the next message. Silence reads as work rather than failure, and a long turn is visibly a
long turn rather than a suspected hang. A screen-reader user hears the phase changes and the
completion, not the seconds.

## Not in this plan

- **The waiting-for-approval variant.** Upstream swaps the label, parks the animation and drops
  the clock when an approval blocks a run. We cannot test it yet: plugin and system-agent
  approvals are currently killed with `decision: null` before they reach us, and exec approvals
  are never created because `tools.exec.mode` is `"full"`. It belongs with **B1** in the
  cost-and-correctness plan, which is what makes approvals deliverable at all. Building an
  inert UI state ahead of that would be untestable.
- **`stream:"usage"` live token counter (D8), and tokens in the recap.** Real, and
  disproportionately useful on the $3/day trial cap, but it is a *spend* surface with its own
  design question — where does a running cost sit without making the product feel metered?
  Its own plan. The recap is deliberately shaped as `{ runtimeMs }` so adding
  `outputTokens` later is additive.
- **Run outcome on session rows (A8)** — same finding family, but it edits
  `hooks/useAgentSessions.ts`, which the session-rail plan owns. Doing it here would collide.
- **Roster badges (UI2)** — depend on B1, as above.
- **Moving the indicator into the transcript item flow.** Upstream's is a `reading-indicator`
  item that scrolls with the work; ours stays pinned below `MessageList`. Pinned is defensible
  for persistent telemetry and avoids a `MessageList` refactor for no behavioural gain.
