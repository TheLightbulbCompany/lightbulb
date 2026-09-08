# Transcript Integrity — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the transcript survive a reconnect, a second tab, a dropped frame and a reload
without silently losing what the agent already said.

**Architecture:** Three independent repairs, of which **only two are proven**. We listen to the
runtime's own "I dropped frames" signal instead of reacting solely to our own send failures, and
we consume the in-flight snapshot `chat.history` already returns so a mid-run reload restores
what was on screen. The third — a suspected truncation in our full-text resync frame — is
**unproven and must be re-derived against live frames before any code is written** (Task 0).

**Tech Stack:** Python 3 / FastAPI backend (`uv`, `pytest`), React 19 / Next.js 16 frontend
(`vitest`), OpenClaw gateway WebSocket protocol.

**Spec:** `docs/superpowers/plans/2026-09-06-openclaw-81-audit-findings.md` — findings D1, D7,
D4, UI3, UI7.

> ⚠️ **D1's diagnosis did not survive review. Read this before Task 0.**
>
> The finding says 8.1 made assistant `text` block-cumulative
> (`scopeAssistantMessageToStreamBlock`, `handlers.messages.stream.ts:149-181`) and that our
> resync therefore truncates. An earlier draft of this plan keyed the fix on `data["itemId"]`.
> **That fix would have been a no-op**, and the bug may not exist on our fleet at all:
>
> - The two emit sites that matter pass **no `itemId`** — `update.ts:468` (the main visible-text
>   emit) and `lifecycle.ts:283` (the final stream). Verified directly.
> - `itemId` is set only on Responses-API commentary emits (`update.ts:260`, `stream.ts:198`),
>   and **both carry `replace: true`**, which sends our `_build_text_frame` down its legacy
>   branch where `_full_content` is never set at all.
> - The runtime agrees there is nothing to scope without one: `server-chat.ts:997` is
>   `if (!input.itemId) { delete run.assistantScope; }`, and
>   `scopeAssistantMessageToStreamBlock` returns the message **unchanged** when neither a
>   `contentIndex` nor a matching item signature resolves — the Anthropic/Bedrock case our fleet
>   runs on.
>
> So `text` may still be fully cumulative for us and there may be no defect. **Task 0 settles
> it from real frames. Do not write Task 1 until Task 0 has an answer.** This is the second time
> a confident diagnosis in this audit dissolved on inspection; treat the pattern as the norm,
> not the exception.

## Global Constraints

- **Image tag we run:** `v2026.8.1-isol8.3`. Verify with `git show v2026.8.1-isol8.3:<path>`
  in `~/dev/agent-fork`. **Its working tree is a stale 2026.7.2 — never read it.** Never
  `git checkout`/`switch` there.
- **The delta-append path is not broken and must not change.** Only the full-text *resync*
  frame is wrong. A change that alters the normal streaming path has overreached.
- **`_fit_rpc_result_for_frame` exists for the API Gateway frame limit**
  (`routers/websocket_chat.py:334-374`), not the gateway's limits. Anything that grows a frame
  must stay under it.
- **Colour literals fail the brand-token scan** outside the documented exemptions. Use
  `var(--…)` tokens.
- **Backend:** `cd apps/backend && uv run pytest tests/ -v`. **Frontend:**
  `cd apps/frontend && pnpm test` (RAM-heavy — run targeted files while iterating).
- **`_transform_agent_event` is a `@staticmethod` (`connection_pool.py:1069`) and the liveness
  plan adds `tests/unit/gateway/test_transform_agent_event.py` against it.** If any task here
  converts it to an instance method, that test file must be updated in the same commit. The
  production call site at `:1953` is already `self._transform_agent_event(payload)` and needs no
  change.
- **`useAgentChat.ts` is shared with the liveness plan** (its Task 5 rewrites the
  `agent_activity` handler and the terminal paths; Task 4 here rewrites the `chat.history`
  handler). One worktree, one agent, **this plan's hydration first** so the liveness terminal
  edits sit on top.
- No PR or issue references in code comments.

---

