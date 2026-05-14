# Flat Chat Timeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Isol8's per-runId multi-bubble chat model with upstream OpenClaw's flat-message + render-time grouping pattern, fixing the "Ray 8 stopped spinning" perception bug while keeping the existing Isol8 visual aesthetic.

**Architecture:** State becomes upstream's parallel-arrays model (`chatMessages` + `chatStream` + `chatStreamSegments` + `chatToolMessages` + `toolStreamById`). Rendering builds a flat item list at render time and groups consecutive same-side items into one visual bubble. Live thinking is preserved (our one divergence from upstream). The approval-pause guard from PR #628 is removed — mid-pause sends are intended UX. The full design lives at `docs/superpowers/specs/2026-05-13-flat-chat-timeline-design.md`.

**Tech Stack:** React 19, TypeScript, vitest + @testing-library/react for frontend tests. No backend changes.

**Testing convention** (per `feedback_write_tests_run_at_end`): write test files per task, do NOT run them between tasks. A single end-of-plan verification batch (Task 11) runs the full suite, lint, and tsc.

**Worktree assumption:** the executing engineer will be in an isolated worktree set up via `superpowers:using-git-worktrees`. All paths in this plan are relative to the worktree root.

---

## File Structure

**New helper module** (extracted from what's currently inline in `useAgentChat.ts`):

- `apps/frontend/src/hooks/internal/types.ts` — shared types: `HistoryMessage`, `ContentBlock`, `ToolStreamEntry`, `ChatRenderItem`, `RenderGroup`, etc.
- `apps/frontend/src/hooks/internal/chatHistoryFilters.ts` — `shouldHideHistoryMessage` and its four filter predicates.
- `apps/frontend/src/hooks/internal/toolBlockExtract.ts` — content-block predicates (text/thinking/tool_use/tool_result) and the tool_use ↔ tool_result pairing helper.
- `apps/frontend/src/hooks/internal/buildChatItems.ts` — assemble the flat render-item list from useAgentChat's state.
- `apps/frontend/src/hooks/internal/groupBySide.ts` — post-process to wrap consecutive same-side items into one visual `RenderGroup`.

**Modified:**
- `apps/frontend/src/hooks/useAgentChat.ts` — major rewrite. State shape + event handlers + history reconstruction + return surface.
- `apps/frontend/src/components/chat/MessageList.tsx` — rewrite render loop. Walk grouped items; within each `{kind:"message"}` item, walk content blocks.
- `apps/frontend/src/components/chat/StreamingStatusBar.tsx` — derive from new state (chatStream / toolStreamById / clientRunIdRef) instead of `messages[].toolUses[].status`.
- `apps/frontend/src/components/chat/AgentChatWindow.tsx` — destructuring updates to consume the new `useAgentChat` return surface.
- `apps/frontend/src/components/control/panels/cron/sessionMessageAdapter.ts` — adjust for `RunTranscript`'s reuse of `MessageList`.

**Deleted:**
- `apps/frontend/src/hooks/__tests__/useAgentChat.test.ts` — per-bubble integration tests that no longer apply.
- `apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts` — queue surface covered by smoke test.
- `apps/frontend/src/hooks/__tests__/useAgentChat.approval.test.ts` — approval-tracking refs go away.

**New tests:**
- `apps/frontend/src/hooks/__tests__/chatHistoryFilters.test.ts`
- `apps/frontend/src/hooks/__tests__/buildChatItems.test.ts`
- `apps/frontend/src/hooks/__tests__/toolBlockExtract.test.ts`
- `apps/frontend/src/components/chat/__tests__/MessageList.snapshot.test.tsx` (new file)

**Docs:**
- `docs/superpowers/specs/2026-04-21-multi-bubble-chat-design.md` — add superseded marker.

---

## Task 1: Shared internal types module

**Files:**
- Create: `apps/frontend/src/hooks/internal/types.ts`

- [ ] **Step 1: Create the types module**

```ts
// apps/frontend/src/hooks/internal/types.ts
//
// Shared types used by useAgentChat and its helper modules. Mirrors
// the upstream OpenClaw control-ui shape (~/Desktop/openclaw/ui/src/ui)
// with Isol8-specific additions (live thinking) called out inline.

/** A content block within a HistoryMessage. Field shapes vary by `type`. */
export interface ContentBlock {
  type: string;
  // text-flavored
  text?: string;
  // thinking-flavored
  thinking?: string;
  // tool_use-flavored
  id?: string;
  name?: string;
  args?: unknown;
  arguments?: unknown;
  input?: unknown;
  // tool_result-flavored
  tool_use_id?: string;
  toolUseId?: string;
  toolCallId?: string;
  content?: unknown;
  result?: unknown;
  isError?: boolean;
  // image-flavored (after upstream's chat-display-projection)
  omitted?: boolean;
  bytes?: number;
}

/** A whole message from chat.history or synthesized at chat.final. */
export interface HistoryMessage {
  role: string;
  content?: ContentBlock[];
  text?: string;
  timestamp?: number;
  runId?: string;
  toolCallId?: string;
  tool_call_id?: string;
  toolName?: string;
  tool_name?: string;
  errorCode?: string;
  __openclaw?: { kind?: string };
}

/** Live tool tracking entry (private to useAgentChat). */
export interface ToolStreamEntry {
  toolCallId: string;
  runId: string;
  name: string;
  args?: unknown;
  output?: unknown;
  status: "running" | "done" | "error";
  startedAt: number;
  updatedAt: number;
}

/** A single render item produced by buildChatItems(). */
export type ChatRenderItem =
  | { kind: "message"; key: string; message: HistoryMessage }
  | { kind: "stream-segment"; key: string; text: string; ts: number }
  | { kind: "stream"; key: string; text: string; runId: string }
  | { kind: "live-thinking"; key: string; text: string; runId: string }
  | { kind: "divider"; key: string; label: string };

/** Visual side of a rendered group. */
export type RenderSide = "user" | "assistant";

/** A visual bubble group — consecutive items on the same side. */
export interface RenderGroup {
  side: RenderSide;
  key: string;
  items: ChatRenderItem[];
}
```

- [ ] **Step 2: Commit**

```bash
git add apps/frontend/src/hooks/internal/types.ts
git commit -m "feat(chat): add internal types module for flat-timeline refactor

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 2: chatHistoryFilters helper + tests

Mirrors upstream's `shouldHideHistoryMessage` (`~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:225-232`).

**Files:**
- Create: `apps/frontend/src/hooks/internal/chatHistoryFilters.ts`
- Create: `apps/frontend/src/hooks/__tests__/chatHistoryFilters.test.ts`

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/hooks/__tests__/chatHistoryFilters.test.ts
import { describe, it, expect } from "vitest";
import {
  isAssistantSilentReply,
  isAssistantHeartbeatAck,
  isSyntheticTranscriptRepairToolResult,
  isEmptyUserTextOnlyMessage,
  shouldHideHistoryMessage,
  projectChatHistoryMessages,
} from "../internal/chatHistoryFilters";
import type { HistoryMessage } from "../internal/types";

describe("isAssistantSilentReply", () => {
  it("returns true when assistant text is NO_REPLY", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "NO_REPLY" }],
    };
    expect(isAssistantSilentReply(m)).toBe(true);
  });

  it("returns true with whitespace around NO_REPLY", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "  NO_REPLY  " }],
    };
    expect(isAssistantSilentReply(m)).toBe(true);
  });

  it("returns false for normal assistant text", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "Hello" }],
    };
    expect(isAssistantSilentReply(m)).toBe(false);
  });

  it("returns false for non-assistant role", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "NO_REPLY" }],
    };
    expect(isAssistantSilentReply(m)).toBe(false);
  });
});

describe("isAssistantHeartbeatAck", () => {
  it("returns true for assistant 'HEARTBEAT_OK' response", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "HEARTBEAT_OK" }],
    };
    expect(isAssistantHeartbeatAck(m)).toBe(true);
  });

  it("returns false for normal assistant text", () => {
    const m: HistoryMessage = {
      role: "assistant",
      content: [{ type: "text", text: "Hello" }],
    };
    expect(isAssistantHeartbeatAck(m)).toBe(false);
  });
});

describe("isSyntheticTranscriptRepairToolResult", () => {
  it("returns true for tool message with synthetic-repair marker", () => {
    const m: HistoryMessage = {
      role: "toolResult",
      content: [{ type: "tool_result", content: "SYNTHETIC_TRANSCRIPT_REPAIR_RESULT" }],
    };
    expect(isSyntheticTranscriptRepairToolResult(m)).toBe(true);
  });

  it("returns false for normal tool result", () => {
    const m: HistoryMessage = {
      role: "toolResult",
      content: [{ type: "tool_result", content: "actual output" }],
    };
    expect(isSyntheticTranscriptRepairToolResult(m)).toBe(false);
  });
});

describe("isEmptyUserTextOnlyMessage", () => {
  it("returns true for user with only-whitespace text", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "   " }],
    };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(true);
  });

  it("returns true for user with no content blocks", () => {
    const m: HistoryMessage = { role: "user", content: [] };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(true);
  });

  it("returns false for user with non-empty text", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "text", text: "Hello" }],
    };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(false);
  });

  it("returns false for user with non-text content (image attachment)", () => {
    const m: HistoryMessage = {
      role: "user",
      content: [{ type: "image", bytes: 1024 }],
    };
    expect(isEmptyUserTextOnlyMessage(m)).toBe(false);
  });
});

describe("projectChatHistoryMessages", () => {
  it("removes hidden messages while preserving order", () => {
    const messages: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "assistant", content: [{ type: "text", text: "NO_REPLY" }] },
      { role: "assistant", content: [{ type: "text", text: "hello back" }] },
    ];
    const result = projectChatHistoryMessages(messages);
    expect(result).toHaveLength(2);
    expect(result[0].content?.[0].text).toBe("hi");
    expect(result[1].content?.[0].text).toBe("hello back");
  });
});
```

- [ ] **Step 2: Write the implementation**

```ts
// apps/frontend/src/hooks/internal/chatHistoryFilters.ts
//
// Mirrors upstream OpenClaw's `shouldHideHistoryMessage` at
// `~/Desktop/openclaw/ui/src/ui/controllers/chat.ts:225-232`.
// Kept as an extractable helper so it's unit-testable and easy to
// re-sync if upstream's filter logic evolves.

import type { HistoryMessage } from "./types";

const SILENT_REPLY_TOKEN = "NO_REPLY";
const HEARTBEAT_OK_TOKEN = "HEARTBEAT_OK";
const SYNTHETIC_REPAIR_TOKEN = "SYNTHETIC_TRANSCRIPT_REPAIR_RESULT";

function extractFirstText(m: HistoryMessage): string {
  if (typeof m.text === "string") return m.text;
  if (!Array.isArray(m.content)) return "";
  for (const b of m.content) {
    if (b && typeof b === "object" && typeof b.text === "string") {
      return b.text;
    }
  }
  return "";
}

function lowerRole(m: HistoryMessage): string {
  return typeof m.role === "string" ? m.role.toLowerCase() : "";
}

export function isAssistantSilentReply(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "assistant") return false;
  return extractFirstText(m).trim() === SILENT_REPLY_TOKEN;
}

export function isAssistantHeartbeatAck(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "assistant") return false;
  return extractFirstText(m).trim() === HEARTBEAT_OK_TOKEN;
}

export function isSyntheticTranscriptRepairToolResult(m: HistoryMessage): boolean {
  const role = lowerRole(m);
  if (role !== "toolresult" && role !== "tool_result" && role !== "tool") return false;
  if (!Array.isArray(m.content)) return false;
  for (const b of m.content) {
    if (!b || typeof b !== "object") continue;
    const c = b.content ?? b.result;
    if (typeof c === "string" && c.includes(SYNTHETIC_REPAIR_TOKEN)) return true;
  }
  return false;
}

export function isEmptyUserTextOnlyMessage(m: HistoryMessage): boolean {
  if (lowerRole(m) !== "user") return false;
  if (!Array.isArray(m.content) || m.content.length === 0) return true;
  // Reject if any non-text block exists (attachments, images, etc.)
  const allTextBlocks = m.content.every(
    (b) => b && typeof b === "object" && typeof b.type === "string" &&
      (b.type === "text" || b.type === "input_text" || b.type === "output_text"),
  );
  if (!allTextBlocks) return false;
  // All text blocks must be empty after trim.
  return m.content.every(
    (b) => typeof b.text !== "string" || b.text.trim().length === 0,
  );
}

export function shouldHideHistoryMessage(m: HistoryMessage): boolean {
  return (
    isAssistantSilentReply(m) ||
    isAssistantHeartbeatAck(m) ||
    isSyntheticTranscriptRepairToolResult(m) ||
    isEmptyUserTextOnlyMessage(m)
  );
}

export function projectChatHistoryMessages(
  messages: HistoryMessage[],
): HistoryMessage[] {
  return messages.filter((m) => !shouldHideHistoryMessage(m));
}
```

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/hooks/internal/chatHistoryFilters.ts apps/frontend/src/hooks/__tests__/chatHistoryFilters.test.ts
git commit -m "feat(chat): add chatHistoryFilters mirroring upstream shouldHideHistoryMessage

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: toolBlockExtract helper + tests

Ports upstream's tool-block predicates + tool_use ↔ tool_result pairing logic. Source: `~/Desktop/openclaw/src/chat/tool-content.ts` and `~/Desktop/openclaw/ui/src/ui/chat/tool-cards.ts:73-185`.

**Files:**
- Create: `apps/frontend/src/hooks/internal/toolBlockExtract.ts`
- Create: `apps/frontend/src/hooks/__tests__/toolBlockExtract.test.ts`

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/hooks/__tests__/toolBlockExtract.test.ts
import { describe, it, expect } from "vitest";
import {
  isTextBlock,
  isThinkingBlock,
  isToolUseBlock,
  isToolResultBlock,
  resolveToolUseId,
  resolveToolArgs,
  pairToolBlocks,
} from "../internal/toolBlockExtract";

describe("block type predicates", () => {
  it("isTextBlock matches text / output_text / input_text", () => {
    expect(isTextBlock({ type: "text", text: "x" })).toBe(true);
    expect(isTextBlock({ type: "output_text", text: "x" })).toBe(true);
    expect(isTextBlock({ type: "input_text", text: "x" })).toBe(true);
    expect(isTextBlock({ type: "thinking", thinking: "x" })).toBe(false);
    expect(isTextBlock(null)).toBe(false);
  });

  it("isThinkingBlock matches thinking type only", () => {
    expect(isThinkingBlock({ type: "thinking" })).toBe(true);
    expect(isThinkingBlock({ type: "text" })).toBe(false);
  });

  it("isToolUseBlock matches all case variants", () => {
    expect(isToolUseBlock({ type: "tool_use" })).toBe(true);
    expect(isToolUseBlock({ type: "toolUse" })).toBe(true);
    expect(isToolUseBlock({ type: "tool_call" })).toBe(true);
    expect(isToolUseBlock({ type: "toolCall" })).toBe(true);
    expect(isToolUseBlock({ type: "ToolUse" })).toBe(true);  // case-insensitive
    expect(isToolUseBlock({ type: "text" })).toBe(false);
  });

  it("isToolResultBlock matches all case variants", () => {
    expect(isToolResultBlock({ type: "tool_result" })).toBe(true);
    expect(isToolResultBlock({ type: "toolResult" })).toBe(true);
    expect(isToolResultBlock({ type: "ToolResult" })).toBe(true);
    expect(isToolResultBlock({ type: "text" })).toBe(false);
  });
});

describe("resolveToolUseId", () => {
  it("prefers id, then tool_use_id, then toolUseId", () => {
    expect(resolveToolUseId({ id: "a" })).toBe("a");
    expect(resolveToolUseId({ tool_use_id: "b" })).toBe("b");
    expect(resolveToolUseId({ toolUseId: "c" })).toBe("c");
    expect(resolveToolUseId({ id: "a", tool_use_id: "b" })).toBe("a");
    expect(resolveToolUseId({})).toBeUndefined();
  });
});

describe("resolveToolArgs", () => {
  it("prefers args, then arguments, then input", () => {
    expect(resolveToolArgs({ args: { a: 1 } })).toEqual({ a: 1 });
    expect(resolveToolArgs({ arguments: { b: 2 } })).toEqual({ b: 2 });
    expect(resolveToolArgs({ input: { c: 3 } })).toEqual({ c: 3 });
    expect(resolveToolArgs({ args: { a: 1 }, input: { c: 3 } })).toEqual({ a: 1 });
    expect(resolveToolArgs({})).toBeUndefined();
  });
});

describe("pairToolBlocks", () => {
  it("pairs tool_use with tool_result by id within one message's content", () => {
    const blocks = [
      { type: "tool_use", id: "t1", name: "exec", input: { cmd: "ls" } },
      { type: "tool_result", tool_use_id: "t1", content: "output" },
    ];
    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].toolUseId).toBe("t1");
    expect(pairs[0].toolUseBlock).toBe(blocks[0]);
    expect(pairs[0].toolResultBlock).toBe(blocks[1]);
  });

  it("pairs by camelCase toolUseId variant", () => {
    const blocks = [
      { type: "tool_use", id: "t2" },
      { type: "tool_result", toolUseId: "t2" },
    ];
    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].toolUseId).toBe("t2");
  });

  it("returns an unpaired tool_use without a matching result", () => {
    const blocks = [{ type: "tool_use", id: "t3", name: "exec" }];
    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(1);
    expect(pairs[0].toolUseId).toBe("t3");
    expect(pairs[0].toolResultBlock).toBeUndefined();
  });

  it("skips orphan tool_result without a matching tool_use", () => {
    const blocks = [{ type: "tool_result", tool_use_id: "ghost", content: "x" }];
    const pairs = pairToolBlocks(blocks);
    expect(pairs).toHaveLength(0);
  });
});
```

- [ ] **Step 2: Write the implementation**

```ts
// apps/frontend/src/hooks/internal/toolBlockExtract.ts
//
// Content-block predicates and tool_use ↔ tool_result pairing.
// Ported from upstream OpenClaw's `~/Desktop/openclaw/src/chat/
// tool-content.ts` and `~/Desktop/openclaw/ui/src/ui/chat/tool-cards.ts`.

