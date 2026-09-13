# iOS Shared Sessions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show shared Claude Code / Codex sessions in the iOS app — a Devices & sessions list per room, a terminal-style live transcript, and a prompt row that sends follow-ups — by moving the transcript model and data hooks into `@lightbulb/shared` and building native screens on top.

**Architecture:** The pure transcript model (`buildRows`, `toolRowView`, glyphs, labels) and the hooks (`useRoomResources`, `useLocalSessionHistory`, a new `useSessionCommand`) move from `apps/frontend` into `packages/shared`; the web imports them from there. iOS adds two pushed routes under the room stack (`room/[roomId]/devices`, `room/[roomId]/session/[sessionId]`) and a header action with a live-count badge on the conversation screen. All HTTP goes through the shared messaging client the mobile `LiveEventsProvider` already mounts; no backend change.

**Tech Stack:** TypeScript, React 19, `@lightbulb/shared` (vitest), Next.js 16 web (vitest + RTL), Expo SDK 57 / RN 0.86 (jest-expo + `@testing-library/react-native`, expo-router, `@expo/vector-icons` Feather, `react-native-markdown-display`).

**Spec:** `docs/superpowers/specs/2026-09-13-ios-shared-sessions-design.md`

## Global Constraints

- `packages/shared` has no `next/*`, no `react-native`, no DOM globals, no `"use client"` pragmas (its `package.json` description is the rule).
- Mobile colours come only from `apps/mobile/src/config/theme.ts` tokens (`colors.*`); fonts only from `fonts.*`; no colour literals in `apps/mobile/src`.
- User-facing copy that exists on web is reused verbatim (the strings are in the tasks). New mobile-only copy is what the tasks say.
- Route files are `devices.tsx` and `session/[sessionId].tsx`; never name a mobile screen "Sessions" (`(app)/control/sessions.tsx` is the OpenClaw gateway screen).
- Every `useRoomResources` call on the phone passes `{ enabled: <this screen is focused> }`; web callers pass nothing.
- Tests: `cd packages/shared && pnpm test`, `cd apps/frontend && pnpm test -- <file>` (vitest), `cd apps/mobile && pnpm test -- <file>` (jest). Typecheck: `pnpm typecheck` in each package. Lint: `pnpm lint` in each package. Never use `sleep` or `tail`.
- One commit per task, conventional-commit subject, ending with:
  ```
  Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01BxwgqVFXCLb1LJiozVpQRx
  ```
- Work on branch `feat/ios-shared-sessions` in this worktree. Do not push.

---

## File structure

| File | Responsibility |
|---|---|
| `packages/shared/src/messaging/localSessionTranscript.ts` (new) | Pure transcript model: row types, glyphs, `buildRows`, `toolHeader`, `fullOutput`, `diffLines`, `stripAnsi`, `toolRowView`, `lifecycleLabel`. |
| `packages/shared/src/messaging/localSessionTranscript.test.ts` (new) | Model tests (buildRows moved from web; tool-row view and label cases). |
| `packages/shared/src/messaging/useRoomResources.ts` (moved from web) | `useRoomResources` (+ `enabled`), `useLocalSessionHistory`, `accessLost`, `RoomResourceSource`. |
| `packages/shared/src/messaging/useRoomResources.test.ts` (new) | The `enabled` gate. |
| `packages/shared/src/messaging/localSessionLabels.ts` (new) | `sessionLabel`, `sessionStatus`, `unavailableReason`, `canSendToSession`. |
| `packages/shared/src/messaging/useSessionCommand.ts` (new) | Send / receipt state machine lifted from web `LocalSessionPanel`; `receiptCopy`. |
| `packages/shared/src/messaging/useSessionCommand.test.ts` (new) | Hook tests. |
| `packages/shared/src/index.ts` | Barrel exports. |
| `apps/frontend/src/components/messaging/NativeSessionTranscript.tsx` | Web renderer only; imports the model from shared. |
| `apps/frontend/src/components/messaging/LocalSessionPanel.tsx` | Uses `useSessionCommand`; re-exports labels. |
| `apps/frontend/src/hooks/useRoomResources.ts` | Deleted; importers repointed. |
| `apps/mobile/src/lib/localSessions.ts` (new) | `liveSessionCount`, `memberName`, `deviceSections`. |
| `apps/mobile/src/components/room/RoomHeaderActions.tsx` | Terminal button + badge. |
| `apps/mobile/src/app/(app)/room/[roomId]/index.tsx` | Wires the header action. |
| `apps/mobile/src/components/session/SessionStatusDot.tsx` (new) | Status dot. |
| `apps/mobile/src/app/(app)/room/[roomId]/devices.tsx` (new) | Devices & sessions screen. |
| `apps/mobile/src/components/session/TranscriptRow.tsx` (new) | `TranscriptRowView`, `WorkingRow`. |
| `apps/mobile/src/components/session/SessionPromptRow.tsx` (new) | Prompt input + receipt states. |
| `apps/mobile/src/app/(app)/room/[roomId]/session/[sessionId].tsx` (new) | Session transcript screen. |

---

### Task 1: Shared transcript model

**Files:**
- Create: `packages/shared/src/messaging/localSessionTranscript.ts`
- Create: `packages/shared/src/messaging/localSessionTranscript.test.ts`
- Modify: `packages/shared/src/index.ts`
- Modify: `apps/frontend/src/components/messaging/NativeSessionTranscript.tsx`
- Modify: `apps/frontend/src/components/messaging/__tests__/NativeSessionTranscript.test.tsx`

**Interfaces:**
- Consumes: `LocalSessionEvent` from `packages/shared/src/messaging/types.ts`.
- Produces (all exported from `@lightbulb/shared`): `type LocalSessionProvider = "claude" | "codex"`, `type TranscriptRow`, `type ToolTranscriptRow`, `interface ToolRowView`, `GLYPH`, `buildRows(events: readonly LocalSessionEvent[]): TranscriptRow[]`, `stripAnsi(text: string): string`, `toolHeader(provider, call): string`, `fullOutput(result: unknown): string`, `diffLines(result: unknown): string[]`, `toolRowView(row: ToolTranscriptRow): ToolRowView`, `lifecycleLabel(row: Extract<TranscriptRow, { kind: "lifecycle" }>): string`.

- [ ] **Step 1: Write the failing model test**

Create `packages/shared/src/messaging/localSessionTranscript.test.ts`:

```ts
import { describe, expect, it } from "vitest";

import { buildRows, lifecycleLabel, toolHeader, toolRowView, type TranscriptRow } from "./localSessionTranscript";
import type { LocalSessionEvent } from "./types";

const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });
const tool = (events: LocalSessionEvent[]) => buildRows(events).find((r): r is Extract<TranscriptRow, { kind: "tool" }> => r.kind === "tool")!;

describe("buildRows", () => {
  it("merges split prose by itemId, pairs tool results and marks the final Codex answer", () => {
    const rows = buildRows([
      ev(1, { kind: "text", role: "user", text: "run it", actor: { userId: "user_a" } }),
      ev(2, { kind: "tool", role: "assistant", text: "git status", itemId: "toolu_1", tool: { name: "Bash", status: "running", input: { command: "git status" } } }),
      ev(3, { kind: "tool", role: "tool", text: "On branch main", parentId: "toolu_1", tool: { name: "result", status: "done", result: { stdout: "On branch main\n" }, lines: 1 } }),
      ev(4, { kind: "text", role: "assistant", text: "first half ", itemId: "m1" }),
      ev(5, { kind: "text", role: "assistant", text: "second half", itemId: "m1" }),
      ev(6, { kind: "lifecycle", role: "system", text: "", lifecycle: "turn_end" }),
    ]);
    expect(rows.map(r => r.kind)).toEqual(["prompt", "tool", "prose"]); // a bare turn_end closes the turn without drawing a row
    expect(rows[0].kind === "prompt" && rows[0].who).toBe("user_a");
    expect(rows[1].kind === "tool" && rows[1].result?.text).toBe("On branch main");
    expect(rows[2].kind === "prose" && rows[2].text).toBe("first half second half");
    expect(rows[2].kind === "prose" && rows[2].final).toBe(true);
  });

  it("reads explicit nulls as absent and draws system text as a notice", () => {
    const rows = buildRows([
      ev(1, { kind: "text", role: "user", text: "ship it", actor: { userId: null, agentId: "friday" } as unknown as LocalSessionEvent["actor"] }),
      ev(2, { kind: "text", role: "system", text: "[Lightbulb could not mirror one event]", truncated: true }),
    ]);
    expect(rows[0].kind === "prompt" && rows[0].who).toBe("friday");
    expect(rows[1].kind).toBe("notice");
  });
});

describe("toolRowView", () => {
  it("Claude stdout+stderr: preview lines, full output, and the remaining line count", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "npm test", itemId: "t1", tool: { name: "Bash", status: "running", input: { command: "npm test" } } }),
      ev(2, { kind: "tool", role: "tool", text: "S1\nS2", parentId: "t1", tool: { name: "result", status: "done", result: { stdout: "S1\nS2\nS3", stderr: "ERRLINE", interrupted: false }, lines: 4 } }),
    ]));
    expect(view).toMatchObject({ status: "done", preview: "S1\nS2", full: "S1\nS2\nS3\nERRLINE", more: 2, showOutput: false, truncated: false, diff: [] });
  });

  it("Claude Edit: diff lines from structuredPatch, non-strings filtered, no expand", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "src/app.ts", itemId: "t2", tool: { name: "Edit", status: "running", input: {} } }),
      ev(2, { kind: "tool", role: "tool", text: "src/app.ts", parentId: "t2", tool: { name: "result", status: "done", result: { filePath: "src/app.ts", structuredPatch: [{ lines: ["-OLDLINE", "+NEWLINE", " CTXLINE", null] }] } } }),
    ]));
    expect(view.diff).toEqual(["-OLDLINE", "+NEWLINE", " CTXLINE"]);
    expect(view.more).toBe(0);
  });

  it("Claude Task: no preview, show-output reveals description and status", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "Investigate", itemId: "t3", tool: { name: "Task", status: "running", input: {} } }),
      ev(2, { kind: "tool", role: "tool", text: "", parentId: "t3", tool: { name: "result", status: "done", result: { description: "Investigating flaky test", status: "completed" } } }),
    ]));
    expect(view).toMatchObject({ preview: "", full: "Investigating flaky test\ncompleted", more: 0, showOutput: true });
  });

  it("Claude Read ({numLines}): preview only, never a JSON stub to expand into", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "README.md", itemId: "t4", tool: { name: "Read", status: "running", input: {} } }),
      ev(2, { kind: "tool", role: "tool", text: "READL1\nREADL2", parentId: "t4", tool: { name: "result", status: "done", result: { numLines: 120 }, lines: 120 } }),
    ]));
    expect(view).toMatchObject({ preview: "READL1\nREADL2", full: "", more: 0, showOutput: false });
  });

  it("Codex CommandExecution: the call carries the preview and the remaining count", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "C1", itemId: "c1", tool: { name: "CommandExecution", status: "done", input: { command: "ls" }, result: { exitCode: 0, output: "C1\nC2\nC3" }, lines: 3 } }),
    ]));
    expect(view).toMatchObject({ preview: "C1", full: "C1\nC2\nC3", more: 2, showOutput: false });
  });

  it("Codex FileChange add and McpToolCall: a label header, output behind show-output", () => {
    const add = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", tool: { name: "FileChange", status: "done", result: { "a.ts": { type: "add", diff: null, content: "FADD1\nFADD2" } } } }),
    ]));
    expect(add).toMatchObject({ preview: "", full: "FADD1\nFADD2", diff: [], showOutput: true });
    const mcp = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "db.query", itemId: "m1", tool: { name: "McpToolCall", status: "done", result: { content: [{ type: "text", text: "MCP_OK_RESULT" }] } } }),
    ]));
    expect(mcp).toMatchObject({ preview: "", full: "MCP_OK_RESULT", showOutput: true });
  });

  it("strips ANSI and carries the truncated flag; a null lines field is not a preview", () => {
    const view = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "LABEL_ONLY", itemId: "a1", tool: { name: "Bash", status: "running", input: {}, result: null, lines: null } as unknown as LocalSessionEvent["tool"] }),
      ev(2, { kind: "tool", role: "tool", text: "\x1b[32mOKLINE\x1b[0m", parentId: "a1", truncated: true, tool: { name: "result", status: "done", result: { stdout: "\x1b[32mOKLINE\x1b[0m" } } }),
    ]));
    expect(view).toMatchObject({ preview: "OKLINE", full: "OKLINE", truncated: true });
    const label = toolRowView(tool([
      ev(1, { kind: "tool", role: "assistant", text: "LABEL_ONLY", itemId: "a2", tool: { name: "Bash", status: "running", input: {}, result: null, lines: null } as unknown as LocalSessionEvent["tool"] }),
    ]));
    expect(label.preview).toBe("");
  });
});

describe("toolHeader", () => {
  it("names each provider's tools the way its TUI does, and an orphan result as Output", () => {
    const call = (name: string, text: string, input?: unknown) => ev(1, { kind: "tool", role: "assistant", text, tool: { name, status: "done", input } });
    expect(toolHeader("claude", call("Bash", "git status"))).toBe("Bash(git status)");
    expect(toolHeader("codex", call("CommandExecution", "preview", { command: "ls" }))).toBe("Ran ls");
    expect(toolHeader("codex", call("FileChange", "a.ts"))).toBe("Edited a.ts");
    expect(toolHeader("codex", call("McpToolCall", "db.query"))).toBe("Called db.query");
    expect(toolHeader("claude", ev(1, { kind: "tool", role: "tool", text: "x", tool: { name: "result", status: "done" } }))).toBe("Output");
  });
});

describe("lifecycleLabel", () => {
  const row = (lifecycle: NonNullable<LocalSessionEvent["lifecycle"]>, text: string): Extract<TranscriptRow, { kind: "lifecycle" }> => ({ kind: "lifecycle", seq: 1, lifecycle, text });
  it("formats durations and names the other lifecycle events", () => {
    expect(lifecycleLabel(row("turn_end", "12000"))).toBe("Worked for 12s");
    expect(lifecycleLabel(row("turn_end", "90000"))).toBe("Worked for 1m 30s");
    expect(lifecycleLabel(row("turn_end", "not-a-number"))).toBe("");
    expect(lifecycleLabel(row("turn_aborted", ""))).toBe("Interrupted");
    expect(lifecycleLabel(row("compaction", ""))).toBe("Context compacted");
    expect(lifecycleLabel(row("approval_pending", ""))).toBe("Waiting for approval on the owner's Mac");
    expect(lifecycleLabel(row("turn_start", ""))).toBe("Turn started");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd packages/shared && pnpm test -- src/messaging/localSessionTranscript.test.ts`