## File Structure

| file | responsibility | change |
|---|---|---|
| `apps/backend/core/gateway/connection_pool.py` | event translation + resync content | Tasks 1, 3 |
| `apps/backend/tests/unit/gateway/test_text_frame_resync.py` | **new** — locks the resync contract | Task 1 |
| `apps/backend/core/gateway/frame_sender.py` | per-connection continuity + resync shaping | Task 3 (reset hook only) |
| `apps/frontend/src/hooks/useAgentChat.ts` | chat state machine | Task 4 |
| `apps/frontend/src/components/chat/MessageList.tsx` | transcript + scroll | Task 5 |
| `apps/frontend/src/components/chat/ChatErrorCard.tsx` | typed error cards | Task 6 |

---

### Task 0: Prove or disprove D1 from real frames

**Nothing in Tasks 1–2 may be written until this returns an answer.** The cost of getting this
wrong is a day spent on a no-op, or a "fix" to a live streaming path the plan's own constraints
forbid touching.

**Files:** none — this is an experiment.

**Interfaces:** Consumes nothing. Produces a go/no-go and, if go, the actual field to key on.

- [ ] **Step 1: Capture raw assistant frames from a dev container**

Add a temporary debug log at the top of `_transform_agent_event`, deploy to dev, and drive a run
that interleaves text and tools:

```python
        if payload.get("stream") == "assistant":
            logger.info("D1-PROBE %s", json.dumps({
                "runId": payload.get("runId"),
                "keys": sorted(payload.get("data", {}).keys()),
                "itemId": payload.get("data", {}).get("itemId"),
                "contentIndex": payload.get("data", {}).get("contentIndex"),
                "replace": payload.get("data", {}).get("replace"),
                "textLen": len(payload.get("data", {}).get("text") or ""),
                "deltaLen": len(payload.get("data", {}).get("delta") or ""),
            }))
```

Prompt: *"Tell me what you're about to do, then read your AGENTS.md, then tell me what you
found."* — text, tool, text.

- [ ] **Step 2: Read the capture and answer three questions**

```bash
aws logs tail /ecs/<dev-backend-log-group> --profile isol8-admin --since 10m \
  --filter-pattern "D1-PROBE" --format short
```

1. Does any frame carry `itemId` or `contentIndex`?
2. After the tool call, does `textLen` **drop** relative to the pre-tool frames (block-scoped)
   or keep growing (still cumulative)?
3. Do the frames carrying `itemId`, if any, also carry `replace: true`?

- [ ] **Step 3: Branch on the answer, and record it in the plan before proceeding**

**(a) `textLen` keeps growing across the tool call — `text` is still cumulative for us.**
There is no bug. **Delete Tasks 1 and 2, strike D1 from the findings doc with this evidence,
and ship the rest of this plan.** This is the most likely outcome given the code reading above.

**(b) `textLen` drops after the tool call, and frames carry `itemId` without `replace: true`.**
The bug is real and the accumulator is the right fix. Write Task 1 keyed on the field the
capture actually shows, and re-run this probe afterwards to confirm `_full_content` spans the
whole reply.

**(c) `textLen` drops, but only on frames with `replace: true`.** The truncation is on the
**live** path, not the resync path — every viewer sees it, not just a reconnecting one. That is
a different bug in a different place, and this plan's Global Constraint ("the delta-append path
is not broken and must not change") no longer holds. **Stop and re-plan; do not edit
`_build_text_frame`.**

- [ ] **Step 4: Remove the probe**

The debug log is temporary. Revert it before any other task ships — an unconditional
`logger.info` on every assistant frame is a real cost at fleet scale.

---

### Tasks 1–2: Reserved for D1's fix — DO NOT WRITE UNTIL TASK 0 RETURNS (b)

If Task 0 lands on **(a)**, delete this section and the D1 references in the header. If it lands
on **(c)**, this plan is superseded. Only **(b)** justifies an accumulator, and its exact shape
depends on which field the capture shows — writing it now would encode the same guess that
already failed review.