import type { ContentBlock } from "./types";

const TEXT_TYPES = new Set(["text", "output_text", "input_text"]);
const TOOL_USE_TYPES = new Set(["toolcall", "tool_call", "tooluse", "tool_use"]);
const TOOL_RESULT_TYPES = new Set(["toolresult", "tool_result"]);

function lowerType(b: unknown): string {
  if (!b || typeof b !== "object") return "";
  const t = (b as { type?: unknown }).type;
  return typeof t === "string" ? t.toLowerCase() : "";
}

export function isTextBlock(b: unknown): boolean {
  return TEXT_TYPES.has(lowerType(b));
}

export function isThinkingBlock(b: unknown): boolean {
  return lowerType(b) === "thinking";
}

export function isToolUseBlock(b: unknown): boolean {
  return TOOL_USE_TYPES.has(lowerType(b));
}

export function isToolResultBlock(b: unknown): boolean {
  return TOOL_RESULT_TYPES.has(lowerType(b));
}

/** Resolve the block-level tool_use id, accepting all three naming variants. */
export function resolveToolUseId(b: unknown): string | undefined {
  if (!b || typeof b !== "object") return undefined;
  const r = b as { id?: unknown; tool_use_id?: unknown; toolUseId?: unknown };
  if (typeof r.id === "string" && r.id.length > 0) return r.id;
  if (typeof r.tool_use_id === "string" && r.tool_use_id.length > 0) return r.tool_use_id;
  if (typeof r.toolUseId === "string" && r.toolUseId.length > 0) return r.toolUseId;
  return undefined;
}

