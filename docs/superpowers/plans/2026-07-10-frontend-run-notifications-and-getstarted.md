# Frontend Run Notifications + Get-Started Dismissal — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship two frontend-only quick wins: (A) the "Get started" pill disappears permanently on completion or dismissal, and (B) bottom-right sonner toasts announce background agent runs (webchat runs on non-watched agents + Teams/Paperclip heartbeat runs), updating in place from working → finished/failed and clicking through to the agent.

**Architecture:** Feature A is a render guard in `GetStarted.tsx` fed by the already-persisted `completedFired` flag. Feature B is a self-contained `RunNotifications` listener mounted once in the `/chat` shell: a pure reducer consumes the existing `onChatMessage` firehose + `teams.heartbeat.run.*` events (via a new refcounted `teams.subscribe` helper shared with `TeamsEventsProvider`), and emits toast actions executed through `src/lib/notify.tsx`. Zero backend changes.

**Tech Stack:** Next.js 16 App Router, React 19, TypeScript, sonner v2 (already mounted bottom-right in root layout), SWR, vitest 3 + React Testing Library, PostHog via `@/lib/analytics`.

**Spec:** `docs/superpowers/specs/2026-07-10-frontend-run-notifications-and-getstarted-design.md` (same branch).

## Global Constraints

- **Never run the full frontend test suite** (`pnpm test` with no file arg) — it is RAM-heavy. Single-file runs only, always in the foreground: `cd apps/frontend && pnpm vitest run <file>`.
- **Typecheck after adding/altering tests**: `cd apps/frontend && pnpm exec tsc --noEmit` (tsconfig includes test files; there is no `typecheck` script). CI runs exactly this plus `eslint` and `next build` — no vitest in CI, so local single-file runs are the only test gate.
- **Commit after every task. NEVER push and NEVER open a PR without the user's explicit go-ahead** — the final task stops and asks.
- Commit messages end with: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`.
- **No new npm dependencies.** Everything uses what's already installed.
- **No code comments referencing PRs, bug numbers, or this plan.**
- Two worktrees: Feature A in `.claude/worktrees/getstarted-dismiss` (branch `fix/getstarted-pill-dismiss`, created in Task 1); Feature B in the existing `.claude/worktrees/frontend-ux-quick-wins` (branch `feat/frontend-ux-quick-wins`).
- New analytics event names MUST be added to the `AnalyticsEvent` union in `apps/frontend/src/lib/analytics.ts` (~line 62) or `capture()` calls will not typecheck.
- All paths below are relative to the relevant worktree root unless absolute.

---

### Task 1: Get-started pill dismissal fix

**Files:**
- Create: worktree `.claude/worktrees/getstarted-dismiss` (branch `fix/getstarted-pill-dismiss` off `origin/main`)
- Modify: `apps/frontend/src/components/getstarted/useGetStarted.ts` (interface ~line 10-26, return ~line 200-210)
- Modify: `apps/frontend/src/components/getstarted/GetStarted.tsx` (12 → guard; whole file is 27 lines)
- Test: `apps/frontend/src/components/getstarted/__tests__/GetStarted.test.tsx` (rewrite — currently 25 lines with a fixed inline mock)

**Interfaces:**
- Consumes: `UseGetStarted` interface (existing fields incl. `dismissed: boolean`, `seen: boolean`, `progress: {done, total}`); the hook already tracks `completedFired` internally as persisted state.
- Produces: `UseGetStarted.completedFired: boolean` (new public field). `GetStarted` returns `null` when `dismissed || completedFired || (progress.total > 0 && progress.done === progress.total)`.

- [ ] **Step 1: Create the worktree**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync
git fetch origin main
git worktree add .claude/worktrees/getstarted-dismiss -b fix/getstarted-pill-dismiss origin/main
```

Expected: `HEAD is now at <sha> …`. All subsequent Task-1 paths are inside this worktree.

- [ ] **Step 2: Rewrite the test file with the mutable-mock idiom and the new failing cases**

Replace the entire contents of `apps/frontend/src/components/getstarted/__tests__/GetStarted.test.tsx` with:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { UseGetStarted } from "../useGetStarted";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const gsRef: { current: UseGetStarted } = { current: baseGs() };

function baseGs(): UseGetStarted {
  return {
    isOpen: true, minimized: false, tab: "do", activeArticle: null,
    open: vi.fn(), minimize: vi.fn(), close: vi.fn(), dismiss: vi.fn(), setTab: vi.fn(),
    openArticle: vi.fn(), backToLearn: vi.fn(),
    completed: { meet: false, chat: false, task: false, hire: false, channel: false },
    progress: { done: 0, total: 5 },
    dismissed: false, seen: true, completedFired: false,
  };
}

vi.mock("../useGetStarted", () => ({ useGetStarted: () => gsRef.current }));

import { GetStarted } from "../GetStarted";

function makeActions() {
  return { chat: vi.fn(), task: vi.fn(), hire: vi.fn(), channel: vi.fn() };
}