What the fix looked like, kept only as a starting point for branch (b) and **not** as an
instruction: track the block-identifying field per `(runId, frameType)` on the connection, fold
a closed block's final text into a prefix when that field changes, and emit
`_full_content = prefix + text`. It requires making `_build_text_frame` an instance method
(it is a `@staticmethod` at `connection_pool.py:1069`; the production call site at `:1953` is
already `self._transform_agent_event(...)`, so only the method itself and the liveness plan's
test file need updating — see the cross-plan note in Global Constraints). It also introduces
per-run state that **must** be evicted on every terminal and on disconnect, or it grows
unbounded on a killed container.

---

### Task 3: Listen when the runtime says it dropped frames

`server-chat.ts:1447-1469` broadcasts an `agent` event `{stream:"error", data:{reason:"seq
gap", expected, received}}` when it detects `evt.seq !== last + 1`. We have no `error` branch,
so the one authoritative "your stream is now incomplete" signal is dropped. Today the resync
machinery fires only on **our own** send failure.

**Files:**
- Modify: `apps/backend/core/gateway/connection_pool.py` (`_transform_agent_event`)
- Modify: `apps/backend/core/gateway/frame_sender.py` (a per-run continuity reset)
- Modify: `apps/backend/tests/unit/gateway/test_text_frame_resync.py`

**Interfaces:**
- Consumes: `FrameSender`'s `_stream_keys` continuity map.
- Produces: `FrameSender.invalidate_run(run_id: str) -> None`, forcing the next frame of that
  run to ship in full-text form on every connection.

- [ ] **Step 1: Write the failing test**

```python
def test_seq_gap_invalidates_run_continuity():
    from core.gateway.frame_sender import FrameSender
    sender = FrameSender.__new__(FrameSender)
    sender._stream_keys = {"conn_a": {"chunk": "r1"}, "conn_b": {"chunk": "r2"}}
    sender.invalidate_run("r1")
    assert sender._stream_keys["conn_a"].get("chunk") is None
    assert sender._stream_keys["conn_b"].get("chunk") == "r2"
```

- [ ] **Step 2: Run to confirm it fails**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/ -v -k seq_gap`

Expected: FAIL — no `invalidate_run`.

- [ ] **Step 3: Add the reset to `FrameSender`**

```python
    def invalidate_run(self, run_id: str) -> None:
        """Force the next frame of *run_id* to ship as full text on every connection.

        Called when the gateway reports a sequence gap: it has told us the stream
        it sent is incomplete, so every connection's delta continuity for that run
        is now untrustworthy. Dropping the continuity entry makes _shape() take
        the full-text branch on the next frame, which is exactly the recovery the
        resync form exists for. Cheap: the map is small and keyed per connection.
        """
        for per_type in self._stream_keys.values():
            for frame_type, rid in list(per_type.items()):
                if rid == run_id:
                    del per_type[frame_type]
```

- [ ] **Step 4: Handle the stream**

In `_transform_agent_event`:

```python
        elif stream == "error":
            # The gateway telling us IT dropped events (server-chat.ts detects
            # evt.seq != last + 1 and broadcasts this). Our resync form otherwise
            # fires only on our own send failure, so without this a run that lost
            # frames upstream renders as a run that lost frames. Invalidate delta
            # continuity so the next frame ships in full, and surface it as an
            # activity so it is visible in support.
            self._sender.invalidate_run(payload["runId"])
            transformed = {
                "type": "agent_activity",
                "activity": "stream_error",
                "title": str(data.get("reason", "stream error")),
                "detail": data,
            }