Expected: FAIL — cannot resolve `./localSessionTranscript`.

- [ ] **Step 3: Create the model module**

Create `packages/shared/src/messaging/localSessionTranscript.ts`. `buildRows`, `toolHeader`, `fullOutput`, `diffLines` and `ANSI_RE` are copied **verbatim** from `apps/frontend/src/components/messaging/NativeSessionTranscript.tsx` (their doc comments included); only the exports, the `TranscriptRow` name and the two new functions differ:

```ts
import type { LocalSessionEvent } from "./types";

/**
 * The transcript model shared by the web pane and the iOS screen: turns the
 * canonical event stream of a shared Claude Code / Codex session into the
 * rows each TUI draws. Pure — no React, no DOM.
 */
export type LocalSessionProvider = "claude" | "codex";
export type TranscriptRow =
  | { kind: "prompt"; seq: number; text: string; who: string | null; ts?: string }
  | { kind: "prose"; seq: number; text: string; final: boolean }
  | { kind: "thinking"; seq: number; text: string }
  | { kind: "tool"; seq: number; call: LocalSessionEvent; result?: LocalSessionEvent }
  | { kind: "notice"; seq: number; text: string }
  | { kind: "lifecycle"; seq: number; lifecycle: NonNullable<LocalSessionEvent["lifecycle"]>; text: string };
export type ToolTranscriptRow = Extract<TranscriptRow, { kind: "tool" }>;

/** Each TUI's own glyphs; renderers draw exactly these. */
export const GLYPH = {
  claude: { prompt: ">", bullet: "⏺", output: "⎿", thinking: "✻", working: "✻" },
  codex: { prompt: "›", bullet: "•", output: "└", thinking: "•", working: "•" },
} as const;

// Strips terminal escape sequences (color codes, cursor moves) that ride
// along in raw tool output — they render as garbage in a <pre>.
// eslint-disable-next-line no-control-regex
const ANSI_RE = /\x1b\[[0-9;?]*[ -/]*[@-~]/g;
export function stripAnsi(text: string): string {
  return text.replace(ANSI_RE, "");
}

export function buildRows(events: readonly LocalSessionEvent[]): TranscriptRow[] {
  // … verbatim body from the web file, with `Row` spelled `TranscriptRow` …
}

export function toolHeader(provider: LocalSessionProvider, call: LocalSessionEvent): string {
  // … verbatim …
}

export function fullOutput(result: unknown): string {
  // … verbatim, with its doc comment …
}

export function diffLines(result: unknown): string[] {
  // … verbatim …
}

export interface ToolRowView {
  status: "running" | "done" | "error";
  /** The harness's own output preview (already ANSI-stripped); "" when the row's text is a label. */
  preview: string;
  /** Everything the adapter recorded, for the expanded view; "" when there is nothing to expand into. */
  full: string;
  diff: string[];
  /** Output lines beyond the preview; 0 when unknown. */
  more: number;
  /** Offer "show output" even though no line count says more exists. */
  showOutput: boolean;
  truncated: boolean;
}

/** What both renderers draw for a tool row — one computation, two skins. */
export function toolRowView(row: ToolTranscriptRow): ToolRowView {
  const status = row.result?.tool?.status ?? row.call.tool?.status ?? "running";
  const resultPayload = row.result?.tool?.result ?? row.call.tool?.result;
  const diff = diffLines(resultPayload).map(stripAnsi);
  // tool.lines is set exactly when `text` is an output preview (Claude
  // tool_result rows and Codex CommandExecution); it's absent when `text` is
  // a label (McpToolCall, FileChange, SubAgentActivity, ...) — so a label
  // never gets shown as if it were output.
  const preview = stripAnsi(row.result?.text ?? (row.call.tool?.lines != null ? row.call.text : ""));
  const full = stripAnsi(fullOutput(resultPayload));
  const shownLines = preview ? preview.split("\n").length : 0;
  const total = row.result?.tool?.lines ?? row.call.tool?.lines;
  const more = full && total != null ? Math.max(0, total - shownLines) : 0;
  const showOutput = more === 0 && full.length > preview.length;
  const truncated = (row.result ?? row.call).truncated;
  return { status, preview, full, diff, more, showOutput, truncated };
}

function formatMs(text: string): string {
  const ms = Number(text);
  if (!Number.isFinite(ms) || ms <= 0) return "";
  const s = Math.round(ms / 1000);
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}

/** Meta-line copy for a lifecycle row; "" means draw nothing. */
export function lifecycleLabel(row: Extract<TranscriptRow, { kind: "lifecycle" }>): string {
  switch (row.lifecycle) {
    case "turn_end": {
      const worked = formatMs(row.text);
      return worked ? `Worked for ${worked}` : "";
    }
    case "turn_aborted": return "Interrupted";
    case "compaction": return "Context compacted";
    case "approval_pending": return "Waiting for approval on the owner's Mac";
    default: return "Turn started";
  }
}
```

- [ ] **Step 4: Export from the barrel**

In `packages/shared/src/index.ts`, after the `useRooms` export block, add:

```ts
export {
  GLYPH,
  buildRows,
  diffLines,
  fullOutput,
  lifecycleLabel,
  stripAnsi,
  toolHeader,
  toolRowView,
} from "./messaging/localSessionTranscript";
export type {
  LocalSessionProvider,
  ToolRowView,
  ToolTranscriptRow,
  TranscriptRow,
} from "./messaging/localSessionTranscript";
```

- [ ] **Step 5: Run the shared tests, typecheck and lint**

Run: `cd packages/shared && pnpm test -- src/messaging/localSessionTranscript.test.ts && pnpm typecheck && pnpm lint`
Expected: PASS, clean.

- [ ] **Step 6: Repoint the web renderer**

Rewrite the top of `apps/frontend/src/components/messaging/NativeSessionTranscript.tsx` so the file keeps only `Person`, `ToolRow`, `Working`, `NativeSessionTranscript`:

```tsx
"use client";

import { useEffect, useState } from "react";
import {
  GLYPH, buildRows, lifecycleLabel, toolHeader, toolRowView,
  type LocalSessionEvent, type LocalSessionProvider, type ToolTranscriptRow,
} from "@lightbulb/shared";
import { MarkdownView } from "@/components/teams/shared/components/MarkdownView";
import { roomMarkdown } from "./roomMarkdown";

export type Provider = LocalSessionProvider;
export type Person = { kind: "human" | "agent"; id: string; displayName: string };
```

Delete from the web file: `Row`, `GLYPH`, `ANSI_RE`, `stripAnsi`, `buildRows`, `toolHeader`, `fullOutput`, `diffLines`, `formatMs`.

`ToolRow` becomes:

```tsx
function ToolRow({ provider, row, seq }: { provider: Provider; row: ToolTranscriptRow; seq: number }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const { status, preview, full, diff, more, showOutput, truncated } = toolRowView(row);
  const isDiff = diff.length > 0;
  const withMarker = (text: string) => (truncated && text ? `${text}\n(truncated)` : text);
  const shown = open ? full || preview : preview;
  return <div className="native-session-row" data-status={status} aria-label={`Native event ${seq}`} role="article">
    {/* … the existing JSX, unchanged … */}
  </div>;
}
```

In `NativeSessionTranscript`, replace the lifecycle branch (from `const worked = …` to the final `return <p …>{label}</p>`) with:

```tsx
      const label = lifecycleLabel(row);
      if (!label) return null;
      return <p key={row.seq} className="native-session-meta" role="article" aria-label={`Native event ${row.seq}`}>{label}</p>;
```

- [ ] **Step 7: Move the web `buildRows` test**

In `apps/frontend/src/components/messaging/__tests__/NativeSessionTranscript.test.tsx`: change the import to `import { NativeSessionTranscript } from "../NativeSessionTranscript";` and delete the whole `describe("buildRows", …)` block (it now lives in shared). Leave every component case in place.

Then check nothing else imports the removed names:

Run: `grep -rn "NativeSessionTranscript\"" apps/frontend/src | grep -v __tests__`
Expected: only `LocalSessionPanel.tsx` (`NativeSessionTranscript, type Person`). If any other file imports `Row`, `Provider` or `buildRows` from it, repoint that import to `@lightbulb/shared` (`TranscriptRow` / `LocalSessionProvider`).

- [ ] **Step 8: Run the web tests, typecheck and lint**

Run: `cd apps/frontend && pnpm test -- src/components/messaging/__tests__/NativeSessionTranscript.test.tsx src/components/messaging/__tests__/RoomResources.test.tsx && pnpm typecheck && pnpm lint`
Expected: PASS, clean.

- [ ] **Step 9: Commit**

```bash
git add packages/shared/src/messaging/localSessionTranscript.ts packages/shared/src/messaging/localSessionTranscript.test.ts packages/shared/src/index.ts apps/frontend/src/components/messaging/NativeSessionTranscript.tsx apps/frontend/src/components/messaging/__tests__/NativeSessionTranscript.test.tsx
git commit -m "refactor(shared): move the native session transcript model out of the web renderer"
```

---

### Task 2: Move `useRoomResources` / `useLocalSessionHistory` to shared, with an `enabled` gate

**Files:**
- Create: `packages/shared/src/messaging/useRoomResources.ts` (from `apps/frontend/src/hooks/useRoomResources.ts`)
- Create: `packages/shared/src/messaging/useRoomResources.test.ts`
- Delete: `apps/frontend/src/hooks/useRoomResources.ts`
- Modify: `packages/shared/src/index.ts`
- Modify (imports only): `apps/frontend/src/components/messaging/RoomResources.tsx`, `apps/frontend/src/components/messaging/LocalSessionPanel.tsx`, `apps/frontend/src/components/messaging/RoomPanel.tsx`, `apps/frontend/src/components/workspace/panels/SessionDockPanel.tsx`, `apps/frontend/src/components/messaging/__tests__/RoomResources.test.tsx`, `apps/frontend/src/components/messaging/__tests__/RoomResourcesDock.test.tsx`, `apps/frontend/src/hooks/__tests__/useRooms.test.tsx`

**Interfaces:**
- Produces (from `@lightbulb/shared`): `useRoomResources(roomId: string, source: RoomResourceSource, options?: { enabled?: boolean })` returning `{ data, error, loading, revision, accessRevision, refresh, revoke }`; `useLocalSessionHistory(client, roomId, sessionId, latestEventSeq, revision, onAccessLost, live?: { busy: boolean })` returning `{ events, loading, error, load, retry, hasOlder, hasNewer }`; `accessLost(error: unknown): boolean`; `interface RoomResourceSource { client; onEvent; connectionRevision }`.
- `useRooms(...)` (already in shared) returns `resourceSource: RoomResourceSource`-shaped `{ client: messaging, onEvent, connectionRevision }`.

- [ ] **Step 1: Write the failing `enabled` test**

Create `packages/shared/src/messaging/useRoomResources.test.ts`:

```ts
// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { useRoomResources, type RoomResourceSource } from "./useRoomResources";

const listResources = vi.fn(async () => ({ devices: [], sessions: [], pairedNodes: [] }));
// Module-level so the source is referentially stable across rerenders.
const source = {
  client: { listResources, listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: vi.fn(), getLocalSessionCommand: vi.fn() },
  onEvent: () => () => {},
  connectionRevision: 1,
} as unknown as RoomResourceSource;

afterEach(() => { cleanup(); vi.useRealTimers(); listResources.mockClear(); });

describe("useRoomResources enabled gate", () => {
  it("neither fetches nor polls while disabled, then fetches once enabled and polls every 5s", async () => {
    vi.useFakeTimers();
    const { rerender } = renderHook(({ enabled }) => useRoomResources("room-1", source, { enabled }), { initialProps: { enabled: false } });
    await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
    expect(listResources).not.toHaveBeenCalled();
    rerender({ enabled: true });
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listResources).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(listResources).toHaveBeenCalledTimes(2);
    rerender({ enabled: false });
    await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
    expect(listResources).toHaveBeenCalledTimes(2);
  });

  it("defaults to enabled", async () => {
    vi.useFakeTimers();
    renderHook(() => useRoomResources("room-1", source));
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(listResources).toHaveBeenCalledTimes(1);
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd packages/shared && pnpm test -- src/messaging/useRoomResources.test.ts`
Expected: FAIL — cannot resolve `./useRoomResources`.

- [ ] **Step 3: Move the hook file**

```bash
git mv apps/frontend/src/hooks/useRoomResources.ts packages/shared/src/messaging/useRoomResources.ts
```

Edit the moved file: delete the `"use client";` line and replace the import block with:

```ts
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import type { LiveEvent } from "../live/types";
import type { createMessagingClient } from "./client";
import type { LocalSessionEvent, RoomResources } from "./types";
```

Change the `useRoomResources` signature and its two effects:

```ts
/**
 * Mounted only by the open pane, keyed by owner, viewer, room and membership.
 * `enabled: false` neither fetches nor polls (a phone stacks several screens of
 * one room; only the focused one should); the last snapshot stays readable.
 */
export function useRoomResources(
  roomId: string,
  { client, onEvent, connectionRevision }: RoomResourceSource,
  { enabled = true }: { enabled?: boolean } = {},
) {
```

In the mount effect, add the guard as its first line and `enabled` to its dependencies:

```ts
  useEffect(() => {
    if (!enabled) return;
    blocked.current = false;
    void refresh();
    // … unchanged …
    return () => { unsubscribe(); clearInterval(timer); blocked.current = true; invalidate(); };
  }, [enabled, onEvent, roomId, refresh, revoke, invalidate]);
```

In the connection-revision effect:

```ts
  useEffect(() => {
    if (enabled && previousConnection.current !== connectionRevision) void refresh();
    previousConnection.current = connectionRevision;
  }, [enabled, connectionRevision, refresh]);
```

Everything else in the file (including `useLocalSessionHistory`) is unchanged.

- [ ] **Step 4: Export from the barrel**

In `packages/shared/src/index.ts`, next to the Task 1 block:

```ts
export { accessLost, useLocalSessionHistory, useRoomResources } from "./messaging/useRoomResources";
export type { RoomResourceSource } from "./messaging/useRoomResources";
```

- [ ] **Step 5: Run the shared tests, typecheck and lint**

Run: `cd packages/shared && pnpm test && pnpm typecheck && pnpm lint`
Expected: PASS, clean.

- [ ] **Step 6: Repoint every web importer**

Run: `grep -rln "hooks/useRoomResources" apps/frontend/src`
Expected: exactly `RoomResources.tsx`, `LocalSessionPanel.tsx`, `RoomPanel.tsx`, `SessionDockPanel.tsx`, `__tests__/RoomResources.test.tsx`, `__tests__/RoomResourcesDock.test.tsx`, `hooks/__tests__/useRooms.test.tsx`.

