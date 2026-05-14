# Flat Chat Timeline — Design

> Status: Implemented (this PR).
> Authors: prez2307, Claude.
> Supersedes: the multi-bubble chat design from PR-branch `fix/e2e-wait-loop-with-wizard` (commit `cf67e689`, never landed on main) that informed the now-retired `runsRef`/`getOrCreateBubble`/`finalizeBubble` model in `useAgentChat`.

## Problem

The chat UI's per-runId bubble model exposes OpenClaw's internal multi-run pattern (caused by upstream issue [#70953](https://github.com/openclaw/openclaw/issues/70953) where exec-approval requests do not pause the agent turn) directly to the user. When the agent emits an approval request:

1. The agent's run terminates normally (chat.final fires).
2. Our `finalizeBubble(runId)` closes the visual bubble — the "Ray 8 stopped spinning" perception.
3. The user, thinking the agent has finished, types a follow-up.
4. Without a busy-gate that survives chat.final, the new send fires immediately. Two concurrent agent runs interleave on the session JSONL.

PR #621 and #628 attempted to patch this with per-user-turn `clientRunIdRef` tracking and an approval-pause guard. The guards work but layer complexity on top of a model that fundamentally contradicts what users see. The right structural fix is to remove the per-runId bubble concept entirely and mirror upstream's flat-message + render-time grouping model.

## Background

### Upstream's model (verified via Explore agents on `~/Desktop/openclaw`)

State (`ui/src/ui/app.ts:201-211, 582-583`):

| Field | Role |
|---|---|
| `chatMessages: HistoryMessage[]` | Persisted/finalized messages from `chat.history` + synthesized final assistants on `chat.final` |
| `chatStream: string \| null` | Current live accumulating assistant text (replaced, not appended) |
| `chatStreamSegments: Array<{text, ts}>` | Committed mid-stream text fragments — captured when a tool interrupts streaming |
| `chatToolMessages: HistoryMessage[]` | Synthesized assistant-role messages built from `toolStreamById` for live tools |
| `toolStreamById / toolStreamOrder` | Private — live tool tracking, throttled sync (80ms) into `chatToolMessages` |
| `chatRunId: string \| null` | Active client-minted UUID for the in-flight run |

Reconstruction (`ui/src/ui/controllers/chat.ts:387-458`): `chat.history` response stored essentially as-is, filtered via `shouldHideHistoryMessage` (silent reply / heartbeat-ack / synthetic-repair / empty-user). **No flattening of content blocks.** Each message stays whole; block-level fan-out happens at render time inside the bubble.

Rendering (`ui/src/ui/chat/build-chat-items.ts:249-364`): builds a flat `ChatItem[]` interleaving messages + stream-segments + tool-messages by index, then `groupMessages` wraps consecutive same-role items into visual `MessageGroup`s. **Grouping is render-only — no state identity attached.**

### Our current model (the per-bubble design we're retiring)

`useAgentChat.ts` keeps `runsRef: Map<runId, RunState>` and `getOrCreateBubble`/`finalizeBubble` lifecycle. Each OpenClaw sub-run produces its own `InternalMessage` bubble in the `messages` array. `MessageList.tsx` renders one rounded card per `InternalMessage` with tools inline as `toolUses[]` and thinking inline.

## Goal

Match upstream's flat-message + render-time grouping model. Keep Isol8's existing visual aesthetic (rounded bubbles, our tool indicator style, live thinking display). The user-perceived behavior is one continuous assistant bubble per user-turn, regardless of how many OpenClaw sub-runs occur.

## Architecture

### State

Replace `useAgentChat.ts`'s `messages: InternalMessage[]` + `runsRef` model with upstream's parallel-arrays model:

```ts
// Persisted/finalized messages — from chat.history + chat.final synthesis
chatMessages: HistoryMessage[]

// LIVE state for the in-flight run
chatStream: string | null              // current cumulative assistant text
chatStreamRunId: string | null
chatStreamStartedAt: number | null

// OUR ADDITION (upstream drops live thinking events; we keep them per user request)
chatStreamThinking: string | null
chatStreamThinkingRunId: string | null

// Committed mid-stream text fragments
chatStreamSegments: Array<{ text: string; ts: number }>

// Live tools — synthesized assistant-role messages from toolStreamById
chatToolMessages: HistoryMessage[]
toolStreamById: Map<string, ToolStreamEntry>  // private
toolStreamOrder: string[]                     // private, insertion order

// Busy gate (preserved from PR #621)
clientRunIdRef: string | null
```

Removed: `runsRef`, `finalizedRunsRef`, `getOrCreateBubble`, `finalizeBubble`, `finalizeAllActiveRuns`, `pendingApprovalsRef`, `awaitingResumeAfterApprovalRef` (the #628 approval-tracking refs go away — we accept upstream's race vulnerability in exchange for the simpler model). `pendingCancelRef` survives for the cancel-before-first-event window.

### Event handlers

Each WS event mutates the live-state fields:

- **`chunk(runId, content)`** — cumulative assistant text. If `chatStreamRunId !== runId` and `chatStream` non-empty, push it into `chatStreamSegments` and reset. Then `chatStream = content`, `chatStreamRunId = runId`.

- **`thinking(runId, content)`** — same pattern for `chatStreamThinking` / `chatStreamThinkingRunId`. (Our addition; upstream drops these events.)

- **`tool_start(runId, toolCallId, name, args)`** — if `chatStream` non-empty, push to `chatStreamSegments`; same for `chatStreamThinking`. Create/update `toolStreamById` entry; throttled sync (80ms, force-flush on result) into `chatToolMessages`.

- **`tool_end / tool_error(runId, toolCallId, result)`** — update matching `toolStreamById` entry's output + status; force-flush sync to `chatToolMessages`.

- **`done(runId)`** — synthesize an assistant message from `chatStream` + `chatStreamThinking` (content array of `[{type:"thinking"}, {type:"text"}]` blocks). Append to `chatMessages`. Clear `chatStream`, `chatStreamThinking`, `chatStreamRunId`, etc. Busy-gate: `if (runId === clientRunIdRef.current) clientRunIdRef.current = null;` — no approval guard.

- **`error(runId?, msg, code?)`** — synthesize an assistant-role error message with `content: [{type:"text", text:"Error: "+msg}]` and an `errorCode` field on the message envelope. Append to `chatMessages`. Existing busy-gate clear logic preserved.

- **`heartbeat`** — no chatItems mutation.

- **user `sendMessage` → `dispatchNow`** — append `{role:"user", content:[{type:"text", text:trimmed}], timestamp:Date.now()}` to `chatMessages`. clientRunIdRef setup unchanged.

**No `loadChatHistory` reload after tool-using chat.final** (upstream does this; we defer it until the 128 KB chat.history fix lands). Live tools accumulate in `chatToolMessages` for the session lifetime; refresh pulls fresh persisted state.

### Rendering — flat-item build + render-time grouping

Mirror upstream's `build-chat-items.ts:249-364`:

```ts
function buildRenderedItems(state): RenderItem[] {
  const items: RenderItem[] = [];

  // 1. History messages → one ChatItem each.
  //    `__openclaw.kind === "compaction"` becomes a divider.
  for (const m of chatMessages) {
    if (m.__openclaw?.kind === "compaction")
      items.push({ kind: "divider", id, label: "Context compacted" });
    else
      items.push({ kind: "message", message: m });
  }

  // 2. Interleave chatStreamSegments and chatToolMessages by index.
  const maxLen = Math.max(chatStreamSegments.length, chatToolMessages.length);
  for (let i = 0; i < maxLen; i++) {
    if (i < chatStreamSegments.length)
      items.push({ kind: "stream-segment", text: chatStreamSegments[i].text });
    if (i < chatToolMessages.length)
      items.push({ kind: "message", message: chatToolMessages[i] });
  }

  // 3. Live thinking (our addition) and live stream at the end.
  if (chatStreamThinking?.length)
    items.push({ kind: "live-thinking", text: chatStreamThinking });
  if (chatStream?.length)
    items.push({ kind: "stream", text: chatStream });

  return items;
}
```

`groupBySide(items)` wraps consecutive items belonging to the same side (user / assistant — tools/streams/live-thinking are assistant-side) into one visual bubble. Implementation mirrors upstream's `groupMessages` post-process.

**Block walking inside `{kind:"message"}`** uses upstream's `extractToolCards` algorithm (`ui/src/ui/chat/tool-cards.ts`) for tool_use ↔ tool_result pairing. Port as a utility — pairing is positional-within-message with message-level `toolCallId` synthesis as fallback.

`MessageList.tsx`'s render loop becomes:
- Walk grouped items.
- For each group, render a `<BubbleGroup side={...}>`.
- Inside the bubble, walk each item:
  - `{kind:"message"}` → walk `message.content[]` blocks: text → `<MarkdownContent>`, thinking → `<ThinkingBlock>`, tool_use → `<ToolUseIndicator>` (using `extractToolCards` for tool_use/tool_result pairing).
  - `{kind:"stream"}` → `<MarkdownContent>` styled as live.
  - `{kind:"live-thinking"}` → `<ThinkingBlock>` styled as live.
  - `{kind:"stream-segment"}` → `<MarkdownContent>` styled as historical.
  - `{kind:"divider"}` → divider element with label.

`StreamingStatusBar` reads from the new state (chatStream non-null OR any toolStreamById entry running OR clientRunIdRef set) instead of `messages[].toolUses[].status`.

`ExecApprovalCards` continues to render above `ChatInput`, orthogonal to the chat scroll. No change to `useExecApprovals`.

### chat.history reconstruction — filter only

Replace `useAgentChat.ts:480-518`'s decoder. The new logic is just filtering:

```ts
function projectChatHistoryMessages(messages: HistoryMessage[]): HistoryMessage[] {
  return messages.filter(m => !shouldHideHistoryMessage(m));
}
```

`shouldHideHistoryMessage` mirrors upstream's `controllers/chat.ts:225-232`:
- `isAssistantSilentReply` (text === NO_REPLY)
- `isAssistantHeartbeatAck` (heartbeat OK response)
- `isSyntheticTranscriptRepairToolResult` (toolResult with synthetic-repair body)
- `isEmptyUserTextOnlyMessage` (user with empty text-only content)

Compaction-marked messages flow through; the divider distinction is made at render-item build time.

Messages stay whole — no block-level transformation. Each becomes one `chatMessages[]` entry.

Filter helpers live in a new `apps/frontend/src/hooks/internal/chatHistoryFilters.ts` for isolated unit testing.

### Busy gate behavior (intentionally limited)

`clientRunIdRef` is set on user send (from `dispatchNow`), cleared on chat.final / error / aborted matching the runId. **No approval-pause guard from #628.**

**Mid-pause sends are an intended feature, not a bug.** When the agent emits an approval request, the user should be free to keep chatting without first deciding on the tool call — for example to add context, ask a clarifying question, or change direction. This matches upstream's behavior and matches the user's mental model of a fluid conversation. The frontend does NOT block mid-pause sends.

The previous PRs (#621 queue, #628 approval guards) were trying to prevent the user from being able to send mid-pause. That was the wrong model. This PR removes that gating.

What the frontend IS responsible for: rendering whatever the server emits accurately and in order. If the server returns interleaved responses (because two concurrent agent runs wrote to the JSONL), we render them in the order they arrived — separated by the second user message that broke the chat flow. The visual model handles this naturally: user-turn-1's items group into one assistant bubble, the user-turn-2 message breaks the chain, user-turn-2's items group into the next assistant bubble.

Any "confused output" the user sees when sending mid-pause comes from the server-side handling of concurrent runs on a single session JSONL — that's a backend concern (and arguably an upstream issue [#69386](https://github.com/openclaw/openclaw/issues/69386)). This frontend spec does not address it.

Queue mechanic from #621 stays: extra sends fired in rapid succession (e.g., double-click) still serialize through `queuedMessages` to avoid clobbering the WS frame ordering.

## Affected files

**Frontend:**
- `apps/frontend/src/hooks/useAgentChat.ts` — major rewrite of state + event handlers.
- `apps/frontend/src/hooks/internal/chatHistoryFilters.ts` — new, the filter helpers.
- `apps/frontend/src/hooks/internal/buildChatItems.ts` — new, the render-item build helper (mirrors upstream).
- `apps/frontend/src/components/chat/MessageList.tsx` — rewrite render loop to walk grouped items + block-level fan-out.
- `apps/frontend/src/components/chat/AgentChatWindow.tsx` — destructure changes only.
- `apps/frontend/src/components/chat/StreamingStatusBar.tsx` — read from new state shape (chatStream / toolStreamById) instead of `messages[].toolUses[].status`.
- `apps/frontend/src/components/control/panels/cron/sessionMessageAdapter.ts` — update for `RunTranscript`'s use of `MessageList`.

**Tests:**
- `apps/frontend/src/hooks/__tests__/useAgentChat.test.ts` — DELETE. The existing 521-line file is integration-style assertions on the per-bubble model (bubble counts, ghost-bubble M1 regression, runId resume-swap, etc.). Post-migration those assertions don't apply and rewriting them as integration tests against the new state shape duplicates what the helper unit tests already cover. The hook's behavior is verified through `chatHistoryFilters` + `buildChatItems` unit tests + the `MessageList` visual snapshot + the human end-of-PR smoke test.
- `apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts` — DELETE for the same reason. The queue surface is small enough that the human end-of-PR smoke test covers it.
- `apps/frontend/src/hooks/__tests__/useAgentChat.approval.test.ts` — DELETE (approval-tracking refs go away).
- `apps/frontend/src/hooks/__tests__/chatHistoryFilters.test.ts` — new.
- `apps/frontend/src/hooks/__tests__/buildChatItems.test.ts` — new.
- `apps/frontend/src/components/chat/__tests__/MessageList.snapshot.test.tsx` — new (or extend existing) visual-snapshot test.
- `apps/frontend/src/lib/__tests__/analytics.instrumentation.test.ts` — adjust per-turn counter sources (`assistant_message_length` redefined as the synthesized assistant message's text length on chat.final).

**Docs:**
- `docs/superpowers/specs/2026-04-21-multi-bubble-chat-design.md` — mark superseded.
- This spec, after user review.

## Risks and tradeoffs

1. **Mid-pause sends are allowed**: not a risk — intended UX. The frontend no longer tries to gate them. Any user-visible interleaving comes from server-side concurrent-run handling, which is out of scope for this spec.

2. **`StreamingStatusBar` brief darkness mid-turn**: between `chat.final` of sub-run A and first chunk of resumed sub-run B, both `chatStream` and `clientRunIdRef` are null. The pill flickers off and on. Same flicker as today; the new continuous-bubble visual compensates.

3. **Live tool accumulation in long sessions**: without `loadChatHistory` reload after tool-using chat.final, `chatToolMessages` grows for the session lifetime. Memory order-of-magnitude: hundreds of items per very long session, single-digit MB. Refresh clears. Not blocking.

4. **`MessageList` is shared with `RunTranscript`** (cron transcripts). Migration touches `sessionMessageAdapter.ts`. Cron-transcript regressions would surface in the SessionsPanel; covered by adapter tests.

5. **Test coverage gap**: `useAgentChat.test.ts` has 521 lines of per-bubble assertions ("creates one bubble per runId", "renders two assistant bubbles when two runIds stream within one chat.send", ghost-bubble M1 regression, etc.). Most of these tests get rewritten or retired. The new equivalents test render-item composition + state-shape correctness — different assertions, comparable coverage.

6. **`extractToolCards` port**: pairing logic from upstream's `tool-cards.ts:73-185`. Direct port; must be kept in sync with upstream if it evolves.

## Out of scope

- **128 KB `chat.history` 413 fix** — separate workstream (REST proxy via ALB, or backend-side chunking). This spec depends on `chat.history` returning the full payload; if the call 413's, the user sees a degraded chat (live state only).

- **Backend event-replay on reconnect** — separate workstream.

- **The async-completion text leak** (e.g., "An async command the user already approved has completed...") — separate display bug, requires upstream filtering. Spec is unaware of this content category; renders it as plain text for now.

- **Visual redesign** of tools, thinking, etc. — keep current aesthetics; no UI rework.

- **Server-side concurrent-run handling** — backend-level serialization of chat.send on a single session, or upstream's [#69386](https://github.com/openclaw/openclaw/issues/69386) work, are separate workstreams. This spec does not address them.

## Testing strategy

Test-driven development per the existing project convention (`feedback_write_tests_run_at_end`): each implementation task writes its tests first, but tests are **not run per-task**. A single end-of-plan verification batch runs the full suite.

1. **Unit tests** for `chatHistoryFilters.ts`: each filter as isolated function with fixture cases (silent reply, heartbeat-ack, synthetic-repair, empty-user, compaction divider passthrough, message-level toolCallId synthesis).

2. **Unit tests** for `buildChatItems.ts`: fixture states (no items, history-only, history + stream-segments + tool-messages, live thinking, live stream, all combined) → assert rendered-item ordering.

3. **Visual snapshot** for `MessageList`: render the same item-state fixture → assert DOM structure. Covers the rendering refactor without trying to test pixel-perfect output.

4. **End-of-PR live smoke** (human):
   - Send a message that triggers an exec-approval card.
   - Verify the assistant bubble does not visually close when chat.final fires.
   - Click Allow; verify the resumed run's content appends to the SAME bubble.
   - Refresh the page; verify the chat.history reload shows the full transcript including tool calls/results.
   - Type and send a message during the approval pause; verify the second message appears as expected and the agent's response (whatever it is) renders cleanly in a new bubble after the user-turn-2 boundary.