describe("GetStarted", () => {
  beforeEach(() => {
    gsRef.current = baseGs();
  });

  it("runs the matching action on a checklist CTA", async () => {
    const actions = makeActions();
    render(<GetStarted derived={{}} actions={actions} />);
    await userEvent.click(screen.getByRole("button", { name: /browse marketplace/i }));
    expect(actions.hire).toHaveBeenCalled();
  });

  it("renders the launcher while steps are incomplete", () => {
    gsRef.current = { ...baseGs(), isOpen: false };
    const { container } = render(<GetStarted derived={{}} actions={makeActions()} />);
    expect(container).not.toBeEmptyDOMElement();
  });

  it("renders nothing once dismissed", () => {
    gsRef.current = { ...baseGs(), dismissed: true };
    const { container } = render(<GetStarted derived={{}} actions={makeActions()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when completedFired was persisted (reload after completion)", () => {
    gsRef.current = { ...baseGs(), completedFired: true };
    const { container } = render(<GetStarted derived={{}} actions={makeActions()} />);
    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing the moment live progress hits total", () => {
    gsRef.current = {
      ...baseGs(),
      completed: { meet: true, chat: true, task: true, hire: true, channel: true },
      progress: { done: 5, total: 5 },
    };
    const { container } = render(<GetStarted derived={{}} actions={makeActions()} />);
    expect(container).toBeEmptyDOMElement();
  });
});
```

(The `gsRef`-referenced-from-`vi.mock`-factory idiom is established in this repo — see `src/components/chat/__tests__/ChatGPTOAuthStep.test.tsx`.)

- [ ] **Step 3: Run the test file — expect the new cases to fail**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/getstarted-dismiss/apps/frontend
pnpm vitest run src/components/getstarted/__tests__/GetStarted.test.tsx
```

Expected: FAIL — the TypeScript build of the test also flags `completedFired` missing from `UseGetStarted`; at runtime "renders nothing once dismissed" fails (a launcher renders today), as do the two completion cases.

- [ ] **Step 4: Expose `completedFired` from the hook**

In `apps/frontend/src/components/getstarted/useGetStarted.ts`:

(a) In the `UseGetStarted` interface (after `seen: boolean;` at ~line 25), add:

```ts
  completedFired: boolean;
```

(b) In the hook's return object (~line 209, the line reading `completed, progress, dismissed, seen,`), change to:

```ts
    completed, progress, dismissed, seen, completedFired,
  };
```

The state already exists (`const [completedFired, setCompletedFired] = useState<boolean>(!!stored.completedFired);` ~line 60) and already hydrates + persists via Clerk `unsafeMetadata`.

- [ ] **Step 5: Add the render guard in GetStarted.tsx**

Replace the body of `GetStarted` in `apps/frontend/src/components/getstarted/GetStarted.tsx` — specifically, replace the current line 12:

```tsx
  if (gs.dismissed) return <GetStartedLauncher done={gs.progress.done} total={gs.progress.total} pulse={false} onClick={gs.open} />;
```

with:

```tsx
  const complete = gs.progress.total > 0 && gs.progress.done === gs.progress.total;
  if (gs.dismissed || gs.completedFired || complete) return null;
```

Nothing else in the file changes. (The persisted `completedFired` is the flicker-free gate on reload — the derived completion signals arrive via async fetches, so `progress.done` briefly reads low on a hard reload; the live `complete` check hides the pill the instant the 5th step ticks, before the Clerk write round-trips.)

- [ ] **Step 6: Run the test file — expect all green**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/getstarted-dismiss/apps/frontend
pnpm vitest run src/components/getstarted/__tests__/GetStarted.test.tsx
```

Expected: PASS (5 tests).

- [ ] **Step 7: Run the neighboring hook test (interface change is additive, but verify)**

```bash
pnpm vitest run src/components/getstarted/__tests__/useGetStarted.test.tsx
```

Expected: PASS. If it asserts on the hook's full return shape, add `completedFired: false` expectations rather than weakening assertions.

- [ ] **Step 8: Typecheck + lint**

```bash
pnpm exec tsc --noEmit
pnpm lint
```

Expected: both clean. If the linter auto-fixes files, re-stage those edits before committing.

- [ ] **Step 9: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/getstarted-dismiss
git add apps/frontend/src/components/getstarted/
git commit -m "fix(frontend): remove Get started pill permanently on completion or dismissal

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: Refcounted teams-events subscription module

**Files:**
- Create: `apps/frontend/src/lib/teamsSubscription.ts`
- Test: `apps/frontend/src/lib/__tests__/teamsSubscription.test.ts`

(This task and all following run in `.claude/worktrees/frontend-ux-quick-wins`.)

**Interfaces:**
- Consumes: the gateway `send` function — `(payload: unknown) => boolean` (from `useGateway()`; silently returns `false` when the socket isn't open — that's why consumers must also re-send on connect).
- Produces (consumed by Tasks 5 & 6):
  - `acquireTeamsEvents(): void` — refcount++ only; deliberately sends nothing. Subscribe attempts are connection-driven — the existing provider test pins "no sends while disconnected" (`TeamsEventsProvider.test.tsx:158-161`), so acquire must never emit.
  - `releaseTeamsEvents(send: GatewaySend): void` — refcount--; sends `{type:"teams.unsubscribe"}` on the 1→0 transition; no-op at 0 (no underflow).
  - `resubscribeTeamsEvents(send: GatewaySend): void` — sends `{type:"teams.subscribe"}` iff refcount > 0. Call whenever `isConnected` is true (mount + every reconnect).
  - `_resetTeamsEventsForTests(): void`.
  - **Effect-ordering contract:** a consumer's acquire/release effect MUST be declared BEFORE its isConnected/resubscribe effect, so at mount-while-connected the refcount is already ≥ 1 when resubscribe runs (otherwise nothing subscribes).

- [ ] **Step 1: Write the failing test**

Create `apps/frontend/src/lib/__tests__/teamsSubscription.test.ts`:

```ts
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  acquireTeamsEvents,
  releaseTeamsEvents,
  resubscribeTeamsEvents,
  _resetTeamsEventsForTests,
} from "../teamsSubscription";

describe("teamsSubscription refcounting", () => {
  const send = vi.fn(() => true);

  beforeEach(() => {
    _resetTeamsEventsForTests();
    send.mockClear();
  });

  it("acquire alone never sends — subscribe attempts are connection-driven", () => {
    acquireTeamsEvents();
    acquireTeamsEvents();
    expect(send).not.toHaveBeenCalled();
  });

  it("resubscribe sends only while held", () => {
    resubscribeTeamsEvents(send);
    expect(send).not.toHaveBeenCalled();
    acquireTeamsEvents();
    resubscribeTeamsEvents(send);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({ type: "teams.subscribe" });
  });

  it("unsubscribes on the 1→0 transition only", () => {
    acquireTeamsEvents();
    acquireTeamsEvents();
    releaseTeamsEvents(send);
    expect(send).not.toHaveBeenCalled();
    releaseTeamsEvents(send);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({ type: "teams.unsubscribe" });
  });

  it("release at zero is a no-op and does not underflow", () => {
    releaseTeamsEvents(send);
    expect(send).not.toHaveBeenCalled();
    acquireTeamsEvents();
    releaseTeamsEvents(send);
    expect(send).toHaveBeenCalledTimes(1);
    expect(send).toHaveBeenCalledWith({ type: "teams.unsubscribe" });
  });
});
```

- [ ] **Step 2: Run it — expect module-not-found failure**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins/apps/frontend
pnpm vitest run src/lib/__tests__/teamsSubscription.test.ts
```

Expected: FAIL — cannot resolve `../teamsSubscription`.

- [ ] **Step 3: Implement the module**

Create `apps/frontend/src/lib/teamsSubscription.ts`:

```ts
/**
 * Refcounted ownership of the per-connection `teams.subscribe` registration.
 *
 * The backend teams broker tracks subscriptions per WebSocket CONNECTION id,
 * and every consumer in a tab shares one gateway connection. A consumer that
 * sends `teams.unsubscribe` on unmount therefore kills the subscription for
 * every other consumer on the same socket. All subscribe/unsubscribe traffic
 * must flow through this module instead of calling `send` directly.
 *
 * Consumer contract (two effects, IN THIS ORDER):
 *   1. acquire on mount, release on unmount — acquire never sends; it only
 *      registers interest.
 *   2. resubscribe whenever isConnected is true (mount + every reconnect) —
 *      the only place subscribe frames are emitted, so nothing is sent while
 *      the socket is closed (`send` would silently drop it anyway).
 * Duplicate subscribe sends are safe — the broker's subscribe is idempotent.
 */
export type GatewaySend = (payload: unknown) => boolean;

let refCount = 0;

export function acquireTeamsEvents(): void {
  refCount += 1;
}

export function releaseTeamsEvents(send: GatewaySend): void {
  if (refCount === 0) return;
  refCount -= 1;
  if (refCount === 0) {
    send({ type: "teams.unsubscribe" });
  }
}

export function resubscribeTeamsEvents(send: GatewaySend): void {
  if (refCount > 0) {
    send({ type: "teams.subscribe" });
  }
}

export function _resetTeamsEventsForTests(): void {
  refCount = 0;
}
```

- [ ] **Step 4: Run the test — expect pass**

```bash
pnpm vitest run src/lib/__tests__/teamsSubscription.test.ts
```

Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins
git add apps/frontend/src/lib/teamsSubscription.ts apps/frontend/src/lib/__tests__/teamsSubscription.test.ts
git commit -m "feat(frontend): refcounted teams.subscribe ownership shared across consumers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: Pure run→toast reducer

**Files:**
- Create: `apps/frontend/src/components/notifications/runToastReducer.ts`
- Test: `apps/frontend/src/components/notifications/__tests__/runToastReducer.test.ts`

**Interfaces:**
- Consumes: `ChatIncomingMessage` from `@/hooks/useGateway` (discriminated union; the relevant members: streaming frames with required `runId` + optional `agent_id`, and `{ type: "run_ended"; runId: string; outcome: "done" | "error" | "aborted"; agent_id?: string }`). Teams payloads arrive as `unknown` with camelCase fields `runId`, `agentId`, `status` (status domain: `queued | scheduled_retry | running | succeeded | failed | cancelled | timed_out`; terminal = `succeeded/failed/cancelled/timed_out`).
- Produces (consumed by Task 5):
  - `createRunToastState(): RunToastState`
  - `reduceChatFrame(state: RunToastState, msg: ChatIncomingMessage, ctx: RunToastContext): RunToastAction[]`
  - `reduceTeamsRunEvent(state: RunToastState, event: string, payload: unknown, ctx: RunToastContext): RunToastAction[]`
  - `RunToastContext = { selectedAgentId: string | null; chatViewActive: boolean; teamsPanelActive: boolean }`
  - `RunToastAction = { kind: "working" | "success" | "failure"; key: string; agentId: string; source: "chat" | "teams" } | { kind: "dismiss"; key: string }`
  - `MAX_TRACKED_RUNS = 200`
  - State mutates its internal Maps in place; the return value is the action list.

- [ ] **Step 1: Write the failing tests**

Create `apps/frontend/src/components/notifications/__tests__/runToastReducer.test.ts`:

```ts
import { describe, it, expect } from "vitest";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import {
  createRunToastState,
  reduceChatFrame,
  reduceTeamsRunEvent,
  MAX_TRACKED_RUNS,
  type RunToastContext,
} from "../runToastReducer";

const bgCtx: RunToastContext = {
  selectedAgentId: "agent-sel",
  chatViewActive: true,
  teamsPanelActive: false,
};

function chunk(runId: string, agentId?: string): ChatIncomingMessage {
  return { type: "chunk", content: "hi", runId, agent_id: agentId };
}

function runEnded(
  runId: string,
  outcome: "done" | "error" | "aborted",
  agentId?: string,
): ChatIncomingMessage {
  return { type: "run_ended", runId, outcome, agent_id: agentId };
}

describe("reduceChatFrame — run start", () => {
  it("emits a working action on the first frame of a background run", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("emits nothing on subsequent frames of a tracked run", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx)).toEqual([]);
  });

  it("suppresses the watched agent in chat view but still tracks the run", () => {
    const s = createRunToastState();
    const watched: ChatIncomingMessage = chunk("r1", "agent-sel");
    expect(reduceChatFrame(s, watched, bgCtx)).toEqual([]);
    // terminal while STILL watched: stays silent
    expect(reduceChatFrame(s, runEnded("r1", "done", "agent-sel"), bgCtx)).toEqual([]);
  });

  it("does not suppress the selected agent while a panel is open", () => {
    const s = createRunToastState();
    const ctx: RunToastContext = { ...bgCtx, chatViewActive: false };
    expect(reduceChatFrame(s, chunk("r1", "agent-sel"), ctx)).toEqual([
      { kind: "working", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
  });

  it("ignores frames without runId or agent_id, btw_result, and passive frames", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, chunk("r1", undefined), bgCtx)).toEqual([]);
    expect(
      reduceChatFrame(s, { type: "btw_result", question: "q", text: "t" }, bgCtx),
    ).toEqual([]);
    expect(reduceChatFrame(s, { type: "heartbeat" }, bgCtx)).toEqual([]);
    expect(
      reduceChatFrame(s, { type: "run_active", active: true, agent_id: "a" }, bgCtx),
    ).toEqual([]);
    expect(
      reduceChatFrame(s, { type: "done", runId: "r1", agent_id: "agent-bg" }, bgCtx),
    ).toEqual([]);
  });
});

describe("reduceChatFrame — terminals (run_ended only)", () => {
  it("flips a shown run to success on outcome done", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "done"), bgCtx)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("flips a shown run to failure on outcome error", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "error"), bgCtx)).toEqual([
      { kind: "failure", key: "r1", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("dismisses a shown run on outcome aborted", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-bg"), bgCtx);
    expect(reduceChatFrame(s, runEnded("r1", "aborted"), bgCtx)).toEqual([
      { kind: "dismiss", key: "r1" },
    ]);
  });

  it("toasts a never-seen terminal when unsuppressed (reconnect gap)", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, runEnded("r9", "done", "agent-bg"), bgCtx)).toEqual([
      { kind: "success", key: "r9", agentId: "agent-bg", source: "chat" },
    ]);
  });

  it("shows completion for a run that started suppressed if the user navigated away", () => {
    const s = createRunToastState();
    reduceChatFrame(s, chunk("r1", "agent-sel"), bgCtx); // suppressed at start
    const awayCtx: RunToastContext = { ...bgCtx, chatViewActive: false };
    expect(reduceChatFrame(s, runEnded("r1", "done"), awayCtx)).toEqual([
      { kind: "success", key: "r1", agentId: "agent-sel", source: "chat" },
    ]);
  });

  it("ignores a never-seen terminal with no agent_id", () => {
    const s = createRunToastState();
    expect(reduceChatFrame(s, runEnded("r9", "done"), bgCtx)).toEqual([]);
  });
});

describe("reduceChatFrame — eviction", () => {
  it("caps tracked runs, evicting oldest first", () => {
    const s = createRunToastState();
    for (let i = 0; i < MAX_TRACKED_RUNS + 5; i++) {
      reduceChatFrame(s, chunk(`r${i}`, "agent-bg"), bgCtx);
    }
    expect(s.chatRuns.size).toBe(MAX_TRACKED_RUNS);
    expect(s.chatRuns.has("r0")).toBe(false);
    expect(s.chatRuns.has(`r${MAX_TRACKED_RUNS + 4}`)).toBe(true);
  });
});

describe("reduceTeamsRunEvent", () => {
  it("emits working on queued when no teams panel is open", () => {
    const s = createRunToastState();
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t1", agentId: "pa1" }, bgCtx),
    ).toEqual([{ kind: "working", key: "teams:t1", agentId: "pa1", source: "teams" }]);
  });

  it("suppresses working while a teams panel is open, then toasts terminal after leaving", () => {
    const s = createRunToastState();
    const onTeams: RunToastContext = { ...bgCtx, teamsPanelActive: true };
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t1", agentId: "pa1" }, onTeams),
    ).toEqual([]);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t1", agentId: "pa1", status: "succeeded" }, bgCtx),
    ).toEqual([{ kind: "success", key: "teams:t1", agentId: "pa1", source: "teams" }]);
  });

  it("treats running/scheduled_retry as working states without re-emitting", () => {
    const s = createRunToastState();
    reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t1", agentId: "pa1" }, bgCtx);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t1", agentId: "pa1", status: "running" }, bgCtx),
    ).toEqual([]);
  });

  it("registers a run first seen via a running status event", () => {
    const s = createRunToastState();
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t2", agentId: "pa1", status: "running" }, bgCtx),
    ).toEqual([{ kind: "working", key: "teams:t2", agentId: "pa1", source: "teams" }]);
  });

  it("maps failed and timed_out to failure", () => {
    const s = createRunToastState();
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t3", agentId: "pa1", status: "failed" }, bgCtx),
    ).toEqual([{ kind: "failure", key: "teams:t3", agentId: "pa1", source: "teams" }]);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t4", agentId: "pa1", status: "timed_out" }, bgCtx),
    ).toEqual([{ kind: "failure", key: "teams:t4", agentId: "pa1", source: "teams" }]);
  });

  it("dismisses a shown run on cancelled, silently drops an unshown one", () => {
    const s = createRunToastState();
    reduceTeamsRunEvent(s, "teams.heartbeat.run.queued", { runId: "t5", agentId: "pa1" }, bgCtx);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t5", agentId: "pa1", status: "cancelled" }, bgCtx),
    ).toEqual([{ kind: "dismiss", key: "teams:t5" }]);
    expect(
      reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t6", agentId: "pa1", status: "cancelled" }, bgCtx),
    ).toEqual([]);
  });

  it("ignores other teams events and malformed payloads", () => {
    const s = createRunToastState();
    expect(reduceTeamsRunEvent(s, "teams.activity.logged", { runId: "x" }, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", null, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { agentId: "pa1", status: "failed" }, bgCtx)).toEqual([]);
    expect(reduceTeamsRunEvent(s, "teams.heartbeat.run.status", { runId: "t7", status: 42 }, bgCtx)).toEqual([]);
  });
});
```

- [ ] **Step 2: Run — expect module-not-found failure**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins/apps/frontend
pnpm vitest run src/components/notifications/__tests__/runToastReducer.test.ts
```

Expected: FAIL — cannot resolve `../runToastReducer`.

- [ ] **Step 3: Implement the reducer**

Create `apps/frontend/src/components/notifications/runToastReducer.ts`:

```ts
import type { ChatIncomingMessage } from "@/hooks/useGateway";

/**
 * Pure frame→toast-action logic for RunNotifications. The component owns
 * subscriptions and toast side effects; everything decidable from data is
 * decided here so it can be tested without React.
 *
 * State maps mutate in place (Map insertion order doubles as the eviction
 * queue); the returned action list is the only output the caller executes.
 */

export interface RunToastContext {
  selectedAgentId: string | null;
  chatViewActive: boolean;
  teamsPanelActive: boolean;
}

export type RunToastAction =
  | { kind: "working" | "success" | "failure"; key: string; agentId: string; source: "chat" | "teams" }
  | { kind: "dismiss"; key: string };

interface TrackedRun {
  agentId: string;
  toastShown: boolean;
}

export interface RunToastState {
  chatRuns: Map<string, TrackedRun>;
  teamsRuns: Map<string, TrackedRun>;
}

export function createRunToastState(): RunToastState {
  return { chatRuns: new Map(), teamsRuns: new Map() };
}

export const MAX_TRACKED_RUNS = 200;

// A run the user is already watching (its agent selected, chat visible) never
// needs a "working" toast. Suppression is re-evaluated per event: a run that
// started watched still gets its completion toast if the user has navigated
// away by the time it ends.
function chatSuppressed(agentId: string, ctx: RunToastContext): boolean {
  return ctx.chatViewActive && ctx.selectedAgentId === agentId;
}

function evictOldest(map: Map<string, TrackedRun>): void {
  while (map.size > MAX_TRACKED_RUNS) {
    const oldest = map.keys().next().value;
    if (oldest === undefined) return;
    map.delete(oldest);
  }
}

// Streaming frame types that indicate a run is producing output. `done` and
// `error` are deliberately absent: only `run_ended` is authoritative (a `done`
// can be the interim final that closes a tool-approval bracket).
const CHAT_START_TYPES = new Set<ChatIncomingMessage["type"]>([
  "chunk",
  "thinking",
  "tool_start",
  "tool_end",
  "tool_error",
  "tool_progress",
  "command_output",
  "agent_activity",
]);

export function reduceChatFrame(
  state: RunToastState,
  msg: ChatIncomingMessage,
  ctx: RunToastContext,
): RunToastAction[] {
  if (msg.type === "run_ended") {
    const tracked = state.chatRuns.get(msg.runId);
    state.chatRuns.delete(msg.runId);
    const agentId = tracked?.agentId ?? msg.agent_id;
    if (!agentId) return [];
    if (msg.outcome === "aborted") {
      return tracked?.toastShown ? [{ kind: "dismiss", key: msg.runId }] : [];
    }
    const show = tracked?.toastShown || !chatSuppressed(agentId, ctx);
    if (!show) return [];
    return [
      {
        kind: msg.outcome === "done" ? "success" : "failure",
        key: msg.runId,
        agentId,
        source: "chat",
      },
    ];
  }

  if (!CHAT_START_TYPES.has(msg.type)) return [];
  const runId = "runId" in msg ? msg.runId : undefined;
  const agentId = "agent_id" in msg ? msg.agent_id : undefined;
  if (!runId || !agentId) return [];
  if (state.chatRuns.has(runId)) return [];

  const toastShown = !chatSuppressed(agentId, ctx);
  state.chatRuns.set(runId, { agentId, toastShown });
  evictOldest(state.chatRuns);
  return toastShown
    ? [{ kind: "working", key: runId, agentId, source: "chat" }]
    : [];
}

const TEAMS_WORKING_STATUSES = new Set(["queued", "running", "scheduled_retry"]);
const TEAMS_FAILURE_STATUSES = new Set(["failed", "timed_out"]);

function readString(v: unknown): string | undefined {
  return typeof v === "string" && v ? v : undefined;
}

export function reduceTeamsRunEvent(
  state: RunToastState,
  event: string,
  payload: unknown,
  ctx: RunToastContext,
): RunToastAction[] {
  if (event !== "teams.heartbeat.run.queued" && event !== "teams.heartbeat.run.status") {
    return [];
  }
  const p = (payload ?? {}) as Record<string, unknown>;
  const runId = readString(p.runId);
  if (!runId) return [];
  const agentId = readString(p.agentId) ?? "";
  const key = `teams:${runId}`;
  const status =
    event === "teams.heartbeat.run.queued" ? "queued" : readString(p.status);
  if (!status) return [];

  if (TEAMS_WORKING_STATUSES.has(status)) {
    if (state.teamsRuns.has(runId)) return [];
    const toastShown = !ctx.teamsPanelActive;
    state.teamsRuns.set(runId, { agentId, toastShown });
    evictOldest(state.teamsRuns);
    return toastShown
      ? [{ kind: "working", key, agentId, source: "teams" }]
      : [];
  }

  const tracked = state.teamsRuns.get(runId);
  state.teamsRuns.delete(runId);
  if (status === "cancelled") {
    return tracked?.toastShown ? [{ kind: "dismiss", key }] : [];
  }
  if (status === "succeeded" || TEAMS_FAILURE_STATUSES.has(status)) {
    const show = tracked?.toastShown || !ctx.teamsPanelActive;
    if (!show) return [];
    return [
      {
        kind: status === "succeeded" ? "success" : "failure",
        key,
        agentId,
        source: "teams",
      },
    ];
  }
  return [];
}
```

- [ ] **Step 4: Run — expect pass**

```bash
pnpm vitest run src/components/notifications/__tests__/runToastReducer.test.ts
```

Expected: PASS (all cases).

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins
git add apps/frontend/src/components/notifications/
git commit -m "feat(frontend): pure run-to-toast reducer for background run notifications

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: notify.tsx — loading, failure-with-action, dismiss helpers

**Files:**
- Modify: `apps/frontend/src/lib/notify.tsx` (current API: `notify`, `notifySuccess` — both `(message, opts?: NotifyOptions)` with `dedupeKey` → sonner `id`, 4s auto-dismiss; `notifyError` — sticky with hardcoded `ReportButton` action; `notifyApiError`)
- Test: `apps/frontend/src/lib/__tests__/notify.test.tsx` (exists; mocks `sonner` with `toastDefault`/`toastError`/`toastSuccess` fns)

**Interfaces:**
- Consumes: existing `NotifyOptions { dedupeKey?: string }`, `AUTO_DISMISS_MS = 4000`, the `sonner` `toast` object.
- Produces (consumed by Task 5):
  - `NotifyActionOptions extends NotifyOptions { action?: { label: string; onClick: () => void } }`
  - `notifyLoading(message: string, opts?: NotifyActionOptions): string | number` — sonner `toast.loading`, `duration: Infinity`.
  - `notifyFailure(message: string, opts?: NotifyActionOptions): string | number` — sonner `toast.error`, sticky, custom action (unlike `notifyError`, which stays reserved for the Report flow).
  - `notifySuccess` signature widens to `(message: string, opts?: NotifyActionOptions)` — backwards compatible.
  - `dismissNotification(id: string | number): void` — sonner `toast.dismiss`.

- [ ] **Step 1: Extend the sonner mock + add failing tests**

In `apps/frontend/src/lib/__tests__/notify.test.tsx`, extend the existing sonner mock — the current block:

```tsx
const toastError = vi.fn();
const toastSuccess = vi.fn();
const toastDefault = vi.fn();
vi.mock("sonner", () => ({
  toast: Object.assign((...args: unknown[]) => toastDefault(...args), {
    error: (...args: unknown[]) => toastError(...args),
    success: (...args: unknown[]) => toastSuccess(...args),
  }),
}));
```

becomes:

```tsx
const toastError = vi.fn();
const toastSuccess = vi.fn();
const toastDefault = vi.fn();
const toastLoading = vi.fn();
const toastDismiss = vi.fn();
vi.mock("sonner", () => ({
  toast: Object.assign((...args: unknown[]) => toastDefault(...args), {
    error: (...args: unknown[]) => toastError(...args),
    success: (...args: unknown[]) => toastSuccess(...args),
    loading: (...args: unknown[]) => toastLoading(...args),
    dismiss: (...args: unknown[]) => toastDismiss(...args),
  }),
}));
```

Add the new imports to the existing import line from `../notify` (`notifyLoading`, `notifyFailure`, `dismissNotification`), add `toastLoading.mockClear(); toastDismiss.mockClear();` to the existing `beforeEach`, and append these describe blocks at the end of the file:

```tsx
describe("notifyLoading", () => {
  it("is persistent and keyed by dedupeKey", () => {
    notifyLoading("working…", { dedupeKey: "run-1" });
    const [msg, opts] = toastLoading.mock.calls.at(-1)!;
    expect(msg).toBe("working…");
    expect(opts).toMatchObject({ id: "run-1", duration: Infinity });
  });
});

describe("notifyFailure", () => {
  it("is sticky and forwards a custom action", () => {
    const onClick = vi.fn();
    notifyFailure("run failed", { dedupeKey: "run-1", action: { label: "View", onClick } });
    const [msg, opts] = toastError.mock.calls.at(-1)!;
    expect(msg).toBe("run failed");
    expect(opts).toMatchObject({ id: "run-1", duration: Infinity, action: { label: "View" } });
  });
});

describe("notifySuccess action passthrough", () => {
  it("forwards a custom action while keeping auto-dismiss", () => {
    const onClick = vi.fn();
    notifySuccess("run finished", { dedupeKey: "run-1", action: { label: "View", onClick } });
    const [, opts] = toastSuccess.mock.calls.at(-1)!;
    expect(opts).toMatchObject({ id: "run-1", action: { label: "View" } });
    expect(opts.duration).not.toBe(Infinity);
  });
});

describe("dismissNotification", () => {
  it("delegates to toast.dismiss with the key", () => {
    dismissNotification("run-1");
    expect(toastDismiss).toHaveBeenCalledWith("run-1");
  });
});
```

- [ ] **Step 2: Run — expect the new tests to fail**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins/apps/frontend
pnpm vitest run src/lib/__tests__/notify.test.tsx
```

Expected: FAIL — `notifyLoading` etc. are not exported. Pre-existing tests must still pass.

- [ ] **Step 3: Implement in notify.tsx**

In `apps/frontend/src/lib/notify.tsx`:

(a) After the existing `NotifyOptions` interface, add:

```tsx
export interface NotifyActionOptions extends NotifyOptions {
  /** Optional action button rendered on the toast's trailing edge. */
  action?: { label: string; onClick: () => void };
}
```

(b) Widen `notifySuccess` to accept and forward the action (auto-dismiss unchanged):

```tsx
/** Success toast (auto-dismisses). */
export function notifySuccess(message: string, opts?: NotifyActionOptions): string | number {
  return toast.success(message, {
    id: opts?.dedupeKey,
    duration: AUTO_DISMISS_MS,
    action: opts?.action,
  });
}
```

(c) Add the two new helpers (below `notifySuccess`):

```tsx
/**
 * Persistent progress toast (spinner, never auto-dismisses). Resolve it by
 * calling notifySuccess/notifyFailure/dismissNotification with the same
 * dedupeKey — sonner replaces the toast in place by id.
 */
export function notifyLoading(message: string, opts?: NotifyActionOptions): string | number {
  return toast.loading(message, {
    id: opts?.dedupeKey,
    duration: Infinity,
    action: opts?.action,
  });
}

/**
 * Sticky failure toast with a caller-supplied action. Distinct from
 * notifyError, which owns the support Report flow and its ReportButton.
 */
export function notifyFailure(message: string, opts?: NotifyActionOptions): string | number {
  return toast.error(message, {
    id: opts?.dedupeKey,
    duration: Infinity,
    action: opts?.action,
  });
}

export function dismissNotification(id: string | number): void {
  toast.dismiss(id);
}
```

- [ ] **Step 4: Run — expect pass (old + new)**

```bash
pnpm vitest run src/lib/__tests__/notify.test.tsx
```

Expected: PASS.

- [ ] **Step 5: Typecheck (notifySuccess signature widened — ~15 existing call sites must still compile)**

```bash
pnpm exec tsc --noEmit
```

Expected: clean.

- [ ] **Step 6: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins
git add apps/frontend/src/lib/notify.tsx apps/frontend/src/lib/__tests__/notify.test.tsx
git commit -m "feat(frontend): notifyLoading/notifyFailure/dismissNotification + action passthrough

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: RunNotifications component

**Files:**
- Create: `apps/frontend/src/components/notifications/RunNotifications.tsx`
- Modify: `apps/frontend/src/lib/analytics.ts` (add two members to the `AnalyticsEvent` union, ~line 62)
- Test: `apps/frontend/src/components/notifications/__tests__/RunNotifications.test.tsx`

**Interfaces:**
- Consumes: `useGateway()` → `{ isConnected, send, onEvent, onChatMessage }` (`onChatMessage(handler): unsub` receives every chat frame; `onEvent(handler): unsub` receives `(eventName, payload)` already unwrapped); `useAgents()` → `{ agents: Agent[] }` with `Agent { id, name?, identity? }` and `agentDisplayName(agent)` (both re-exported from `@/hooks/useAgents`); Task 2's `acquireTeamsEvents`/`releaseTeamsEvents`/`resubscribeTeamsEvents`; Task 3's reducer; Task 4's `notifyLoading`/`notifySuccess`/`notifyFailure`/`dismissNotification`; `capture` from `@/lib/analytics`.
- Produces (consumed by Task 7): `RunNotifications` component with props `{ selectedAgentId: string | null; activeView: "chat" | "control"; activePanel: string }`, rendering `null`. Toast click-through dispatches `new CustomEvent("selectAgent", { detail: { agentId, source: "notification" } })` — Task 7 adds the ChatLayout listener keyed on `source === "notification"`.

- [ ] **Step 1: Write the failing component test**

Create `apps/frontend/src/components/notifications/__tests__/RunNotifications.test.tsx`:

```tsx
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, act } from "@testing-library/react";
import type { ComponentProps } from "react";
import type { ChatIncomingMessage } from "@/hooks/useGateway";
import { _resetTeamsEventsForTests } from "@/lib/teamsSubscription";

vi.mock("@/lib/analytics", () => ({ capture: vi.fn() }));

const notifyLoading = vi.fn();
const notifySuccess = vi.fn();
const notifyFailure = vi.fn();
const dismissNotification = vi.fn();
vi.mock("@/lib/notify", () => ({
  notifyLoading: (...a: unknown[]) => notifyLoading(...a),
  notifySuccess: (...a: unknown[]) => notifySuccess(...a),
  notifyFailure: (...a: unknown[]) => notifyFailure(...a),
  dismissNotification: (...a: unknown[]) => dismissNotification(...a),
}));

vi.mock("@/hooks/useAgents", () => ({
  useAgents: () => ({ agents: [{ id: "agent-bg", name: "Atlas" }] }),
  agentDisplayName: (a: { id: string; name?: string; identity?: { name?: string } }) =>
    a.identity?.name || a.name || a.id,
}));

let chatHandlers: Array<(msg: ChatIncomingMessage) => void> = [];
let eventHandlers: Array<(event: string, data: unknown) => void> = [];
const send = vi.fn(() => true);

vi.mock("@/hooks/useGateway", () => ({
  useGateway: () => ({
    isConnected: true,
    nodeConnected: false,
    error: null,
    reconnectAttempt: 0,
    send,
    sendReq: vi.fn(),
    sendChat: vi.fn(),
    onEvent: (h: (event: string, data: unknown) => void) => {
      eventHandlers.push(h);
      return () => {
        eventHandlers = eventHandlers.filter((x) => x !== h);
      };
    },
    onChatMessage: (h: (msg: ChatIncomingMessage) => void) => {
      chatHandlers.push(h);
      return () => {
        chatHandlers = chatHandlers.filter((x) => x !== h);
      };
    },
    reconnect: vi.fn(),
  }),
}));

import { RunNotifications } from "../RunNotifications";

function dispatchChat(msg: ChatIncomingMessage): void {
  act(() => {
    chatHandlers.forEach((h) => h(msg));
  });
}

function dispatchEvent(event: string, payload: unknown): void {
  act(() => {
    eventHandlers.forEach((h) => h(event, payload));
  });
}

function mount(props?: Partial<ComponentProps<typeof RunNotifications>>) {
  return render(
    <RunNotifications
      selectedAgentId="agent-sel"
      activeView="chat"
      activePanel="overview"
      {...props}
    />,
  );
}

beforeEach(() => {
  chatHandlers = [];
  eventHandlers = [];
  _resetTeamsEventsForTests();
  send.mockClear();
  notifyLoading.mockClear();
  notifySuccess.mockClear();
  notifyFailure.mockClear();
  dismissNotification.mockClear();
});

describe("RunNotifications", () => {
  it("acquires the teams subscription on mount and releases on unmount", () => {
    const { unmount } = mount();
    expect(send).toHaveBeenCalledWith({ type: "teams.subscribe" });
    unmount();
    expect(send).toHaveBeenCalledWith({ type: "teams.unsubscribe" });
  });

  it("shows a named working toast for a background chat run", () => {
    mount();
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-bg" });
    expect(notifyLoading).toHaveBeenCalledTimes(1);
    const [msg, opts] = notifyLoading.mock.calls[0] as [string, { dedupeKey: string }];
    expect(msg).toContain("Atlas");
    expect(opts.dedupeKey).toBe("r1");
  });

  it("suppresses the run the user is watching", () => {
    mount();
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-sel" });
    expect(notifyLoading).not.toHaveBeenCalled();
  });

  it("resolves a shown run to success with a View action on run_ended done", () => {
    mount();
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-bg" });
    dispatchChat({ type: "run_ended", runId: "r1", outcome: "done", agent_id: "agent-bg" });
    expect(notifySuccess).toHaveBeenCalledTimes(1);
    const [, opts] = notifySuccess.mock.calls[0] as [
      string,
      { dedupeKey: string; action: { label: string; onClick: () => void } },
    ];
    expect(opts.dedupeKey).toBe("r1");
    expect(opts.action.label).toBe("View");
  });

  it("clicking View dispatches a sourced selectAgent event", () => {
    mount();
    const listener = vi.fn();
    window.addEventListener("selectAgent", listener);
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-bg" });
    dispatchChat({ type: "run_ended", runId: "r1", outcome: "done", agent_id: "agent-bg" });
    const [, opts] = notifySuccess.mock.calls[0] as [
      string,
      { action: { onClick: () => void } },
    ];
    act(() => opts.action.onClick());
    const evt = listener.mock.calls[0][0] as CustomEvent<{ agentId: string; source?: string }>;
    expect(evt.detail).toEqual({ agentId: "agent-bg", source: "notification" });
    window.removeEventListener("selectAgent", listener);
  });

  it("dismisses on aborted and marks failures sticky", () => {
    mount();
    dispatchChat({ type: "chunk", content: "…", runId: "r1", agent_id: "agent-bg" });
    dispatchChat({ type: "run_ended", runId: "r1", outcome: "aborted", agent_id: "agent-bg" });
    expect(dismissNotification).toHaveBeenCalledWith("r1");

    dispatchChat({ type: "chunk", content: "…", runId: "r2", agent_id: "agent-bg" });
    dispatchChat({ type: "run_ended", runId: "r2", outcome: "error", agent_id: "agent-bg" });
    expect(notifyFailure).toHaveBeenCalledTimes(1);
  });

  it("toasts teams heartbeat runs through their lifecycle", () => {
    mount();
    dispatchEvent("teams.heartbeat.run.queued", { runId: "t1", agentId: "pa1" });
    expect(notifyLoading).toHaveBeenCalledTimes(1);
    expect((notifyLoading.mock.calls[0] as [string, { dedupeKey: string }])[1].dedupeKey).toBe("teams:t1");
    dispatchEvent("teams.heartbeat.run.status", { runId: "t1", agentId: "pa1", status: "succeeded" });
    expect(notifySuccess).toHaveBeenCalledTimes(1);
  });

  it("ignores btw_result and non-run teams events", () => {
    mount();
    dispatchChat({ type: "btw_result", question: "q", text: "t" });
    dispatchEvent("teams.activity.logged", { runId: "t1" });
    expect(notifyLoading).not.toHaveBeenCalled();
    expect(notifySuccess).not.toHaveBeenCalled();
  });
});
```

- [ ] **Step 2: Run — expect module-not-found failure**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins/apps/frontend
pnpm vitest run src/components/notifications/__tests__/RunNotifications.test.tsx
```

Expected: FAIL — cannot resolve `../RunNotifications`.

- [ ] **Step 3: Add the analytics events**

In `apps/frontend/src/lib/analytics.ts`, find the `AnalyticsEvent` union (declared ~line 62, grouped/alphabetized per surface) and add two members in the appropriate alphabetical position:

```ts
  | "run_notification_clicked"
  | "run_notification_shown"
```

- [ ] **Step 4: Implement the component**

Create `apps/frontend/src/components/notifications/RunNotifications.tsx`:

```tsx
"use client";

import { useEffect, useRef } from "react";
import { useGateway } from "@/hooks/useGateway";
import { useAgents, agentDisplayName, type Agent } from "@/hooks/useAgents";
import { capture } from "@/lib/analytics";
import {
  notifyLoading,
  notifySuccess,
  notifyFailure,
  dismissNotification,
} from "@/lib/notify";
import { pushSearchParams } from "@/lib/shellNav";
import {
  acquireTeamsEvents,
  releaseTeamsEvents,
  resubscribeTeamsEvents,
} from "@/lib/teamsSubscription";
import {
  createRunToastState,
  reduceChatFrame,
  reduceTeamsRunEvent,
  type RunToastAction,
  type RunToastContext,
} from "./runToastReducer";

/**
 * Global bottom-right run notifications. Renders nothing; consumes the
 * unfiltered chat-frame stream (background webchat runs already reach this
 * browser and are dropped by useAgentChat's demux) plus teams.heartbeat.run.*
 * events, and drives one live toast per run keyed by runId.
 *
 * Every handler is wrapped so a notification bug can never break chat.
 */
export interface RunNotificationsProps {
  selectedAgentId: string | null;
  activeView: "chat" | "control";
  activePanel: string;
}

function agentName(agentId: string, agents: Agent[]): string {
  const agent = agents.find((a) => a.id === agentId);
  return agent ? agentDisplayName(agent) : "Agent";
}

function viewAgentAction(agentId: string): { label: string; onClick: () => void } {
  return {
    label: "View",
    onClick: () => {
      capture("run_notification_clicked", { source: "chat" });
      // ChatLayout owns selection (sidebar, session store, ?panel cleanup); it
      // listens for externally-sourced selectAgent events and routes them
      // through its own handleSelectAgent.
      window.dispatchEvent(
        new CustomEvent("selectAgent", {
          detail: { agentId, source: "notification" },
        }),
      );
    },
  };
}

function viewTeamsAction(): { label: string; onClick: () => void } {
  return {
    label: "View",
    onClick: () => {
      capture("run_notification_clicked", { source: "teams" });
      const params = new URLSearchParams(window.location.search);
      params.set("panel", "teams-activity");
      params.delete("detailId");
      pushSearchParams(params);
    },
  };
}

function executeAction(action: RunToastAction, agents: Agent[]): void {
  if (action.kind === "dismiss") {
    dismissNotification(action.key);
    return;
  }
  const isChat = action.source === "chat";
  const name = isChat ? agentName(action.agentId, agents) : null;
  const view = isChat ? viewAgentAction(action.agentId) : viewTeamsAction();
  capture("run_notification_shown", { source: action.source, kind: action.kind });
  if (action.kind === "working") {
    notifyLoading(isChat ? `${name} is working…` : "A team agent run started…", {
      dedupeKey: action.key,
      action: view,
    });
  } else if (action.kind === "success") {
    notifySuccess(isChat ? `${name} finished a run` : "Team agent run completed", {
      dedupeKey: action.key,
      action: view,
    });
  } else {
    notifyFailure(isChat ? `${name} run failed` : "Team agent run failed", {
      dedupeKey: action.key,
      action: view,
    });
  }
}

export function RunNotifications({
  selectedAgentId,
  activeView,
  activePanel,
}: RunNotificationsProps): null {
  const { isConnected, send, onEvent, onChatMessage } = useGateway();
  const { agents } = useAgents();

  const stateRef = useRef(createRunToastState());
  const ctxRef = useRef<RunToastContext>({
    selectedAgentId: null,
    chatViewActive: true,
    teamsPanelActive: false,
  });
  ctxRef.current = {
    selectedAgentId,
    chatViewActive: activeView === "chat",
    teamsPanelActive: activeView === "control" && activePanel.startsWith("teams-"),
  };
  const agentsRef = useRef<Agent[]>([]);
  agentsRef.current = agents;

  // Ordering contract (see teamsSubscription): acquire/release MUST be
  // declared before the resubscribe effect so the refcount is ≥ 1 when the
  // mount-while-connected resubscribe fires.
  useEffect(() => {
    acquireTeamsEvents();
    return () => releaseTeamsEvents(send);
  }, [send]);

  useEffect(() => {
    if (isConnected) resubscribeTeamsEvents(send);
  }, [isConnected, send]);

  useEffect(() => {
    const unsubChat = onChatMessage((msg) => {
      try {
        for (const action of reduceChatFrame(stateRef.current, msg, ctxRef.current)) {
          executeAction(action, agentsRef.current);
        }
      } catch {
        // Never let a notification bug interfere with chat delivery.
      }
    });
    const unsubEvent = onEvent((event, payload) => {
      try {
        for (const action of reduceTeamsRunEvent(stateRef.current, event, payload, ctxRef.current)) {
          executeAction(action, agentsRef.current);
        }
      } catch {
        // Never let a notification bug interfere with event delivery.
      }
    });
    return () => {
      unsubChat();
      unsubEvent();
    };
  }, [onChatMessage, onEvent]);

  return null;
}
```

- [ ] **Step 5: Run — expect pass**

```bash
pnpm vitest run src/components/notifications/__tests__/RunNotifications.test.tsx
```

Expected: PASS (8 tests).

- [ ] **Step 6: Typecheck**

```bash
pnpm exec tsc --noEmit
```

Expected: clean.

- [ ] **Step 7: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins
git add apps/frontend/src/components/notifications/ apps/frontend/src/lib/analytics.ts
git commit -m "feat(frontend): RunNotifications listener — background run toasts for chat + teams runs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: Switch TeamsEventsProvider to the refcounted helper

**Files:**
- Modify: `apps/frontend/src/components/teams/TeamsEventsProvider.tsx` (lines 71-84 subscribe effect; lines 103-108 unmount-unsubscribe effect)
- Test: `apps/frontend/src/__tests__/teams/TeamsEventsProvider.test.tsx` (exists; mocks `@/hooks/useGateway` at line 13)

**Interfaces:**
- Consumes: Task 2's `acquireTeamsEvents`/`releaseTeamsEvents`/`resubscribeTeamsEvents`/`_resetTeamsEventsForTests`.
- Produces: unchanged external behavior for a sole subscriber; the direct `send({type:"teams.subscribe"})` / `send({type:"teams.unsubscribe"})` calls are gone.

- [ ] **Step 1: Modify the provider**

In `apps/frontend/src/components/teams/TeamsEventsProvider.tsx`:

(a) Add the import (after the existing `import { useGateway } …` line 5):

```tsx
import {
  acquireTeamsEvents,
  releaseTeamsEvents,
  resubscribeTeamsEvents,
} from "@/lib/teamsSubscription";
```

(b) Insert the acquire/release effect **immediately BEFORE** the connect/reconnect effect (i.e. before the `// (re)subscribe + full invalidation on connect / reconnect.` comment at line 71) — the ordering contract requires acquire to run first at mount-while-connected:

```tsx
  // Refcounted hold on the shared per-connection subscription: another
  // consumer (RunNotifications) may still need teams events after this
  // panel-scoped provider unmounts. Declared BEFORE the resubscribe effect
  // per the teamsSubscription ordering contract.
  useEffect(() => {
    acquireTeamsEvents();
    return () => releaseTeamsEvents(send);
  }, [send]);
```

(c) In the connect/reconnect effect (lines 71-84), replace the single line `send({ type: "teams.subscribe" });` with:

```tsx
    resubscribeTeamsEvents(send);
```

(d) Delete the unmount-unsubscribe effect entirely (currently lines 103-108, including its `// Best-effort unsubscribe on unmount.` comment):

```tsx
  // Best-effort unsubscribe on unmount.
  useEffect(() => {
    return () => {
      send({ type: "teams.unsubscribe" });
    };
  }, [send]);
```

(Release in the new acquire/release effect replaces it.)

- [ ] **Step 2: Update the provider's test**

In `apps/frontend/src/__tests__/teams/TeamsEventsProvider.test.tsx` (mocks: `sendMock`, `gatewayState.isConnected`, `eventHandler`; `beforeEach` at lines 72-78):

(a) Add the import next to the existing imports:

```tsx
import { acquireTeamsEvents, releaseTeamsEvents, _resetTeamsEventsForTests } from "@/lib/teamsSubscription";
```

(b) Add one line inside the existing `beforeEach` block (module-level refcount must not leak across tests):

```tsx
  _resetTeamsEventsForTests();
```

(c) The existing assertions all remain valid under the new design — verify, don't relax: `"sends teams.subscribe when connected"` (line 81) still passes via `resubscribeTeamsEvents` (exactly one subscribe: acquire is silent); `"re-subscribes … on isConnected false→true"` (line 158) still passes because acquire never sends while disconnected; `"sends teams.unsubscribe on unmount"` (line 171) still passes because the provider is the sole holder there.

(d) Append one new test inside the `describe("TeamsEventsProvider", …)` block, pinning the bug fix:

```tsx
  it("does NOT unsubscribe on unmount while another consumer holds the subscription", () => {
    gatewayState.isConnected = true;
    // Simulate RunNotifications holding the shared subscription.
    acquireTeamsEvents();
    const { unmount } = render(<TeamsEventsProvider><div /></TeamsEventsProvider>);
    sendMock.mockClear();
    unmount();
    expect(sendMock).not.toHaveBeenCalledWith({ type: "teams.unsubscribe" });
    // Drop the simulated holder so no state escapes this test.
    releaseTeamsEvents(sendMock);
  });
```

- [ ] **Step 3: Run the provider test**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins/apps/frontend
pnpm vitest run src/__tests__/teams/TeamsEventsProvider.test.tsx
```

Expected: PASS after adjustments described in Step 2.

- [ ] **Step 4: Typecheck + commit**

```bash
pnpm exec tsc --noEmit
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins
git add apps/frontend/src/components/teams/TeamsEventsProvider.tsx apps/frontend/src/__tests__/teams/TeamsEventsProvider.test.tsx
git commit -m "fix(frontend): TeamsEventsProvider uses refcounted subscription — panel close no longer kills co-subscribers

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: Mount RunNotifications in the /chat shell + ChatLayout click-through listener

**Files:**
- Modify: `apps/frontend/src/app/chat/page.tsx` (import + mount next to `MarketplaceToast`, ~line 323)
- Modify: `apps/frontend/src/components/chat/ChatLayout.tsx` (external selectAgent listener, after `handleSelectAgent` ~line 488)

**Interfaces:**
- Consumes: Task 5's `RunNotifications` + its `selectAgent`/`source:"notification"` CustomEvent contract; page-owned `selectedAgentId` (state, line ~143), `activeView` (derived, line ~40), `activePanel` (derived, line ~41); ChatLayout's existing `handleSelectAgent(agentId)` (~line 479 — does capture + selection + `onClearPanel` + sidebar close) and its `dispatchSelectAgentEvent` (dispatches WITHOUT `source`, so no loop).
- Produces: the feature is live on `/chat`.

- [ ] **Step 1: Mount the component in page.tsx**

In `apps/frontend/src/app/chat/page.tsx`:

(a) Add the import next to the MarketplaceToast import (~line 11):

```tsx
import { RunNotifications } from "@/components/notifications/RunNotifications";
```

(b) In the JSX, directly after `<MarketplaceToast />` (~line 323, still inside `GatewayProvider`):

```tsx
        <RunNotifications
          selectedAgentId={selectedAgentId}
          activeView={activeView}
          activePanel={activePanel}
        />
```

- [ ] **Step 2: Add the externally-sourced selectAgent listener in ChatLayout**

In `apps/frontend/src/components/chat/ChatLayout.tsx`, directly after the `handleSelectAgent` function definition (~line 488), add:

```tsx
  // Toast click-through: RunNotifications dispatches selectAgent with
  // source:"notification". Route it through handleSelectAgent so selection,
  // ?panel cleanup, and the sidebar all behave exactly like a sidebar click.
  // ChatLayout's own dispatches carry no source, so this cannot loop.
  const handleSelectAgentRef = useRef(handleSelectAgent);
  handleSelectAgentRef.current = handleSelectAgent;
  useEffect(() => {
    function onExternalSelectAgent(e: Event) {
      const detail = (e as CustomEvent<{ agentId?: string; source?: string }>).detail;
      if (detail?.source !== "notification" || !detail.agentId) return;
      handleSelectAgentRef.current(detail.agentId);
    }
    window.addEventListener("selectAgent", onExternalSelectAgent);
    return () => window.removeEventListener("selectAgent", onExternalSelectAgent);
  }, []);
```

(`useRef`/`useEffect` are already imported in this file.)

- [ ] **Step 3: Run the existing ChatLayout + page-adjacent tests as regression gates (single files, in sequence)**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins/apps/frontend
pnpm vitest run src/components/chat/__tests__/ChatLayout.test.tsx
pnpm vitest run src/components/chat/__tests__/ChatLayout.agentActions.test.tsx
pnpm vitest run src/components/chat/__tests__/ChatLayout.fetchHygiene.test.tsx
```

Expected: PASS each. If a failure appears, the cause is almost certainly the new effect (e.g. a test dispatching `selectAgent` events) — fix forward, don't skip.

- [ ] **Step 4: Typecheck + lint**

```bash
pnpm exec tsc --noEmit
pnpm lint
```

Expected: clean. Re-stage any linter auto-edits.

- [ ] **Step 5: Commit**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins
git add apps/frontend/src/app/chat/page.tsx apps/frontend/src/components/chat/ChatLayout.tsx
git commit -m "feat(frontend): mount RunNotifications in the chat shell with toast click-through

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: Final verification + stop for push approval

**Files:** none new — verification only.

- [ ] **Step 1: Re-run every test file this branch touched (single files, foreground, in sequence)**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/frontend-ux-quick-wins/apps/frontend
pnpm vitest run src/lib/__tests__/teamsSubscription.test.ts
pnpm vitest run src/components/notifications/__tests__/runToastReducer.test.ts
pnpm vitest run src/lib/__tests__/notify.test.tsx
pnpm vitest run src/components/notifications/__tests__/RunNotifications.test.tsx
pnpm vitest run src/__tests__/teams/TeamsEventsProvider.test.tsx
```

Expected: all PASS.

- [ ] **Step 2: CI parity — lint, typecheck, production build**

```bash
pnpm lint
pnpm exec tsc --noEmit
pnpm build
```

Expected: all clean (this is exactly what `frontend-ci.yml` runs — no vitest in CI).

- [ ] **Step 3: Repeat CI parity in the getstarted worktree**

```bash
cd /Users/prasiddhaparthsarthy/Desktop/isol8.nosync/.claude/worktrees/getstarted-dismiss/apps/frontend
pnpm vitest run src/components/getstarted/__tests__/GetStarted.test.tsx
pnpm exec tsc --noEmit
pnpm lint
pnpm build
```

Expected: all clean.

- [ ] **Step 4: STOP — report and ask the user for push approval**

Do NOT push. Summarize: both branches ready (`fix/getstarted-pill-dismiss`, `feat/frontend-ux-quick-wins` with the spec + plan + implementation), all local gates green. Ask the user to approve pushing the two branches and opening the two PRs (Codex review follows; the Codex fix-loop push is pre-authorized once the initial push is approved).

---

## Post-plan notes for the executor

- **Manual verification after merge/deploy (dev):** open two browser tabs on dev, start a run on agent B in tab 2 while tab 1 has agent A selected → tab 1 shows "…is working…" bottom-right, morphing to finished; click View → lands on agent B's chat with the sidebar highlighting B. Trigger a Teams routine run → toast appears without any Teams panel open; close/reopen a Teams panel and confirm teams toasts still arrive afterward (the refcount fix).
- **Known accepted residuals (per spec):** side runs from another tab may toast; channel/cron runs never toast (backend-gated); teams toasts use generic copy (payload has no agent name — Paperclip resolves names via a separate cache; v2 can add a `/teams/agents` lookup).
- The `run_active` frame (server-truth busy signal) exists but carries **no runId** — that is why run starts are detected from the first streaming frame instead.