In each, replace the `@/hooks/useRoomResources` (or `../useRoomResources`) import with the same names from `@lightbulb/shared`, merging into an existing `@lightbulb/shared` import line where one exists. For example in `LocalSessionPanel.tsx`:

```ts
import { ApiError, accessLost, useLocalSessionHistory, type LocalSession, type LocalSessionCommand, type LocalSessionCommandReceipt, type RoomResourceSource } from "@lightbulb/shared";
```

- [ ] **Step 7: Run the web tests, typecheck and lint**

Run: `cd apps/frontend && pnpm test -- src/components/messaging src/hooks/__tests__/useRooms.test.tsx && pnpm typecheck && pnpm lint`
Expected: PASS, clean.

- [ ] **Step 8: Commit**

```bash
git add -A packages/shared/src apps/frontend/src
git commit -m "refactor(shared): move the room resources and session history hooks out of the web app"
```

---

### Task 3: Shared session labels and `useSessionCommand`; web panel uses them

**Files:**
- Create: `packages/shared/src/messaging/localSessionLabels.ts`
- Create: `packages/shared/src/messaging/useSessionCommand.ts`
- Create: `packages/shared/src/messaging/useSessionCommand.test.ts`
- Modify: `packages/shared/src/index.ts`
- Modify: `apps/frontend/src/components/messaging/LocalSessionPanel.tsx`

**Interfaces:**
- Consumes: `accessLost`, `RoomResourceSource` (Task 2); `ApiError`; `LocalSession`, `LocalSessionCommand`, `LocalSessionCommandReceipt`.
- Produces (from `@lightbulb/shared`): `sessionLabel(s: LocalSession): string`, `sessionStatus: Record<LocalSession["status"], string>`, `unavailableReason: Record<NonNullable<LocalSession["controlUnavailableReason"]>, string>`, `canSendToSession(s: LocalSession): boolean`, `COMMAND_MAX_BYTES = 8192`, `receiptCopy(receipt?: LocalSessionCommandReceipt): string | null`, `type SessionCommandAttempt`, and
  `useSessionCommand(client: RoomResourceSource["client"], roomId: string, session: LocalSession, options: { uuid: () => string; onAccessLost: () => void; onSent?: () => void })` returning `{ attempt, receipt, receiptText, canSend, locked, checking, canCheck, send(text), retry(), checkDelivery() }`.

- [ ] **Step 1: Write the failing hook test**

Create `packages/shared/src/messaging/useSessionCommand.test.ts`:

```ts
// @vitest-environment jsdom
import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "../api/error";
import type { LocalSession, LocalSessionCommand, LocalSessionCommandReceipt } from "./types";
import type { RoomResourceSource } from "./useRoomResources";
import { canSendToSession, sessionLabel } from "./localSessionLabels";
import { useSessionCommand } from "./useSessionCommand";

const session: LocalSession = {
  sessionId: "session-123456", deviceId: "mac", humanUserId: "alice", provider: "claude",
  providerVersion: "2.1.224", generation: "g1", status: "idle", activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  controlUnavailableReason: null, latestEventSeq: 10, title: "Fix relay",
};
const receipt = (request: LocalSessionCommand, state: LocalSessionCommandReceipt["state"] = "applied"): LocalSessionCommandReceipt => ({
  ...request, text: request.text ?? null, expectedTurnId: request.expectedTurnId ?? null,
  sessionId: session.sessionId, roomId: "room", actorUserId: "bob", commandSeq: 1, state,
  createdAt: 1, deliverBy: 31, held: false, providerMessageId: null,
});
const submit = vi.fn();
const get = vi.fn();
const client = { listResources: vi.fn(), listLocalSessionEvents: vi.fn(), submitLocalSessionCommand: submit, getLocalSessionCommand: get } as unknown as RoomResourceSource["client"];
const onAccessLost = vi.fn();
const onSent = vi.fn();
const hook = (s: LocalSession = session) => renderHook(() => useSessionCommand(client, "room", s, { uuid: () => "req-1", onAccessLost, onSent }));

afterEach(() => { cleanup(); submit.mockReset(); get.mockReset(); onAccessLost.mockClear(); onSent.mockClear(); });

describe("labels", () => {
  it("labels a session by provider and title, and gates sending on verified control", () => {
    expect(sessionLabel(session)).toBe("Claude · Fix relay");
    expect(sessionLabel({ ...session, provider: "codex", title: null })).toBe("Codex · session-");
    expect(canSendToSession(session)).toBe(true);
    expect(canSendToSession({ ...session, status: "offline" })).toBe(false);
    expect(canSendToSession({ ...session, controlUnavailableReason: "history-gap" })).toBe(false);
    expect(canSendToSession({ ...session, status: "busy", capabilities: { ...session.capabilities, sendMode: "immediate" }, activeTurnId: null })).toBe(false);
  });
});

describe("useSessionCommand", () => {
  it("sends a trimmed follow-up and reports the receipt", async () => {
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
    const { result } = hook();
    await act(async () => { result.current.send("  hello  "); });
    expect(submit).toHaveBeenCalledWith("room", session.sessionId, { requestId: "req-1", expectedGeneration: "g1", kind: "send", text: "hello" });
    expect(result.current.attempt?.state).toBe("received");
    expect(result.current.receiptText).toBe("Admitted to the native session — not completed work.");
    expect(result.current.locked).toBe(false);
    expect(onSent).toHaveBeenCalledTimes(1);
  });

  it("carries the active turn when input is immediate and the session is busy", async () => {
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
    const { result } = hook({ ...session, status: "busy", activeTurnId: "turn-9", capabilities: { ...session.capabilities, sendMode: "immediate" } });
    await act(async () => { result.current.send("go"); });
    expect(submit.mock.calls[0][2]).toMatchObject({ expectedTurnId: "turn-9" });
  });

  it("marks a 409 rejected with the refresh copy, and a 5xx uncertain with a retry", async () => {
    submit.mockRejectedValueOnce(new ApiError(409, null));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.attempt).toMatchObject({ state: "rejected", error: "Session changed or is offline. Refresh before sending again." });
    submit.mockRejectedValueOnce(new ApiError(503, null));
    await act(async () => { result.current.send("again"); });
    expect(result.current.attempt).toMatchObject({ state: "uncertain", error: "Delivery unknown. Check delivery or retry the same request; it may already have been admitted." });
    expect(result.current.locked).toBe(true);
    expect(result.current.canCheck).toBe(true);
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
    await act(async () => { result.current.retry(); });
    expect(submit).toHaveBeenLastCalledWith("room", session.sessionId, expect.objectContaining({ requestId: "req-1", text: "again" }));
    expect(result.current.attempt?.state).toBe("received");
  });

  it("check delivery resolves an uncertain send and clears the draft", async () => {
    submit.mockRejectedValueOnce(new TypeError("network"));
    get.mockImplementation(async () => receipt({ requestId: "req-1", expectedGeneration: "g1", kind: "send", text: "hello" }, "dispatched"));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.attempt?.state).toBe("uncertain");
    expect(onSent).not.toHaveBeenCalled();
    await act(async () => { await result.current.checkDelivery(); });
    expect(get).toHaveBeenCalledWith("room", session.sessionId, "req-1");
    expect(result.current.attempt?.state).toBe("received");
    expect(result.current.receiptText).toBe("Dispatched; native admission is not yet confirmed.");
    expect(onSent).toHaveBeenCalledTimes(1);
  });

  it("refuses a follow-up over 8 KiB without calling the server", async () => {
    const { result } = hook();
    await act(async () => { result.current.send("x".repeat(8200)); });
    expect(submit).not.toHaveBeenCalled();
    expect(result.current.attempt).toMatchObject({ state: "rejected", error: "This follow-up is too long. Shorten it to fit the 8 KiB request limit." });
  });

  it("does nothing when the session cannot take input, and revokes on 403", async () => {
    const { result } = hook({ ...session, capabilities: { ...session.capabilities, sendMode: "none" } });
    expect(result.current.canSend).toBe(false);
    await act(async () => { result.current.send("hello"); });
    expect(submit).not.toHaveBeenCalled();
    submit.mockRejectedValueOnce(new ApiError(403, null));
    const open = hook();
    await act(async () => { open.result.current.send("hello"); });
    expect(onAccessLost).toHaveBeenCalledTimes(1);
  });

  it("prefixes an agent-issued receipt with the agent", async () => {
    submit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand): Promise<LocalSessionCommandReceipt> => ({
      ...request, text: request.text ?? null, expectedTurnId: null, sessionId: session.sessionId, roomId: "room",
      commandSeq: 1, state: "pending", createdAt: 1, deliverBy: 31, held: false, providerMessageId: null,
      actor: { kind: "agent", id: "friday" }, invocationExpiresAt: 99,
    }));
    const { result } = hook();
    await act(async () => { result.current.send("hello"); });
    expect(result.current.receiptText).toBe("Agent friday: Pending delivery to the desktop.");
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd packages/shared && pnpm test -- src/messaging/useSessionCommand.test.ts`
Expected: FAIL — cannot resolve `./localSessionLabels` / `./useSessionCommand`.

- [ ] **Step 3: Create the labels module**

Create `packages/shared/src/messaging/localSessionLabels.ts` (copy from `apps/frontend/src/components/messaging/LocalSessionPanel.tsx` — `sessionLabel`, `sessionStatus`, the `unavailable` record, the `canSend` expression):

```ts
import type { LocalSession } from "./types";

export const sessionLabel = (s: LocalSession) => `${s.provider === "claude" ? "Claude" : "Codex"} · ${s.title?.trim() || s.sessionId.slice(0, 8)}`;

export const sessionStatus: Record<LocalSession["status"], string> = {
  idle: "Idle", busy: "Working", "waiting-approval": "Waiting for approval on owner's machine", unknown: "Activity unknown", offline: "Offline",
};

export const unavailableReason: Record<NonNullable<LocalSession["controlUnavailableReason"]>, string> = {
  "runtime-unverified": "This session's native runtime has not been verified.",
  "endpoint-unavailable": "The native input connection is unavailable.",
  "unsupported-version": "This provider version does not support verified control.",
  "history-gap": "History has a gap. Control is unavailable until the desktop reconnects safely.",
};

/** Verified control, a send mode, and — when input is immediate — a known turn. */
export const canSendToSession = (session: LocalSession): boolean =>
  session.status !== "offline" && session.capabilitiesVerified &&
  !session.controlUnavailableReason && session.capabilities.sendMode !== "none" &&
  !(session.status === "busy" && session.capabilities.sendMode === "immediate" && !session.activeTurnId);
```

- [ ] **Step 4: Create the command hook**

Create `packages/shared/src/messaging/useSessionCommand.ts`:

```ts
import { useEffect, useRef, useState } from "react";

import { ApiError } from "../api/error";
import { canSendToSession } from "./localSessionLabels";
import type { LocalSession, LocalSessionCommand, LocalSessionCommandReceipt } from "./types";
import { accessLost, type RoomResourceSource } from "./useRoomResources";

export type SessionCommandAttempt = {
  request: LocalSessionCommand;
  state: "sending" | "uncertain" | "rejected" | "received";
  receipt?: LocalSessionCommandReceipt;
  error?: string;
};

/** Canonical command JSON is bounded to 8 KiB UTF-8, including every field. */
export const COMMAND_MAX_BYTES = 8192;

/** The receipt line both composers show; null when there is nothing to say. */
export function receiptCopy(receipt: LocalSessionCommandReceipt | undefined): string | null {
  if (!receipt) return null;
  const text = receipt.state === "unknown"
    ? "Delivery unknown. The request may have been admitted. Further remote input remains paused until this is resolved or the native session restarts. A restart does not undo an admitted command; do not send a duplicate."
    : receipt.held ? "Queued or held by the native session."
      : receipt.state === "applied" ? "Admitted to the native session — not completed work."
        : receipt.state === "pending" ? "Pending delivery to the desktop."
          : receipt.state === "dispatched" ? "Dispatched; native admission is not yet confirmed."
            : receipt.state === "rejected" ? "The desktop rejected this request." : null;
  return text && receipt.actor ? `Agent ${receipt.actor.id}: ${text}` : text;
}

/**
 * One follow-up at a time into a shared native session: send, remember the
 * attempt, check delivery, retry the SAME request. Never resends implicitly —
 * an uncertain send may already have been admitted on the owner's Mac.
 * `uuid` is injected because the web has `crypto.randomUUID` and the phone
 * has expo-crypto. `onSent` fires when the composer should clear its draft.
 */
export function useSessionCommand(
  client: RoomResourceSource["client"], roomId: string, session: LocalSession,
  { uuid, onAccessLost, onSent }: { uuid: () => string; onAccessLost: () => void; onSent?: () => void },
) {
  const [attempt, setAttempt] = useState<SessionCommandAttempt | null>(null);
  const [checking, setChecking] = useState(false);
  const alive = useRef(true);
  const pending = useRef(false);
  useEffect(() => { alive.current = true; return () => { alive.current = false; }; }, []);
  const canSend = canSendToSession(session);
  const unknownCurrent = attempt?.receipt?.state === "unknown" && attempt.receipt.expectedGeneration === session.generation;
  const locked = checking || attempt?.state === "sending" || attempt?.state === "uncertain" || unknownCurrent;

  async function submit(request: LocalSessionCommand) {
    if (pending.current || !canSend) return;
    pending.current = true;
    setAttempt({ request, state: "sending" });
    try {
      const receipt = await client.submitLocalSessionCommand(roomId, session.sessionId, request);
      if (!alive.current) return;
      setAttempt({ request, state: "received", receipt });
      onSent?.();
    } catch (e) {
      if (!alive.current) return;
      if (accessLost(e)) { onAccessLost(); return; }
      const rejected = e instanceof ApiError && e.status < 500;
      setAttempt({ request, state: rejected ? "rejected" : "uncertain", error: rejected
        ? e.status === 409 ? "Session changed or is offline. Refresh before sending again."
          : "The session rejected this request. Check its availability and capabilities."
        : "Delivery unknown. Check delivery or retry the same request; it may already have been admitted." });
    } finally { pending.current = false; }
  }

  function send(text: string) {
    const trimmed = text.trim();
    if (locked || !trimmed || !canSend) return;
    const request: LocalSessionCommand = { requestId: uuid(), expectedGeneration: session.generation, kind: "send", text: trimmed,
      ...(session.status === "busy" && session.capabilities.sendMode === "immediate" ? { expectedTurnId: session.activeTurnId } : {}) };
    if (new TextEncoder().encode(JSON.stringify({ ...request, expectedTurnId: request.expectedTurnId ?? null })).length > COMMAND_MAX_BYTES) {
      setAttempt({ request, state: "rejected", error: "This follow-up is too long. Shorten it to fit the 8 KiB request limit." });
      return;
    }
    void submit(request);
  }

  function retry() {
    if (attempt?.state === "uncertain") void submit(attempt.request);
  }

  async function checkDelivery() {
    if (!attempt || pending.current) return;
    pending.current = true;
    setChecking(true);
    try {
      const receipt = await client.getLocalSessionCommand(roomId, session.sessionId, attempt.request.requestId);
      if (alive.current) {
        setAttempt({ ...attempt, state: "received", receipt, error: undefined });
        if (attempt.state === "uncertain") onSent?.();
      }
    } catch (e) {
      if (!alive.current) return;
      // A missing receipt can mean the original request never arrived. Do not resend implicitly.
      if (e instanceof ApiError && e.status === 403) { onAccessLost(); return; }
      setAttempt({ ...attempt, error: "Delivery is still unconfirmed. Checking does not resend the request." });
    } finally { pending.current = false; if (alive.current) setChecking(false); }
  }

  const receipt = attempt?.receipt;
  const canCheck = attempt?.state === "uncertain" || (!!receipt && ["pending", "dispatched", "unknown"].includes(receipt.state));
  return { attempt, receipt, receiptText: receiptCopy(receipt), canSend, locked, checking, canCheck, send, retry, checkDelivery };
}
```