```

Confirm the sender is reachable as `self._sender` from `GatewayConnection`; if the pool owns it
instead, route through the pool rather than threading a new reference.

- [ ] **Step 5: Run the tests**

Run: `cd apps/backend && uv run pytest tests/unit/gateway/ -v`

Expected: all PASS.

- [ ] **Step 6: Commit**

```bash
git add apps/backend/core/gateway/
git commit -m "fix(gateway): resync deliberately when the runtime reports a sequence gap"
```

---

### Task 4: Restore the in-flight run on reload

`chat.history` already returns `inFlightRun` — a bounded snapshot of the still-streaming run,
retaining up to 50 tool/item/`run_status`/notice events
(`src/gateway/server-methods/chat-history-handler.ts:531-598`, backed by
`server-chat-progress-snapshot.ts`, **new at 8.1**). Its comment: *"Surface any run still
streaming for this session+agent so a client that switched away … can restore the in-flight
assistant text on switch-back."* `grep -rn "inFlightRun"` across backend, frontend, mobile and
shared returns **zero hits**, against 61 references to `"chat.history"`.

Reload mid-run today and the partial text and tool pills are gone until the run terminates.

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (the `chat.history` response handling)
- Test: `apps/frontend/src/hooks/__tests__/useAgentChat.test.ts`

**Interfaces:**
- Consumes: the existing `chat.history` RPC result.
- Produces: no new exports — it hydrates the existing streaming-message state.

- [ ] **Step 1: Read the response shape at the tag**

```bash
cd ~/dev/agent-fork && git show v2026.8.1-isol8.3:src/gateway/server-methods/chat-history-handler.ts | sed -n '525,600p'
```

Record the exact field names before writing the parser — do not infer them from this plan.

- [ ] **Step 2: Write the failing test**

```ts
it("restores in-flight assistant text from chat.history on mount", async () => {
  mockRpc("chat.history", {
    messages: [{ role: "user", text: "hi" }],
    inFlightRun: { runId: "r1", assistantText: "I'm partway through…" },
  });
  const { result } = renderChatHook();
  await waitFor(() => {
    expect(result.current.renderGroups.some((g) => g.text?.includes("partway through"))).toBe(true);
  });
});

it("does not restore anything when no run is in flight", async () => {
  mockRpc("chat.history", { messages: [{ role: "user", text: "hi" }] });
  const { result } = renderChatHook();
  await waitFor(() => expect(result.current.renderGroups).toHaveLength(1));
});

it("a live frame for the same run supersedes the restored snapshot", async () => {
  mockRpc("chat.history", {
    messages: [],
    inFlightRun: { runId: "r1", assistantText: "stale" },
  });
  const { result } = renderChatHook();
  act(() => {
    deliverFrame(result, { type: "chunk", content: "fresh and complete", runId: "r1" });
  });
  await waitFor(() => {
    const joined = result.current.renderGroups.map((g) => g.text).join("");
    expect(joined).toContain("fresh and complete");
    expect(joined).not.toContain("stalefresh");
  });
});
```

- [ ] **Step 3: Run to confirm they fail**

Run: `cd apps/frontend && pnpm test -- useAgentChat`

Expected: FAIL — nothing reads `inFlightRun`.

- [ ] **Step 4: Hydrate from the snapshot**

In the `chat.history` result handler, after the historical messages are applied, seed the
streaming bubble from `inFlightRun` when present, keyed by its `runId` so a live frame for the
same run replaces rather than concatenates. Use the field names recorded in Step 1.

**Do not** treat the snapshot as history — it belongs in the same live-message slot a `chunk`
frame writes to, or a subsequent delta will append to a duplicate bubble.

- [ ] **Step 5: Run the tests and typecheck**

```bash
cd apps/frontend && pnpm test -- useAgentChat && pnpm exec tsc --noEmit
```

Expected: PASS, no type errors.

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/hooks/
git commit -m "feat(chat): restore the in-flight run from chat.history on reload"
```

---

### Task 5: Jump to latest

`scrollToBottom` exists only as an imperative handle (`MessageList.tsx:1183-1238`), called once
50ms after a send (`AgentChatWindow.tsx:406`). Scroll up mid-stream and the only way back is
manual scrolling through a transcript that is still growing.

**The gating is the whole task, and upstream has the scar.** `ChatView.swift:38-49` records
that `hasNewerContentBelow` is **structurally true from the first Writing tick of every turn**,
so gating on it alone made the button flash on every reply — issue #108693. The shipped
predicate also requires `!isAtLiveEdge`, from real scroll geometry (`:407-417`).