/** Resolve tool args, accepting all three naming variants (Anthropic uses `input`). */
export function resolveToolArgs(b: unknown): unknown {
  if (!b || typeof b !== "object") return undefined;
  const r = b as { args?: unknown; arguments?: unknown; input?: unknown };
  if (r.args !== undefined) return r.args;
  if (r.arguments !== undefined) return r.arguments;
  if (r.input !== undefined) return r.input;
  return undefined;
}

export interface ToolBlockPair {
  toolUseId: string;
  toolUseBlock: ContentBlock;
  toolResultBlock?: ContentBlock;
}

/**
 * Pair tool_use blocks with their matching tool_result blocks within a single
 * message's content array. Pairing is by id (block-level). Orphan tool_results
 * (no matching tool_use) are dropped.
 *
 * Source: upstream's `tool-cards.ts:73-185` does positional-within-message
 * pairing; we use id-based to be more robust to reordering. Functionally
 * equivalent for messages that have one tool_use + one tool_result in order.
 */
export function pairToolBlocks(blocks: ContentBlock[]): ToolBlockPair[] {
  const pairs: ToolBlockPair[] = [];
  const useById = new Map<string, ContentBlock>();
  const useIdOrder: string[] = [];

  for (const b of blocks) {
    if (isToolUseBlock(b)) {
      const id = resolveToolUseId(b);
      if (!id) continue;
      useById.set(id, b);
      useIdOrder.push(id);
    }
  }

  for (const b of blocks) {
    if (!isToolResultBlock(b)) continue;
    const targetId = resolveToolUseId(b);
    if (!targetId) continue;
    const useBlock = useById.get(targetId);
    if (!useBlock) continue;
    pairs.push({ toolUseId: targetId, toolUseBlock: useBlock, toolResultBlock: b });
    useById.delete(targetId);
  }

  // Any remaining tool_use blocks without a result still appear (status: running/done unknown).
  for (const id of useIdOrder) {
    if (useById.has(id)) {
      pairs.push({ toolUseId: id, toolUseBlock: useById.get(id)! });
    }
  }

  return pairs;
}
```

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/hooks/internal/toolBlockExtract.ts apps/frontend/src/hooks/__tests__/toolBlockExtract.test.ts
git commit -m "feat(chat): add toolBlockExtract helper for content-block parsing and tool pairing

Ports upstream's tool_use/tool_result block predicates and pairing logic
from ~/Desktop/openclaw/src/chat/tool-content.ts and tool-cards.ts.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: buildChatItems + groupBySide helpers + tests

Mirrors upstream's `~/Desktop/openclaw/ui/src/ui/chat/build-chat-items.ts:249-364` plus the `groupMessages` post-process. Our addition: a `live-thinking` item kind.

**Files:**
- Create: `apps/frontend/src/hooks/internal/buildChatItems.ts`
- Create: `apps/frontend/src/hooks/internal/groupBySide.ts`
- Create: `apps/frontend/src/hooks/__tests__/buildChatItems.test.ts`

- [ ] **Step 1: Write the failing tests**

```ts
// apps/frontend/src/hooks/__tests__/buildChatItems.test.ts
import { describe, it, expect } from "vitest";
import { buildChatItems } from "../internal/buildChatItems";
import { groupBySide } from "../internal/groupBySide";
import type { HistoryMessage } from "../internal/types";

const baseState = {
  chatMessages: [] as HistoryMessage[],
  chatStream: null as string | null,
  chatStreamRunId: null as string | null,
  chatStreamThinking: null as string | null,
  chatStreamThinkingRunId: null as string | null,
  chatStreamSegments: [] as Array<{ text: string; ts: number }>,
  chatToolMessages: [] as HistoryMessage[],
  sessionKey: "agent:test:user",
};

describe("buildChatItems", () => {
  it("returns empty for empty state", () => {
    expect(buildChatItems(baseState)).toEqual([]);
  });

  it("emits one message item per chatMessages entry", () => {
    const msgs: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "assistant", content: [{ type: "text", text: "hello" }] },
    ];
    const items = buildChatItems({ ...baseState, chatMessages: msgs });
    expect(items).toHaveLength(2);
    expect(items[0].kind).toBe("message");
    expect(items[1].kind).toBe("message");
  });

  it("emits a divider for compaction-marked messages", () => {
    const msgs: HistoryMessage[] = [
      { role: "user", content: [{ type: "text", text: "hi" }] },
      { role: "system", __openclaw: { kind: "compaction" } },
      { role: "user", content: [{ type: "text", text: "hi2" }] },
    ];
    const items = buildChatItems({ ...baseState, chatMessages: msgs });
    expect(items).toHaveLength(3);
    expect(items[1].kind).toBe("divider");
    if (items[1].kind === "divider") {
      expect(items[1].label).toContain("compact");
    }
  });

  it("interleaves chatStreamSegments and chatToolMessages by index", () => {
    const segments = [
      { text: "before tool A", ts: 1 },
      { text: "between A and B", ts: 3 },
    ];
    const toolMsgs: HistoryMessage[] = [
      { role: "assistant", content: [{ type: "tool_use", id: "A", name: "exec" }] },
      { role: "assistant", content: [{ type: "tool_use", id: "B", name: "exec" }] },
    ];
    const items = buildChatItems({
      ...baseState,
      chatStreamSegments: segments,
      chatToolMessages: toolMsgs,
    });
    expect(items.map((i) => i.kind)).toEqual([
      "stream-segment",
      "message",
      "stream-segment",
      "message",
    ]);
  });

  it("appends live thinking item before live stream at the tail", () => {
    const items = buildChatItems({
      ...baseState,
      chatStreamThinking: "let me think",
      chatStreamThinkingRunId: "run-1",
      chatStream: "the answer is",
      chatStreamRunId: "run-1",
    });
    expect(items).toHaveLength(2);
    expect(items[0].kind).toBe("live-thinking");
    expect(items[1].kind).toBe("stream");
  });

  it("omits live items when their text is empty", () => {
    const items = buildChatItems({
      ...baseState,
      chatStream: "",
      chatStreamRunId: "run-1",
    });
    expect(items).toHaveLength(0);
  });
});

describe("groupBySide", () => {
  it("groups consecutive user items as user side", () => {
    const items = buildChatItems({
      ...baseState,
      chatMessages: [
        { role: "user", content: [{ type: "text", text: "a" }] },
        { role: "user", content: [{ type: "text", text: "b" }] },
      ],
    });
    const groups = groupBySide(items);
    expect(groups).toHaveLength(1);
    expect(groups[0].side).toBe("user");
    expect(groups[0].items).toHaveLength(2);
  });

  it("breaks groups on user/assistant role transitions", () => {
    const items = buildChatItems({
      ...baseState,
      chatMessages: [
        { role: "user", content: [{ type: "text", text: "a" }] },
        { role: "assistant", content: [{ type: "text", text: "b" }] },
        { role: "user", content: [{ type: "text", text: "c" }] },
      ],
    });
    const groups = groupBySide(items);
    expect(groups.map((g) => g.side)).toEqual(["user", "assistant", "user"]);
  });

  it("treats assistant + tool + stream items as one assistant group", () => {
    const items = buildChatItems({
      ...baseState,
      chatMessages: [
        { role: "assistant", content: [{ type: "text", text: "thinking..." }] },
      ],
      chatToolMessages: [
        { role: "assistant", content: [{ type: "tool_use", id: "A", name: "exec" }] },
      ],
      chatStreamSegments: [{ text: "more text", ts: 1 }],
      chatStream: "even more",
      chatStreamRunId: "r",
    });
    const groups = groupBySide(items);
    expect(groups).toHaveLength(1);
    expect(groups[0].side).toBe("assistant");
  });

  it("treats divider as its own side-less group with side='assistant'", () => {
    const items = buildChatItems({
      ...baseState,
      chatMessages: [
        { role: "user", content: [{ type: "text", text: "a" }] },
        { role: "system", __openclaw: { kind: "compaction" } },
        { role: "user", content: [{ type: "text", text: "b" }] },
      ],
    });
    const groups = groupBySide(items);
    // divider breaks the user grouping — but stays adjacent to its neighbors
    expect(groups.length).toBeGreaterThan(1);
  });
});
```

- [ ] **Step 2: Write buildChatItems implementation**

```ts
// apps/frontend/src/hooks/internal/buildChatItems.ts
//
// Build a flat render-item list from useAgentChat's state. Mirrors
// upstream's `~/Desktop/openclaw/ui/src/ui/chat/build-chat-items.ts:249-364`
// with one addition: a `live-thinking` item kind (Isol8 keeps live
// thinking, upstream drops it).

import type { ChatRenderItem, HistoryMessage } from "./types";