- [ ] **Step 5: Export from the barrel**

In `packages/shared/src/index.ts`, next to the Task 2 block:

```ts
export { canSendToSession, sessionLabel, sessionStatus, unavailableReason } from "./messaging/localSessionLabels";
export { COMMAND_MAX_BYTES, receiptCopy, useSessionCommand } from "./messaging/useSessionCommand";
export type { SessionCommandAttempt } from "./messaging/useSessionCommand";
```

- [ ] **Step 6: Run the shared tests, typecheck and lint**

Run: `cd packages/shared && pnpm test && pnpm typecheck && pnpm lint`
Expected: PASS, clean.

- [ ] **Step 7: Switch the web panel to the hook**

In `apps/frontend/src/components/messaging/LocalSessionPanel.tsx`:

1. Import block becomes:
   ```ts
   import { useId, useState } from "react";
   import { sessionLabel, sessionStatus, unavailableReason, useLocalSessionHistory, useSessionCommand, type LocalSession, type RoomResourceSource } from "@lightbulb/shared";
   import { Button } from "@/components/ui/button";
   import { copyText, sessionPermalink } from "./messageLinks";
   import { LocalSessionWork } from "./LocalSessionWork";
   import { NativeSessionTranscript, type Person } from "./NativeSessionTranscript";

   export { sessionLabel, sessionStatus } from "@lightbulb/shared";
   ```
   (`useId` stays for the `<label htmlFor>`; `useEffect`/`useRef`/`ApiError`/`accessLost` go — the hook owns them.)
2. Delete the local `sessionLabel`, `sessionStatus`, `unavailable`, `Attempt`, and inside the component the `attempt`/`checking`/`alive`/`pending` state, the `alive` effect, `canSend`, `unknownCurrent`, `locked`, `submit`, `checkDelivery`, `receipt`, `receiptText`.
3. After `const [text, setText] = useState("");` add:
   ```ts
   const command = useSessionCommand(client, roomId, session, { uuid: () => crypto.randomUUID(), onAccessLost, onSent: () => setText("") });
   ```
4. `{unavailable[session.controlUnavailableReason]}` → `{unavailableReason[session.controlUnavailableReason]}`.
5. The form:
   ```tsx
   <form className="room-composer local-session-composer native-session-composer" onSubmit={event => { event.preventDefault(); command.send(text); }}>
     <label htmlFor={id} className="text-sm font-semibold">Send to {target}</label>
     <div className="native-session-prompt-row"><span className="native-session-glyph" aria-hidden="true">{glyph}</span>
       <textarea id={id} className="room-input" rows={2} value={text} disabled={!command.canSend || command.locked} onChange={event => setText(event.target.value)}
         onKeyDown={event => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing && event.keyCode !== 229) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }}
         placeholder="Ask this session a follow-up… (Enter sends, Shift+Enter newline)" /></div>
     {!command.canSend && <p className="text-sm text-muted-foreground">Sending is unavailable for this session.</p>}
     <Button type="submit" disabled={!command.canSend || command.locked || !text.trim()}>Send follow-up</Button>
     {command.attempt?.state === "sending" && <p role="status">Sending follow-up…</p>}
     {command.attempt?.error && <p role="alert" className="text-sm text-warn">{command.attempt.error}</p>}
     {command.receiptText && <p role="status" className="text-sm">{command.receiptText}</p>}
     {command.receipt?.held && <p className="text-sm text-muted-foreground">Admission is not completed work.</p>}
     {command.canCheck && <Button type="button" variant="outline" disabled={command.checking} onClick={() => void command.checkDelivery()}>Check delivery</Button>}
     {command.attempt?.state === "uncertain" && <Button type="button" variant="outline" disabled={!command.canSend || command.checking} onClick={() => command.retry()}>Retry same request</Button>}
   </form>
   ```
   Nothing else in the file needs `canSendToSession`; do not import it.

- [ ] **Step 8: Run the web tests, typecheck and lint**

Run: `cd apps/frontend && pnpm test -- src/components/messaging && pnpm typecheck && pnpm lint`
Expected: PASS, clean (the `RoomResources.test.tsx` send/receipt cases exercise the hook through the panel).

- [ ] **Step 9: Commit**

```bash
git add packages/shared/src apps/frontend/src/components/messaging/LocalSessionPanel.tsx
git commit -m "refactor(shared): lift the session follow-up state machine and labels into the shared package"
```

---

### Task 4: Header action with a live badge on the conversation screen

**Files:**
- Create: `apps/mobile/src/lib/localSessions.ts`
- Modify: `apps/mobile/src/components/room/RoomHeaderActions.tsx`
- Modify: `apps/mobile/src/components/room/__tests__/RoomHeaderActions.test.tsx`
- Modify: `apps/mobile/src/app/(app)/room/[roomId]/index.tsx`
- Modify: `apps/mobile/src/app/(app)/room/__tests__/conversation.test.tsx`

**Interfaces:**
- Consumes: `useRoomResources(roomId, source, { enabled })` from Task 2; `rooms.resourceSource` from `useMobileRooms`.
- Produces: `liveSessionCount(sessions?: readonly LocalSession[]): number` and `memberName(members: readonly RoomMember[] | undefined, humanUserId: string): string` in `apps/mobile/src/lib/localSessions.ts`; `RoomHeaderActions` props `onOpenDevices?: () => void; liveSessions?: number`.

- [ ] **Step 1: Write the failing header test**

Append to `apps/mobile/src/components/room/__tests__/RoomHeaderActions.test.tsx`:

```tsx
  it("shows the Devices & sessions button only when given a handler, with the live count in its label", async () => {
    const onOpenDevices = jest.fn();
    const { rerender } = await render(
      <RoomHeaderActions starred={false} muted={false} onToggleStar={() => {}} onToggleMute={() => {}} />,
    );
    expect(screen.queryByLabelText(/Devices & sessions/)).toBeNull();
    rerender(
      <RoomHeaderActions starred={false} muted={false} onToggleStar={() => {}} onToggleMute={() => {}} onOpenDevices={onOpenDevices} liveSessions={0} />,
    );
    await fireEvent.press(screen.getByLabelText("Devices & sessions"));
    expect(onOpenDevices).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("2")).toBeNull();
    rerender(
      <RoomHeaderActions starred={false} muted={false} onToggleStar={() => {}} onToggleMute={() => {}} onOpenDevices={onOpenDevices} liveSessions={2} />,
    );
    expect(screen.getByLabelText("Devices & sessions, 2 live")).toBeTruthy();
    expect(screen.getByText("2")).toBeTruthy();
  });
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/mobile && pnpm test -- src/components/room/__tests__/RoomHeaderActions.test.tsx`
Expected: FAIL — no element with label "Devices & sessions".

- [ ] **Step 3: Add the button and badge**

`apps/mobile/src/components/room/RoomHeaderActions.tsx` — add `Text` to the `react-native` import and `fonts` to the theme import; extend the props and render the terminal button first:

```tsx
export function RoomHeaderActions({
  starred, muted, disabled, onToggleStar, onToggleMute, onOpenDevices, liveSessions = 0,
}: {
  starred: boolean;
  muted: boolean;
  disabled?: boolean;
  onToggleStar: () => void;
  onToggleMute: () => void;
  /** Present only on rooms that can show shared devices & sessions. */
  onOpenDevices?: () => void;
  /** Sessions working or waiting for approval; drawn as a badge. */
  liveSessions?: number;
}) {
  return (
    <View style={styles.row}>
      {onOpenDevices && (
        <Pressable
          onPress={onOpenDevices}
          disabled={disabled}
          hitSlop={8}
          accessibilityRole="button"
          accessibilityLabel={liveSessions > 0 ? `Devices & sessions, ${liveSessions} live` : "Devices & sessions"}
          accessibilityState={{ disabled: !!disabled }}
          style={({ pressed }) => [styles.button, pressed && styles.buttonPressed]}
        >
          <Feather name="terminal" size={18} color={colors.inkSoft} />
          {liveSessions > 0 && (
            <View style={styles.badge}>
              <Text style={styles.badgeText}>{liveSessions > 9 ? "9+" : String(liveSessions)}</Text>
            </View>
          )}
        </Pressable>
      )}
      {/* … the existing star and mute Pressables, unchanged … */}
    </View>
  );
}
```

Add to `styles`:

```ts
  badge: {
    position: "absolute",
    top: 3,
    right: 3,
    minWidth: 14,
    height: 14,
    borderRadius: 7,
    paddingHorizontal: 3,
    backgroundColor: colors.green,
    alignItems: "center",
    justifyContent: "center",
  },
  badgeText: { fontFamily: fonts.sansSemiBold, fontSize: 9, lineHeight: 11, color: colors.white },
```

- [ ] **Step 4: Run the header test to verify it passes**

Run: `cd apps/mobile && pnpm test -- src/components/room/__tests__/RoomHeaderActions.test.tsx`
Expected: PASS.

- [ ] **Step 5: Create the mobile helpers**

Create `apps/mobile/src/lib/localSessions.ts`:

```ts
import type { LocalSession, RoomDetail } from "@lightbulb/shared";

type RoomMember = RoomDetail["members"][number];

/** Sessions a reader would want to glance at now: working, or blocked on the owner. */
export const liveSessionCount = (sessions: readonly LocalSession[] | undefined): number =>
  (sessions ?? []).filter((s) => s.status === "busy" || s.status === "waiting-approval").length;

/** A room member's display name by human id; the web's fallback copy when unknown. */
export const memberName = (members: readonly RoomMember[] | undefined, humanUserId: string): string =>
  members?.find((m) => m.kind === "human" && m.id === humanUserId)?.displayName ?? "Room member";
```

- [ ] **Step 6: Write the failing conversation test**

In `apps/mobile/src/app/(app)/room/__tests__/conversation.test.tsx`:

1. Change the `listResources` mock in `mockLive.messaging` to a named, hoisted mock so a test can override it:
   ```ts
   const mockListResources = jest.fn(async () => ({ devices: [], sessions: [], pairedNodes: [] }));
   ```
   and in `messaging`: `listResources: mockListResources,`. In `beforeEach` add `mockListResources.mockReset(); mockListResources.mockResolvedValue({ devices: [], sessions: [], pairedNodes: [] });`.
2. Add a fixture near `mockRoom`:
   ```ts
   const mockSession = {
     sessionId: "sess-1", deviceId: "mac-1", humanUserId: "user-a", provider: "claude" as const, generation: "g1",
     status: "busy" as const, providerVersion: "2.1.224", controlUnavailableReason: null, activeTurnId: "t1",
     capabilities: { sendMode: "queue" as const, steer: false, interrupt: false }, capabilitiesVerified: true,
     latestEventSeq: 4, title: "Fix relay", cwdName: "lightbulb",
   };
   ```
3. Add a case:
   ```tsx
   it("shows the devices header action, badges live sessions, and pushes the devices screen", async () => {
     mockListResources.mockResolvedValue({ devices: [{ deviceId: "mac-1", humanUserId: "user-a", cliDeviceId: null }], sessions: [mockSession], pairedNodes: [] });
     await render(<Conversation />);
     const button = await screen.findByLabelText("Devices & sessions, 1 live");
     await fireEvent.press(button);
     expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1/devices");
   });
   ```

- [ ] **Step 7: Run it to verify it fails**

Run: `cd apps/mobile && pnpm test -- "src/app/\(app\)/room/__tests__/conversation.test.tsx" -t "devices header action"`
Expected: FAIL — no element with that label.

- [ ] **Step 8: Wire the conversation screen**

In `apps/mobile/src/app/(app)/room/[roomId]/index.tsx`:

1. Add `useRoomResources` to the `@lightbulb/shared` import and `import { liveSessionCount } from "../../../../lib/localSessions";`.
2. Directly after `const rooms = useMobileRooms({ … });` add:
   ```ts
   // The header badge: shared sessions working right now. The same 5s cadence
   // the web room tab pays; paused while a pushed screen (thread, devices,
   // session) has focus, since each of those polls for itself.
   const resources = useRoomResources(roomId ?? "", rooms.resourceSource, { enabled: focused && !!roomId });
   const liveSessions = liveSessionCount(resources.data?.sessions);
   ```
3. In `headerOptions.headerRight`, pass the two new props:
   ```tsx
   <RoomHeaderActions
     starred={!!rooms.activeRoom?.starred}
     muted={!!rooms.activeRoom?.muted}
     disabled={!rooms.activeRoom}
     liveSessions={liveSessions}
     onOpenDevices={() => router.push(`/(app)/room/${roomId}/devices`)}
     onToggleStar={() => void savePrefs({ starred: !rooms.activeRoom?.starred }, STAR_FAILED)}
     onToggleMute={() => void savePrefs({ muted: !rooms.activeRoom?.muted }, MUTE_FAILED)}
   />
   ```

- [ ] **Step 9: Run the conversation suite, typecheck and lint**

Run: `cd apps/mobile && pnpm test -- "src/app/\(app\)/room/__tests__/conversation.test.tsx" src/components/room && pnpm typecheck && pnpm lint`
Expected: PASS, clean.

- [ ] **Step 10: Commit**

```bash
git add apps/mobile/src/lib/localSessions.ts apps/mobile/src/components/room/RoomHeaderActions.tsx apps/mobile/src/components/room/__tests__/RoomHeaderActions.test.tsx "apps/mobile/src/app/(app)/room/[roomId]/index.tsx" "apps/mobile/src/app/(app)/room/__tests__/conversation.test.tsx"
git commit -m "feat(mobile): devices & sessions header action with a live badge"
```

---

### Task 5: Devices & sessions screen