**Files:**
- Modify: `apps/frontend/src/components/chat/MessageList.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/MessageList.test.tsx` (extend)

**Interfaces:** Consumes nothing. Produces no new exports.

- [ ] **Step 1: Write the failing tests**

```tsx
it("hides the jump button when the user is at the live edge", () => {
  renderList({ scrolledAway: false, contentArrivedSince: true });
  expect(screen.queryByRole("button", { name: /latest/i })).toBeNull();
});

it("hides it when the user has scrolled away but nothing new arrived", () => {
  renderList({ scrolledAway: true, contentArrivedSince: false });
  expect(screen.queryByRole("button", { name: /latest/i })).toBeNull();
});

it("shows it only when BOTH are true", () => {
  renderList({ scrolledAway: true, contentArrivedSince: true });
  expect(screen.getByRole("button", { name: /latest/i })).toBeInTheDocument();
});

it("clicking it returns to the bottom and hides the button", async () => {
  const { rerender } = renderList({ scrolledAway: true, contentArrivedSince: true });
  await userEvent.click(screen.getByRole("button", { name: /latest/i }));
  rerender(listWith({ scrolledAway: false, contentArrivedSince: false }));
  expect(screen.queryByRole("button", { name: /latest/i })).toBeNull();
});
```

Adapt `renderList` to however the existing `MessageList` tests drive scroll state — the
component measures real geometry at a 120px threshold (`:1258`), so the harness must set
`scrollTop`/`scrollHeight`/`clientHeight` rather than pass booleans, if that is what the
existing tests do.

- [ ] **Step 2: Run to confirm they fail**

Run: `cd apps/frontend && pnpm test -- MessageList`

Expected: FAIL — no such button.

- [ ] **Step 3: Implement**

Derive `scrolledAway` from the geometry already measured at `:1253-1262` (the 120px near-bottom
threshold), and `contentArrivedSince` from a counter that increments on each appended chunk and
resets when the user returns to the bottom. Render the button only when **both** hold:

```tsx
        {scrolledAway && contentArrivedSince && (
          <button
            type="button"
            aria-label="Scroll to latest"
            className="absolute bottom-4 left-1/2 -translate-x-1/2 rounded-full bg-[var(--cream-deep)] text-[var(--midnight)] shadow-md px-3 py-1.5 text-xs"
            onClick={() => scrollToBottom()}
          >
            Jump to latest
          </button>
        )}
```

Keep it clear of the composer — Android reserves a 56dp strip for exactly this reason
(`ui/chat/ChatScreen.kt:218-224`).

- [ ] **Step 4: Run tests, typecheck, brand scan**

```bash
cd apps/frontend && pnpm test -- MessageList && pnpm exec tsc --noEmit && node scripts/brand-tokens-scan.mjs
```

Expected: PASS, no type errors, no new colour-literal offenders.

- [ ] **Step 5: Commit**

```bash
git add apps/frontend/src/components/chat/
git commit -m "feat(chat): jump-to-latest, gated on scrolled-away AND content-arrived"
```

---

### Task 6: Fix or delete the dead Retry path

`ChatErrorCard.tsx:326-336` renders a Retry button gated on `Boolean(onRetry)` (`:293`), and
the only caller (`MessageList.tsx:704-710`) passes `message`, `code`, `interactive` and **never
`onRetry`**. `MONEY_CODES` (`:84-89`) would suppress it for four of the nine states anyway.
This is a bug, not a design difference.

**Files:**
- Modify: `apps/frontend/src/components/chat/ChatErrorCard.tsx` and/or
  `apps/frontend/src/components/chat/MessageList.tsx`
- Test: `apps/frontend/src/components/chat/__tests__/ChatErrorCard.test.tsx`

**Interfaces:** Consumes nothing. Produces either a wired `onRetry` or a smaller component.

- [ ] **Step 1: Decide, and record the decision in the commit message**