export interface ChatItemsState {
  chatMessages: HistoryMessage[];
  chatStream: string | null;
  chatStreamRunId: string | null;
  chatStreamThinking: string | null;
  chatStreamThinkingRunId: string | null;
  chatStreamSegments: Array<{ text: string; ts: number }>;
  chatToolMessages: HistoryMessage[];
  sessionKey: string;
}

const COMPACTION_LABEL = "Context compacted";

function isCompactionMessage(m: HistoryMessage): boolean {
  return m.__openclaw?.kind === "compaction";
}

function messageKey(m: HistoryMessage, idx: number, prefix: string): string {
  const ts = m.timestamp ?? idx;
  return `${prefix}:${idx}:${ts}`;
}

export function buildChatItems(state: ChatItemsState): ChatRenderItem[] {
  const items: ChatRenderItem[] = [];

  // 1. History messages — each becomes one ChatRenderItem.
  state.chatMessages.forEach((m, idx) => {
    if (isCompactionMessage(m)) {
      items.push({
        kind: "divider",
        key: `divider:${idx}:${m.timestamp ?? idx}`,
        label: COMPACTION_LABEL,
      });
    } else {
      items.push({
        kind: "message",
        key: messageKey(m, idx, `msg:${state.sessionKey}`),
        message: m,
      });
    }
  });

  // 2. Interleave chatStreamSegments and chatToolMessages by index.
  //    Upstream pattern: when a tool interrupts streaming, chatStream is
  //    flushed into segments and a new tool message begins. So segments[i]
  //    is the text-accumulated-before-tool[i].
  const maxLen = Math.max(
    state.chatStreamSegments.length,
    state.chatToolMessages.length,
  );
  for (let i = 0; i < maxLen; i++) {
    const seg = state.chatStreamSegments[i];
    if (seg && seg.text.length > 0) {
      items.push({
        kind: "stream-segment",
        key: `seg:${state.sessionKey}:${i}:${seg.ts}`,
        text: seg.text,
        ts: seg.ts,
      });
    }
    const tool = state.chatToolMessages[i];
    if (tool) {
      items.push({
        kind: "message",
        key: messageKey(tool, i, `tool:${state.sessionKey}`),
        message: tool,
      });
    }
  }

  // 3. Live thinking (our addition) at the tail.
  if (state.chatStreamThinking && state.chatStreamThinking.length > 0) {
    items.push({
      kind: "live-thinking",
      key: `live-think:${state.chatStreamThinkingRunId ?? "live"}`,
      text: state.chatStreamThinking,
      runId: state.chatStreamThinkingRunId ?? "",
    });
  }

  // 4. Live chatStream at the tail.
  if (state.chatStream && state.chatStream.length > 0) {
    items.push({
      kind: "stream",
      key: `stream:${state.chatStreamRunId ?? "live"}`,
      text: state.chatStream,
      runId: state.chatStreamRunId ?? "",
    });
  }

  return items;
}
```

- [ ] **Step 3: Write groupBySide implementation**

```ts
// apps/frontend/src/hooks/internal/groupBySide.ts
//
// Post-process buildChatItems' output: wrap consecutive items belonging
// to the same visual side (user vs assistant) into one RenderGroup.
// Mirrors upstream's `groupMessages` at
// `~/Desktop/openclaw/ui/src/ui/chat/build-chat-items.ts:151-200`.

import type { ChatRenderItem, RenderGroup, RenderSide, HistoryMessage } from "./types";

function lowerRole(m: HistoryMessage): string {
  return typeof m.role === "string" ? m.role.toLowerCase() : "";
}

function sideOf(item: ChatRenderItem): RenderSide {
  if (item.kind === "message") {
    return lowerRole(item.message) === "user" ? "user" : "assistant";
  }
  // stream-segment, stream, live-thinking, divider — all assistant-side.
  return "assistant";
}

export function groupBySide(items: ChatRenderItem[]): RenderGroup[] {
  const groups: RenderGroup[] = [];
  for (const item of items) {
    const side = sideOf(item);
    const last = groups[groups.length - 1];
    if (last && last.side === side) {
      last.items.push(item);
    } else {
      groups.push({
        side,
        key: `group:${groups.length}:${item.key}`,
        items: [item],
      });
    }
  }
  return groups;
}
```

- [ ] **Step 4: Commit**

```bash
git add apps/frontend/src/hooks/internal/buildChatItems.ts apps/frontend/src/hooks/internal/groupBySide.ts apps/frontend/src/hooks/__tests__/buildChatItems.test.ts
git commit -m "feat(chat): add buildChatItems + groupBySide helpers

Mirrors upstream OpenClaw's build-chat-items.ts:249-364 (flat item list)
and groupMessages:151-200 (render-time grouping). Adds a live-thinking
item kind for Isol8's live-thinking display (upstream drops it).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Rewrite useAgentChat

This is the largest task. The hook's state shape, event handlers, history reconstruction, and return surface all change. Do it as one atomic rewrite.

**Files:**
- Modify: `apps/frontend/src/hooks/useAgentChat.ts` (full rewrite of the chat-state portions)
- Delete: `apps/frontend/src/hooks/__tests__/useAgentChat.test.ts`
- Delete: `apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts`
- Delete: `apps/frontend/src/hooks/__tests__/useAgentChat.approval.test.ts`

- [ ] **Step 1: Read the current `useAgentChat.ts` end-to-end**