**Files:**
- Modify: `apps/mobile/src/lib/localSessions.ts`
- Create: `apps/mobile/src/components/session/SessionStatusDot.tsx`
- Create: `apps/mobile/src/app/(app)/room/[roomId]/devices.tsx`
- Create: `apps/mobile/src/app/(app)/room/__tests__/devices.test.tsx`

**Interfaces:**
- Consumes: `useRoomResources`, `sessionLabel`, `sessionStatus`, `LocalSession`, `RoomResources` from shared; `memberName` (Task 4); `useMobileRooms`; `HeaderBack`; `RoomBlockedState`.
- Produces: `deviceSections(data: RoomResources, name: (humanUserId: string) => string): DeviceSection[]` with `type DeviceRow = { kind: "mac"; key; deviceId; pairedLabel: string | null; sessions: LocalSession[] } | { kind: "cli"; key; deviceId }` and `interface DeviceSection { key: string; title: string; data: DeviceRow[] }`; `SessionStatusDot({ status })`.

- [ ] **Step 1: Write the failing screen test**

Create `apps/mobile/src/app/(app)/room/__tests__/devices.test.tsx`:

```tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError, type LiveEvent, type LocalSession, type RoomDetail, type RoomResources } from "@lightbulb/shared";

jest.setTimeout(20000);

const mockPush = jest.fn();
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: (...args: unknown[]) => mockPush(...args), back: jest.fn(), canGoBack: () => true, replace: jest.fn() },
    Stack: { Screen: () => null },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(() => cb(), [cb]),
    useLocalSearchParams: () => ({ roomId: "room-1" }),
  };
});
let mockScope = 0;
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: { id: `org-${mockScope}` } }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));

const mockRoom: RoomDetail = {
  roomId: "room-1", name: "Launch", kind: "channel", roomRevision: 1, deliveryKey: "k",
  pinnedMessageIds: [], topic: null, starred: false, muted: false, sectionId: null,
  lastMessageSeq: 3, lastMessageAt: "2026-09-11T10:02:00Z", unreadCount: 0,
  updatedAt: "2026-09-11T10:02:00Z", lastReadSeq: 3, createdAt: "2026-09-11T09:00:00Z",
  members: [
    { kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "x", joinedSeq: 0 },
    { kind: "human", id: "user-b", displayName: "Bob", mentionHandle: null, role: "member", listenMode: null, joinedAt: "x", joinedSeq: 0 },
  ],
};
const session: LocalSession = {
  sessionId: "sess-1", deviceId: "mac-1234abcd", humanUserId: "user-a", provider: "claude", generation: "g1",
  status: "busy", providerVersion: "2.1.224", controlUnavailableReason: null, activeTurnId: "t1",
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 4, title: "Fix relay", cwdName: "lightbulb",
};
const full: RoomResources = {
  devices: [{ deviceId: "mac-1234abcd", humanUserId: "user-a", cliDeviceId: "cli-1" }],
  sessions: [session],
  pairedNodes: [
    { deviceId: "mac-1234abcd", humanUserId: "user-a", clientId: "openclaw-macos", macDeviceId: "mac-1234abcd" },
    { deviceId: "cli-1", humanUserId: "user-a", clientId: "node-host", macDeviceId: "mac-1234abcd" },
    { deviceId: "cli-lonely99", humanUserId: "user-b", clientId: "node-host", macDeviceId: null },
  ],
};
const mockListResources = jest.fn(async (): Promise<RoomResources> => full);
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null, liveChannels: ["/rooms/room-1/k"], connectionRevision: 1,
  messaging: {
    listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
    getRoom: jest.fn(async () => mockRoom),
    listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
    markRead: jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 3 })),
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    listResources: mockListResources,
    sendMessage: jest.fn(), downloadAttachment: jest.fn(), uploadAttachment: jest.fn(), markUnread: jest.fn(),
    setTyping: jest.fn(), abortRun: jest.fn(), steerRun: jest.fn(), createRoom: jest.fn(), updateRoom: jest.fn(),
    upsertMember: jest.fn(), removeMember: jest.fn(), updateMessage: jest.fn(), setReaction: jest.fn(),
    getLiveConfig: jest.fn(), setMemberPrefs: jest.fn(), putSections: jest.fn(),
  },
  claimActiveRoomSubscription: jest.fn(() => () => {}),
  onEvent: (_handler: (event: LiveEvent) => void) => () => {},
  onChatMessage: () => () => {},
  runtime: {}, rpc: jest.fn(), getOperation: jest.fn(),
};
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import DevicesScreen from "../[roomId]/devices";

describe("devices & sessions screen", () => {
  beforeEach(() => {
    mockScope += 1;
    mockPush.mockClear();
    mockListResources.mockReset();
    mockListResources.mockResolvedValue(full);
  });

  it("groups Macs and sessions by member and opens a session", async () => {
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText("Alice")).toBeTruthy());
    expect(screen.getByText("Mac · mac-1234")).toBeTruthy();
    expect(screen.getByText("Paired Mac + associated CLI · availability unknown")).toBeTruthy();
    expect(screen.getByText("Claude · Fix relay")).toBeTruthy();
    expect(screen.getByText("Working · ~/lightbulb")).toBeTruthy();
    expect(screen.getByText("Bob")).toBeTruthy();
    expect(screen.getByText("Paired CLI endpoint · cli-lone")).toBeTruthy();
    await fireEvent.press(screen.getByLabelText("Open Claude · Fix relay"));
    expect(mockPush).toHaveBeenCalledWith("/(app)/room/room-1/session/sess-1");
  });

  it("says so when nothing is shared", async () => {
    mockListResources.mockResolvedValue({ devices: [], sessions: [], pairedNodes: [] });
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText("No paired devices or shared sessions in this conversation.")).toBeTruthy());
  });

  it("shows the refresh error with a retry that loads the list", async () => {
    mockListResources.mockRejectedValueOnce(new TypeError("offline")).mockResolvedValueOnce(full);
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText("Could not refresh devices and sessions. Try again.")).toBeTruthy());
    expect(screen.queryByText("Claude · Fix relay")).toBeNull();
    await fireEvent.press(screen.getByLabelText("Try again"));
    await waitFor(() => expect(screen.getByText("Claude · Fix relay")).toBeTruthy());
    expect(screen.queryByText("Could not refresh devices and sessions. Try again.")).toBeNull();
  });

  it("reports removed access", async () => {
    mockListResources.mockRejectedValue(new ApiError(403, null));
    await render(<DevicesScreen />);
    await waitFor(() => expect(screen.getByText(/Access to these resources was removed/)).toBeTruthy());
  });
});
```

Note for the implementer: do not add fake timers to this suite — the room machine runs on real timers.

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/mobile && pnpm test -- "src/app/\(app\)/room/__tests__/devices.test.tsx"`
Expected: FAIL — cannot find module `../[roomId]/devices`.

- [ ] **Step 3: Add `deviceSections` to the helpers**

Append to `apps/mobile/src/lib/localSessions.ts` (add `RoomResources` to the type import):

```ts
export type DeviceRow =
  | { kind: "mac"; key: string; deviceId: string; pairedLabel: string | null; sessions: LocalSession[] }
  | { kind: "cli"; key: string; deviceId: string };
export interface DeviceSection { key: string; title: string; data: DeviceRow[] }

/**
 * The web RoomResources grouping: one section per human, a row per Mac (with
 * its sessions) and one per CLI endpoint that has no Mac association.
 */
export function deviceSections(data: RoomResources, name: (humanUserId: string) => string): DeviceSection[] {
  const humans = new Set([
    ...data.devices.map((d) => d.humanUserId),
    ...data.pairedNodes.map((n) => n.humanUserId),
    ...data.sessions.map((s) => s.humanUserId),
  ]);
  return [...humans].map((human) => {
    const macs = new Set([
      ...data.devices.filter((d) => d.humanUserId === human).map((d) => d.deviceId),
      ...data.pairedNodes.filter((n) => n.humanUserId === human && n.clientId === "openclaw-macos").map((n) => n.deviceId),
      ...data.sessions.filter((s) => s.humanUserId === human).map((s) => s.deviceId),
    ]);
    const rows: DeviceRow[] = [...macs].map((mac) => {
      const nodes = data.pairedNodes.filter((n) => n.humanUserId === human && n.macDeviceId === mac);
      return {
        kind: "mac",
        key: `mac:${mac}`,
        deviceId: mac,
        pairedLabel: nodes.length > 0
          ? `Paired ${nodes.some((n) => n.clientId === "node-host") ? "Mac + associated CLI" : "Mac"} · availability unknown`
          : null,
        sessions: data.sessions.filter((s) => s.humanUserId === human && s.deviceId === mac),
      };
    });
    for (const node of data.pairedNodes.filter((n) => n.humanUserId === human && n.clientId === "node-host" && !n.macDeviceId)) {
      rows.push({ kind: "cli", key: `cli:${node.deviceId}`, deviceId: node.deviceId });
    }
    return { key: human, title: name(human), data: rows };
  });
}
```

- [ ] **Step 4: Create the status dot**

Create `apps/mobile/src/components/session/SessionStatusDot.tsx`:

```tsx
import { sessionStatus, type LocalSession } from "@lightbulb/shared";
import { StyleSheet, View } from "react-native";

import { colors } from "../../config/theme";

const DOT: Record<LocalSession["status"], { color: string; hollow: boolean }> = {
  busy: { color: colors.green, hollow: false },
  "waiting-approval": { color: colors.warn, hollow: false },
  idle: { color: colors.mute, hollow: false },
  unknown: { color: colors.mute, hollow: false },
  offline: { color: colors.border, hollow: true },
};

/** Never colour alone: the status text sits beside it; this carries the label for readers. */
export function SessionStatusDot({ status }: { status: LocalSession["status"] }) {
  const dot = DOT[status];
  return (
    <View
      accessibilityLabel={sessionStatus[status]}
      style={[styles.dot, dot.hollow ? { borderWidth: 1.5, borderColor: dot.color } : { backgroundColor: dot.color }]}
    />
  );
}

const styles = StyleSheet.create({
  dot: { width: 8, height: 8, borderRadius: 4 },
});
```

- [ ] **Step 5: Create the screen**

Create `apps/mobile/src/app/(app)/room/[roomId]/devices.tsx`:

```tsx
import Feather from "@expo/vector-icons/Feather";
import { sessionLabel, sessionStatus, useRoomResources, type LocalSession } from "@lightbulb/shared";
import { router, Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useRef, useState } from "react";
import { Pressable, SectionList, StyleSheet, Text, View } from "react-native";

import { HeaderBack } from "../../../../components/room/HeaderBack";
import { RoomBlockedState } from "../../../../components/room/RoomBlockedState";
import { SessionStatusDot } from "../../../../components/session/SessionStatusDot";
import { colors, fonts, radii, spacing } from "../../../../config/theme";
import { deviceSections, memberName, type DeviceRow } from "../../../../lib/localSessions";
import { useMobileRooms } from "../../../../lib/rooms";

/**
 * Devices & sessions for one room: the Macs and shared Claude / Codex sessions
 * its members expose. The web room tab's list, pushed over the conversation.
 */
export default function DevicesScreen() {
  const { roomId } = useLocalSearchParams<{ roomId: string }>();
  const [focused, setFocused] = useState(true);
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => setFocused(false);
    }, []),
  );
  // visible: false — this is not the room; it marks nothing read (the thread screen's rule).
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: false });
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);
  const resources = useRoomResources(roomId ?? "", rooms.resourceSource, { enabled: focused && !!roomId });
  const members = rooms.activeRoom?.members;
  const sections = resources.data ? deviceSections(resources.data, (human) => memberName(members, human)) : [];

  const headerOptions = {
    title: "Devices & sessions",
    headerLeft: () => <HeaderBack fallback={`/(app)/room/${roomId}`} />,
  };

  if (rooms.accessRemoved || rooms.roomError || rooms.selectRoomError) {
    return (
      <View style={styles.container}>
        <Stack.Screen options={headerOptions} />
        <RoomBlockedState
          state={rooms.accessRemoved ? "access-removed" : "unavailable"}
          onRetry={rooms.accessRemoved ? undefined : rooms.refreshRoom}
        />
      </View>
    );
  }

  return (
    <View style={styles.container}>
      <Stack.Screen options={headerOptions} />
      {resources.error && (
        <View style={styles.errorCard}>
          <Text accessibilityRole="alert" style={styles.errorText}>{resources.error}</Text>
          {resources.data && <Text style={styles.meta}>Showing the last successful snapshot; availability may be stale.</Text>}
          <Pressable onPress={() => void resources.refresh()} accessibilityRole="button" accessibilityLabel="Try again">
            <Text style={styles.link}>Try again</Text>
          </Pressable>
        </View>
      )}
      <SectionList
        sections={sections}
        keyExtractor={(row) => row.key}
        contentContainerStyle={[styles.list, sections.length === 0 && styles.listEmpty]}
        stickySectionHeadersEnabled={false}
        renderSectionHeader={({ section }) => <Text style={styles.owner}>{section.title}</Text>}
        renderItem={({ item }) => (
          <DeviceCard row={item} onOpen={(session) => router.push(`/(app)/room/${roomId}/session/${session.sessionId}`)} />
        )}
        ListEmptyComponent={
          resources.loading ? (
            <Text style={styles.meta}>Loading devices and sessions…</Text>
          ) : resources.error ? null : (
            <Text style={styles.meta}>No paired devices or shared sessions in this conversation.</Text>
          )
        }
        ListFooterComponent={
          sections.length > 0 ? (
            <Text style={styles.footer}>
              Paired endpoints are listed independently of session sharing. Pairing does not confirm they are online or available to run work.
            </Text>
          ) : null
        }
      />
    </View>
  );
}