Retry is only meaningful for the transient codes — `timeout`, and arguably `rate_limit`. It is
wrong for `refusal`, `context_length` and all four `MONEY_CODES`, where retrying repeats the
failure. **Wire it for the transient codes; delete the branch for the rest.** If wiring it
means threading a resend through `MessageList`, prefer deleting the dead code and filing the
retry affordance as its own change — a dead button is worse than no button, and a button that
retries an out-of-credits error is worse than both.

- [ ] **Step 2: Write the test for whichever you chose**

If wiring:

```tsx
it("offers Retry for a timeout and calls back", async () => {
  const onRetry = vi.fn();
  render(<ChatErrorCard code="timeout" message="timed out" onRetry={onRetry} />);
  await userEvent.click(screen.getByRole("button", { name: /retry/i }));
  expect(onRetry).toHaveBeenCalled();
});

it("does NOT offer Retry for an out-of-credits error", () => {
  render(<ChatErrorCard code="out_of_credits" message="no credits" onRetry={vi.fn()} />);
  expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
});
```

If deleting:

```tsx
it("renders no Retry button — the affordance was never wired", () => {
  render(<ChatErrorCard code="timeout" message="timed out" />);
  expect(screen.queryByRole("button", { name: /retry/i })).toBeNull();
});
```

- [ ] **Step 3: Run to confirm it fails, implement, run again**

Run: `cd apps/frontend && pnpm test -- ChatErrorCard`

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/components/chat/
git commit -m "fix(chat): <wire|remove> the ChatErrorCard retry path, which never rendered"
```

---

## Verification — the whole feature, on dev

- [ ] **Step 1: Ship to dev.** Merge; watch the run with
  `gh run watch <id> --repo TheLightbulbCompany/lightbulb --exit-status`.

- [ ] **Step 2: The reconnect test (this is D1's real proof).** Start a reply that interleaves
  text and tools. Mid-stream, open the same chat in a second tab.
  **PASS:** the second tab has the whole reply so far. **FAIL:** it starts at the last block.

- [ ] **Step 3: The reload test.** Start a long run. Reload the page mid-run.
  **PASS:** the partial reply is still there and continues streaming.
  **FAIL:** an empty bubble until the run ends.

- [ ] **Step 4: The scroll test.** During a long streaming reply, scroll up.
  **PASS:** "Jump to latest" appears; clicking returns to the bottom and it disappears.
  **FAIL:** it flashes on every reply while you are already at the bottom — the predicate is
  gated on one condition instead of both.

- [ ] **Step 5: Regression — normal streaming is untouched.** A short single-block reply must
  stream exactly as before, with no duplicated text and no visible resync flicker.

- [ ] **Step 6: Promote to prod** only after 2–5 pass — `gh workflow run deploy.yml --ref main`.

---

## Expected UX

**Before:** open a chat in a second tab while the agent is mid-reply and you see only what it
said after its last tool call — the earlier paragraphs are gone, with no indication anything is
missing. Reload mid-run and the bubble is empty until the run finishes. Scroll up during a long
reply and you cannot get back down.

**After:** a reload restores what was on screen and keeps streaming. Scrolling up offers one
button back. And when the runtime itself reports lost frames, the next frame re-establishes a
full-text baseline instead of continuing to append onto a gap.

**Note what that last one does NOT do:** the baseline is built from *our* accumulated view, so
it cannot recover content the gateway never sent us. It stops the transcript diverging further;
it does not fill the hole. Say so plainly rather than implying recovery.

Whether every tab shows the whole reply depends on Task 0's answer.

## Not in this plan

- **`tool phase:"input_delta"` live edit diffs (D5)** and **`spawnedBy` subagent attribution
  (D6)** — both DROPPED events worth forwarding, but they add *new* information rather than
  repairing existing information. They belong with the subagent-activity work, which D6 gates.
- **Structured `item`/`command_output`/`patch` fields (D10)** — exit codes and durations we
  currently flatten away. Additive, not integrity.
- **"Whose problem is this" on error cards (UI14)** — a good change to the same file as Task 6,
  but it is copy and taxonomy work rather than transcript integrity, and mixing it in would
  make Task 6's decision harder to review.