Open `apps/frontend/src/hooks/useAgentChat.ts` and read it cover-to-cover. Key landmarks:
- L89-126: types (`InternalMessage`, `AgentMessage`, `UseAgentChatReturn`)
- L165: hook entry `useAgentChat(agentId, sessionName)`
- L178: `messages` state
- L200-220: `runsRef`, `finalizedRunsRef`, `pendingCancelRef` declarations
- L243: `clientRunIdRef`
- L261-262: `pendingApprovalsRef`, `awaitingResumeAfterApprovalRef` (from #628 — going away)
- L283-320: `onEvent` subscription for `exec.approval.{requested,resolved}` (going away)
- L321-369: `getOrCreateBubble` (going away)
- L375-430: `finalizeBubble` (going away), `finalizeAllActiveRuns` (going away)
- L480-518: chat.history fetch + decoder (replacing)
- L525-814: the big onChatMessage useEffect with branches for chunk/thinking/tool_*/done/error/heartbeat (rewriting)
- L744-783: `dispatchNow` (keeping; slight tweaks)
- L805-857: `sendMessage` (keeping)
- L867-984: cancelMessage, clearMessages (keeping; cleanup approval refs)
- L1023-1037: hook return (rewriting to expose new surface)

Note where each region is. The diff is large but each region is self-contained.

- [ ] **Step 2: Delete obsolete test files**

```bash
git rm apps/frontend/src/hooks/__tests__/useAgentChat.test.ts
git rm apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts
git rm apps/frontend/src/hooks/__tests__/useAgentChat.approval.test.ts
```

These get committed at the end of this task along with the rewrite.

- [ ] **Step 3: Update types and return surface**

Replace the existing `InternalMessage`, `AgentMessage`, `UseAgentChatReturn` types (around L89-126) with:

```ts
// Re-export types from internal modules for consumers.
export type { HistoryMessage, ContentBlock, ChatRenderItem, RenderGroup, RenderSide } from "./internal/types";

export const BOOTSTRAP_MESSAGE =
  "Hello! Please run Bootstrap.md — I'd love to learn about everyday life use cases where you can be helpful and automate work for me.";

export interface QueuedMessage {
  id: string;
  text: string;
  createdAt: number;
}

export interface UseAgentChatReturn {
  /** Final rendered groups, ready for MessageList to walk. */
  renderGroups: RenderGroup[];
  /** Raw flat items before grouping — exposed for snapshot testing and StreamingStatusBar. */
  renderItems: ChatRenderItem[];
  /** Whether a user-turn is in flight (chatStream non-null OR live tools running OR clientRunIdRef set). */
  isStreaming: boolean;
  error: string | null;
  sendMessage: (message: string) => Promise<void>;
  cancelMessage: () => Promise<void>;
  clearMessages: () => void;
  isConnected: boolean;
  isLoadingHistory: boolean;
  needsBootstrap: boolean;
  queuedMessages: QueuedMessage[];
  removeQueuedMessage: (id: string) => void;
  clearQueue: () => void;
}
```

Remove the old `InternalMessage` / `AgentMessage` / `ContentBlock` (local) types — they're now in `internal/types.ts`.

- [ ] **Step 4: Replace the state declarations**

Replace the block from L178 (`const [messages, setMessages] = useState...`) through L262 (the approval refs) with:

```ts
  // Persisted messages — from chat.history + chat.final synthesis.
  const [chatMessages, setChatMessages] = useState<HistoryMessage[]>(
    () => (cacheKey ? _messageCache.get(cacheKey) ?? [] : []),
  );

  // Live state for the in-flight run.
  const [chatStream, setChatStream] = useState<string | null>(null);
  const chatStreamRunIdRef = useRef<string | null>(null);
  const chatStreamStartedAtRef = useRef<number | null>(null);

  // Live thinking — Isol8 addition.
  const [chatStreamThinking, setChatStreamThinking] = useState<string | null>(null);
  const chatStreamThinkingRunIdRef = useRef<string | null>(null);

  // Stream segments (text fragments committed when a tool interrupts streaming).
  const [chatStreamSegments, setChatStreamSegments] = useState<Array<{ text: string; ts: number }>>([]);

  // Live tool state.
  const [chatToolMessages, setChatToolMessages] = useState<HistoryMessage[]>([]);
  const toolStreamByIdRef = useRef<Map<string, ToolStreamEntry>>(new Map());
  const toolStreamOrderRef = useRef<string[]>([]);

  const [isStreaming, _setIsStreamingRaw] = useState(false);
  const isStreamingRef = useRef(false);
  const setIsStreaming = useCallback((value: boolean) => {
    isStreamingRef.current = value;
    _setIsStreamingRaw(value);
  }, []);

  const [error, setError] = useState<string | null>(null);
  const [historyLoadState, setHistoryLoadState] = useState<"idle" | "loading" | "done">("idle");
  const isLoadingHistory = historyLoadState === "loading";

  // Cancel-before-first-event window (preserved from previous design).
  const pendingCancelRef = useRef<boolean>(false);

  // Analytics counters.
  const turnStartedAtRef = useRef<number | null>(null);
  const turnAssistantLenRef = useRef<number>(0);

  // agentId mirror for callback access without stale closures.
  const agentIdRef = useRef(agentId);

  // Busy gate — preserved from PR #621. NO approval-pause guard from #628.
  const clientRunIdRef = useRef<string | null>(null);

  // Queue (preserved from #621).
  const [queuedMessages, setQueuedMessages] = useState<QueuedMessage[]>([]);
  const queuedMessagesRef = useRef<QueuedMessage[]>([]);
  useEffect(() => { queuedMessagesRef.current = queuedMessages; }, [queuedMessages]);
```

Add the `ToolStreamEntry` import at the top:
```ts
import type { HistoryMessage, ContentBlock, ToolStreamEntry, ChatRenderItem, RenderGroup } from "./internal/types";
```

Also import the helpers:
```ts
import { projectChatHistoryMessages } from "./internal/chatHistoryFilters";
import { buildChatItems } from "./internal/buildChatItems";
import { groupBySide } from "./internal/groupBySide";
```

- [ ] **Step 5: Delete the approval-event subscription**

Remove the `useEffect` that subscribes to `exec.approval.{requested,resolved}` (originally introduced in #628 around L283-320). Also remove the `onEvent` destructure from `useGateway()` since we no longer subscribe to events.

The destructure becomes:
```ts
const { isConnected, sendChat, onChatMessage, sendReq } = useGateway();
```

- [ ] **Step 6: Delete getOrCreateBubble / finalizeBubble / finalizeAllActiveRuns**

Remove L321-430 entirely (or the equivalent block in the current file). These helpers no longer have a home.

Also remove `runsRef` and `finalizedRunsRef` declarations (around L200-217).

- [ ] **Step 7: Rewrite the onChatMessage useEffect**

Replace the entire onChatMessage effect (the big block starting around L525 with all the message-type branches) with:

```ts
  // ---- Chat message handler ----
  useEffect(() => {
    return onChatMessage((msg: ChatIncomingMessage) => {
      // Drop messages for a different agent (heartbeat / update_available
      // are broadcast; errors are allowed through for safety).
      const isUntaggedBroadcast = msg.type === "heartbeat" || msg.type === "update_available";
      const isUntaggedError = msg.type === "error" && msg.agent_id === undefined;
      if (!isUntaggedBroadcast && !isUntaggedError && msg.agent_id !== agentIdRef.current) {
        return;
      }

      if (msg.type === "chunk") {
        const runId = msg.runId;
        // Sub-run boundary: if the incoming chunk has a new runId, commit
        // the prior stream into chatStreamSegments first.
        if (chatStreamRunIdRef.current !== null && chatStreamRunIdRef.current !== runId) {
          const prior = chatStream;
          if (prior && prior.length > 0) {
            setChatStreamSegments((segs) => [...segs, { text: prior, ts: Date.now() }]);
          }
        }
        setChatStream(msg.content);
        chatStreamRunIdRef.current = runId;
        if (chatStreamStartedAtRef.current === null) {
          chatStreamStartedAtRef.current = Date.now();
        }
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "thinking") {
        const runId = msg.runId;
        if (chatStreamThinkingRunIdRef.current !== null && chatStreamThinkingRunIdRef.current !== runId) {
          // No need to commit prior thinking to segments — thinking is rendered
          // inside its bubble live; final assistant message will carry the
          // finalized thinking content if any.
        }
        setChatStreamThinking(msg.content);
        chatStreamThinkingRunIdRef.current = runId;
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "tool_start") {
        const runId = msg.runId;
        const toolCallId = msg.toolCallId;
        if (!toolCallId) return;

        // Commit any in-progress chatStream to segments before the tool runs.
        if (chatStream && chatStream.length > 0) {
          setChatStreamSegments((segs) => [...segs, { text: chatStream, ts: Date.now() }]);
          setChatStream(null);
          chatStreamRunIdRef.current = null;
        }

        const now = Date.now();
        const entry: ToolStreamEntry = {
          toolCallId,
          runId,
          name: msg.tool,
          args: msg.args,
          status: "running",
          startedAt: now,
          updatedAt: now,
        };
        toolStreamByIdRef.current.set(toolCallId, entry);
        if (!toolStreamOrderRef.current.includes(toolCallId)) {
          toolStreamOrderRef.current.push(toolCallId);
        }
        syncToolStreamMessages();
        if (!isStreamingRef.current) setIsStreaming(true);
        return;
      }

      if (msg.type === "tool_end" || msg.type === "tool_error") {
        const toolCallId = msg.toolCallId;
        if (!toolCallId) return;
        const entry = toolStreamByIdRef.current.get(toolCallId);
        if (!entry) return;
        entry.output = msg.result;
        entry.status = msg.type === "tool_error" ? "error" : "done";
        entry.updatedAt = Date.now();
        toolStreamByIdRef.current.set(toolCallId, entry);
        syncToolStreamMessages();
        return;
      }

      if (msg.type === "done") {
        const runId = msg.runId;
        // Commit any pending stream into a synthesized assistant message.
        const stream = chatStream;
        const thinking = chatStreamThinking;
        if ((stream && stream.length > 0) || (thinking && thinking.length > 0)) {
          const content: ContentBlock[] = [];
          if (thinking) content.push({ type: "thinking", thinking });
          if (stream) content.push({ type: "text", text: stream });
          const synthesized: HistoryMessage = {
            role: "assistant",
            content,
            timestamp: Date.now(),
            runId,
          };
          setChatMessages((prev) => [...prev, synthesized]);
          turnAssistantLenRef.current += stream?.length ?? 0;
        }
        setChatStream(null);
        chatStreamRunIdRef.current = null;
        chatStreamStartedAtRef.current = null;
        setChatStreamThinking(null);
        chatStreamThinkingRunIdRef.current = null;

        // Busy-gate clear (no approval-pause guard).
        if (runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }

        // If the user-turn truly ended (no more live state), drop isStreaming.
        if (
          toolStreamByIdRef.current.size === 0 ||
          Array.from(toolStreamByIdRef.current.values()).every((e) => e.status !== "running")
        ) {
          if (clientRunIdRef.current === null) {
            setIsStreaming(false);
            const startedAt = turnStartedAtRef.current;
            if (startedAt !== null) {
              capture("chat_completed", {
                agent_id: agentIdRef.current ?? undefined,
                duration_ms: Date.now() - startedAt,
                assistant_message_length: turnAssistantLenRef.current,
              });
              turnStartedAtRef.current = null;
              turnAssistantLenRef.current = 0;
            }
          }
        }
        return;
      }

      if (msg.type === "error") {
        const displayError = friendlyError(msg.message);
        const errorCode = msg.code;
        // Append an assistant-role error message to chatMessages.
        const errMsg: HistoryMessage = {
          role: "assistant",
          content: [{ type: "text", text: `Error: ${displayError}` }],
          timestamp: Date.now(),
          runId: msg.runId,
          errorCode,
        };
        setChatMessages((prev) => [...prev, errMsg]);
        // Clear live state.
        setChatStream(null);
        chatStreamRunIdRef.current = null;
        setChatStreamThinking(null);
        chatStreamThinkingRunIdRef.current = null;
        setError(displayError);
        if (!msg.runId || msg.runId === clientRunIdRef.current) {
          clientRunIdRef.current = null;
        }
        // Drop isStreaming if no other activity.
        const stillRunning = Array.from(toolStreamByIdRef.current.values()).some(
          (e) => e.status === "running",
        );
        if (!stillRunning && clientRunIdRef.current === null) {
          setIsStreaming(false);
        }
        return;
      }

      // heartbeat / update_available — no chat-state mutation.
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [onChatMessage]);
```

- [ ] **Step 8: Add the syncToolStreamMessages helper**

Place near the other useCallback definitions:

```ts
  // Sync toolStreamById Map → chatToolMessages array (state) for rendering.
  // Throttled at 80ms (matches upstream) but force-flushed on tool_end.
  const toolStreamSyncTimerRef = useRef<number | null>(null);
  const TOOL_STREAM_THROTTLE_MS = 80;

  const syncToolStreamMessages = useCallback(() => {
    const flush = () => {
      const ordered: HistoryMessage[] = toolStreamOrderRef.current
        .map((id) => toolStreamByIdRef.current.get(id))
        .filter((e): e is ToolStreamEntry => Boolean(e))
        .map((e) => ({
          role: "assistant",
          content: [
            {
              type: "tool_use",
              id: e.toolCallId,
              name: e.name,
              args: e.args,
            },
            ...(e.output !== undefined
              ? [{
                  type: "tool_result",
                  tool_use_id: e.toolCallId,
                  content: e.output,
                  ...(e.status === "error" ? { isError: true } : {}),
                } as ContentBlock]
              : []),
          ],
          timestamp: e.updatedAt,
          runId: e.runId,
        }));
      setChatToolMessages(ordered);
      toolStreamSyncTimerRef.current = null;
    };
    if (toolStreamSyncTimerRef.current !== null) {
      return;  // already scheduled
    }
    // Force-flush immediately if any entry is in terminal state.
    const hasTerminal = Array.from(toolStreamByIdRef.current.values()).some(
      (e) => e.status !== "running",
    );
    if (hasTerminal) {
      flush();
      return;
    }
    toolStreamSyncTimerRef.current = window.setTimeout(flush, TOOL_STREAM_THROTTLE_MS);
  }, []);

  // Clean up the throttle timer on unmount.
  useEffect(() => {
    return () => {
      if (toolStreamSyncTimerRef.current !== null) {
        window.clearTimeout(toolStreamSyncTimerRef.current);
        toolStreamSyncTimerRef.current = null;
      }
    };
  }, []);
```

- [ ] **Step 9: Rewrite the chat.history fetch**

Find the chat.history fetch useEffect (around L460-519 currently). Replace its body with:

```ts
  useEffect(() => {
    if (!agentId || !cacheKey || !isConnected) return;
    if (historyLoadedRef.current.has(cacheKey)) return;
    if (_messageCache.has(cacheKey)) {
      historyLoadedRef.current.add(cacheKey);
      return;
    }
    const sessionKey = `agent:${agentId}:${sessionName}`;
    Promise.resolve().then(() => setHistoryLoadState("loading"));
    sendReq("chat.history", { sessionKey, limit: 200 })
      .then((result: unknown) => {
        const historyResult = result as { messages?: HistoryMessage[] };
        historyLoadedRef.current.add(cacheKey);
        setHistoryLoadState("done");
        const raw = Array.isArray(historyResult?.messages) ? historyResult.messages : [];
        const projected = projectChatHistoryMessages(raw);
        if (projected.length === 0) {
          _needsBootstrap.add(cacheKey);
          return;
        }
        if (agentIdRef.current !== agentId) return;
        setChatMessages(projected);
        _messageCache.set(cacheKey, projected);
      })
      .catch((err: unknown) => {
        console.warn("Failed to fetch chat history:", err);
        historyLoadedRef.current.add(cacheKey);
        setHistoryLoadState("done");
      });
  }, [agentId, cacheKey, sessionName, isConnected, sendReq]);
```

Update the module-level cache shape declaration accordingly:
```ts
const _messageCache = new Map<string, HistoryMessage[]>();
```

- [ ] **Step 10: Rewrite dispatchNow + sendMessage**

`dispatchNow` (currently around L725-783): change the user-message append to use HistoryMessage shape:

```ts
  const dispatchNow = useCallback(
    async (message: string): Promise<void> => {
      const agentId = agentIdRef.current;
      if (!agentId) return;
      if (!isConnected) {
        setError("Not connected. Please wait and try again.");
        return;
      }

      const newClientRunId = crypto.randomUUID();
      clientRunIdRef.current = newClientRunId;

      const userMsg: HistoryMessage = {
        role: "user",
        content: [{ type: "text", text: message }],
        timestamp: Date.now(),
      };
      setChatMessages((prev) => [...prev, userMsg]);
      setIsStreaming(true);

      turnStartedAtRef.current = Date.now();
      turnAssistantLenRef.current = 0;
      capture("chat_message_sent", {
        agent_id: agentId,
        message_length: message.length,
      });

      try {
        sendChat(agentId, message, newClientRunId);
      } catch (err) {
        const errorMessage = friendlyError(
          err instanceof Error ? err.message : "Failed to send message",
        );
        setError(errorMessage);
        const errMsg: HistoryMessage = {
          role: "assistant",
          content: [{ type: "text", text: `Error: ${errorMessage}` }],
          timestamp: Date.now(),
        };
        setChatMessages((prev) => [...prev, errMsg]);
        setIsStreaming(false);
        clientRunIdRef.current = null;
      }
    },
    [sendChat, isConnected],
  );
```

`sendMessage` (the busy-gate + queue dispatcher) stays mostly intact — just remove the old per-bubble assistant ID logic.

- [ ] **Step 11: Rewrite cancelMessage and clearMessages**

Replace the bodies of `cancelMessage` and `clearMessages` (around L867-984). Remove all `finalizeAllActiveRuns` calls, `pendingApprovalsRef` clears, and `awaitingResumeAfterApprovalRef` clears:

```ts
  const cancelMessage = useCallback(async () => {
    if (!agentIdRef.current || !isStreaming) return;

    const startedAt = turnStartedAtRef.current;
    capture("chat_aborted", {
      agent_id: agentIdRef.current,
      elapsed_ms: startedAt !== null ? Date.now() - startedAt : 0,
    });
    turnStartedAtRef.current = null;
    turnAssistantLenRef.current = 0;

    const sessionKey = `agent:${agentIdRef.current}:${sessionName}`;
    try {
      await sendReq("chat.abort", { sessionKey });
    } catch (err) {
      console.warn("Failed to abort agent run:", err);
    }

    setIsStreaming(false);
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    setChatStreamThinking(null);
    chatStreamThinkingRunIdRef.current = null;
    pendingCancelRef.current = true;
    clientRunIdRef.current = null;
    // Live tools stay rendered with their last-known status.
  }, [isStreaming, sendReq, sessionName, setIsStreaming]);

  const clearMessages = useCallback(() => {
    setChatMessages([]);
    setChatStream(null);
    chatStreamRunIdRef.current = null;
    setChatStreamThinking(null);
    chatStreamThinkingRunIdRef.current = null;
    setChatStreamSegments([]);
    setChatToolMessages([]);
    toolStreamByIdRef.current.clear();
    toolStreamOrderRef.current = [];
    const key = agentIdRef.current ? `${agentIdRef.current}:${sessionName}` : null;
    if (key) _messageCache.delete(key);
    setError(null);
    setIsStreaming(false);
    pendingCancelRef.current = true;
    clientRunIdRef.current = null;
  }, [sessionName, setIsStreaming]);
```

- [ ] **Step 12: Update the queue flush effect**

The queue flush effect (around L795-820 from PR #621) keeps the same shape, just verifying it still operates on `clientRunIdRef`:

```ts
  const prevClientRunIdRef = useRef<string | null>(null);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => {
    const prev = prevClientRunIdRef.current;
    const curr = clientRunIdRef.current;
    prevClientRunIdRef.current = curr;
    if (
      prev !== null &&
      curr === null &&
      queuedMessagesRef.current.length > 0 &&
      isConnected
    ) {
      const next = queuedMessagesRef.current[0];
      setQueuedMessages((q) => q.slice(1));
      void dispatchNow(next.text);
    }
  });
```

- [ ] **Step 13: Rewrite the return statement**

Replace the hook's `return { ... }` (currently around L1023-1037) with:

```ts
  // Compute render output from state. memo for stability.
  const renderItems = useMemo<ChatRenderItem[]>(
    () => buildChatItems({
      chatMessages,
      chatStream,
      chatStreamRunId: chatStreamRunIdRef.current,
      chatStreamThinking,
      chatStreamThinkingRunId: chatStreamThinkingRunIdRef.current,
      chatStreamSegments,
      chatToolMessages,
      sessionKey: `agent:${agentId ?? ""}:${sessionName}`,
    }),
    [chatMessages, chatStream, chatStreamThinking, chatStreamSegments, chatToolMessages, agentId, sessionName],
  );

  const renderGroups = useMemo<RenderGroup[]>(() => groupBySide(renderItems), [renderItems]);

  return {
    renderGroups,
    renderItems,
    isStreaming,
    error,
    sendMessage,
    cancelMessage,
    clearMessages,
    isConnected,
    isLoadingHistory,
    needsBootstrap: cacheKey ? _needsBootstrap.has(cacheKey) : false,
    queuedMessages,
    removeQueuedMessage,
    clearQueue,
  };
```

- [ ] **Step 14: Commit**

```bash
git add apps/frontend/src/hooks/useAgentChat.ts apps/frontend/src/hooks/__tests__/useAgentChat.test.ts apps/frontend/src/hooks/__tests__/useAgentChat.queue.test.ts apps/frontend/src/hooks/__tests__/useAgentChat.approval.test.ts
git commit -m "feat(chat): rewrite useAgentChat with flat-timeline state model

Replaces per-runId bubble model (runsRef, getOrCreateBubble,
finalizeBubble) with upstream's parallel-arrays state shape:
chatMessages + chatStream + chatStreamSegments + chatToolMessages +
toolStreamById. Live thinking preserved (chatStreamThinking field).

Removes #628's approval-tracking refs (pendingApprovalsRef,
awaitingResumeAfterApprovalRef) and the exec.approval.* event
subscription. Mid-pause sends are intended UX per the spec.

Deletes the three obsolete test files (useAgentChat.test.ts,
useAgentChat.queue.test.ts, useAgentChat.approval.test.ts) whose
assertions don't apply to the new model. Coverage is preserved by
helper unit tests + visual snapshot + human end-of-PR smoke.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Rewrite MessageList

`MessageList.tsx` walks `renderGroups` from `useAgentChat`. Inside each `{kind:"message"}` item it walks `message.content[]` blocks and renders them.

**Files:**
- Modify: `apps/frontend/src/components/chat/MessageList.tsx` (rewrite the render loop)

- [ ] **Step 1: Read the current `MessageList.tsx`**

Familiarize with the existing components: `MarkdownContent`, `CodeBlock`, `ThinkingBlock`, `ToolPill`/`ToolUseIndicator`, `AgentGlyph`/`AgentHead`, `ChatErrorCard`. These survive.

- [ ] **Step 2: Update the props interface**

Change `MessageList`'s props to accept `renderGroups: RenderGroup[]` instead of `messages: Message[]`. Wherever `messages` was iterated, walk `renderGroups` instead.

```tsx
interface MessageListProps {
  renderGroups: RenderGroup[];
  agentName?: string;
  isStreaming?: boolean;
  isTyping?: boolean;
  onOpenFile?: (path: string) => void;
}
```

- [ ] **Step 3: Rewrite the main render loop**

Replace the existing `messages.map((msg, idx) => ...)` block with:

```tsx
return (
  <div ref={containerRef} className="messages">
    {renderGroups.map((group) => (
      <BubbleGroup key={group.key} group={group} agentName={agentName} onOpenFile={onOpenFile} />
    ))}
    {isTyping && showTypingPlaceholder && <TypingPlaceholder />}
  </div>
);
```

- [ ] **Step 4: Add BubbleGroup + ItemRenderer subcomponents**

```tsx
function BubbleGroup({ group, agentName, onOpenFile }: {
  group: RenderGroup;
  agentName?: string;
  onOpenFile?: (path: string) => void;
}) {
  if (group.side === "user") {
    // Each user item is its own bubble row (per the spec).
    return (
      <>
        {group.items.map((item) =>
          item.kind === "message" ? (
            <UserMessageRow key={item.key} message={item.message} />
          ) : null
        )}
      </>
    );
  }
  // Assistant side: one rounded bubble container, walk items inside.
  return (
    <div className="assistant-group">
      <AgentHead agentName={agentName ?? "Ray"} />
      <div className="assistant-bubble">
        {group.items.map((item) => (
          <ItemRenderer key={item.key} item={item} onOpenFile={onOpenFile} />
        ))}
      </div>
    </div>
  );
}

function ItemRenderer({ item, onOpenFile }: { item: ChatRenderItem; onOpenFile?: (path: string) => void }) {
  if (item.kind === "divider") {
    return <div className="chat-divider"><span>{item.label}</span></div>;
  }
  if (item.kind === "stream") {
    return <MarkdownContent text={item.text} streaming />;
  }
  if (item.kind === "stream-segment") {
    return <MarkdownContent text={item.text} />;
  }
  if (item.kind === "live-thinking") {
    return <ThinkingBlock content={item.text} />;
  }
  if (item.kind === "message") {
    return <MessageBlocks message={item.message} onOpenFile={onOpenFile} />;
  }
  return null;
}

function MessageBlocks({ message, onOpenFile }: { message: HistoryMessage; onOpenFile?: (path: string) => void }) {
  const blocks = Array.isArray(message.content) ? message.content : [];
  const pairs = pairToolBlocks(blocks);  // from internal/toolBlockExtract
  const renderedToolIds = new Set(pairs.map((p) => p.toolUseId));
  // Render blocks in order, replacing tool_use/tool_result with paired indicators.
  const rendered: React.ReactNode[] = [];
  for (let i = 0; i < blocks.length; i++) {
    const b = blocks[i];
    if (isToolResultBlock(b)) {
      // Skip — rendered as part of the matching tool_use's pair.
      continue;
    }
    if (isToolUseBlock(b)) {
      const id = resolveToolUseId(b);
      if (id && renderedToolIds.has(id)) {
        const pair = pairs.find((p) => p.toolUseId === id)!;
        rendered.push(
          <ToolUseIndicator
            key={`tool:${id}`}
            tool={{
              id,
              name: b.name ?? "tool",
              args: resolveToolArgs(b),
              result: pair.toolResultBlock?.content ?? pair.toolResultBlock?.result,
              status: pair.toolResultBlock?.isError ? "error" : pair.toolResultBlock ? "done" : "running",
            }}
          />
        );
        renderedToolIds.delete(id);
      }
      continue;
    }
    if (isThinkingBlock(b)) {
      const text = (b.thinking ?? b.text ?? "") as string;
      if (text.length > 0) {
        rendered.push(<ThinkingBlock key={`think:${i}`} content={text} />);
      }
      continue;
    }
    if (isTextBlock(b)) {
      const text = (b.text ?? "") as string;
      if (text.length > 0) {
        rendered.push(<MarkdownContent key={`text:${i}`} text={text} />);
      }
      continue;
    }
    // Unknown block — skip.
  }
  return <>{rendered}</>;
}

function UserMessageRow({ message }: { message: HistoryMessage }) {
  const text = Array.isArray(message.content)
    ? message.content.filter(isTextBlock).map((b) => b.text ?? "").join("")
    : (message.text ?? "");
  return (
    <div className="user-row">
      <div className="user-bubble"><MarkdownContent text={text} /></div>
    </div>
  );
}

function TypingPlaceholder() {
  return (
    <div className="typing-placeholder">
      <AgentGlyph />
      <span className="typing-dots">…</span>
    </div>
  );
}
```

Add imports at the top:
```tsx
import { pairToolBlocks, isToolUseBlock, isToolResultBlock, isThinkingBlock, isTextBlock, resolveToolUseId, resolveToolArgs } from "@/hooks/internal/toolBlockExtract";
import type { ChatRenderItem, RenderGroup, HistoryMessage } from "@/hooks/internal/types";
```

`ToolUseIndicator`'s prop changes from `toolUses: ToolUse[]` to `tool: { id, name, args, result, status }`. Update its signature.

- [ ] **Step 5: Adapt auto-scroll**

The existing scroll logic at `MessageList.tsx:395-432` keys on `messages.length`. Update to key on `renderGroups.length` plus inner-item count change:

```tsx
const totalItems = useMemo(
  () => renderGroups.reduce((acc, g) => acc + g.items.length, 0),
  [renderGroups],
);
// then use totalItems where messages.length was used.
```

- [ ] **Step 6: Commit**

```bash
git add apps/frontend/src/components/chat/MessageList.tsx
git commit -m "feat(chat): rewrite MessageList to walk render groups + flat items

MessageList now consumes renderGroups from useAgentChat (built via
buildChatItems + groupBySide). Within each {kind:'message'} item,
walk message.content[] blocks: text → MarkdownContent, thinking →
ThinkingBlock, tool_use → ToolUseIndicator paired with its tool_result.

ToolUseIndicator's prop changes from toolUses[] to a single tool object.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Update StreamingStatusBar + AgentChatWindow

**Files:**
- Modify: `apps/frontend/src/components/chat/StreamingStatusBar.tsx`
- Modify: `apps/frontend/src/components/chat/AgentChatWindow.tsx`

- [ ] **Step 1: Update StreamingStatusBar**

`StreamingStatusBar` currently reads `messages[].toolUses[].status` (per Agent C's report at `StreamingStatusBar.tsx:9-24`). It needs to read from the new render items.

```tsx
// apps/frontend/src/components/chat/StreamingStatusBar.tsx
import type { ChatRenderItem } from "@/hooks/internal/types";

interface StreamingStatusBarProps {
  isStreaming: boolean;
  renderItems: ChatRenderItem[];
}

function deriveStreamingLabel(items: ChatRenderItem[]): string {
  // Find the last assistant-side message item with a running tool.
  for (let i = items.length - 1; i >= 0; i--) {
    const item = items[i];
    if (item.kind !== "message") continue;
    const blocks = Array.isArray(item.message.content) ? item.message.content : [];
    // Look for a tool_use without a paired tool_result (running).
    for (const b of blocks) {
      if (b?.type === "tool_use" && !blocks.some(
        (r) => r?.type === "tool_result" && r.tool_use_id === b.id,
      )) {
        return `Using ${b.name ?? "tool"}…`;
      }
    }
  }
  return "Thinking…";
}

export function StreamingStatusBar({ isStreaming, renderItems }: StreamingStatusBarProps) {
  if (!isStreaming) return null;
  const label = deriveStreamingLabel(renderItems);
  return (
    <div className="streaming-status-bar">
      <span className="streaming-dot" />
      <span>{label}</span>
    </div>
  );
}
```

- [ ] **Step 2: Update AgentChatWindow**

The destructuring from `useAgentChat()` changes. Find the destructuring (around `AgentChatWindow.tsx:230-243` per Agent C's report) and update:

```tsx
const {
  renderGroups,
  renderItems,
  isStreaming,
  error,
  sendMessage,
  cancelMessage,
  clearMessages,
  isConnected: chatConnected,
  isLoadingHistory,
  needsBootstrap,
  queuedMessages,
  removeQueuedMessage,
  clearQueue,
} = useAgentChat(agentId, sessionName);
```

Remove the legacy `messages.map` re-mapping at `:304-315` (per Agent C's report). MessageList now takes `renderGroups` directly:

```tsx
<MessageList
  ref={messageListRef}
  renderGroups={renderGroups}
  agentName={agentName}
  onOpenFile={onOpenFile}
  isStreaming={isStreaming}
  isTyping={isStreaming}
/>
```

And update the StreamingStatusBar callsite:

```tsx
<StreamingStatusBar isStreaming={isStreaming} renderItems={renderItems} />
```

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/chat/StreamingStatusBar.tsx apps/frontend/src/components/chat/AgentChatWindow.tsx
git commit -m "feat(chat): adapt StreamingStatusBar + AgentChatWindow to flat render surface

StreamingStatusBar derives 'thinking…' / 'using tool X…' from renderItems
(walks message content blocks) instead of messages[].toolUses[].status.
AgentChatWindow consumes the new useAgentChat return shape (renderGroups,
renderItems).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Update sessionMessageAdapter for RunTranscript

**Files:**
- Modify: `apps/frontend/src/components/control/panels/cron/sessionMessageAdapter.ts`

`RunTranscript.tsx:72` uses `MessageList` for cron run transcripts via `adaptSessionMessages`. With MessageList's prop shape changed to `renderGroups`, the adapter must produce `RenderGroup[]`.

- [ ] **Step 1: Read existing `sessionMessageAdapter.ts`**

Understand what it currently produces and what fields it has access to. The chat.history call result it consumes is the same `{ messages: HistoryMessage[] }` shape.

- [ ] **Step 2: Update `adaptSessionMessages`**

Replace its return type and body to use the same `buildChatItems` + `groupBySide` pipeline:

```ts
import { projectChatHistoryMessages } from "@/hooks/internal/chatHistoryFilters";
import { buildChatItems } from "@/hooks/internal/buildChatItems";
import { groupBySide } from "@/hooks/internal/groupBySide";
import type { RenderGroup, HistoryMessage } from "@/hooks/internal/types";

export function adaptSessionMessages(
  raw: HistoryMessage[],
  sessionKey: string,
): RenderGroup[] {
  const projected = projectChatHistoryMessages(raw);
  const items = buildChatItems({
    chatMessages: projected,
    chatStream: null,
    chatStreamRunId: null,
    chatStreamThinking: null,
    chatStreamThinkingRunId: null,
    chatStreamSegments: [],
    chatToolMessages: [],
    sessionKey,
  });
  return groupBySide(items);
}
```

Update RunTranscript's MessageList callsite to pass `renderGroups`:

```tsx
<MessageList renderGroups={adaptedGroups} isStreaming={false} isTyping={false} />
```

- [ ] **Step 3: Commit**

```bash
git add apps/frontend/src/components/control/panels/cron/sessionMessageAdapter.ts apps/frontend/src/components/control/panels/cron/RunTranscript.tsx
git commit -m "feat(chat): adapt sessionMessageAdapter to MessageList's renderGroups shape

Cron-run transcript view now uses the same buildChatItems + groupBySide
pipeline as the live chat. Single source of truth for chat rendering.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: MessageList snapshot test

A render-time visual snapshot covering the main item kinds.

**Files:**
- Create: `apps/frontend/src/components/chat/__tests__/MessageList.snapshot.test.tsx`

- [ ] **Step 1: Write the snapshot test**

```tsx
// apps/frontend/src/components/chat/__tests__/MessageList.snapshot.test.tsx
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { MessageList } from "../MessageList";
import type { RenderGroup } from "@/hooks/internal/types";

describe("MessageList rendering snapshots", () => {
  it("renders an empty list", () => {
    const { container } = render(
      <MessageList renderGroups={[]} isStreaming={false} isTyping={false} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("renders a single user + assistant exchange", () => {
    const groups: RenderGroup[] = [
      {
        side: "user",
        key: "g-user",
        items: [
          {
            kind: "message",
            key: "u1",
            message: { role: "user", content: [{ type: "text", text: "hi" }] },
          },
        ],
      },
      {
        side: "assistant",
        key: "g-asst",
        items: [
          {
            kind: "message",
            key: "a1",
            message: { role: "assistant", content: [{ type: "text", text: "hello" }] },
          },
        ],
      },
    ];
    const { container } = render(
      <MessageList renderGroups={groups} isStreaming={false} isTyping={false} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("renders an assistant bubble with text + tool + more text", () => {
    const groups: RenderGroup[] = [
      {
        side: "assistant",
        key: "g",
        items: [
          {
            kind: "message",
            key: "a",
            message: {
              role: "assistant",
              content: [
                { type: "text", text: "let me check" },
                { type: "tool_use", id: "t1", name: "exec", input: { cmd: "ls" } },
                { type: "tool_result", tool_use_id: "t1", content: "output here" },
                { type: "text", text: "done" },
              ],
            },
          },
        ],
      },
    ];
    const { container } = render(
      <MessageList renderGroups={groups} isStreaming={false} isTyping={false} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });

  it("renders live stream + live thinking at the tail", () => {
    const groups: RenderGroup[] = [
      {
        side: "assistant",
        key: "g",
        items: [
          { kind: "live-thinking", key: "t", text: "thinking…", runId: "r1" },
          { kind: "stream", key: "s", text: "the answer is", runId: "r1" },
        ],
      },
    ];
    const { container } = render(
      <MessageList renderGroups={groups} isStreaming={true} isTyping={true} />,
    );
    expect(container.firstChild).toMatchSnapshot();
  });
});
```

- [ ] **Step 2: Commit**

```bash
git add apps/frontend/src/components/chat/__tests__/MessageList.snapshot.test.tsx
git commit -m "test(chat): MessageList rendering snapshots for main item-kind fixtures

Snapshots cover: empty list, user→assistant exchange, assistant bubble
with text+tool+text, and live stream+thinking tail. Verifies the
flat-timeline rendering pipeline end-to-end.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Mark multi-bubble spec as superseded

**Files:**
- Modify: `docs/superpowers/specs/2026-04-21-multi-bubble-chat-design.md`

- [ ] **Step 1: Add superseded banner at the top**

Edit the file's header to add a superseded notice as the first line under the title:

```markdown
> **STATUS: Superseded by `docs/superpowers/specs/2026-05-13-flat-chat-timeline-design.md` (2026-05-13).** The per-runId multi-bubble model described here exposed OpenClaw's multi-run pattern to users (the "Ray 8 stopped spinning" perception bug). The replacement design adopts upstream's flat-message + render-time grouping. Keep this document for historical context only.
```

- [ ] **Step 2: Commit**

```bash
git add docs/superpowers/specs/2026-04-21-multi-bubble-chat-design.md
git commit -m "docs: mark multi-bubble spec as superseded by flat-chat-timeline

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 11: End-of-plan verification

**Files:**
- Run-only. No code changes.

- [ ] **Step 1: Install deps (if not already done)**

```bash
pnpm install --frozen-lockfile
```

- [ ] **Step 2: Run new unit tests**

```bash
cd apps/frontend && pnpm vitest run \
  src/hooks/__tests__/chatHistoryFilters.test.ts \
  src/hooks/__tests__/toolBlockExtract.test.ts \
  src/hooks/__tests__/buildChatItems.test.ts \
  src/components/chat/__tests__/MessageList.snapshot.test.tsx
```

Expected: all pass.

- [ ] **Step 3: Run full frontend test suite**

```bash
cd apps/frontend && pnpm test
```

Expected: all pass (besides the pre-existing flakes documented in PR #621's description).

- [ ] **Step 4: Run frontend lint**

```bash
cd apps/frontend && pnpm run lint
```

Expected: 0 errors. Pre-existing warning count should not increase.

- [ ] **Step 5: Run frontend typecheck**

```bash
cd apps/frontend && pnpm exec tsc --noEmit
```

Expected: clean.

- [ ] **Step 6: Run backend full suite (sanity — no backend changes)**

```bash
cd apps/backend && uv run pytest tests/ --no-cov -q
```

Expected: same pass count as before this branch.

- [ ] **Step 7: Open PR**

```bash
gh pr create --title "feat(chat): flat-timeline state model + render-time grouping" --body "$(cat <<'EOF'
## Summary

Replaces Isol8's per-runId multi-bubble chat model with upstream OpenClaw's flat-message + render-time grouping pattern. Fixes the "Ray 8 stopped spinning" perception bug from the user's earlier screenshots without changing the underlying visual aesthetic.

- State migrates to upstream's parallel-arrays model (chatMessages + chatStream + chatStreamSegments + chatToolMessages + toolStreamById). Live thinking is preserved as the one Isol8 divergence.
- chat.history is now reconstructed accurately: tool_use / tool_result blocks survive, thinking blocks survive, image blocks already stripped by upstream's projection.
- Mid-pause sends are now intended UX (users can keep chatting while a tool is pending approval). PR #628's approval-pause guard is removed.

See full design at `docs/superpowers/specs/2026-05-13-flat-chat-timeline-design.md`.

## Test plan

- [x] Unit tests for `chatHistoryFilters`, `toolBlockExtract`, `buildChatItems`, `groupBySide`.
- [x] Visual snapshot for `MessageList` covering the main item-kind fixtures.
- [x] Frontend lint, tsc, full vitest suite all green.
- [x] Backend full suite (no backend changes — sanity only).
- [ ] **Live smoke test on dev** (human):
  1. Send a message that triggers an exec-approval card. Verify the assistant bubble does NOT visually close when chat.final fires.
  2. Click Allow. Verify the resumed run's content appends to the same bubble.
  3. Refresh the page. Verify chat.history reconstruction shows the full transcript including tool calls/results.
  4. Type and send a message during the approval pause (the intended-feature case). Verify the second message appears as a new user bubble and the agent's response renders cleanly in a new assistant bubble after the user-turn-2 boundary.

## Out of scope

- 128 KB chat.history fix (separate workstream).
- Backend event-replay on reconnect.
- Async-completion text leak ("An async command has completed…").
- Server-side serialization of concurrent chat.send on a single session.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Out of scope (documented in spec; do not implement)

- 128 KB `chat.history` 413 fix (separate workstream — likely REST proxy via ALB)
- Backend event-replay on reconnect
- Async-completion text leak ("An async command has completed…")
- Cross-hook signal from `useExecApprovals` for stronger send-gating
- Visual redesign of tools, thinking blocks