function DeviceCard({ row, onOpen }: { row: DeviceRow; onOpen: (session: LocalSession) => void }) {
  if (row.kind === "cli") {
    return (
      <View style={styles.card}>
        <View style={styles.cardHeader}>
          <Feather name="terminal" size={16} color={colors.inkSoft} />
          <Text style={styles.cardTitle}>Paired CLI endpoint · {row.deviceId.slice(0, 8)}</Text>
        </View>
        <Text style={styles.meta}>Availability unknown · no Mac association verified</Text>
      </View>
    );
  }
  return (
    <View style={styles.card}>
      <View style={styles.cardHeader}>
        <Feather name="monitor" size={16} color={colors.inkSoft} />
        <Text style={styles.cardTitle}>Mac · {row.deviceId.slice(0, 8)}</Text>
      </View>
      {row.pairedLabel && <Text style={styles.meta}>{row.pairedLabel}</Text>}
      {row.sessions.map((session) => (
        <Pressable
          key={session.sessionId}
          onPress={() => onOpen(session)}
          accessibilityRole="button"
          accessibilityLabel={`Open ${sessionLabel(session)}`}
          style={({ pressed }) => [styles.session, pressed && styles.sessionPressed]}
        >
          <SessionStatusDot status={session.status} />
          <View style={styles.sessionBody}>
            <Text style={styles.sessionTitle} numberOfLines={1}>{sessionLabel(session)}</Text>
            <Text style={styles.sessionMeta} numberOfLines={1}>
              {sessionStatus[session.status]}{session.cwdName ? ` · ~/${session.cwdName}` : ""}
            </Text>
          </View>
          <Feather name="chevron-right" size={16} color={colors.mute} />
        </Pressable>
      ))}
      {row.sessions.length === 0 && <Text style={styles.meta}>No shared local sessions.</Text>}
    </View>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  list: { paddingHorizontal: spacing.lg, paddingTop: spacing.sm, paddingBottom: spacing.xl },
  listEmpty: { flexGrow: 1, justifyContent: "center", alignItems: "center" },
  owner: { fontFamily: fonts.sansSemiBold, fontSize: 15, lineHeight: 20, color: colors.ink, marginTop: spacing.lg, marginBottom: spacing.sm },
  card: {
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radii.md,
    padding: spacing.md,
    marginBottom: spacing.sm,
    gap: spacing.xs,
  },
  cardHeader: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  cardTitle: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  session: {
    flexDirection: "row",
    alignItems: "center",
    gap: spacing.sm,
    paddingVertical: spacing.sm,
    borderRadius: radii.sm,
  },
  sessionPressed: { backgroundColor: colors.rowPressed },
  sessionBody: { flex: 1 },
  sessionTitle: { fontFamily: fonts.sansMedium, fontSize: 15, lineHeight: 20, color: colors.ink },
  sessionMeta: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 16, color: colors.mute },
  meta: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.mute },
  footer: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute, marginTop: spacing.lg },
  errorCard: { padding: spacing.lg, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 14, lineHeight: 18, color: colors.forest },
});
```

- [ ] **Step 6: Run the screen test, typecheck and lint**

Run: `cd apps/mobile && pnpm test -- "src/app/\(app\)/room/__tests__/devices.test.tsx" && pnpm typecheck && pnpm lint`
Expected: PASS, clean.

- [ ] **Step 7: Commit**

```bash
git add apps/mobile/src/lib/localSessions.ts apps/mobile/src/components/session/SessionStatusDot.tsx "apps/mobile/src/app/(app)/room/[roomId]/devices.tsx" "apps/mobile/src/app/(app)/room/__tests__/devices.test.tsx"
git commit -m "feat(mobile): devices & sessions screen"
```

---

### Task 6: Transcript row renderer

**Files:**
- Create: `apps/mobile/src/components/session/TranscriptRow.tsx`
- Create: `apps/mobile/src/components/session/__tests__/TranscriptRow.test.tsx`

**Interfaces:**
- Consumes: `GLYPH`, `lifecycleLabel`, `toolHeader`, `toolRowView`, `LocalSessionProvider`, `TranscriptRow`, `ToolTranscriptRow` (Task 1); `MessageMarkdown` (`apps/mobile/src/components/chat/MessageMarkdown.tsx`, props `{ text: string }`).
- Produces: `TranscriptRowView({ provider, row, name })` where `name: (who: string | null) => string`; `WorkingRow({ provider, since? })`.

- [ ] **Step 1: Write the failing row test**

Create `apps/mobile/src/components/session/__tests__/TranscriptRow.test.tsx`:

```tsx
import { fireEvent, render, screen } from "@testing-library/react-native";
import { buildRows, type LocalSessionEvent } from "@lightbulb/shared";

import { TranscriptRowView, WorkingRow } from "../TranscriptRow";

const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });
const name = (who: string | null) => (who === "friday" ? "Friday" : who ? "Teammate" : "Prasiddha");
const draw = (provider: "claude" | "codex", events: LocalSessionEvent[]) =>
  render(<>{buildRows(events).map((row) => <TranscriptRowView key={row.seq} provider={provider} row={row} name={name} />)}</>);

