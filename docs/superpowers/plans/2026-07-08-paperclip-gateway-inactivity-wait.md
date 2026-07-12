# Activity-Aware Run Wait (Paperclip openclaw-gateway adapter) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the openclaw-gateway adapter from stamping a still-working agent run `timed_out` on a fixed wall clock; instead poll `agent.wait` in short slices and keep waiting as long as the OpenClaw gateway reports the run still executing, up to a generous absolute ceiling.

**Architecture:** Today the adapter issues **one** `agent.wait` RPC with a wall-clock `timeoutMs` and marks the run `timed_out` when it elapses, ignoring whether the run is still alive. We replace that single call with an inner **re-wait loop**: each iteration waits a short slice (default 120s), and on a slice timeout it consults the liveness fields the gateway *already returns* (`timeoutPhase: "gateway_draining"` / `providerStarted`) to decide continue-vs-give-up. The short slices double as WebSocket keepalive (each response resets API Gateway's idle timer), so silent-but-working runs survive. The continue/stop decision is extracted into a pure, unit-tested function.

**Tech Stack:** TypeScript, Node, vitest 4. Code lives in the **Isol8 Paperclip fork** `Isol8AI/paperclip` (NOT the gitignored vanilla `paperclip/` clone in this repo). Shipped as `ghcr.io/isol8ai/paperclip` → ECR `isol8/paperclip-extended` → CDK `isol8-{env}-paperclip`.

## Global Constraints

- **Edit the fork, not the repo clone.** Target file: `packages/adapters/openclaw-gateway/src/server/execute.ts` in `Isol8AI/paperclip`, branch `isol8/2026.6-circuit-breaker`. Local checkout: `~/Desktop/paperclip-fork`. The in-repo `isol8.nosync/paperclip/` is **vanilla upstream** (`PROTOCOL_VERSION = 3`, still has `agentParams.paperclip`) and is not deployed — do not edit it. **Pull latest `isol8/2026.6-circuit-breaker` before starting**; the local checkout (HEAD dated Jun 20) is behind the deployed tip, so verify anchors by code, not line number alone.
- **Per-slice wait must stay under the AWS API Gateway WebSocket idle limit of 10 min** (hard, non-configurable). Default slice 120s; clamp ≤ 300s.
- **A single WS connection cannot exceed the AWS 2-hour max connection duration** (hard, non-configurable). Keep `maxRunMs` well under 2h (clamp ≤ 110 min); do not add cross-connection resumption in this plan.
- **No behavior regression for existing agents.** Agents currently carry `waitTimeoutMs=300000` (from the 2026-07-08 backfill). Defaults must make those strictly better (longer effective ceiling), require no config migration, and keep the terminal/error/retry paths byte-for-byte unchanged.
- **Adapter-only change — no DB migration.** Do not touch Drizzle/schema; the Paperclip migrate task is not required for this deploy.
- **Deploy dev → validate → prod.** Prod CDK job `needs` the dev job; never pin `prod.tag` before dev is validated.

---

## Verified Evidence (read before implementing)

All file:line refs below were confirmed by reading source on 2026-07-08.

**Adapter (fork `execute.ts`; anchors from local checkout, re-verify after pull):**
- `:1088` `const timeoutSec = … asNumber(ctx.config.timeoutSec, 120)` — timeout config origin. `:1090` `connectTimeoutMs`. `:1091` `waitTimeoutMs`.
- `:1194` outer `while (true)` retry loop; `:1192` `MAX_RETRIES = 2`. Retries only transient connect / pairing errors; `:1488` explicitly excludes `agent.wait` from transient retry.
- `:1200` `onEvent = async (frame) =>`; `:1224` `stream === "assistant"` → `assistantChunks.push`; `:1240` `stream === "lifecycle"`.
- `:1249`(vanilla) fresh `GatewayWsClient` per outer iteration, closed in `finally`.
- `:1322` `client.request("agent", …)` submit (returns fast with `runId`, `status`); `:1351` `client.request("agent.wait", { runId, timeoutMs: waitTimeoutMs }, { timeoutMs: waitTimeoutMs + connectTimeoutMs })` — **the single blocking wait to replace**.
- `:1359` `waitStatus === "timeout"` → returns `timedOut:true`, `errorCode:"openclaw_gateway_wait_timeout"`; `:1364` the `run timed out after ${waitTimeoutMs}ms` message. `:1371` `"error"`; `:1385` `waitStatus && !== "ok"` → unexpected.
- `GatewayWsClient.handleMessage` (`:787`) routes event frames to `onEvent` (`:795`–`:807`) **independently** of the pending-request map (`:810`–`:840`) — so `onEvent` fires while `agent.wait` is in flight. `:752`–`:759` each request has its own timeout timer.

**OpenClaw gateway server (fork `~/Desktop/agent.nosync`, `Isol8AI/agent`):**
- `src/gateway/server-methods/agent.ts:2917` `"agent.wait"` handler. On timeout it `respond(true, { runId, status: "timeout", timeoutPhase, providerStarted })` (`:3025`–`:3033`) and **never cancels the run** — it only aborts its own wait listeners (local `AbortController`s). → **timeout leaves the run executing. VERIFIED.**
- Re-entrant: each call first checks a cached terminal snapshot (`:2941` `readTerminalSnapshotFromGatewayDedupe`) and otherwise sets up **fresh** waiters (`:3000` `waitForAgentJob` + dedupe race). No per-run state is consumed on timeout. → **re-wait on the same `runId` works. VERIFIED.**
- Liveness in the timeout response: `timeoutPhase === "gateway_draining"` when the run is still registered (`activeRunRegistered`, `:3030`); `"queue"` + `providerStarted:false` when not started (`:3030`–`:3031`). An agent-kind run registers an abort entry via the `agent` RPC (`:2935`–`:2939`), so a live agent run reports `gateway_draining`.
- `waitForAgentJob` (`src/gateway/server-methods/agent-job.ts:307`) returns `AgentRunSnapshot | null` — terminal snapshot when the run finishes, `null` on timeout; re-subscribes each call (`ensureAgentRunListener`/`getCachedAgentRun`).
- `agent.wait` normalized status vocabulary: **`"ok" | "timeout" | "error" | "pending"`** (`src/agents/run-wait.ts:60`).
- `livenessState` vocabulary: `"working"` (`src/agents/embedded-agent-subscribe.ts:221`), `"paused"` (`src/agents/cli-runner.ts:916`), `"blocked"` (`src/agents/cli-runner.ts:586`); helper `isBlockedLivenessState` (`src/shared/agent-liveness.ts:2`). Present on terminal/cached snapshots, **not** on the plain running-timeout response — so we key liveness off `timeoutPhase`/`providerStarted`, not `livenessState`.
- Assistant output streams incrementally: `emit({ runId, stream:"assistant", data:{ delta } })` (`src/gateway/test-helpers.agent-results.ts:80`; `src/gateway/server-chat.ts:1075`). **No dedicated per-run heartbeat frame during a silent stretch was found** — hence liveness must come from the `agent.wait` response, not from event inactivity.

**Infra / deploy (this repo):**
- `paperclip/` is gitignored (`.gitignore:72` `/paperclip/`; `git check-ignore paperclip` → exit 0) and is vanilla upstream.
- Deployed image: `paperclip-version.json` `upstream` = `ghcr.io/isol8ai/paperclip@sha256:…` (built from fork branch `isol8/2026.6-circuit-breaker`, tag `v2026.6-isol8.4`); re-tagged to ECR `isol8/paperclip-extended` by `.github/workflows/build-paperclip-image.yml`; consumed at `apps/infra/lib/stacks/paperclip-stack.ts:144`.
- Fork already carries hand-written source patches deployed to dev+prod (protocol-v4, routine circuit-breaker, routines-FK) — precedent for this change.
- AWS WS limits (official): idle timeout **10 min**, max connection duration **2 h**, neither increasable — https://docs.aws.amazon.com/apigateway/latest/developerguide/apigateway-execution-service-websocket-limits-table.html . `apps/infra/lib/stacks/api-stack.ts` does not override them.

---

## File Structure

- **Modify (fork):** `packages/adapters/openclaw-gateway/src/server/execute.ts`
  - Add exported pure fn `decideRunWaitContinuation` (new logic; ~15 lines).
  - Add config parsing: `pollWaitMs`, `inactivityMs`, `maxRunMs` (near existing `:1088`–`:1091`).
  - Replace the single `agent.wait` block (`:1350`–`:1395`) with an inner re-wait loop calling the pure fn.
- **Modify (fork):** `packages/adapters/openclaw-gateway/src/server/execute.test.ts`
  - Add `describe("decideRunWaitContinuation")` cases (pure-fn unit tests; matches the existing `resolveSessionKey` test style — no WS mock needed).
- **Modify (this repo):** `paperclip-version.json` — re-pin `upstream` digest + bump `dev.tag`, later `prod.tag` (deploy tasks).

Rationale: the continue/stop decision is the only nontrivial logic and the only thing worth testing in isolation; extracting it as a pure fn makes it unit-testable without mocking the WebSocket (which has no existing harness). The loop wiring around it is thin and validated on dev.

---

### Task 1: Pure continue/stop decision function + unit tests

**Files:**
- Modify: `packages/adapters/openclaw-gateway/src/server/execute.ts` (add exported fn)
- Test: `packages/adapters/openclaw-gateway/src/server/execute.test.ts`

**Interfaces:**
- Produces: `decideRunWaitContinuation(input: { runActive: boolean; msSinceActive: number; elapsedMs: number; maxRunMs: number; inactivityMs: number }): "continue" | "timeout"` — consumed by the loop in Task 3.

Semantics: keep waiting while the gateway reports the run active; give up only when (a) total elapsed hits the absolute ceiling `maxRunMs`, or (b) the gateway has not reported the run active for `inactivityMs` (covers both never-started and lost-after-active, and tolerates the brief window between run finish and the terminal snapshot landing).

- [ ] **Step 1: Write the failing tests**

In `execute.test.ts`, add (keep existing imports; add `decideRunWaitContinuation` to the import from `"./execute"`):

```ts
import { decideRunWaitContinuation } from "./execute";

describe("decideRunWaitContinuation", () => {
  const base = { runActive: true, msSinceActive: 0, elapsedMs: 0, maxRunMs: 1_800_000, inactivityMs: 180_000 };

  it("continues while the run is reported active", () => {
    expect(decideRunWaitContinuation({ ...base, runActive: true, elapsedMs: 600_000 })).toBe("continue");
  });

  it("times out at the absolute ceiling even if still active", () => {
    expect(decideRunWaitContinuation({ ...base, runActive: true, elapsedMs: 1_800_000 })).toBe("timeout");
  });

  it("continues through an inactive slice within the grace window", () => {
    expect(decideRunWaitContinuation({ ...base, runActive: false, msSinceActive: 120_000 })).toBe("continue");
  });

  it("times out after inactivity grace with no active report", () => {
    expect(decideRunWaitContinuation({ ...base, runActive: false, msSinceActive: 180_000 })).toBe("timeout");
  });

  it("ceiling takes precedence over an active report", () => {
    expect(decideRunWaitContinuation({ ...base, runActive: true, msSinceActive: 0, elapsedMs: 2_000_000 })).toBe("timeout");
  });
});
```

- [ ] **Step 2: Run tests, verify they fail**

Run (from `~/Desktop/paperclip-fork`): `pnpm exec vitest run packages/adapters/openclaw-gateway/src/server/execute.test.ts`
Expected: FAIL — `decideRunWaitContinuation is not exported` / not a function.

- [ ] **Step 3: Implement the function**

Add to `execute.ts` (top-level, near the other exported helpers such as `resolveSessionKey`):

```ts
export type RunWaitDecision = "continue" | "timeout";

/**
 * Decide whether to keep polling agent.wait or give up. The OpenClaw gateway
 * leaves a run executing when a wait slice times out (server-methods/agent.ts
 * agent.wait), and reports liveness via timeoutPhase; we keep waiting while the
 * run is reported active, bounded by an absolute ceiling and an inactivity grace.
 */
export function decideRunWaitContinuation(input: {
  runActive: boolean;    // this slice reported the run still executing
  msSinceActive: number; // ms since the run was last reported active (or since submit)
  elapsedMs: number;     // ms since the run was submitted
  maxRunMs: number;      // absolute ceiling (must stay well under the 2h WS connection cap)
  inactivityMs: number;  // give up after this long with no "active" report
}): RunWaitDecision {
  if (input.elapsedMs >= input.maxRunMs) return "timeout";
  if (input.runActive) return "continue";
  if (input.msSinceActive >= input.inactivityMs) return "timeout";
  return "continue";
}
```

- [ ] **Step 4: Run tests, verify they pass**

Run: `pnpm exec vitest run packages/adapters/openclaw-gateway/src/server/execute.test.ts`
Expected: PASS (all `decideRunWaitContinuation` cases + the existing `resolveSessionKey` cases).

- [ ] **Step 5: Commit**

```bash
git add packages/adapters/openclaw-gateway/src/server/execute.ts packages/adapters/openclaw-gateway/src/server/execute.test.ts
git commit -m "feat(openclaw-gateway): add decideRunWaitContinuation for activity-aware run wait"
```

---

### Task 2: Parse the new timeout knobs with safe defaults

**Files:**
- Modify: `packages/adapters/openclaw-gateway/src/server/execute.ts` (near the existing `timeoutSec`/`waitTimeoutMs` block, ~`:1088`–`:1091`)

**Interfaces:**
- Consumes: `ctx.config.pollWaitMs`, `ctx.config.inactivityMs`, `ctx.config.maxRunMs` (all optional), plus existing `waitTimeoutMs`.
- Produces: `pollWaitMs`, `inactivityMs`, `maxRunMs` locals consumed by the loop in Task 3.

Defaults are chosen so existing agents (`waitTimeoutMs=300000`, no new knobs) become strictly better: 120s slices, ≥30-min ceiling, no config migration.

- [ ] **Step 1: Add the parsing block**

Immediately after the existing `const waitTimeoutMs = …` line (~`:1091`), add:

```ts
// Activity-aware wait: poll agent.wait in short slices instead of one wall-clock
// deadline. `pollWaitMs` is the per-slice wait — kept under the API Gateway WS
// 10-min idle limit; each slice response also resets that idle timer so silent
// runs don't drop the socket. `inactivityMs` is how long we tolerate the gateway
// NOT reporting the run active before giving up. `maxRunMs` is the absolute
// ceiling, kept well under the 2h WS max connection duration.
const clampMs = (v: number, lo: number, hi: number) => Math.min(hi, Math.max(lo, v));
const pollWaitMs = clampMs(parseOptionalPositiveInteger(ctx.config.pollWaitMs) ?? 120_000, 5_000, 300_000);
const inactivityMs = clampMs(
  parseOptionalPositiveInteger(ctx.config.inactivityMs) ?? 180_000,
  pollWaitMs,
  600_000,
);
const maxRunMs = clampMs(
  parseOptionalPositiveInteger(ctx.config.maxRunMs) ?? Math.max(waitTimeoutMs, 1_800_000),
  pollWaitMs,
  6_600_000, // < 2h WS connection cap
);
```

- [ ] **Step 2: Typecheck**

Run (from `~/Desktop/paperclip-fork`): `pnpm --filter @paperclipai/adapter-openclaw-gateway exec tsc --noEmit`
Expected: PASS (no unused-var or type errors). `parseOptionalPositiveInteger` is already imported/used at the existing `waitTimeoutMs` line.

- [ ] **Step 3: Commit**

```bash
git add packages/adapters/openclaw-gateway/src/server/execute.ts
git commit -m "feat(openclaw-gateway): parse pollWaitMs/inactivityMs/maxRunMs with safe defaults"
```

---

### Task 3: Replace the single agent.wait with the re-wait loop

**Files:**
- Modify: `packages/adapters/openclaw-gateway/src/server/execute.ts` — the `if (acceptedStatus !== "ok") { … }` block containing the single `agent.wait` (local anchor `:1350`–`:1395`).

**Interfaces:**
- Consumes: `decideRunWaitContinuation` (Task 1); `pollWaitMs`/`inactivityMs`/`maxRunMs` (Task 2); existing `client`, `acceptedRunId`, `connectTimeoutMs`, `latestResultPayload`, `ctx.onLog`.
- Produces: unchanged control flow after the block (falls through to the existing summary/usage extraction at local `:1397`).

**Before (verify by code, not line number — the current single wait):**

```ts
if (acceptedStatus !== "ok") {
  const waitPayload = await client.request<Record<string, unknown>>(
    "agent.wait",
    { runId: acceptedRunId, timeoutMs: waitTimeoutMs },
    { timeoutMs: waitTimeoutMs + connectTimeoutMs },
  );
  latestResultPayload = waitPayload;
  const waitStatus = nonEmpty(waitPayload?.status)?.toLowerCase() ?? "";
  if (waitStatus === "timeout") {
    return { exitCode: 1, signal: null, timedOut: true,
      errorMessage: `OpenClaw gateway run timed out after ${waitTimeoutMs}ms`,
      errorCode: "openclaw_gateway_wait_timeout", resultJson: waitPayload };
  }
  if (waitStatus === "error") { /* …unchanged… */ }
  if (waitStatus && waitStatus !== "ok") { /* …unchanged… */ }
}
```

- [ ] **Step 1: Replace the wait call with the loop**

Replace the single `const waitPayload = await client.request("agent.wait", …)` and its `if (waitStatus === "timeout")` branch with the loop below. **Keep the existing `"error"` and unexpected-status branches exactly as they are** — they run against the final `waitPayload` after the loop breaks.

```ts
if (acceptedStatus !== "ok") {
  const runStartedAt = Date.now();
  let lastActiveAt = runStartedAt;
  let waitPayload: Record<string, unknown>;
  let waitStatus: string;

  while (true) {
    waitPayload = await client.request<Record<string, unknown>>(
      "agent.wait",
      { runId: acceptedRunId, timeoutMs: pollWaitMs },
      { timeoutMs: pollWaitMs + connectTimeoutMs },
    );
    latestResultPayload = waitPayload;
    waitStatus = nonEmpty(waitPayload?.status)?.toLowerCase() ?? "";

    // Only "timeout"/"pending" are non-terminal; anything else (ok/error/…)
    // breaks to the existing terminal handling below.
    if (waitStatus !== "timeout" && waitStatus !== "pending") break;

    const timeoutPhase = nonEmpty(waitPayload?.timeoutPhase)?.toLowerCase() ?? "";
    const runActive =
      waitStatus === "pending" ||
      timeoutPhase === "gateway_draining" ||
      waitPayload?.providerStarted === true;
    const now = Date.now();
    if (runActive) lastActiveAt = now;

    const decision = decideRunWaitContinuation({
      runActive,
      msSinceActive: now - lastActiveAt,
      elapsedMs: now - runStartedAt,
      maxRunMs,
      inactivityMs,
    });

    if (decision === "continue") {
      await ctx.onLog(
        "stdout",
        `[openclaw-gateway] run ${acceptedRunId} still waiting (status=${waitStatus} phase=${timeoutPhase || "-"} elapsed=${Math.round((now - runStartedAt) / 1000)}s); polling again\n`,
      );
      continue;
    }

    return {
      exitCode: 1,
      signal: null,
      timedOut: true,
      errorMessage: `OpenClaw gateway run exceeded ${Math.round(maxRunMs / 1000)}s or went inactive (phase=${timeoutPhase || "unknown"})`,
      errorCode: "openclaw_gateway_wait_timeout",
      resultJson: waitPayload,
    };
  }

  if (waitStatus === "error") {
    // …existing error branch, unchanged (operates on waitPayload)…
  }
  if (waitStatus && waitStatus !== "ok") {
    // …existing unexpected-status branch, unchanged…
  }
}
```

Note: `pending` is now treated as non-terminal/continue — previously it would have fallen into the unexpected-status branch (`:1385`), so this also removes a latent spurious-error path.

- [ ] **Step 2: Typecheck**

Run: `pnpm --filter @paperclipai/adapter-openclaw-gateway exec tsc --noEmit`
Expected: PASS. Confirm `waitPayload`/`waitStatus` are declared with `let` (used after the loop) and the `error`/unexpected branches still compile against them.

- [ ] **Step 3: Run the adapter test file**

Run: `pnpm exec vitest run packages/adapters/openclaw-gateway/src/server/execute.test.ts`
Expected: PASS (Task 1 cases still green; no behavioral test exists for the loop wiring — that is validated on dev in Task 5).

- [ ] **Step 4: Full adapter build**

Run: `pnpm --filter @paperclipai/adapter-openclaw-gateway build`
Expected: `tsc` succeeds, no errors.

- [ ] **Step 5: Commit**

```bash
git add packages/adapters/openclaw-gateway/src/server/execute.ts
git commit -m "feat(openclaw-gateway): poll agent.wait in slices; keep waiting while run is active"
```

---

### Task 4: Push the fork branch and build the image

**Files:** none in this repo (fork CI produces the image).

- [ ] **Step 1: Push the fork branch**

```bash
cd ~/Desktop/paperclip-fork
git push origin isol8/2026.6-circuit-breaker
```

- [ ] **Step 2: Watch the fork's image build**

The fork's own CI (in `Isol8AI/paperclip`, not this repo) builds and publishes `ghcr.io/isol8ai/paperclip`. Watch it:
`gh run watch --repo Isol8AI/paperclip --exit-status` (or the fork's Actions tab).
Expected: a new `ghcr.io/isol8ai/paperclip` image + digest. Record the new `@sha256:…` digest and the fork tag (e.g. `v2026.6-isol8.5`).

- [ ] **Step 3: Note the digest** for Task 5. No commit here.

---

### Task 5: Deploy to dev, validate, promote to prod

**Files:**
- Modify (this repo): `paperclip-version.json`

- [ ] **Step 1: Re-pin the fork digest + bump the dev tag**

In `paperclip-version.json`: set `upstream` to the new `ghcr.io/isol8ai/paperclip@sha256:<digest>` from Task 4, update `upstreamCommit`, and set `dev.tag` per the build convention (`<upstreamCommit8>-<repoSha7>`; the value is emitted by `build-paperclip-image.yml`). Leave `prod.tag` unchanged for now.

- [ ] **Step 2: Open a PR, merge, watch dev deploy**

Pushing `paperclip-version.json` triggers `.github/workflows/build-paperclip-image.yml` (re-tags fork image → ECR `isol8/paperclip-extended`) and, on merge to `main`, `deploy.yml` job `dev-isol8-dev-paperclip-Deploy` (stack `isol8-dev-paperclip`).
Run: `gh run watch <run-id> --repo Isol8AI/isol8 --exit-status`
Expected: dev deploy green. No migrate task needed (adapter-only, no schema change).

- [ ] **Step 3: Validate on dev — the actual behavior check**

Trigger a long-running agent task on a dev org (a research-style task that historically exceeded 120s) and confirm the fix end-to-end:
- Tail the dev Paperclip adapter logs (CloudWatch, log group for `isol8-dev-paperclip`) and grep for the new line: `still waiting (status=… phase=gateway_draining … polling again)` — proves the loop is re-waiting on a live run instead of timing out.
- Confirm the issue's run does **not** produce a `openclaw_gateway_wait_timeout` at ~120s and that the run reaches a terminal `ok` and posts its result.
- Confirm a genuinely-hung/no-op run still terminates at `maxRunMs` (~30 min) rather than hanging forever.
Expected: multi-minute runs complete without false `timed_out`; the RUNS list no longer shows the stacked `Timed_out` cascade.

- [ ] **Step 4: Promote to prod**

After dev validation, bump `prod.tag` to the same tag in a new PR; merge. `deploy.yml` job `prod-isol8-prod-paperclip-Deploy` (`needs` dev) deploys `isol8-prod-paperclip`.
Run: `gh run watch <run-id> --repo Isol8AI/isol8 --exit-status`
Expected: prod deploy green; ECS service circuit-breaker (`rollback:true`) guards a bad rollout.

- [ ] **Step 5: Confirm prod**

Re-run or observe a real multi-minute prod agent task (e.g. a research task like the one that motivated this) and confirm no false `timed_out` and a clean terminal result.

---

## Self-Review

**Spec coverage:**
- "Don't time out while still working" → Task 3 loop + Task 1 decision fn (continue while `runActive`). ✓
- "Wait however long it takes" → bounded by `maxRunMs` (default 30 min, clamp < 2h) — honest ceiling, documented as the WS-connection-duration limit; truly longer work is a checkpointing problem, out of scope. ✓
- Keepalive under the 10-min idle cap → `pollWaitMs` default 120s, clamp ≤ 300s; each slice response resets the idle timer. ✓
- No regression for backfilled agents → defaults derived from `waitTimeoutMs` via `Math.max(…, 1_800_000)`; terminal/error/retry branches untouched. ✓
- Liveness signal exists → keyed off `timeoutPhase`/`providerStarted` from the `agent.wait` response (verified), not the unconfirmed event-heartbeat. ✓

**Placeholder scan:** The two "…existing … branch, unchanged…" markers in Task 3 Step 1 refer to code the implementer is told to leave verbatim (the current `"error"` and unexpected-status branches); they are preservation instructions, not new code to author. No other placeholders.

**Type consistency:** `decideRunWaitContinuation` signature is identical in Task 1 (definition), Task 1 tests, and Task 3 (call site): `{ runActive, msSinceActive, elapsedMs, maxRunMs, inactivityMs } → "continue" | "timeout"`. `pollWaitMs`/`inactivityMs`/`maxRunMs` are defined in Task 2 and consumed in Task 3. `waitPayload`/`waitStatus` promoted to `let` so the post-loop branches still see them.

**Open risk (flagged, not blocking):** the completion race — run finishes but the terminal snapshot hasn't landed, so a slice returns `timeout`+`queue`. The `inactivityMs` grace (default 3 min, ≥ one slice) absorbs it, and the gateway's cached-terminal-snapshot check (`agent.ts:2941`) means the next slice returns the real terminal status. Validated empirically in Task 5 Step 3.