describe("TranscriptRowView", () => {
  it("draws a prompt with the Claude glyph and attribution, prose and a thinking marker", async () => {
    await draw("claude", [
      ev(1, { kind: "text", role: "user", text: "run it", actor: { agentId: "friday" }, ts: "2026-09-12T10:00:00.000Z" }),
      ev(2, { kind: "thinking", role: "assistant", text: "" }),
      ev(3, { kind: "text", role: "assistant", text: "All green.", itemId: "m1" }),
    ]);
    expect(screen.getByText(">")).toBeTruthy();
    expect(screen.getByText("run it")).toBeTruthy();
    expect(screen.getByText(/^Friday · /)).toBeTruthy();
    expect(screen.getByText("Thinking…")).toBeTruthy();
    expect(screen.getByText("All green.")).toBeTruthy();
  });

  it("collapses tool output to the preview and expands the rest on tap", async () => {
    await draw("claude", [
      ev(1, { kind: "tool", role: "assistant", text: "npm test", itemId: "t1", tool: { name: "Bash", status: "running", input: { command: "npm test" } } }),
      ev(2, { kind: "tool", role: "tool", text: "S1\nS2", parentId: "t1", tool: { name: "result", status: "done", result: { stdout: "S1\nS2\nS3" }, lines: 3 } }),
    ]);
    expect(screen.getByText("Bash(npm test)")).toBeTruthy();
    expect(screen.getByText(/S1/)).toBeTruthy();
    expect(screen.queryByText(/S3/)).toBeNull();
    await fireEvent.press(screen.getByText("… +1 lines"));
    expect(screen.getByText(/S3/)).toBeTruthy();
    await fireEvent.press(screen.getByText("collapse"));
    expect(screen.queryByText(/S3/)).toBeNull();
  });

  it("draws a diff line by line, the truncated marker, an error line and a Codex final answer", async () => {
    await draw("codex", [
      ev(1, { kind: "tool", role: "assistant", text: "a.ts", itemId: "f1", truncated: true, tool: { name: "FileChange", status: "error", result: { "a.ts": { type: "update", diff: "-OLD\n+NEW\n CTX" } } } }),
      ev(2, { kind: "text", role: "assistant", text: "Done.", itemId: "m1" }),
      ev(3, { kind: "lifecycle", role: "system", text: "12000", lifecycle: "turn_end" }),
    ]);
    expect(screen.getByText("Edited a.ts")).toBeTruthy();
    expect(screen.getByText("└ Error")).toBeTruthy();
    expect(screen.getByText("+NEW")).toBeTruthy();
    expect(screen.getByText("-OLD")).toBeTruthy();
    expect(screen.getByText("(truncated)")).toBeTruthy();
    expect(screen.getByText("codex")).toBeTruthy();
    expect(screen.getByText("Worked for 12s")).toBeTruthy();
  });

  it("shows a working line with elapsed seconds", async () => {
    await render(<WorkingRow provider="claude" since={new Date(Date.now() - 4000).toISOString()} />);
    expect(screen.getByLabelText("Working")).toBeTruthy();
    expect(screen.getByText(/Working…/)).toBeTruthy();
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/mobile && pnpm test -- src/components/session/__tests__/TranscriptRow.test.tsx`
Expected: FAIL — cannot find module `../TranscriptRow`.

- [ ] **Step 3: Create the renderer**

Create `apps/mobile/src/components/session/TranscriptRow.tsx`:

```tsx
import {
  GLYPH, lifecycleLabel, toolHeader, toolRowView,
  type LocalSessionProvider, type ToolTranscriptRow, type TranscriptRow,
} from "@lightbulb/shared";
import { useEffect, useState } from "react";
import { Pressable, StyleSheet, Text, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";
import { MessageMarkdown } from "../chat/MessageMarkdown";

/** HH:MM in the reader's locale; null when the event carries no usable timestamp. */
function time(ts?: string): string | null {
  const d = ts ? new Date(ts) : null;
  return d && !Number.isNaN(d.getTime()) ? d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : null;
}

/**
 * One row of a shared session in its TUI's grammar — the phone twin of the
 * web NativeSessionTranscript. `name` resolves a prompt's author: null is the
 * session owner, otherwise a room member's id.
 */
export function TranscriptRowView({ provider, row, name }: {
  provider: LocalSessionProvider;
  row: TranscriptRow;
  name: (who: string | null) => string;
}) {
  const g = GLYPH[provider];
  switch (row.kind) {
    case "prompt": {
      const at = time(row.ts);
      return (
        <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
          <View style={styles.promptBand}>
            <Text style={styles.glyph}>{g.prompt}</Text>
            <Text style={styles.promptText}>{row.text}</Text>
          </View>
          <Text style={styles.meta}>{name(row.who)}{at ? ` · ${at}` : ""}</Text>
        </View>
      );
    }
    case "prose": {
      const final = provider === "codex" && row.final;
      return (
        <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
          {final && <Text style={styles.codexLabel}>codex</Text>}
          <View style={styles.line}>
            {final ? <View style={styles.glyphSpace} /> : <Text style={styles.glyph}>{g.bullet}</Text>}
            <View style={styles.prose}><MessageMarkdown text={row.text} /></View>
          </View>
        </View>
      );
    }
    case "thinking":
      return (
        <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
          <View style={styles.line}>
            <Text style={[styles.glyph, styles.thinkingGlyph]}>{g.thinking}</Text>
            {row.text ? (
              <View style={[styles.prose, styles.thinking]}><MessageMarkdown text={row.text} /></View>
            ) : (
              <Text style={styles.metaInline}>Thinking…</Text>
            )}
          </View>
        </View>
      );
    case "tool":
      return <ToolRow provider={provider} row={row} />;
    case "notice":
      return <Text style={styles.meta} accessibilityLabel={`Native event ${row.seq}`}>{row.text}</Text>;
    case "lifecycle": {
      const label = lifecycleLabel(row);
      return label ? <Text style={styles.meta} accessibilityLabel={`Native event ${row.seq}`}>{label}</Text> : null;
    }
  }
}

function ToolRow({ provider, row }: { provider: LocalSessionProvider; row: ToolTranscriptRow }) {
  const [open, setOpen] = useState(false);
  const g = GLYPH[provider];
  const { status, preview, full, diff, more, showOutput, truncated } = toolRowView(row);
  const isDiff = diff.length > 0;
  const shown = open ? full || preview : preview;
  const body = truncated && shown ? `${shown}\n(truncated)` : shown;
  return (
    <View style={styles.row} accessibilityLabel={`Native event ${row.seq}`}>
      <View style={styles.line}>
        <Text style={styles.glyph}>{g.bullet}</Text>
        <Text style={styles.toolHeader}>
          {toolHeader(provider, row.call)}
          {status === "running" && <Text style={styles.metaInline}> {provider === "claude" ? "running…" : "…"}</Text>}
        </Text>
      </View>
      {status === "error" && <Text style={[styles.output, styles.errorText]}>{g.output} Error</Text>}
      {isDiff && (
        <View style={styles.outputBlock}>
          {diff.map((line, i) => (
            <Text key={i} style={[styles.output, line.startsWith("+") ? styles.diffAdd : line.startsWith("-") ? styles.diffDel : styles.diffCtx]}>
              {line}
            </Text>
          ))}
          {truncated && <Text style={styles.output}>(truncated)</Text>}
        </View>
      )}
      {!isDiff && body ? (
        <View style={styles.outputBlock}>
          <Text style={styles.output}>{g.output} {body}</Text>
        </View>
      ) : null}
      {!isDiff && (more > 0 || showOutput) && (
        <Pressable onPress={() => setOpen((o) => !o)} accessibilityRole="button" accessibilityState={{ expanded: open }} hitSlop={8}>
          <Text style={styles.more}>{open ? "collapse" : more > 0 ? `… +${more} lines` : "show output"}</Text>
        </Pressable>
      )}
    </View>
  );
}

/** The trailing spinner line while a turn is open, with the seconds since it started. */
export function WorkingRow({ provider, since }: { provider: LocalSessionProvider; since?: string }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(t);
  }, []);
  const started = since ? Date.parse(since) : NaN;
  const seconds = Number.isFinite(started) ? Math.max(0, Math.round((now - started) / 1000)) : null;
  return (
    <View style={styles.line} accessibilityLabel="Working">
      <Text style={[styles.glyph, styles.workingGlyph]}>{GLYPH[provider].working}</Text>
      <Text style={styles.working}>
        Working…{seconds !== null && <Text style={styles.metaInline}> {seconds}s</Text>}
      </Text>
    </View>
  );
}

const GLYPH_WIDTH = 18;

const styles = StyleSheet.create({
  row: { marginBottom: spacing.sm },
  line: { flexDirection: "row", alignItems: "flex-start" },
  glyph: { width: GLYPH_WIDTH, fontFamily: fonts.mono, fontSize: 14, lineHeight: 20, color: colors.inkSoft },
  glyphSpace: { width: GLYPH_WIDTH },
  thinkingGlyph: { color: colors.mute },
  workingGlyph: { color: colors.green },
  promptBand: {
    flexDirection: "row",
    alignItems: "flex-start",
    backgroundColor: colors.codeBg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radii.sm,
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  promptText: { flex: 1, fontFamily: fonts.mono, fontSize: 14, lineHeight: 20, color: colors.ink },
  prose: { flex: 1 },
  thinking: { opacity: 0.7 },
  codexLabel: { fontFamily: fonts.sansSemiBold, fontSize: 11, lineHeight: 14, color: colors.mute, marginBottom: 2 },
  toolHeader: { flex: 1, fontFamily: fonts.mono, fontSize: 13, lineHeight: 20, color: colors.ink },
  outputBlock: {
    marginLeft: GLYPH_WIDTH,
    marginTop: 2,
    backgroundColor: colors.codeBg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radii.sm,
    paddingVertical: spacing.xs,
    paddingHorizontal: spacing.sm,
  },
  output: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17, color: colors.inkSoft },
  errorText: { color: colors.error, marginLeft: GLYPH_WIDTH },
  diffAdd: { color: colors.green },
  diffDel: { color: colors.error },
  diffCtx: { color: colors.mutedSecondary },
  more: { fontFamily: fonts.mono, fontSize: 12, lineHeight: 17, color: colors.forest, marginLeft: GLYPH_WIDTH, marginTop: 2 },
  meta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute, marginTop: 2, marginLeft: GLYPH_WIDTH },
  metaInline: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 20, color: colors.mute },
  working: { fontFamily: fonts.mono, fontSize: 14, lineHeight: 20, color: colors.ink },
});
```

- [ ] **Step 4: Run the row test, typecheck and lint**

Run: `cd apps/mobile && pnpm test -- src/components/session/__tests__/TranscriptRow.test.tsx && pnpm typecheck && pnpm lint`
Expected: PASS, clean. If `getByText("… +1 lines")` fails because the preview and `more` differ from the fixture, fix the fixture's `lines` value, not the model.

- [ ] **Step 5: Commit**

```bash
git add apps/mobile/src/components/session/TranscriptRow.tsx apps/mobile/src/components/session/__tests__/TranscriptRow.test.tsx
git commit -m "feat(mobile): terminal-grammar transcript rows for shared sessions"
```

---

### Task 7: Session screen with prompt row

**Files:**
- Create: `apps/mobile/src/components/session/SessionPromptRow.tsx`
- Create: `apps/mobile/src/app/(app)/room/[roomId]/session/[sessionId].tsx`
- Create: `apps/mobile/src/app/(app)/room/__tests__/session.test.tsx`

**Interfaces:**
- Consumes: `useRoomResources`, `useLocalSessionHistory` (Task 2); `useSessionCommand`, `sessionStatus`, `unavailableReason`, `sessionLabel`, `GLYPH` (Tasks 1, 3); `buildRows`, `TranscriptRow`; `TranscriptRowView`, `WorkingRow` (Task 6); `SessionStatusDot` (Task 5); `memberName` (Task 4); `useComposerKeyboardOffset` from `components/room/RoomComposer.tsx`; `randomUUID` from `expo-crypto`.
- Produces: `SessionPromptRow({ session, text, onChangeText, command })` where `command: ReturnType<typeof useSessionCommand>`.

- [ ] **Step 1: Write the failing screen test**

Create `apps/mobile/src/app/(app)/room/__tests__/session.test.tsx`:

```tsx
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react-native";
import { ApiError, type LiveEvent, type LocalSession, type LocalSessionCommand, type LocalSessionCommandReceipt, type LocalSessionEvent, type RoomDetail, type RoomResources } from "@lightbulb/shared";

jest.setTimeout(20000);

const mockPush = jest.fn();
jest.mock("expo-router", () => {
  const { useEffect } = require("react");
  return {
    router: { push: (...args: unknown[]) => mockPush(...args), back: jest.fn(), canGoBack: () => true, replace: jest.fn() },
    Stack: { Screen: () => null },
    useFocusEffect: (cb: () => undefined | (() => void)) => useEffect(() => cb(), [cb]),
    useLocalSearchParams: () => ({ roomId: "room-1", sessionId: "sess-1" }),
  };
});
let mockScope = 0;
jest.mock("@clerk/clerk-expo", () => ({
  useAuth: () => ({ userId: "user-a", getToken: jest.fn(async () => "token") }),
  useOrganization: () => ({ organization: { id: `org-${mockScope}` } }),
}));
jest.mock("expo-crypto", () => ({ randomUUID: () => "33333333-3333-4333-8333-333333333333" }));
jest.mock("react-native-safe-area-context", () => ({ useSafeAreaInsets: () => ({ top: 0, bottom: 0, left: 0, right: 0 }) }));

const mockRoom: RoomDetail = {
  roomId: "room-1", name: "Launch", kind: "channel", roomRevision: 1, deliveryKey: "k",
  pinnedMessageIds: [], topic: null, starred: false, muted: false, sectionId: null,
  lastMessageSeq: 3, lastMessageAt: "2026-09-11T10:02:00Z", unreadCount: 0,
  updatedAt: "2026-09-11T10:02:00Z", lastReadSeq: 3, createdAt: "2026-09-11T09:00:00Z",
  members: [
    { kind: "human", id: "user-a", displayName: "Alice", mentionHandle: null, role: "owner", listenMode: null, joinedAt: "x", joinedSeq: 0 },
    { kind: "agent", id: "friday", displayName: "Friday", mentionHandle: "friday", role: "member", listenMode: "active", joinedAt: "x", joinedSeq: 0 },
  ],
};
const base: LocalSession = {
  sessionId: "sess-1", deviceId: "mac-1", humanUserId: "user-a", provider: "claude", generation: "g1",
  status: "idle", providerVersion: "2.1.224", controlUnavailableReason: null, activeTurnId: null,
  capabilities: { sendMode: "queue", steer: false, interrupt: false }, capabilitiesVerified: true,
  latestEventSeq: 5, title: "Fix relay", cwdName: "lightbulb",
};
let mockSession: LocalSession = base;
const ev = (seq: number, e: Omit<LocalSessionEvent, "seq" | "truncated"> & { truncated?: boolean }): LocalSessionEvent => ({ seq, truncated: false, ...e });
const events: LocalSessionEvent[] = [
  ev(1, { kind: "text", role: "user", text: "run it", actor: { userId: "user-a" }, ts: "2026-09-12T10:00:00.000Z" }),
  ev(2, { kind: "tool", role: "assistant", text: "git status", itemId: "t1", tool: { name: "Bash", status: "running", input: { command: "git status" } } }),
  ev(3, { kind: "tool", role: "tool", text: "S1\nS2", parentId: "t1", tool: { name: "result", status: "done", result: { stdout: "S1\nS2\nS3\nS4" }, lines: 4 } }),
  ev(4, { kind: "text", role: "assistant", text: "All green.", itemId: "m1" }),
  ev(5, { kind: "lifecycle", role: "system", text: "12000", lifecycle: "turn_end" }),
];
let mockEvents = events;
const receipt = (request: LocalSessionCommand, state: LocalSessionCommandReceipt["state"] = "applied"): LocalSessionCommandReceipt => ({
  ...request, text: request.text ?? null, expectedTurnId: request.expectedTurnId ?? null,
  sessionId: "sess-1", roomId: "room-1", actorUserId: "user-a", commandSeq: 1, state,
  createdAt: 1, deliverBy: 31, held: false, providerMessageId: null,
});
const mockListResources = jest.fn(async (): Promise<RoomResources> => ({ devices: [], sessions: [mockSession], pairedNodes: [] }));
const mockListEvents = jest.fn(async () => ({ events: mockEvents, nextAfterSeq: null }));
const mockSubmit = jest.fn(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
const mockGetCommand = jest.fn();
const mockLive = {
  httpReady: true, liveConnected: true, liveError: null, liveChannels: ["/rooms/room-1/k"], connectionRevision: 1,
  messaging: {
    listRooms: jest.fn(async () => ({ rooms: [mockRoom] })),
    getRoom: jest.fn(async () => mockRoom),
    listMessages: jest.fn(async () => ({ messages: [], nextAfterSeq: null })),
    markRead: jest.fn(async () => ({ roomId: "room-1", lastReadSeq: 3 })),
    getRunStatus: jest.fn(async () => ({ runId: "r", state: "terminal" })),
    listResources: mockListResources,
    listLocalSessionEvents: mockListEvents,
    submitLocalSessionCommand: mockSubmit,
    getLocalSessionCommand: mockGetCommand,
    sendMessage: jest.fn(), downloadAttachment: jest.fn(), uploadAttachment: jest.fn(), markUnread: jest.fn(),
    setTyping: jest.fn(), abortRun: jest.fn(), steerRun: jest.fn(), createRoom: jest.fn(), updateRoom: jest.fn(),
    upsertMember: jest.fn(), removeMember: jest.fn(), updateMessage: jest.fn(), setReaction: jest.fn(),
    getLiveConfig: jest.fn(), setMemberPrefs: jest.fn(), putSections: jest.fn(),
  },
  claimActiveRoomSubscription: jest.fn(() => () => {}),
  onEvent: (_handler: (event: LiveEvent) => void) => () => {},
  onChatMessage: () => () => {},
  runtime: {}, rpc: jest.fn(), getOperation: jest.fn(),
};
jest.mock("../../../../providers/live-events", () => ({ useLiveEvents: () => mockLive }));

import SessionScreen from "../[roomId]/session/[sessionId]";

describe("session screen", () => {
  beforeEach(() => {
    mockScope += 1;
    mockSession = base;
    mockEvents = events;
    mockPush.mockClear();
    mockListResources.mockClear();
    mockListEvents.mockClear();
    mockSubmit.mockReset();
    mockSubmit.mockImplementation(async (_r: string, _s: string, request: LocalSessionCommand) => receipt(request));
    mockGetCommand.mockReset();
  });

  it("renders the transcript in the Claude grammar with the owner's attribution", async () => {
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("run it")).toBeTruthy());
    expect(mockListEvents).toHaveBeenCalledWith("room-1", "sess-1", 0, 50);
    // The summary line and the prompt's attribution both start with the owner's name.
    expect(screen.getAllByText(/^Alice · /)).toHaveLength(2);
    expect(screen.getByText("Bash(git status)")).toBeTruthy();
    expect(screen.queryByText(/S3/)).toBeNull();
    await fireEvent.press(screen.getByText("… +2 lines"));
    expect(screen.getByText(/S3/)).toBeTruthy();
    expect(screen.getByText("All green.")).toBeTruthy();
    expect(screen.getByText("Worked for 12s")).toBeTruthy();
    expect(screen.getByText("Alice · Idle · ~/lightbulb")).toBeTruthy();
    expect(screen.queryByLabelText("Working")).toBeNull();
  });

  it("shows the working line while a turn is open", async () => {
    mockSession = { ...base, status: "busy", activeTurnId: "t2", latestEventSeq: 6 };
    mockEvents = [...events, ev(6, { kind: "lifecycle", role: "system", text: "", lifecycle: "turn_start", ts: new Date().toISOString() })];
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByLabelText("Working")).toBeTruthy());
  });

  it("sends a follow-up and shows the receipt", async () => {
    await render(<SessionScreen />);
    const input = await screen.findByLabelText("Send a follow-up to Claude");
    await fireEvent.changeText(input, "  also run lint  ");
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(mockSubmit).toHaveBeenCalledWith("room-1", "sess-1", {
      requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1", kind: "send", text: "also run lint",
    }));
    await waitFor(() => expect(screen.getByText("Admitted to the native session — not completed work.")).toBeTruthy());
    expect(input.props.value).toBe("");
  });

  it("explains a 409 and offers check delivery after an uncertain send", async () => {
    mockSubmit.mockRejectedValueOnce(new ApiError(409, null));
    await render(<SessionScreen />);
    const input = await screen.findByLabelText("Send a follow-up to Claude");
    await fireEvent.changeText(input, "hello");
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(screen.getByText("Session changed or is offline. Refresh before sending again.")).toBeTruthy());
    mockSubmit.mockRejectedValueOnce(new TypeError("network"));
    mockGetCommand.mockResolvedValue(receipt({ requestId: "33333333-3333-4333-8333-333333333333", expectedGeneration: "g1", kind: "send", text: "hello" }, "dispatched"));
    await fireEvent.press(screen.getByLabelText("Send follow-up"));
    await waitFor(() => expect(screen.getByLabelText("Check delivery")).toBeTruthy());
    expect(screen.getByLabelText("Retry same request")).toBeTruthy();
    await act(async () => { await fireEvent.press(screen.getByLabelText("Check delivery")); });
    await waitFor(() => expect(screen.getByText("Dispatched; native admission is not yet confirmed.")).toBeTruthy());
  });

  it("replaces the input with the reason when the session cannot take input", async () => {
    mockSession = { ...base, controlUnavailableReason: "history-gap" };
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getAllByText("History has a gap. Control is unavailable until the desktop reconnects safely.").length).toBeGreaterThan(0));
    expect(screen.queryByLabelText("Send a follow-up to Claude")).toBeNull();
  });

  it("says when the session is no longer shared", async () => {
    mockListResources.mockResolvedValue({ devices: [], sessions: [], pairedNodes: [] });
    await render(<SessionScreen />);
    await waitFor(() => expect(screen.getByText("This session is no longer shared.")).toBeTruthy());
  });
});
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cd apps/mobile && pnpm test -- "src/app/\(app\)/room/__tests__/session.test.tsx"`
Expected: FAIL — cannot find module `../[roomId]/session/[sessionId]`.

- [ ] **Step 3: Create the prompt row**

Create `apps/mobile/src/components/session/SessionPromptRow.tsx`:

```tsx
import Feather from "@expo/vector-icons/Feather";
import { GLYPH, unavailableReason, type LocalSession, type useSessionCommand } from "@lightbulb/shared";
import { Pressable, StyleSheet, Text, TextInput, View } from "react-native";

import { colors, fonts, radii, spacing } from "../../config/theme";

/**
 * The follow-up composer under a shared session: one line in the TUI's prompt
 * glyph, the same receipt states the web shows, and never an implicit resend.
 */
export function SessionPromptRow({ session, text, onChangeText, command }: {
  session: LocalSession;
  text: string;
  onChangeText: (text: string) => void;
  command: ReturnType<typeof useSessionCommand>;
}) {
  const target = session.provider === "claude" ? "Claude" : "Codex";
  const reason = command.canSend ? null
    : session.status === "offline" ? "Offline"
      : session.controlUnavailableReason ? unavailableReason[session.controlUnavailableReason]
        : "This session does not accept remote input.";
  const sendDisabled = command.locked || !text.trim();
  return (
    <View style={styles.wrap}>
      {reason ? (
        <Text style={styles.reason}>{reason}</Text>
      ) : (
        <View style={styles.row}>
          <Text style={styles.glyph}>{GLYPH[session.provider].prompt}</Text>
          <TextInput
            style={styles.input}
            value={text}
            onChangeText={onChangeText}
            multiline
            editable={!command.locked}
            placeholder={`Send a follow-up to ${target}…`}
            placeholderTextColor={colors.placeholder}
            accessibilityLabel={`Send a follow-up to ${target}`}
          />
          <Pressable
            onPress={() => command.send(text)}
            disabled={sendDisabled}
            accessibilityRole="button"
            accessibilityLabel="Send follow-up"
            accessibilityState={{ disabled: sendDisabled }}
            style={[styles.send, sendDisabled && styles.sendDisabled]}
          >
            <Feather name="arrow-up" size={18} color={colors.white} />
          </Pressable>
        </View>
      )}
      {command.attempt?.state === "sending" && <Text style={styles.status}>Sending follow-up…</Text>}
      {command.attempt?.error && <Text accessibilityRole="alert" style={styles.error}>{command.attempt.error}</Text>}
      {command.receiptText && <Text style={styles.status}>{command.receiptText}</Text>}
      {command.receipt?.held && <Text style={styles.status}>Admission is not completed work.</Text>}
      {(command.canCheck || command.attempt?.state === "uncertain") && (
        <View style={styles.actions}>
          {command.canCheck && (
            <Pressable onPress={() => void command.checkDelivery()} disabled={command.checking} accessibilityRole="button" accessibilityLabel="Check delivery">
              <Text style={styles.link}>Check delivery</Text>
            </Pressable>
          )}
          {command.attempt?.state === "uncertain" && (
            <Pressable onPress={() => command.retry()} disabled={!command.canSend || command.checking} accessibilityRole="button" accessibilityLabel="Retry same request">
              <Text style={styles.link}>Retry same request</Text>
            </Pressable>
          )}
        </View>
      )}
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    borderTopWidth: StyleSheet.hairlineWidth,
    borderTopColor: colors.border,
    backgroundColor: colors.cream,
    paddingHorizontal: spacing.md,
    paddingVertical: spacing.sm,
    gap: spacing.xs,
  },
  row: { flexDirection: "row", alignItems: "flex-end", gap: spacing.sm },
  glyph: { fontFamily: fonts.mono, fontSize: 15, lineHeight: 22, color: colors.inkSoft, paddingBottom: spacing.sm },
  input: {
    flex: 1,
    minHeight: 38,
    maxHeight: 120,
    fontFamily: fonts.mono,
    fontSize: 14,
    lineHeight: 20,
    color: colors.ink,
    backgroundColor: colors.codeBg,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.border,
    borderRadius: radii.md,
    paddingHorizontal: spacing.sm,
    paddingVertical: spacing.sm,
  },
  send: { width: 36, height: 36, borderRadius: 18, backgroundColor: colors.forest, alignItems: "center", justifyContent: "center" },
  sendDisabled: { opacity: 0.4 },
  reason: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.warn },
  status: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  error: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.errorText },
  actions: { flexDirection: "row", gap: spacing.lg },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18, color: colors.forest },
});
```

- [ ] **Step 4: Create the screen**

Create `apps/mobile/src/app/(app)/room/[roomId]/session/[sessionId].tsx`:

```tsx
import Feather from "@expo/vector-icons/Feather";
import {
  buildRows, sessionLabel, sessionStatus, unavailableReason, useLocalSessionHistory, useRoomResources, useSessionCommand,
  type LocalSession, type RoomResourceSource, type TranscriptRow,
} from "@lightbulb/shared";
import { randomUUID } from "expo-crypto";
import { Stack, useFocusEffect, useLocalSearchParams } from "expo-router";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AppState, FlatList, KeyboardAvoidingView, Platform, Pressable, StyleSheet, Text, View,
  type NativeScrollEvent, type NativeSyntheticEvent,
} from "react-native";

import { TimelineSkeleton } from "../../../../../components/chat/TimelineSkeleton";
import { HeaderBack } from "../../../../../components/room/HeaderBack";
import { RoomBlockedState } from "../../../../../components/room/RoomBlockedState";
import { useComposerKeyboardOffset } from "../../../../../components/room/RoomComposer";
import { SessionPromptRow } from "../../../../../components/session/SessionPromptRow";
import { SessionStatusDot } from "../../../../../components/session/SessionStatusDot";
import { TranscriptRowView, WorkingRow } from "../../../../../components/session/TranscriptRow";
import { colors, fonts, spacing } from "../../../../../config/theme";
import { memberName } from "../../../../../lib/localSessions";
import { useMobileRooms } from "../../../../../lib/rooms";

/** Distance from the bottom, in px, still counted as "at the bottom" (the room screen's value). */
const BOTTOM_SLACK = 80;

/**
 * One shared Claude Code / Codex session, drawn in its TUI's grammar, with a
 * prompt row to send follow-ups. The session row itself always comes fresh
 * from the room's resources (status, generation, control availability); the
 * body remounts when the session restarts or the reader's access epoch changes,
 * exactly as the web pane does.
 */
export default function SessionScreen() {
  const { roomId, sessionId } = useLocalSearchParams<{ roomId: string; sessionId: string }>();
  const [focused, setFocused] = useState(true);
  useFocusEffect(
    useCallback(() => {
      setFocused(true);
      return () => setFocused(false);
    }, []),
  );
  // A backgrounded app stops tailing; jest-expo reports an undefined state, which counts as active.
  const [appActive, setAppActive] = useState(AppState.currentState !== "background");
  useEffect(() => {
    const subscription = AppState.addEventListener("change", (state) => setAppActive(state === "active"));
    return () => subscription.remove();
  }, []);
  const live = focused && appActive;
  const rooms = useMobileRooms({ subscribeToRoom: focused, autoSelect: false, visible: false });
  const selectRoomRef = useRef(rooms.selectRoom);
  selectRoomRef.current = rooms.selectRoom;
  useEffect(() => {
    if (roomId) selectRoomRef.current(roomId);
  }, [roomId]);
  const resources = useRoomResources(roomId ?? "", rooms.resourceSource, { enabled: live && !!roomId });
  const session = resources.data?.sessions.find((s) => s.sessionId === sessionId);
  const members = rooms.activeRoom?.members;
  const ownerName = session ? memberName(members, session.humanUserId) : "Room member";
  const name = useCallback(
    (who: string | null) => (who ? members?.find((m) => m.id === who)?.displayName ?? "Teammate" : ownerName),
    [members, ownerName],
  );
  const keyboardOffset = useComposerKeyboardOffset();

  const headerOptions = {
    title: session ? sessionLabel(session) : "Session",
    headerLeft: () => <HeaderBack fallback={`/(app)/room/${roomId}/devices`} />,
  };

  if (rooms.accessRemoved || rooms.roomError || rooms.selectRoomError) {
    return (
      <View style={styles.container}>
        <Stack.Screen options={headerOptions} />
        <RoomBlockedState
          state={rooms.accessRemoved ? "access-removed" : "unavailable"}
          onRetry={rooms.accessRemoved ? undefined : rooms.refreshRoom}
        />
      </View>
    );
  }
  if (!session) {
    return (
      <View style={styles.container}>
        <Stack.Screen options={headerOptions} />
        <View style={styles.centered}>
          {resources.error ? (
            <>
              <Text accessibilityRole="alert" style={styles.errorText}>{resources.error}</Text>
              <Pressable onPress={() => void resources.refresh()} accessibilityRole="button" accessibilityLabel="Try again">
                <Text style={styles.link}>Try again</Text>
              </Pressable>
            </>
          ) : resources.loading || !resources.data ? (
            <TimelineSkeleton />
          ) : (
            <Text style={styles.meta}>This session is no longer shared.</Text>
          )}
        </View>
      </View>
    );
  }
  return (
    <View style={styles.container}>
      <Stack.Screen options={headerOptions} />
      <SessionBody
        key={`${session.sessionId}:${session.generation}:${resources.accessRevision}`}
        roomId={roomId ?? ""}
        session={session}
        client={rooms.resourceSource.client}
        revision={resources.revision}
        onAccessLost={resources.revoke}
        live={live}
        ownerName={ownerName}
        name={name}
        keyboardOffset={keyboardOffset}
      />
    </View>
  );
}

function SessionBody({ roomId, session, client, revision, onAccessLost, live, ownerName, name, keyboardOffset }: {
  roomId: string;
  session: LocalSession;
  client: RoomResourceSource["client"];
  revision: number;
  onAccessLost: () => void;
  live: boolean;
  ownerName: string;
  name: (who: string | null) => string;
  keyboardOffset: number;
}) {
  const busy = session.status === "busy";
  const history = useLocalSessionHistory(client, roomId, session.sessionId, session.latestEventSeq, revision, onAccessLost, { busy: busy && live });
  const [text, setText] = useState("");
  const command = useSessionCommand(client, roomId, session, { uuid: randomUUID, onAccessLost, onSent: () => setText("") });
  const rows = useMemo(() => buildRows(history.events), [history.events]);
  const last = history.events.at(-1);
  const turnOpen = busy && !history.hasNewer && last?.lifecycle !== "turn_end" && last?.lifecycle !== "turn_aborted";

  const listRef = useRef<FlatList<TranscriptRow>>(null);
  const atBottomRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);
  const onScroll = useCallback((event: NativeSyntheticEvent<NativeScrollEvent>) => {
    const { contentOffset, contentSize, layoutMeasurement } = event.nativeEvent;
    const next = contentSize.height - layoutMeasurement.height - contentOffset.y <= BOTTOM_SLACK;
    if (next !== atBottomRef.current) {
      atBottomRef.current = next;
      setAtBottom(next);
    }
  }, []);

  return (
    <KeyboardAvoidingView
      style={styles.container}
      behavior={Platform.OS === "ios" ? "padding" : undefined}
      keyboardVerticalOffset={keyboardOffset}
    >
      <View style={styles.summary}>
        <View style={styles.summaryLine}>
          <SessionStatusDot status={session.status} />
          <Text style={styles.summaryText} numberOfLines={1}>
            {ownerName} · {sessionStatus[session.status]}{session.cwdName ? ` · ~/${session.cwdName}` : ""}
          </Text>
        </View>
        <Text style={styles.meta} numberOfLines={2}>
          {`${session.providerVersion} · Follow-ups ${session.capabilities.sendMode === "queue" ? "queue" : "wait"} on ${ownerName}’s Mac. Approvals stay in their terminal.${session.workspace?.overlap ? " Multiple sessions may overlap this checkout." : ""}`}
        </Text>
        {session.controlUnavailableReason && <Text style={styles.warn}>{unavailableReason[session.controlUnavailableReason]}</Text>}
      </View>
      <FlatList
        ref={listRef}
        testID="session-transcript"
        data={rows}
        keyExtractor={(row) => String(row.seq)}
        contentContainerStyle={[styles.list, rows.length === 0 && styles.listEmpty]}
        keyboardDismissMode="interactive"
        keyboardShouldPersistTaps="handled"
        onScroll={onScroll}
        scrollEventThrottle={16}
        onContentSizeChange={() => {
          if (atBottomRef.current) listRef.current?.scrollToEnd({ animated: false });
        }}
        ListHeaderComponent={
          <View>
            {history.hasOlder && (
              <Pressable disabled={history.loading} onPress={() => void history.load("older")} accessibilityRole="button" accessibilityLabel="Load older">
                <Text style={styles.link}>Load older</Text>
              </Pressable>
            )}
            {history.error && (
              <View style={styles.errorCard}>
                <Text accessibilityRole="alert" style={styles.errorText}>{history.error}</Text>
                <Pressable onPress={() => void history.retry()} accessibilityRole="button" accessibilityLabel="Retry history">
                  <Text style={styles.link}>Retry</Text>
                </Pressable>
              </View>
            )}
          </View>
        }
        ListEmptyComponent={
          history.loading ? <TimelineSkeleton /> : history.error ? null : <Text style={styles.meta}>No shared output yet.</Text>
        }
        ListFooterComponent={
          <View>
            {turnOpen && <WorkingRow provider={session.provider} since={last?.ts} />}
            {history.hasNewer && (
              <Pressable disabled={history.loading} onPress={() => void history.load("latest")} accessibilityRole="button" accessibilityLabel="Jump to latest">
                <Text style={styles.link}>More recent activity is available. Jump to latest</Text>
              </Pressable>
            )}
          </View>
        }
        renderItem={({ item }) => <TranscriptRowView provider={session.provider} row={item} name={name} />}
      />
      {!atBottom && !history.hasNewer && (
        <Pressable
          style={styles.jump}
          onPress={() => listRef.current?.scrollToEnd({ animated: true })}
          accessibilityRole="button"
          accessibilityLabel="Scroll to bottom"
        >
          <Feather name="arrow-down" size={16} color={colors.white} />
        </Pressable>
      )}
      <SessionPromptRow session={session} text={text} onChangeText={setText} command={command} />
    </KeyboardAvoidingView>
  );
}

const styles = StyleSheet.create({
  container: { flex: 1, backgroundColor: colors.cream },
  centered: { flex: 1, padding: spacing.lg, gap: spacing.sm },
  summary: {
    paddingHorizontal: spacing.lg,
    paddingVertical: spacing.sm,
    gap: 2,
    borderBottomWidth: StyleSheet.hairlineWidth,
    borderBottomColor: colors.border,
  },
  summaryLine: { flexDirection: "row", alignItems: "center", gap: spacing.sm },
  summaryText: { flex: 1, fontFamily: fonts.sansMedium, fontSize: 14, lineHeight: 18, color: colors.ink },
  list: { paddingHorizontal: spacing.md, paddingTop: spacing.sm, paddingBottom: spacing.lg },
  listEmpty: { flexGrow: 1 },
  meta: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.mute },
  warn: { fontFamily: fonts.sans, fontSize: 12, lineHeight: 16, color: colors.warn },
  errorCard: { paddingVertical: spacing.sm, gap: spacing.xs },
  errorText: { fontFamily: fonts.sans, fontSize: 13, lineHeight: 18, color: colors.errorText },
  link: { fontFamily: fonts.sansSemiBold, fontSize: 13, lineHeight: 18, color: colors.forest, paddingVertical: spacing.xs },
  jump: {
    position: "absolute",
    right: spacing.lg,
    bottom: 96,
    width: 36,
    height: 36,
    borderRadius: 18,
    backgroundColor: colors.forest,
    alignItems: "center",
    justifyContent: "center",
  },
});
```

- [ ] **Step 5: Run the screen test, then the whole mobile suite, typecheck and lint**

Run: `cd apps/mobile && pnpm test -- "src/app/\(app\)/room/__tests__/session.test.tsx" && pnpm test && pnpm typecheck && pnpm lint`
Expected: PASS, clean. If the "no longer shared" case races the skeleton, it is because `resources.data` is null until the first fetch resolves — the `waitFor` covers it; do not add timers.

- [ ] **Step 6: Metro resolution gate**

Run: `cd apps/mobile && pnpm export:ios`
Expected: exits 0 (the CI gate `mobile-ci.yml` runs the same). Delete the generated `dist/` directory afterwards if it is not git-ignored (`git status` must show nothing new outside the task's files).

- [ ] **Step 7: Commit**

```bash
git add apps/mobile/src/components/session/SessionPromptRow.tsx "apps/mobile/src/app/(app)/room/[roomId]/session/[sessionId].tsx" "apps/mobile/src/app/(app)/room/__tests__/session.test.tsx"
git commit -m "feat(mobile): live shared-session transcript with a follow-up prompt"
```

---

### Task 8: Remove the planning docs

**Files:**
- Delete: `docs/superpowers/specs/2026-09-13-ios-shared-sessions-design.md`
- Delete: `docs/superpowers/plans/2026-09-13-ios-shared-sessions.md`

- [ ] **Step 1: Delete and commit**

CLAUDE.md: planning docs are disposable; the PR description is the durable record.

```bash
git rm docs/superpowers/specs/2026-09-13-ios-shared-sessions-design.md docs/superpowers/plans/2026-09-13-ios-shared-sessions.md
git commit -m "docs: remove the shipped iOS shared sessions planning docs"
```

- [ ] **Step 2: Whole-tree check**

Run: `cd packages/shared && pnpm test && pnpm typecheck && pnpm lint && cd ../../apps/frontend && pnpm typecheck && pnpm lint && cd ../mobile && pnpm test && pnpm typecheck && pnpm lint`
Expected: all clean.
